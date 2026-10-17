"""Saved answer runs: the recorded events of each chat answer, for the Activity page.

One sealed ``agent_runs`` row per answer (or failed attempt) in a profile that saves history:
the question's first ``MAX_QUESTION_CHARS`` characters, the task contract and its verdict, the
event log from ``agent_events`` (already redacted, the answer kept only as its length) and a
summary. Only ``status`` and the times are plain columns.

Kept like the chats they explain: the newest ``KEEP_RUNS`` per profile, none older than the chat
lifetime, deleted with their chat, with history clearing, private mode, INDmoney disconnect and
profile deletion, and included in the data export. Private mode never saves a run.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Sequence
from typing import Any, Final

from . import users
from .agent_events import redact_trace_text, summarize
from .db import connect_db
from .sealing import ProfileKeys

logger = logging.getLogger("ca_mode.agent_runs")

KEEP_RUNS: Final = 200
MAX_QUESTION_CHARS: Final = 200
MAX_ERROR_CHARS: Final = 300
# Runs explain chat answers, so they last no longer than a chat does (users.SESSION_MAX_AGE_SECONDS, as chats.py).
MAX_AGE_MS: Final = users.SESSION_MAX_AGE_SECONDS * 1000


def saves_runs(user: dict[str, Any]) -> bool:
    """Whether this profile keeps answer runs: it saves history on this computer."""
    return user.get("storageMode") == "device"


def save_run(
    user: dict[str, Any], *, chat_id: str, agent_type: str, provider: str, question: str,
    events: Sequence[dict[str, Any]], verdict: dict[str, Any] | None, error: str | None = None,
) -> str | None:
    """Save one answer run and return its id; ``None`` in private mode or when the database refuses it.

    A failure to save never fails the answer: it is logged and the answer is returned without a run id.
    """
    if not saves_runs(user):
        return None
    now = users.now_ms()
    summary = summarize(events)
    record = {
        "id": uuid.uuid4().hex, "createdAt": now - int(summary["durationMs"] or 0), "completedAt": now,
        "chatId": chat_id, "agentType": agent_type, "provider": provider,
        "question": redact_trace_text(question[:MAX_QUESTION_CHARS]),
        "status": summary["termination"] or ("failed" if error else "answered"),
        "task": (verdict or {}).get("task"), "verdict": verdict, "summary": summary,
        "error": redact_trace_text(error[:MAX_ERROR_CHARS]) if error else None,
        "events": list(events),
    }
    keys: ProfileKeys = user.get("keys") or ProfileKeys()
    try:
        sealed = keys.seal(json.dumps(record, ensure_ascii=False))
        with connect_db() as connection:
            connection.execute(
                "INSERT INTO agent_runs(id, user_id, chat_id, status, created_at, data) VALUES (?, ?, ?, ?, ?, ?)",
                (record["id"], user["id"], chat_id, record["status"], record["createdAt"], sealed),
            )
            _prune(connection, user["id"])
    except sqlite3.Error:
        logger.error("agent_run_save_failed", extra={"user_id": user["id"]}, exc_info=True)
        return None
    return record["id"]


def _prune(connection: sqlite3.Connection, user_id: str) -> None:
    connection.execute("DELETE FROM agent_runs WHERE user_id = ? AND created_at < ?", (user_id, users.now_ms() - MAX_AGE_MS))
    connection.execute(
        "DELETE FROM agent_runs WHERE user_id = ? AND id NOT IN "
        "(SELECT id FROM agent_runs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?)",
        (user_id, user_id, KEEP_RUNS),
    )


def _rows(user_id: str, *, limit: int | None = None, run_id: str | None = None) -> list[sqlite3.Row]:
    query = "SELECT data FROM agent_runs WHERE user_id = ? AND created_at >= ?"
    parameters: list[Any] = [user_id, users.now_ms() - MAX_AGE_MS]
    if run_id is not None:
        query += " AND id = ?"
        parameters.append(run_id)
    query += " ORDER BY created_at DESC"
    if limit is not None:
        query += " LIMIT ?"
        parameters.append(limit)
    with connect_db() as connection:
        return connection.execute(query, parameters).fetchall()


def get_run(user_id: str, run_id: str, keys: ProfileKeys) -> dict[str, Any] | None:
    """One saved run of this profile, events included. Raises ``sealing.LockedError`` when locked."""
    rows = _rows(user_id, run_id=run_id)
    return json.loads(keys.open(rows[0]["data"])) if rows else None


def list_runs(user_id: str, keys: ProfileKeys, *, limit: int = 50) -> list[dict[str, Any]]:
    """Summaries of saved runs, newest first (the events are fetched by id)."""
    items = []
    for row in _rows(user_id, limit=limit):
        record = json.loads(keys.open(row["data"]))
        items.append({key: record.get(key) for key in (
            "id", "createdAt", "completedAt", "chatId", "agentType", "provider", "question", "status", "task", "error",
        )} | {"verdict": (record.get("verdict") or {}).get("status"), "summary": record.get("summary")})
    return items


def all_runs(user_id: str, keys: ProfileKeys) -> list[dict[str, Any]]:
    """Every saved run in full, newest first (for export)."""
    return [json.loads(keys.open(row["data"])) for row in _rows(user_id)]


def delete_run(user_id: str, run_id: str) -> bool:
    with connect_db() as connection:
        return connection.execute("DELETE FROM agent_runs WHERE user_id = ? AND id = ?", (user_id, run_id)).rowcount > 0


def delete_chat_runs(user_id: str, chat_id: str) -> None:
    """Delete the runs of one chat (the chat was deleted)."""
    with connect_db() as connection:
        connection.execute("DELETE FROM agent_runs WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))

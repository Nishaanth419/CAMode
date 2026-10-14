"""Chat history per profile: SQLite rows when saving on this device, process memory in private mode.

Private-mode chats never touch the database. They live in this process until the
profile signs out, switches storage mode, or the server restarts.
"""

from __future__ import annotations

import json
from typing import Any

from . import users
from .db import connect_db

MAX_HISTORY_MESSAGES = 20
MAX_HISTORY_MESSAGE_CHARS = 12_000
MAX_CHATS_PER_USER = 50
CHAT_TTL_MS = users.SESSION_MAX_AGE_SECONDS * 1000

# user_id -> chat_id -> chat, in the same shape ``read_chat`` returns.
_private_chats: dict[str, dict[str, dict[str, Any]]] = {}


def is_private(user: dict[str, Any]) -> bool:
    return user["storageMode"] == "private"


def _copy(chat: dict[str, Any]) -> dict[str, Any]:
    return {**chat, "messages": list(chat["messages"])}


def _live_private_chats(user_id: str) -> dict[str, dict[str, Any]]:
    """Return the user's in-memory chats, dropping any that have expired."""
    store = _private_chats.get(user_id, {})
    now = users.now_ms()
    for chat_id in [chat_id for chat_id, chat in store.items() if chat["sessionExpiresAt"] <= now]:
        del store[chat_id]
    return store


def purge_expired(user_id: str) -> None:
    with connect_db() as connection:
        connection.execute("DELETE FROM chats WHERE user_id = ? AND expires_at <= ?", (user_id, users.now_ms()))
    _live_private_chats(user_id)


def forget_private_chats(user_id: str) -> None:
    _private_chats.pop(user_id, None)


def list_chats(user: dict[str, Any]) -> list[dict[str, Any]]:
    """Return chat summaries, newest first."""
    if is_private(user):
        recent = sorted(_live_private_chats(user["id"]).values(), key=lambda chat: chat["updatedAt"], reverse=True)
        return [
            {"id": chat["id"], "title": chat["title"], "agentType": chat["agentType"], "updatedAt": chat["updatedAt"]}
            for chat in recent[:MAX_CHATS_PER_USER]
        ]
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT id, title, updated_at, agent_type FROM chats WHERE user_id = ? AND expires_at > ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (user["id"], users.now_ms(), MAX_CHATS_PER_USER),
        ).fetchall()
    return [{"id": row["id"], "title": row["title"], "agentType": row["agent_type"], "updatedAt": row["updated_at"]} for row in rows]


def create_chat(user: dict[str, Any], agent_type: str) -> dict[str, Any]:
    """Start an empty chat, evicting the oldest one when the profile is at its limit."""
    now = users.now_ms()
    chat = {
        "id": users.random_token(), "title": "New chat", "messages": [], "agentType": agent_type,
        "updatedAt": now, "sessionExpiresAt": now + CHAT_TTL_MS,
    }
    if is_private(user):
        store = _private_chats.setdefault(user["id"], {})
        _live_private_chats(user["id"])
        if len(store) >= MAX_CHATS_PER_USER:
            del store[min(store.values(), key=lambda item: item["updatedAt"])["id"]]
        store[chat["id"]] = chat
        return _copy(chat)
    with connect_db() as connection:
        existing = connection.execute("SELECT COUNT(*) AS n FROM chats WHERE user_id = ?", (user["id"],)).fetchone()["n"]
        if existing >= MAX_CHATS_PER_USER:
            connection.execute(
                "DELETE FROM chats WHERE user_id = ? AND id = "
                "(SELECT id FROM chats WHERE user_id = ? ORDER BY updated_at ASC LIMIT 1)", (user["id"], user["id"]),
            )
        connection.execute(
            "INSERT INTO chats(id, user_id, title, messages, updated_at, expires_at, agent_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat["id"], user["id"], chat["title"], "[]", now, chat["sessionExpiresAt"], agent_type),
        )
    return chat


def read_chat(user: dict[str, Any], chat_id: str) -> dict[str, Any] | None:
    if is_private(user):
        chat = _live_private_chats(user["id"]).get(chat_id)
        return _copy(chat) if chat else None
    with connect_db() as connection:
        row = connection.execute(
            "SELECT id, title, messages, updated_at, expires_at, agent_type FROM chats "
            "WHERE user_id = ? AND id = ? AND expires_at > ?",
            (user["id"], chat_id, users.now_ms()),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"], "title": row["title"], "messages": json.loads(row["messages"]), "agentType": row["agent_type"],
        "updatedAt": row["updated_at"], "sessionExpiresAt": row["expires_at"],
    }


def append_turn(user: dict[str, Any], chat: dict[str, Any], message: str, answer: str, trace: list[dict[str, Any]]) -> None:
    """Store a question and its answer, keeping the newest ``MAX_HISTORY_MESSAGES`` messages."""
    now = users.now_ms()
    assistant_message: dict[str, Any] = {"role": "assistant", "content": answer[:MAX_HISTORY_MESSAGE_CHARS]}
    if trace:
        assistant_message["trace"] = trace
    messages = [*chat["messages"], {"role": "user", "content": message[:MAX_HISTORY_MESSAGE_CHARS]}, assistant_message]
    messages = messages[-MAX_HISTORY_MESSAGES:]
    title = message[:60] if chat["title"] == "New chat" else chat["title"]
    if is_private(user):
        stored = _live_private_chats(user["id"]).get(chat["id"])
        if stored is not None:
            stored.update(messages=messages, title=title, updatedAt=now, sessionExpiresAt=now + CHAT_TTL_MS)
        return
    with connect_db() as connection:
        connection.execute(
            "UPDATE chats SET messages = ?, title = ?, updated_at = ?, expires_at = ? WHERE user_id = ? AND id = ?",
            (json.dumps(messages), title, now, now + CHAT_TTL_MS, user["id"], chat["id"]),
        )


def delete_chat(user: dict[str, Any], chat_id: str) -> bool:
    """Delete one chat; return False when the profile has no such chat."""
    if is_private(user):
        return _live_private_chats(user["id"]).pop(chat_id, None) is not None
    with connect_db() as connection:
        result = connection.execute("DELETE FROM chats WHERE user_id = ? AND id = ?", (user["id"], chat_id))
    return result.rowcount > 0


def export_chats(user: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every unexpired chat with its messages, newest first."""
    return [chat for summary in list_chats(user) if (chat := read_chat(user, summary["id"])) is not None]

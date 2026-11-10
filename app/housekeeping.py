"""Scheduled clean-up of rows that would otherwise wait for their owner to come back.

Run from ``snapshots.run_scheduler`` on every tick:

- ``sweep_sessions()``: browser session rows idle past ``users.SESSION_MAX_AGE_SECONDS``, and rows
  bound to no profile (a Google sign-in in progress, a Google account waiting to create or claim a
  profile, an admin sign-in, a signed-out cookie) idle past ``UNBOUND_SESSION_SECONDS``. Unlocked
  private keys whose session is gone are dropped from memory at the same time.
- ``sweep_expired_data()`` (once per local day): chats past ``expires_at``, answer runs past
  ``agent_runs.MAX_AGE_MS``, and INDmoney and broker snapshots older than each profile's retention.
  Every filter column is plain, so no profile key is needed.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Final

from . import accounts, admin, agent_runs, snapshots, unlock, users
from .db import connect_db

logger = logging.getLogger("ca_mode.housekeeping")

# Unbound rows outlive their longest use: an admin session lasts admin.SESSION_SECONDS.
UNBOUND_SESSION_SECONDS: Final = max(admin.SESSION_SECONDS, accounts.PENDING_SECONDS)

_last_data_sweep: date | None = None


def sweep_sessions(*, now_ms: int | None = None) -> dict[str, int]:
    """Delete expired and abandoned session rows and forget unlocked keys without a live session."""
    now = users.now_ms() if now_ms is None else now_ms
    max_age_ms = users.SESSION_MAX_AGE_SECONDS * 1000
    with connect_db() as connection:
        expired = connection.execute(
            "DELETE FROM oauth_sessions WHERE updated_at < ?", (now - max_age_ms,),
        ).rowcount
        unbound = connection.execute(
            "DELETE FROM oauth_sessions WHERE user_id IS NULL AND updated_at < ?",
            (now - UNBOUND_SESSION_SECONDS * 1000,),
        ).rowcount
    forgotten = unlock.sweep(max_age_ms, now_ms=now)
    return {"expired": expired, "unbound": unbound, "keysForgotten": forgotten}


def sweep_expired_data(today: date, *, now_ms: int | None = None) -> dict[str, int]:
    """Delete expired chats and answer runs, and snapshots past each profile's retention, for every profile."""
    now = users.now_ms() if now_ms is None else now_ms
    with connect_db() as connection:
        chats_deleted = connection.execute("DELETE FROM chats WHERE expires_at <= ?", (now,)).rowcount
        runs_deleted = connection.execute(
            "DELETE FROM agent_runs WHERE created_at < ?", (now - agent_runs.MAX_AGE_MS,),
        ).rowcount
        before = connection.total_changes
        user_ids = [row["id"] for row in connection.execute("SELECT id FROM users")]
        for user_id in user_ids:
            snapshots.prune(connection, user_id, today)
        snapshots_deleted = connection.total_changes - before
    return {"chats": chats_deleted, "agentRuns": runs_deleted, "snapshots": snapshots_deleted}


def run_due(now: datetime) -> None:
    """Sweep sessions on every call and expired data once per local day. Logs counts, never row contents."""
    global _last_data_sweep
    ended = sweep_sessions()
    if any(ended.values()):
        logger.info("sessions_swept", extra=ended)
    today = now.astimezone(snapshots.SNAPSHOT_TIMEZONE).date()
    if _last_data_sweep == today:
        return
    deleted = sweep_expired_data(today)
    _last_data_sweep = today
    if any(deleted.values()):
        logger.info("expired_data_swept", extra=deleted)

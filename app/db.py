"""SQLite connection, schema, and migrations for CA Mode's local data."""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger("ca_mode.db")

DATA_DIR = Path(os.getenv("APP_DATA_DIR", Path(__file__).resolve().parent.parent / ".data"))
DATABASE_PATH = DATA_DIR / "codemode.sqlite3"

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        pin_hash TEXT NOT NULL,
        profile TEXT NOT NULL DEFAULT '{}',
        indmoney TEXT NOT NULL DEFAULT '{}',
        mcp_tools TEXT,
        mcp_tools_fetched_at INTEGER,
        snapshot_attempted_at INTEGER NOT NULL DEFAULT 0,
        storage_mode TEXT NOT NULL DEFAULT 'device',
        snapshot_retention_days INTEGER NOT NULL DEFAULT 365,
        failed_logins INTEGER NOT NULL DEFAULT 0,
        locked_until INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS oauth_sessions (
        id TEXT PRIMARY KEY,
        data TEXT NOT NULL,
        updated_at INTEGER NOT NULL,
        user_id TEXT REFERENCES users(id) ON DELETE SET NULL
    );
    CREATE TABLE IF NOT EXISTS chats (
        id TEXT NOT NULL,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        messages TEXT NOT NULL,
        updated_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        agent_type TEXT NOT NULL DEFAULT 'portfolio',
        PRIMARY KEY (user_id, id)
    );
    CREATE INDEX IF NOT EXISTS chats_by_user_update ON chats(user_id, updated_at DESC);
    CREATE TABLE IF NOT EXISTS portfolio_snapshots (
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        as_of_date TEXT NOT NULL,
        captured_at INTEGER NOT NULL,
        networth_inr REAL,
        data TEXT NOT NULL,
        PRIMARY KEY (user_id, as_of_date)
    );
    CREATE TABLE IF NOT EXISTS consents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        policy_version TEXT NOT NULL,
        storage_mode TEXT NOT NULL,
        snapshot_retention_days INTEGER NOT NULL,
        accepted_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS consents_by_user ON consents(user_id, id DESC);
"""

# Columns added to ``users`` after its first release; CREATE TABLE IF NOT EXISTS
# leaves existing tables alone, so these are added in place.
_ADDED_USER_COLUMNS = {
    "storage_mode": "TEXT NOT NULL DEFAULT 'device'",
    "snapshot_retention_days": "INTEGER NOT NULL DEFAULT 365",
}


@contextmanager
def connect_db() -> Iterator[sqlite3.Connection]:
    """Yield a connection that commits on success, rolls back on error, and always closes."""
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            yield connection
    finally:
        connection.close()


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def _migrate_legacy_tables(connection: sqlite3.Connection) -> None:
    """Move pre-profile data aside so profiles can adopt it on first sign-in.

    Before profiles existed, chats were keyed by browser session. That table is
    renamed ``chats_legacy``; ``users.adopt_session_data`` moves a session's rows
    into the signed-in profile.
    """
    tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "chats" in tables and "session_id" in _columns(connection, "chats"):
        if "agent_type" not in _columns(connection, "chats"):
            connection.execute("ALTER TABLE chats ADD COLUMN agent_type TEXT NOT NULL DEFAULT 'portfolio'")
        connection.execute("ALTER TABLE chats RENAME TO chats_legacy")
        connection.execute("DROP INDEX IF EXISTS chats_by_session_update")
    if "oauth_sessions" in tables and "user_id" not in _columns(connection, "oauth_sessions"):
        connection.execute("ALTER TABLE oauth_sessions ADD COLUMN user_id TEXT REFERENCES users(id) ON DELETE SET NULL")


def _add_user_columns(connection: sqlite3.Connection) -> None:
    existing = _columns(connection, "users")
    for name, definition in _ADDED_USER_COLUMNS.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")


def initialize_db() -> None:
    with connect_db() as connection:
        # WAL is persistent in the database file, so it only needs setting once.
        connection.execute("PRAGMA journal_mode = WAL")
        _migrate_legacy_tables(connection)
        connection.executescript(_SCHEMA)
        _add_user_columns(connection)
    try:
        os.chmod(DATABASE_PATH, 0o600)
    except OSError:
        logger.warning("Could not restrict local database file permissions")

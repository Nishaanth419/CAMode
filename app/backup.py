"""Local database backups: daily consistent copies, verification, pruning, and restore.

Copies are made with SQLite's online backup API, so they are consistent even while
the app is running. Only the database is copied, never the master key: chats,
snapshots and profile fields in it are sealed with each profile's PIN, and keeping
the key beside the database would undo the token encryption. Losing the key only
means reconnecting INDmoney.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

from . import db

logger = logging.getLogger("ca_mode.backup")

BACKUP_INTERVAL: Final = timedelta(hours=24)
_NAME_RE: Final = re.compile(r"^codemode-(\d{8}-\d{6})\.sqlite3$")
# cas_statements is optional: backups made before CAS uploads existed lack it.
_REQUIRED_TABLES: Final = {"users", "chats", "portfolio_snapshots", "consents"}


class BackupError(RuntimeError):
    """A backup could not be made, verified or restored; the message is safe to show."""


def backup_dir() -> Path:
    return Path(os.getenv("BACKUP_DIR", "").strip() or Path.home() / "ca-mode-backups").expanduser()


def backups_to_keep() -> int:
    try:
        return max(1, int(os.getenv("BACKUP_KEEP", "14")))
    except ValueError:
        return 14


def backups_enabled() -> bool:
    return os.getenv("BACKUP_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


def list_backups() -> list[Path]:
    """Backup files, newest first."""
    directory = backup_dir()
    if not directory.is_dir():
        return []
    return sorted((path for path in directory.iterdir() if _NAME_RE.match(path.name)), reverse=True)


def backup_time(path: Path) -> datetime:
    match = _NAME_RE.match(path.name)
    if not match:
        raise BackupError(f"{path.name} is not a CA Mode backup file name.")
    return datetime.strptime(match.group(1), "%Y%m%d-%H%M%S")


def verify_backup(path: Path) -> dict[str, int]:
    """Check a backup opens, passes SQLite's integrity check and has CA Mode's tables; return row counts."""
    if not path.is_file():
        raise BackupError(f"{path} does not exist.")
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError(f"{path.name} failed SQLite's integrity check.")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if missing := _REQUIRED_TABLES - tables:
                raise BackupError(f"{path.name} is missing tables: {', '.join(sorted(missing))}.")
            return {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 - fixed names
                for table in ("users", "chats", "portfolio_snapshots")
            }
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"{path.name} is not a readable SQLite database: {exc}") from exc


def _prune(keep: int) -> None:
    for old in list_backups()[keep:]:
        old.unlink(missing_ok=True)


def create_backup(*, now: datetime | None = None) -> Path:
    """Write a verified copy of the database to ``backup_dir()`` and prune old copies."""
    directory = backup_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / f"codemode-{(now or datetime.now()):%Y%m%d-%H%M%S}.sqlite3"
    partial = target.with_name(target.name + ".partial")
    try:
        with closing(sqlite3.connect(db.DATABASE_PATH, timeout=30)) as source, \
                closing(sqlite3.connect(partial)) as destination:
            source.backup(destination)
            # The copy inherits WAL mode, which makes every later open create -wal/-shm files;
            # a backup should be one self-contained file.
            destination.execute("PRAGMA journal_mode = DELETE")
        os.chmod(partial, 0o600)
        verify_backup(partial)
        partial.replace(target)
    except (sqlite3.Error, OSError) as exc:
        partial.unlink(missing_ok=True)
        raise BackupError(f"Could not write a backup to {directory}: {exc}") from exc
    except BackupError:
        partial.unlink(missing_ok=True)
        raise
    _prune(backups_to_keep())
    logger.info("backup_created", extra={"path": str(target)})
    return target


def backup_if_due(*, now: datetime | None = None) -> Path | None:
    """Create a backup when the newest one is older than ``BACKUP_INTERVAL`` (or none exists)."""
    if not backups_enabled() or not db.DATABASE_PATH.exists():
        return None
    current = now or datetime.now()
    existing = list_backups()
    if existing and current - backup_time(existing[0]) < BACKUP_INTERVAL:
        return None
    return create_backup(now=current)


def status() -> dict[str, Any]:
    existing = list_backups()
    return {
        "directory": str(backup_dir()),
        "enabled": backups_enabled(),
        "count": len(existing),
        "keep": backups_to_keep(),
        "latest": backup_time(existing[0]).isoformat(timespec="seconds") if existing else None,
    }


def restore_backup(path: Path, *, now: datetime | None = None) -> Path:
    """Replace the database with a verified backup; the current database is moved aside, not deleted.

    Stop the app first: a running server keeps the old database open. Returns where the
    previous database was moved.
    """
    verify_backup(path)
    database = db.DATABASE_PATH
    aside = database.with_name(f"codemode-before-restore-{(now or datetime.now()):%Y%m%d-%H%M%S}.sqlite3")
    if database.exists():
        database.replace(aside)
    for suffix in ("-wal", "-shm"):
        sidecar = database.with_name(database.name + suffix)
        if sidecar.exists():
            sidecar.replace(aside.with_name(aside.name + suffix))
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copyfile(path, database)
    os.chmod(database, 0o600)
    return aside

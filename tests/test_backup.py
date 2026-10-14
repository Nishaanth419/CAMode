from __future__ import annotations

import asyncio
import sqlite3
import stat
import sys
from datetime import datetime, timedelta

import pytest

from app import backup, db, snapshots, users

NOON = datetime(2026, 10, 14, 12, 0, 0)


def make_profile(name: str = "Asha") -> str:
    return users.create_user(name, users.hash_pin("2468"))["id"]


def test_backup_is_a_private_verified_copy():
    make_profile()

    path = backup.create_backup(now=NOON)

    assert path.name == "codemode-20261014-120000.sqlite3"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert backup.verify_backup(path) == {"users": 1, "chats": 0, "portfolio_snapshots": 0}
    assert sorted(item.name for item in path.parent.iterdir()) == [path.name]  # No .partial or -wal/-shm files.


def test_backup_is_consistent_while_a_write_transaction_is_open():
    make_profile()
    writer = sqlite3.connect(db.DATABASE_PATH)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO users(id, display_name, pin_hash, created_at, updated_at) VALUES ('x', 'X', 'h', 1, 1)")
    try:
        path = backup.create_backup(now=NOON)
    finally:
        writer.rollback()
        writer.close()

    assert backup.verify_backup(path)["users"] == 1  # The uncommitted row is not in the copy.


def test_old_backups_are_pruned(monkeypatch):
    monkeypatch.setenv("BACKUP_KEEP", "2")
    make_profile()

    for day in range(4):
        backup.create_backup(now=NOON + timedelta(days=day))

    assert [path.name for path in backup.list_backups()] == [
        "codemode-20261017-120000.sqlite3", "codemode-20261016-120000.sqlite3",
    ]


def test_backup_if_due_runs_once_a_day(monkeypatch):
    make_profile()

    assert backup.backup_if_due(now=NOON) is not None
    assert backup.backup_if_due(now=NOON + timedelta(hours=23)) is None
    assert backup.backup_if_due(now=NOON + timedelta(hours=24)) is not None

    monkeypatch.setenv("BACKUP_ENABLED", "false")
    assert backup.backup_if_due(now=NOON + timedelta(days=5)) is None


def test_verify_rejects_bad_files(tmp_path):
    with pytest.raises(backup.BackupError, match="does not exist"):
        backup.verify_backup(tmp_path / "missing.sqlite3")
    garbage = tmp_path / "garbage.sqlite3"
    garbage.write_bytes(b"not a database" * 100)
    with pytest.raises(backup.BackupError, match="not a readable SQLite database"):
        backup.verify_backup(garbage)
    other = tmp_path / "other.sqlite3"
    with sqlite3.connect(other) as connection:
        connection.execute("CREATE TABLE notes(x)")
    with pytest.raises(backup.BackupError, match="missing tables"):
        backup.verify_backup(other)


def test_restore_replaces_the_database_and_keeps_the_old_one():
    kept = make_profile("Asha")
    path = backup.create_backup(now=NOON)
    later = make_profile("Ravi")

    aside = backup.restore_backup(path, now=NOON + timedelta(hours=1))

    assert users.get_user(kept) is not None and users.get_user(later) is None
    assert aside.name == "codemode-before-restore-20261014-130000.sqlite3"
    with sqlite3.connect(f"file:{aside}?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
    assert stat.S_IMODE(db.DATABASE_PATH.stat().st_mode) == 0o600


def test_restore_refuses_a_corrupt_backup_without_touching_the_database(tmp_path):
    user_id = make_profile()
    bad = tmp_path / "codemode-20261014-120000.sqlite3"
    bad.write_bytes(b"broken")

    with pytest.raises(backup.BackupError):
        backup.restore_backup(bad)

    assert users.get_user(user_id) is not None


def test_backup_api(client, signed_in):
    assert client.get("/api/backups").json()["count"] == 0

    created = client.post("/api/backups")

    assert created.status_code == 201
    body = created.json()
    assert body["file"].startswith("codemode-") and body["count"] == 1 and body["latest"]


def test_backup_api_requires_a_profile(client):
    client.get("/api/session")
    assert client.get("/api/backups").status_code == 401
    assert client.post("/api/backups").status_code == 401


def test_scheduler_makes_the_daily_backup(monkeypatch):
    calls = []

    async def no_refresh(*, mcp_url, now=None):
        return 0

    def fake_backup_if_due(**kwargs):
        calls.append("backup")
        stop.set()

    stop = asyncio.Event()
    monkeypatch.setattr(snapshots, "refresh_due_users", no_refresh)
    monkeypatch.setattr(backup, "backup_if_due", fake_backup_if_due)

    asyncio.run(snapshots.run_scheduler(stop, mcp_url="https://mcp", poll_seconds=0.01))

    assert calls == ["backup"]


def test_backup_command_line(monkeypatch, capsys):
    from scripts import backup as cli

    make_profile()
    monkeypatch.setattr(sys, "argv", ["backup", "now"])
    assert cli.main() == 0
    path = backup.list_backups()[0]
    monkeypatch.setattr(sys, "argv", ["backup", "verify", str(path)])
    assert cli.main() == 0
    assert "OK: 1 users, 0 chats, 0 portfolio_snapshots" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["backup", "verify", str(path.with_name("missing.sqlite3"))])
    assert cli.main() == 1

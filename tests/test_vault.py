from __future__ import annotations

import json
import stat
from datetime import datetime

import pytest
from cryptography.fernet import Fernet

from app import db, privacy, snapshots, users, vault
from conftest import sign_up

TOKENS = {
    "accessToken": "access-token-value", "refreshToken": "refresh-token-value",
    "expiresAt": 1, "clientId": "client-id", "clientSecret": "client-secret-value",
}


@pytest.fixture
def use_key(monkeypatch):
    """Switch the master key for the rest of the test, restoring the suite's key afterwards."""

    def switch(key: str | None = None, *, key_file=None) -> None:
        monkeypatch.delenv("APP_MASTER_KEY", raising=False)
        monkeypatch.delenv("APP_MASTER_KEY_FILE", raising=False)
        if key is not None:
            monkeypatch.setenv("APP_MASTER_KEY", key)
        if key_file is not None:
            monkeypatch.setenv("APP_MASTER_KEY_FILE", str(key_file))
        vault._fernet.cache_clear()

    yield switch
    vault._fernet.cache_clear()


def raw_column(table: str, column: str, row_id: str) -> str:
    with db.connect_db() as connection:
        return connection.execute(f"SELECT {column} FROM {table} WHERE id = ?", (row_id,)).fetchone()[0]


def test_encrypt_round_trips_and_marks_values():
    stored = vault.encrypt("secret")

    assert stored.startswith(vault.PREFIX) and "secret" not in stored
    assert vault.decrypt(stored) == "secret"
    assert vault.encrypt("secret") != stored  # Fresh IV each time.


def test_decrypt_with_another_key_fails(use_key):
    stored = vault.encrypt("secret")
    use_key(Fernet.generate_key().decode())

    with pytest.raises(vault.VaultError, match="current master key"):
        vault.decrypt(stored)
    with pytest.raises(vault.VaultError, match="not encrypted"):
        vault.decrypt("plain")


def test_key_file_is_created_private_and_reused(use_key, tmp_path):
    key_file = tmp_path / "keys" / "master.key"
    use_key(key_file=key_file)

    stored = vault.encrypt("secret")

    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_file.parent.stat().st_mode) == 0o700
    use_key(key_file=key_file)
    assert vault.decrypt(stored) == "secret"


def test_invalid_key_is_rejected_at_startup(use_key, tmp_path):
    use_key("not-a-key")
    with pytest.raises(vault.VaultError, match="APP_MASTER_KEY is not a valid Fernet key"):
        vault.check_key()

    bad_file = tmp_path / "master.key"
    bad_file.write_text("short")
    use_key(key_file=bad_file)
    with pytest.raises(vault.VaultError, match="master key file"):
        vault.check_key()


def test_indmoney_tokens_are_stored_encrypted():
    user_id = users.create_user("Asha", users.hash_pin("2468"))["id"]

    users.save_indmoney(user_id, TOKENS)

    stored = raw_column("users", "indmoney", user_id)
    assert vault.is_encrypted(stored)
    assert not any(value in stored for value in ("access-token-value", "refresh-token-value", "client-secret-value"))
    assert users.get_indmoney(user_id) == TOKENS
    assert users.users_with_indmoney() == [user_id]

    users.clear_indmoney(user_id)
    assert raw_column("users", "indmoney", user_id) == "{}"
    assert users.users_with_indmoney() == []


def test_session_data_is_stored_encrypted():
    users.save_session("sid", {"clientSecret": "client-secret-value", "verifier": "pkce-verifier"})

    stored = raw_column("oauth_sessions", "data", "sid")
    assert vault.is_encrypted(stored) and "client-secret-value" not in stored
    assert users.get_session("sid")["clientSecret"] == "client-secret-value"

    users.touch_session("new-sid")
    assert raw_column("oauth_sessions", "data", "new-sid") == "{}"


def test_startup_migration_encrypts_plaintext_rows_once():
    user_id = users.create_user("Asha", users.hash_pin("2468"))["id"]
    with db.connect_db() as connection:
        connection.execute("UPDATE users SET indmoney = ? WHERE id = ?", (json.dumps(TOKENS), user_id))
        connection.execute(
            "INSERT INTO oauth_sessions(id, data, updated_at) VALUES ('legacy', ?, ?)",
            (json.dumps({"accessToken": "session-token"}), users.now_ms()),
        )
    assert users.get_indmoney(user_id) == TOKENS  # Plaintext still reads before migration.

    assert users.encrypt_stored_secrets() == {"encrypted": 2, "unreadable": 0}

    assert vault.is_encrypted(raw_column("users", "indmoney", user_id))
    assert vault.is_encrypted(raw_column("oauth_sessions", "data", "legacy"))
    assert users.get_indmoney(user_id) == TOKENS
    assert users.get_session("legacy")["accessToken"] == "session-token"
    assert users.encrypt_stored_secrets() == {"encrypted": 0, "unreadable": 0}


def test_tokens_under_another_key_read_as_disconnected_and_are_kept(use_key, caplog):
    user = users.create_user("Asha", users.hash_pin("2468"))
    privacy.record_consent(user, privacy.ConsentRequest(policyVersion=privacy.POLICY_VERSION, storageMode="device"))
    users.save_indmoney(user["id"], TOKENS)
    stored = raw_column("users", "indmoney", user["id"])
    use_key(Fernet.generate_key().decode())

    assert users.encrypt_stored_secrets() == {"encrypted": 0, "unreadable": 1}
    assert "stored_secrets_unreadable" in caplog.text

    assert users.get_indmoney(user["id"]) == {}
    assert users.users_with_indmoney() == []
    assert user["id"] not in snapshots.due_user_ids(datetime.now(snapshots.SNAPSHOT_TIMEZONE))
    assert raw_column("users", "indmoney", user["id"]) == stored


def test_export_reports_connection_without_tokens(client):
    user = sign_up(client)
    users.save_indmoney(user["id"], TOKENS)

    response = client.post("/api/users/me/export", json={"pin": "2468"})

    assert response.json()["indmoneyConnected"] is True
    assert "access-token-value" not in response.text

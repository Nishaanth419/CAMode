from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from app import chats, db, main, privacy, snapshots, unlock, users
from conftest import sign_up


def accept(client, storage_mode: str = "device", **extra):
    return client.post("/api/privacy/consent", json={
        "policyVersion": privacy.POLICY_VERSION, "storageMode": storage_mode, **extra,
    })


def connect_indmoney(user_id: str) -> None:
    users.save_indmoney(user_id, {
        "accessToken": "access-token", "refreshToken": "refresh", "expiresAt": users.now_ms() + 3_600_000,
        "clientId": "client-id", "clientSecret": "client-secret",
    })


def store_days_ago(user_id: str, *days_ago: int) -> None:
    today = datetime.now(snapshots.SNAPSHOT_TIMEZONE).replace(hour=9, minute=0, second=0, microsecond=0)
    for days in days_ago:
        snapshots.store_snapshot(user_id, today - timedelta(days=days), {"networth_snapshot": '{"net_worth": 1}'}, {})


def saved_chat_count(user_id: str) -> int:
    with db.connect_db() as connection:
        return connection.execute("SELECT COUNT(*) AS n FROM chats WHERE user_id = ?", (user_id,)).fetchone()["n"]


def fake_answer(monkeypatch, text: str = "Diversification spreads risk.") -> None:
    async def fake_run_agent(*args, **kwargs):
        return text

    monkeypatch.setattr(main, "run_agent", fake_run_agent)


@pytest.fixture
def unconsented(client):
    return sign_up(client, storage_mode=None)


def test_new_profile_must_accept_notice_before_chatting(client, unconsented):
    session = client.get("/api/session").json()
    assert session["consentRequired"] is True

    state = client.get("/api/privacy").json()
    assert state["consentRequired"] is True
    assert state["policyVersion"] == privacy.POLICY_VERSION
    titles = [item["title"] for item in state["notice"]]
    assert titles[0] == "What is saved on this computer" and "What leaves this computer" in titles

    blocked = client.post("/api/chats", json={"agentType": "finance"})
    assert blocked.status_code == 403
    assert blocked.json()["consentRequired"] is True
    assert client.post("/api/chat", json={"message": "hi", "chatId": "x"}).status_code == 403
    assert client.post("/api/snapshots/refresh").status_code == 403
    assert client.get("/api/tools").status_code == 403

    assert accept(client).status_code == 200
    assert client.get("/api/session").json()["consentRequired"] is False
    assert client.post("/api/chats", json={"agentType": "finance"}).status_code == 201


def test_connect_indmoney_redirects_home_until_notice_accepted(client, unconsented, monkeypatch):
    def fail_registration(*args, **kwargs):
        raise AssertionError("OAuth must not start before consent")

    monkeypatch.setattr(main, "register_oauth_client", fail_registration)
    response = client.get("/auth/indmoney/connect")

    assert response.status_code == 302
    assert response.headers["location"].endswith("/")


def test_consent_for_stale_notice_is_rejected(client, unconsented):
    response = client.post("/api/privacy/consent", json={"policyVersion": "2000-01-01", "storageMode": "device"})

    assert response.status_code == 409
    assert response.json()["policyVersion"] == privacy.POLICY_VERSION
    assert privacy.latest_consent(unconsented["id"]) is None


def test_consent_rejects_unknown_modes_and_retention(client, unconsented):
    assert accept(client, storage_mode="server").status_code == 422
    assert accept(client, snapshotRetentionDays=7).status_code == 422
    assert accept(client, extra="field").status_code == 422


def test_new_notice_version_asks_again(client, signed_in, monkeypatch):
    monkeypatch.setattr(privacy, "POLICY_VERSION", "2099-01-01")

    assert client.get("/api/session").json()["consentRequired"] is True
    assert client.post("/api/chats", json={"agentType": "finance"}).status_code == 403


def test_every_choice_is_recorded(client, signed_in):
    assert accept(client, snapshotRetentionDays=90).json()["snapshotRetentionDays"] == 90

    exported = client.post("/api/users/me/export", json={"pin": "2468"}).json()
    assert [(item["storageMode"], item["snapshotRetentionDays"]) for item in exported["consents"]] == [
        ("device", 365), ("device", 90),
    ]


def test_private_mode_keeps_chats_in_memory_only(client, monkeypatch):
    user = sign_up(client, storage_mode="private")
    fake_answer(monkeypatch)

    chat = client.post("/api/chats", json={"agentType": "finance"}).json()
    reply = client.post("/api/chat", json={"message": "What is diversification?", "chatId": chat["id"]})

    assert reply.status_code == 200
    assert saved_chat_count(user["id"]) == 0
    assert client.get("/api/chats").json()["chats"][0]["title"] == "What is diversification?"
    assert [item["role"] for item in client.get(f"/api/chats/{chat['id']}").json()["messages"]] == ["user", "assistant"]

    client.post("/api/session/logout")
    client.post("/api/session/login", json={"userId": user["id"], "pin": "2468"})
    assert client.get("/api/chats").json()["chats"] == []


def test_private_chats_are_isolated_and_deletable(client, monkeypatch):
    user = sign_up(client, storage_mode="private")
    chat = client.post("/api/chats", json={"agentType": "finance"}).json()

    assert chats.read_chat({"id": "someone-else", "storageMode": "private"}, chat["id"]) is None
    assert client.delete(f"/api/chats/{chat['id']}").json() == {"deleted": True}
    assert client.delete(f"/api/chats/{chat['id']}").status_code == 404
    assert chats.list_chats(users.get_user(user["id"])) == []


def test_private_chat_limit_evicts_oldest(client, monkeypatch):
    sign_up(client, storage_mode="private")
    monkeypatch.setattr(chats, "MAX_CHATS_PER_USER", 3)
    first = client.post("/api/chats", json={"agentType": "finance"}).json()
    for _ in range(3):
        client.post("/api/chats", json={"agentType": "finance"})

    listed = [item["id"] for item in client.get("/api/chats").json()["chats"]]
    assert len(listed) == 3 and first["id"] not in listed


def test_switching_to_private_deletes_saved_history(client, signed_in):
    client.post("/api/chats", json={"agentType": "finance"})
    connect_indmoney(signed_in["id"])
    store_days_ago(signed_in["id"], 1, 0)

    state = accept(client, "private").json()

    assert state["storageMode"] == "private"
    assert saved_chat_count(signed_in["id"]) == 0
    assert snapshots.latest_snapshot(signed_in["id"]) is None
    assert users.get_indmoney(signed_in["id"])["accessToken"] == "access-token"
    refresh = client.post("/api/snapshots/refresh")
    assert refresh.status_code == 409


def test_switching_back_to_device_drops_private_chats(client):
    user = sign_up(client, storage_mode="private")
    client.post("/api/chats", json={"agentType": "finance"})

    accept(client, "device")

    assert client.get("/api/chats").json()["chats"] == []
    client.post("/api/chats", json={"agentType": "finance"})
    assert saved_chat_count(user["id"]) == 1


def test_shorter_retention_prunes_existing_snapshots(client, signed_in):
    store_days_ago(signed_in["id"], 120, 60, 10, 0)

    accept(client, snapshotRetentionDays=30)

    keys = unlock.keys_from_pin(users.get_user(signed_in["id"]), "2468")
    dates = [point["date"] for point in snapshots.networth_history(signed_in["id"], 365, keys=keys)]
    assert len(dates) == 2
    store_days_ago(signed_in["id"], 45, 0)  # Pruning runs relative to each capture's date.
    assert len(snapshots.networth_history(signed_in["id"], 365, keys=keys)) == 2


def test_export_requires_pin_and_omits_secrets(client, signed_in, monkeypatch):
    connect_indmoney(signed_in["id"])
    store_days_ago(signed_in["id"], 0)
    fake_answer(monkeypatch)
    chat = client.post("/api/chats", json={"agentType": "finance"}).json()
    client.post("/api/chat", json={"message": "What is diversification?", "chatId": chat["id"]})

    assert client.post("/api/users/me/export", json={"pin": "0000"}).status_code == 401
    response = client.post("/api/users/me/export", json={"pin": "2468"})

    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    body = response.json()
    assert body["profile"]["displayName"] == "Asha"
    assert body["indmoneyConnected"] is True
    assert body["chats"][0]["messages"][0]["content"] == "What is diversification?"
    assert body["snapshots"][0]["results"] == {"networth_snapshot": '{"net_worth": 1}'}
    raw = response.text
    for secret in ("access-token", "client-secret", "refresh", "scrypt$"):
        assert secret not in raw


def test_clear_history_keeps_profile_and_connection(client, signed_in):
    connect_indmoney(signed_in["id"])
    client.post("/api/chats", json={"agentType": "finance"})
    store_days_ago(signed_in["id"], 0)

    assert client.delete("/api/users/me/history").json() == {"cleared": True}

    assert saved_chat_count(signed_in["id"]) == 0
    assert snapshots.latest_snapshot(signed_in["id"]) is None
    assert users.get_indmoney(signed_in["id"])["accessToken"] == "access-token"
    assert client.get("/api/session").json()["consentRequired"] is False


def test_privacy_routes_require_profile(client):
    client.get("/api/session")
    assert client.get("/api/privacy").status_code == 401
    assert accept(client).status_code == 401
    assert client.post("/api/users/me/export", json={"pin": "2468"}).status_code == 401
    assert client.delete("/api/users/me/history").status_code == 401


def test_refresh_user_refuses_private_or_unconsented_profiles(monkeypatch):
    user = users.create_user("Ravi", users.hash_pin("1357"))
    connect_indmoney(user["id"])

    async def must_not_capture(*args, **kwargs):
        raise AssertionError("INDmoney must not be called")

    monkeypatch.setattr(snapshots, "capture_snapshot", must_not_capture)
    with pytest.raises(snapshots.SnapshotError, match="off for this profile"):
        asyncio.run(snapshots.refresh_user(user["id"], mcp_url="https://mcp"))

    privacy.record_consent(user, privacy.ConsentRequest(policyVersion=privacy.POLICY_VERSION, storageMode="private"))
    with pytest.raises(snapshots.SnapshotError, match="off for this profile"):
        asyncio.run(snapshots.refresh_user(user["id"], mcp_url="https://mcp"))


def test_due_users_skip_private_unconsented_and_stale_consent(monkeypatch):
    def make(name: str, mode: str | None) -> str:
        user = users.create_user(name, users.hash_pin("1357"))
        connect_indmoney(user["id"])
        if mode:
            privacy.record_consent(user, privacy.ConsentRequest(policyVersion=privacy.POLICY_VERSION, storageMode=mode))
        return user["id"]

    saving, private, unconsented = make("Asha", "device"), make("Ravi", "private"), make("Meera", None)
    now = datetime.now(snapshots.SNAPSHOT_TIMEZONE)

    due = snapshots.due_user_ids(now)
    assert saving in due and private not in due and unconsented not in due

    monkeypatch.setattr(privacy, "POLICY_VERSION", "2099-01-01")
    assert snapshots.due_user_ids(now) == []


def test_initialize_db_adds_storage_columns_to_existing_users_table():
    with db.connect_db() as connection:
        connection.executescript(
            "DROP TABLE consents; DROP TABLE portfolio_snapshots; DROP TABLE chats; DROP TABLE oauth_sessions; "
            "DROP TABLE users; "
            "CREATE TABLE users (id TEXT PRIMARY KEY, display_name TEXT NOT NULL UNIQUE COLLATE NOCASE, "
            "pin_hash TEXT NOT NULL, profile TEXT NOT NULL DEFAULT '{}', indmoney TEXT NOT NULL DEFAULT '{}', "
            "mcp_tools TEXT, mcp_tools_fetched_at INTEGER, snapshot_attempted_at INTEGER NOT NULL DEFAULT 0, "
            "failed_logins INTEGER NOT NULL DEFAULT 0, locked_until INTEGER NOT NULL DEFAULT 0, "
            "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);"
            "INSERT INTO users(id, display_name, pin_hash, created_at, updated_at) VALUES ('old', 'Old', 'x', 1, 1);",
        )

    db.initialize_db()

    user = users.get_user("old")
    assert (user["storageMode"], user["snapshotRetentionDays"]) == ("device", 365)
    assert privacy.has_current_consent("old") is False
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect_db() as connection:
            connection.execute(
                "INSERT INTO consents(user_id, policy_version, storage_mode, snapshot_retention_days, accepted_at) "
                "VALUES ('missing', 'v', 'device', 365, 1)",
            )


def test_deleting_profile_removes_consents(client, signed_in):
    assert privacy.latest_consent(signed_in["id"]) is not None
    client.request("DELETE", "/api/users/me", json={"pin": "2468"})
    assert privacy.latest_consent(signed_in["id"]) is None


def test_notice_mentions_ai_providers_and_permanent_loss():
    text = json.dumps(privacy.NOTICE)
    assert "OpenAI or Groq" in text
    assert "Ollama (on this device)" in text and "none of it leaves" in text
    assert "recovery code" in text and "cannot be recovered" in text


def test_capture_finishing_after_switch_to_private_is_not_stored(client, signed_in):
    accept(client, "private")

    with pytest.raises(snapshots.SnapshotError, match="off for this profile"):
        store_days_ago(signed_in["id"], 0)
    assert snapshots.latest_snapshot(signed_in["id"]) is None

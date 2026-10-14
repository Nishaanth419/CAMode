from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import chats, db, main, privacy, sealing, snapshots, unlock, users
from conftest import sign_up

PIN = "2468"


def raw(sql: str, *params) -> list[tuple]:
    with db.connect_db() as connection:
        return [tuple(row) for row in connection.execute(sql, params).fetchall()]


def fake_answer(monkeypatch, text: str = "Diversification spreads risk.") -> None:
    async def fake_run_agent(*args, **kwargs):
        return text

    monkeypatch.setattr(main, "run_agent", fake_run_agent)


def chat_once(client, message: str = "What is diversification?") -> str:
    chat = client.post("/api/chats", json={"agentType": "finance"}).json()
    assert client.post("/api/chat", json={"message": message, "chatId": chat["id"]}).status_code == 200
    return chat["id"]


def restart_server() -> None:
    """Simulate a restart: unlocked keys live only in memory."""
    unlock._unlocked.clear()


def store_today(user_id: str, networth: float = 4200000.0) -> None:
    snapshots.store_snapshot(
        user_id, datetime.now(snapshots.SNAPSHOT_TIMEZONE), {"networth_snapshot": json.dumps({"net_worth": networth})}, {},
    )


def accept_notice(client) -> None:
    assert client.post("/api/privacy/consent", json={
        "policyVersion": privacy.POLICY_VERSION, "storageMode": "device",
    }).status_code == 200


def test_sign_up_returns_recovery_code_once(client):
    client.get("/api/session")
    created = client.post("/api/users", json={"displayName": "Asha", "pin": PIN}).json()

    assert len(sealing.normalize_recovery_code(created["recoveryCode"]).replace("-", "")) == 20
    user = users.get_user(created["user"]["id"])
    assert user["publicKey"] is not None and user["keyWrappedWithPin"] and user["keyWrappedWithRecovery"]
    assert client.get("/api/session").json()["locked"] is False

    client.post("/api/session/logout")
    login = client.post("/api/session/login", json={"userId": user["id"], "pin": PIN}).json()
    assert "recoveryCode" not in login


def test_saved_history_is_sealed_in_the_database(client, signed_in, monkeypatch):
    fake_answer(monkeypatch)
    chat_once(client, "How much is my emergency fund?")
    client.put("/api/profile", json={"primaryGoal": "Buy a home"})
    store_today(signed_in["id"])

    title, messages = raw("SELECT title, messages FROM chats WHERE user_id = ?", signed_in["id"])[0]
    (profile,) = raw("SELECT profile FROM users WHERE id = ?", signed_in["id"])[0]
    data, networth = raw("SELECT data, networth_inr FROM portfolio_snapshots WHERE user_id = ?", signed_in["id"])[0]
    for value in (title, messages, profile, data):
        assert sealing.is_sealed(value)
    dump = json.dumps([title, messages, profile, data])
    assert "emergency fund" not in dump and "Buy a home" not in dump and "4200000" not in dump
    assert networth is None

    assert client.get("/api/chats").json()["chats"][0]["title"] == "How much is my emergency fund?"
    assert client.get("/api/profile").json()["profile"] == {"primaryGoal": "Buy a home"}
    assert client.get("/api/snapshots").json()["latest"]["networthInr"] == 4200000.0


def test_restart_locks_history_until_pin_is_entered(client, signed_in, monkeypatch):
    fake_answer(monkeypatch)
    chat_id = chat_once(client)
    restart_server()

    assert client.get("/api/session").json()["locked"] is True
    for method, path in (("GET", "/api/chats"), ("GET", f"/api/chats/{chat_id}"), ("POST", "/api/chats"),
                         ("GET", "/api/profile"), ("GET", "/api/snapshots"), ("DELETE", f"/api/chats/{chat_id}")):
        response = client.request(method, path, json={"agentType": "finance"} if method == "POST" else None)
        assert response.status_code == 403, path
        assert response.json()["unlockRequired"] is True
    assert client.post("/api/chat", json={"message": "hi", "chatId": chat_id}).json()["unlockRequired"] is True

    assert client.post("/api/session/unlock", json={"pin": "0000"}).status_code == 401
    unlocked = client.post("/api/session/unlock", json={"pin": PIN})

    assert unlocked.status_code == 200 and "recoveryCode" not in unlocked.json()
    assert client.get("/api/session").json()["locked"] is False
    assert client.get(f"/api/chats/{chat_id}").json()["messages"][0]["content"] == "What is diversification?"


def test_unlock_requires_a_signed_in_profile(client):
    client.get("/api/session")
    assert client.post("/api/session/unlock", json={"pin": PIN}).status_code == 401


def test_scheduler_seals_snapshots_while_locked(client, signed_in):
    restart_server()
    store_today(signed_in["id"], 123.0)

    with pytest.raises(sealing.LockedError):
        snapshots.latest_snapshot(signed_in["id"])
    client.post("/api/session/unlock", json={"pin": PIN})
    assert client.get("/api/snapshots").json()["latest"]["networthInr"] == 123.0


def test_unlock_is_per_browser_session(client, signed_in):
    other = TestClient(main.app, follow_redirects=False)
    other.get("/api/session")
    other.post("/api/session/login", json={"userId": signed_in["id"], "pin": PIN})

    client.post("/api/session/logout")

    assert other.get("/api/session").json()["locked"] is False
    assert other.get("/api/chats").status_code == 200


def test_profile_from_before_encryption_is_sealed_on_next_sign_in(client):
    user_id = users.create_user("Ravi", users.hash_pin(PIN))["id"]
    plain = {"id": user_id, "storageMode": "device", "keys": sealing.ProfileKeys()}
    chat = chats.create_chat(plain, "finance")
    chats.append_turn(plain, chat, "Old question", "Old answer", [])
    users.update_profile(user_id, users.ProfileFields(riskAppetite="high"), sealing.ProfileKeys())
    store_today(user_id, 99.0)
    assert raw("SELECT networth_inr FROM portfolio_snapshots WHERE user_id = ?", user_id) == [(99.0,)]

    client.get("/api/session")
    login = client.post("/api/session/login", json={"userId": user_id, "pin": PIN}).json()
    accept_notice(client)

    assert "recoveryCode" in login
    title, messages = raw("SELECT title, messages FROM chats WHERE user_id = ?", user_id)[0]
    data, networth = raw("SELECT data, networth_inr FROM portfolio_snapshots WHERE user_id = ?", user_id)[0]
    (profile,) = raw("SELECT profile FROM users WHERE id = ?", user_id)[0]
    assert all(sealing.is_sealed(value) for value in (title, messages, data, profile)) and networth is None
    assert client.get("/api/chats").json()["chats"][0]["title"] == "Old question"
    assert client.get("/api/profile").json()["profile"] == {"riskAppetite": "high"}
    assert client.get("/api/snapshots").json()["latest"]["networthInr"] == 99.0


def test_recovery_code_resets_pin_and_is_replaced(client):
    client.get("/api/session")
    created = client.post("/api/users", json={"displayName": "Asha", "pin": PIN}).json()
    user_id, code = created["user"]["id"], created["recoveryCode"]
    accept_notice(client)
    client.put("/api/profile", json={"primaryGoal": "Retire early"})
    client.post("/api/session/logout")
    restart_server()

    typed = code.lower().replace("-", " ")
    recovered = client.post("/api/session/recover", json={"userId": user_id, "recoveryCode": typed, "newPin": "1357"})

    assert recovered.status_code == 200
    new_code = recovered.json()["recoveryCode"]
    assert new_code != code
    assert client.get("/api/profile").json()["profile"] == {"primaryGoal": "Retire early"}
    client.post("/api/session/logout")
    assert client.post("/api/session/login", json={"userId": user_id, "pin": PIN}).status_code == 401
    assert client.post("/api/session/login", json={"userId": user_id, "pin": "1357"}).status_code == 200
    reused = client.post("/api/session/recover", json={"userId": user_id, "recoveryCode": code, "newPin": "9999"})
    assert reused.status_code == 401


def test_wrong_recovery_codes_share_the_pin_lockout(client, signed_in):
    client.post("/api/session/logout")
    wrong = {"userId": signed_in["id"], "recoveryCode": "AAAA-AAAA-AAAA-AAAA-AAAA", "newPin": "1357"}

    statuses = [client.post("/api/session/recover", json=wrong).status_code for _ in range(users.MAX_FAILED_LOGINS)]

    assert statuses == [401] * (users.MAX_FAILED_LOGINS - 1) + [429]
    assert client.post("/api/session/login", json={"userId": signed_in["id"], "pin": PIN}).status_code == 429


def test_recovery_rejects_bad_new_pin_and_profiles_without_keys(client):
    user_id = users.create_user("Ravi", users.hash_pin(PIN))["id"]
    client.get("/api/session")
    no_keys = client.post("/api/session/recover", json={"userId": user_id, "recoveryCode": "x", "newPin": "1357"})
    assert no_keys.status_code == 400 and "no recovery code" in no_keys.json()["error"]

    created = client.post("/api/users", json={"displayName": "Asha", "pin": PIN}).json()
    short_pin = client.post("/api/session/recover", json={
        "userId": created["user"]["id"], "recoveryCode": created["recoveryCode"], "newPin": "1",
    })
    assert short_pin.status_code == 400
    assert client.post("/api/session/recover", json={"userId": "nobody", "recoveryCode": "x", "newPin": "1357"}).status_code == 401


def test_new_recovery_code_replaces_the_old_one(client):
    client.get("/api/session")
    created = client.post("/api/users", json={"displayName": "Asha", "pin": PIN}).json()
    user_id, old_code = created["user"]["id"], created["recoveryCode"]

    assert client.post("/api/users/me/recovery-code", json={"pin": "0000"}).status_code == 401
    new_code = client.post("/api/users/me/recovery-code", json={"pin": PIN}).json()["recoveryCode"]

    client.post("/api/session/logout")
    assert client.post("/api/session/recover", json={
        "userId": user_id, "recoveryCode": old_code, "newPin": "1357",
    }).status_code == 401
    assert client.post("/api/session/recover", json={
        "userId": user_id, "recoveryCode": new_code, "newPin": "1357",
    }).status_code == 200


def test_export_decrypts_with_the_pin_even_when_locked(client, signed_in, monkeypatch):
    fake_answer(monkeypatch)
    chat_once(client)
    client.put("/api/profile", json={"riskAppetite": "low"})
    store_today(signed_in["id"], 77.0)
    restart_server()

    body = client.post("/api/users/me/export", json={"pin": PIN}).json()

    assert body["profile"]["fields"] == {"riskAppetite": "low"}
    assert body["chats"][0]["messages"][0]["content"] == "What is diversification?"
    assert body["snapshots"][0]["networthInr"] == 77.0
    assert "sealed:v1:" not in json.dumps(body)


def test_database_and_master_key_alone_cannot_read_history(client, signed_in, monkeypatch):
    fake_answer(monkeypatch)
    chat_once(client)
    user = users.get_user(signed_in["id"])
    locked = {**user, "keys": sealing.ProfileKeys(public_key=user["publicKey"])}

    with pytest.raises(sealing.LockedError):
        chats.list_chats(locked)
    with pytest.raises(sealing.WrongSecretError):
        sealing.unwrap_private_key(user["keyWrappedWithPin"], "0000")


def test_deleting_a_profile_forgets_its_unlocked_keys(client, signed_in):
    assert any(owner == signed_in["id"] for owner, _ in unlock._unlocked.values())

    client.request("DELETE", "/api/users/me", json={"pin": PIN})

    assert not any(owner == signed_in["id"] for owner, _ in unlock._unlocked.values())


def test_create_keys_twice_keeps_the_first_keys():
    user_id = users.create_user("Asha", users.hash_pin(PIN))["id"]

    first = unlock.create_keys(user_id, PIN)
    second = unlock.create_keys(user_id, PIN)

    assert first.recovery_code and second.recovery_code is None
    assert bytes(second.private_key) == bytes(first.private_key)


def test_private_mode_chats_need_unlock_but_are_not_written(client, monkeypatch):
    user = sign_up(client, storage_mode="private")
    fake_answer(monkeypatch)
    chat_once(client)
    restart_server()

    assert client.get("/api/chats").status_code == 403
    client.post("/api/session/unlock", json={"pin": PIN})
    assert raw("SELECT COUNT(*) FROM chats WHERE user_id = ?", user["id"]) == [(0,)]

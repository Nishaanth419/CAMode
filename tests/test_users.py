from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from fastapi.testclient import TestClient

from app import db, main, snapshots, users
from conftest import sign_up


def test_pin_hash_round_trip_and_rejects_wrong_pin():
    stored = users.hash_pin("2468")

    assert stored.startswith("scrypt$")
    assert "2468" not in stored
    assert users.verify_pin("2468", stored) is True
    assert users.verify_pin("2469", stored) is False
    assert users.verify_pin("2468", "not-a-hash") is False
    assert users.hash_pin("2468") != stored  # Salted.


def test_create_profile_signs_in_and_rejects_duplicate_name(client):
    user = sign_up(client, "Asha")

    session = client.get("/api/session").json()
    assert session["user"] == user
    assert session["profiles"] == [user]
    duplicate = client.post("/api/users", json={"displayName": "  asha ", "pin": "1111"})
    assert duplicate.status_code == 409
    short_pin = client.post("/api/users", json={"displayName": "Ravi", "pin": "12"})
    assert short_pin.status_code == 400


def test_login_wrong_pin_then_lockout(client):
    user = sign_up(client, "Asha", "2468")
    client.post("/api/session/logout")

    for _ in range(users.MAX_FAILED_LOGINS - 1):
        wrong = client.post("/api/session/login", json={"userId": user["id"], "pin": "0000"})
        assert wrong.status_code == 401
    locked = client.post("/api/session/login", json={"userId": user["id"], "pin": "0000"})
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) == users.LOCKOUT_SECONDS

    still_locked = client.post("/api/session/login", json={"userId": user["id"], "pin": "2468"})
    assert still_locked.status_code == 429
    assert client.get("/api/session").json()["user"] is None


def test_concurrent_wrong_pins_cannot_bypass_lockout():
    """Parallel wrong guesses with a stale user dict must still lock after MAX_FAILED_LOGINS."""
    user = users.create_user("Asha", users.hash_pin("2468"))
    attempts = users.MAX_FAILED_LOGINS * 4
    barrier = threading.Barrier(attempts)

    def guess(_: int) -> str:
        barrier.wait()
        try:
            return "wrong" if users.check_pin(user, "0000") is False else "accepted"
        except users.ProfileLockedError:
            return "locked"

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        outcomes = list(pool.map(guess, range(attempts)))

    assert outcomes.count("accepted") == 0
    assert outcomes.count("wrong") <= users.MAX_FAILED_LOGINS - 1
    assert outcomes.count("locked") >= attempts - (users.MAX_FAILED_LOGINS - 1)
    assert users.get_user(user["id"])["lockedUntil"] > users.now_ms()
    # The correct PIN is refused while locked, even with the stale (unlocked) dict.
    try:
        users.check_pin(user, "2468")
    except users.ProfileLockedError as exc:
        assert 0 < exc.retry_after_seconds <= users.LOCKOUT_SECONDS
    else:
        raise AssertionError("correct PIN accepted while the account is locked")


def test_login_rotates_session_id(client):
    user = sign_up(client, "Asha", "2468")
    client.post("/api/session/logout")
    before = client.cookies.get(main.SESSION_COOKIE)

    response = client.post("/api/session/login", json={"userId": user["id"], "pin": "2468"})

    assert response.status_code == 200
    after = client.cookies.get(main.SESSION_COOKIE)
    assert after != before
    assert users.session_user_id(after) == user["id"]
    assert users.session_user_id(before) is None


def test_legacy_chats_and_tokens_are_adopted_on_sign_in(tmp_path, monkeypatch):
    legacy_path = tmp_path / "legacy" / "codemode.sqlite3"
    legacy_path.parent.mkdir()
    connection = sqlite3.connect(legacy_path)
    connection.executescript("""
        CREATE TABLE oauth_sessions (id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at INTEGER NOT NULL);
        CREATE TABLE chats (
            id TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES oauth_sessions(id) ON DELETE CASCADE,
            title TEXT NOT NULL, messages TEXT NOT NULL, updated_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
            PRIMARY KEY (session_id, id)
        );
    """)
    now = users.now_ms()
    tokens = {"accessToken": "old-access", "refreshToken": "old-refresh", "expiresAt": now + 3_600_000,
              "clientId": "client", "clientSecret": "secret"}
    connection.execute("INSERT INTO oauth_sessions VALUES (?, ?, ?)", ("old-sid", json.dumps(tokens), now))
    connection.execute(
        "INSERT INTO chats VALUES ('chat-1', 'old-sid', 'Old chat', '[]', ?, ?)", (now, now + 86_400_000),
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(db, "DATA_DIR", legacy_path.parent)
    monkeypatch.setattr(db, "DATABASE_PATH", legacy_path)

    db.initialize_db()
    with TestClient(main.app, follow_redirects=False) as client:
        client.cookies.set(main.SESSION_COOKIE, "old-sid")
        user = client.post("/api/users", json={"displayName": "Asha", "pin": "2468"}).json()["user"]
        chats = client.get("/api/chats").json()["chats"]

    assert [(chat["id"], chat["title"], chat["agentType"]) for chat in chats] == [("chat-1", "Old chat", "portfolio")]
    assert users.get_indmoney(user["id"])["refreshToken"] == "old-refresh"
    with db.connect_db() as connection:
        assert connection.execute("SELECT COUNT(*) FROM chats_legacy").fetchone()[0] == 0


def test_profile_update_validates_fields(client, signed_in):
    assert client.get("/api/profile").json() == {"displayName": "Asha", "profile": {}, "updatedAt": None}
    assert client.put("/api/profile", json={"riskAppetite": "reckless"}).status_code == 422
    assert client.put("/api/profile", json={"salary": 10}).status_code == 422
    assert client.put("/api/profile", json={"investmentHorizonYears": 99}).status_code == 422

    saved = client.put("/api/profile", json={
        "riskAppetite": "high", "investmentHorizonYears": 15, "primaryGoal": "Retire early",
        "monthlyInvestmentInr": 25000, "ageBand": "25-34",
    })

    assert saved.status_code == 200
    assert isinstance(saved.json()["updatedAt"], int)
    assert client.get("/api/profile").json()["profile"] == {
        "riskAppetite": "high", "investmentHorizonYears": 15, "primaryGoal": "Retire early",
        "monthlyInvestmentInr": 25000, "ageBand": "25-34",
    }


def test_delete_profile_cascades(client, signed_in):
    chat = client.post("/api/chats", json={"agentType": "finance"}).json()
    snapshots.store_snapshot(signed_in["id"], datetime.now(snapshots.SNAPSHOT_TIMEZONE), {"networth_snapshot": "{}"}, {})

    wrong = client.request("DELETE", "/api/users/me", json={"pin": "0000"})
    assert wrong.status_code == 401
    deleted = client.request("DELETE", "/api/users/me", json={"pin": "2468"})

    assert deleted.json() == {"deleted": True}
    assert users.get_user(signed_in["id"]) is None
    assert client.get("/api/session").json()["user"] is None
    with db.connect_db() as connection:
        assert connection.execute("SELECT 1 FROM chats WHERE id = ?", (chat["id"],)).fetchone() is None
        assert connection.execute("SELECT 1 FROM portfolio_snapshots WHERE user_id = ?", (signed_in["id"],)).fetchone() is None


def test_chat_routes_require_profile(client):
    client.get("/api/session")
    for method, path in (("GET", "/api/chats"), ("POST", "/api/chats"), ("GET", "/api/snapshots"), ("GET", "/api/profile")):
        response = client.request(method, path, json={"agentType": "finance"} if method == "POST" else None)
        assert response.status_code == 401, path
        assert response.json()["profileRequired"] is True

    sign_up(client)
    assert client.get("/api/chats").status_code == 200
    client.post("/api/session/logout")
    assert client.get("/api/chats").status_code == 401


def test_profiles_do_not_see_each_others_chats(client):
    sign_up(client, "Asha", "2468")
    asha_chat = client.post("/api/chats", json={"agentType": "finance"}).json()
    client.post("/api/session/logout")

    sign_up(client, "Ravi", "1357")

    assert client.get("/api/chats").json()["chats"] == []
    assert client.get(f"/api/chats/{asha_chat['id']}").status_code == 404
    assert client.delete(f"/api/chats/{asha_chat['id']}").status_code == 404

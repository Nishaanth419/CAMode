"""The operator's admin page: off by default, its own Google sign-in, what it shows and its account actions."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import accounts, admin, limits, main, privacy, users
from conftest import sign_up
from test_api import (
    GOOGLE_CLIENT_ID,
    OAuthResponse,
    connect_user_to_indmoney,
    create_account_profile,
    fake_id_token,
    google_sign_in,
)

ADMIN_EMAIL = "owner@example.com"


@pytest.fixture
def admin_mode(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", f"{ADMIN_EMAIL}, second@example.com")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", GOOGLE_CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "google-secret")
    return monkeypatch


def fake_google(monkeypatch, claims: dict) -> None:
    class GoogleTokenClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, data):
            return OAuthResponse({"id_token": fake_id_token(claims), "access_token": "unused"})

    monkeypatch.setattr(accounts.httpx, "AsyncClient", GoogleTokenClient)


def admin_sign_in(client, monkeypatch, email=ADMIN_EMAIL, *, subject="google-owner", params=None):
    """Run /auth/admin/start and the shared Google callback; return the callback response."""
    start = client.get(admin.START_PATH)
    assert start.status_code == 302, start.text
    query = parse_qs(urlparse(start.headers["location"]).query)
    now = int(time.time())
    fake_google(monkeypatch, {
        "iss": "https://accounts.google.com", "aud": GOOGLE_CLIENT_ID, "sub": subject, "email": email,
        "email_verified": True, "nonce": query["nonce"][0], "iat": now, "exp": now + 3600,
    })
    return client.get("/auth/google/callback", params=params or {"state": query["state"][0], "code": "auth-code"})


@pytest.fixture
def operator(client, admin_mode):
    callback = admin_sign_in(client, admin_mode)
    assert callback.status_code == 302 and callback.headers["location"] == "http://testserver/admin"
    return client


# ── Off by default ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("method", "path"), [
    ("get", "/admin"), ("get", admin.START_PATH), ("get", "/api/admin/session"), ("get", "/api/admin/overview"),
    ("post", "/api/admin/sign-out"), ("post", "/api/admin/backups"),
    ("post", "/api/admin/profiles/someone/clear-lockout"), ("post", "/api/admin/profiles/someone/sign-out"),
])
def test_admin_paths_are_not_found_without_admin_emails(client, method, path):
    assert getattr(client, method)(path).status_code == 404


def test_admin_emails_need_a_google_client(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    with pytest.raises(accounts.AccountConfigError):
        admin.check_config()


def test_admin_emails_are_trimmed_and_case_insensitive(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", " Owner@Example.com ,, other@example.com")
    assert admin.admin_emails() == {"owner@example.com", "other@example.com"}
    assert admin.is_admin_email("OWNER@example.COM")
    assert not admin.is_admin_email("stranger@example.com")


# ── Signing in ─────────────────────────────────────────────────────────────────

def test_admin_page_is_served_noindex_and_robots_disallows_it(client, admin_mode):
    page = client.get("/admin")
    assert page.status_code == 200
    assert '<meta name="robots" content="noindex" />' in page.text
    assert "/js/admin.js" in page.text
    assert "Disallow: /admin" in client.get("/robots.txt").text.splitlines()


def test_overview_needs_an_admin_session(client, admin_mode):
    response = client.get("/api/admin/overview")
    assert response.status_code == 401
    assert response.json()["adminRequired"] is True and response.json()["signInUrl"] == admin.START_PATH
    assert client.get("/api/admin/session").json()["admin"] is None


def test_listed_google_account_signs_in_to_the_admin_page_only(operator):
    assert operator.cookies.get(admin.ADMIN_COOKIE)
    assert operator.get("/api/admin/session").json()["admin"]["email"] == ADMIN_EMAIL
    assert operator.get("/api/admin/overview").status_code == 200
    # No profile, and no pending Google account waiting to create one.
    session = operator.get("/api/session").json()
    assert session["user"] is None and session["account"] is None


def test_unlisted_google_account_is_refused(client, admin_mode):
    callback = admin_sign_in(client, admin_mode, email="stranger@example.com")
    assert callback.headers["location"] == "http://testserver/admin?error=not_admin"
    assert client.get("/api/admin/overview").status_code == 401


def test_cancelled_admin_sign_in_returns_to_the_admin_page(client, admin_mode):
    start = client.get(admin.START_PATH)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    callback = client.get("/auth/google/callback", params={"state": state, "error": "access_denied"})
    assert callback.headers["location"] == "http://testserver/admin?error=cancelled"


def test_removing_an_address_ends_its_access(operator, admin_mode):
    admin_mode.setenv("ADMIN_EMAILS", "second@example.com")
    assert operator.get("/api/admin/overview").status_code == 401


def test_admin_session_expires(operator, monkeypatch):
    monkeypatch.setattr(admin, "SESSION_SECONDS", -1)
    assert operator.get("/api/admin/overview").status_code == 401


def test_admin_sign_out_ends_the_admin_session(operator):
    assert operator.post("/api/admin/sign-out").status_code == 200
    assert operator.get("/api/admin/overview").status_code == 401


def test_a_profile_session_is_not_an_admin_session(client, admin_mode):
    sign_up(client)
    client.cookies.set(admin.ADMIN_COOKIE, client.cookies.get(main.SESSION_COOKIE))
    assert client.get("/api/admin/overview").status_code == 401


def test_admin_sign_in_leaves_the_profile_session_alone(client, admin_mode):
    user = sign_up(client)
    admin_sign_in(client, admin_mode)
    assert client.get("/api/session").json()["user"] == user
    assert client.get("/api/admin/overview").status_code == 200


def test_google_profile_sign_in_still_works_and_gives_no_admin_access(client, admin_mode):
    admin_mode.setenv("AUTH_MODE", "google")
    # The operator's own Google account can also have a profile; signing in to it is not admin sign-in.
    callback = google_sign_in(client, admin_mode, subject="google-owner", email=ADMIN_EMAIL)
    assert callback.headers["location"] == "http://testserver/"
    create_account_profile(client)
    assert client.get("/api/session").json()["user"]["displayName"] == "Asha"
    assert client.get("/api/admin/overview").status_code == 401


def test_profile_callback_is_still_not_found_in_local_mode(client, admin_mode):
    assert client.get("/auth/google/callback", params={"state": "x", "code": "y"}).status_code == 404


# ── What the page shows ────────────────────────────────────────────────────────

def test_overview_lists_profiles_without_their_sealed_history(operator):
    user = sign_up(operator, "Asha")
    goal = operator.post("/api/goals", json={"name": "Secret beach house", "status": "draft"})
    assert goal.status_code == 201, goal.text
    connect_user_to_indmoney(user["id"])

    overview = operator.get("/api/admin/overview").json()

    assert overview["totals"]["profiles"] == 1
    assert overview["totals"]["byPlan"] == {"free": 1, "individual": 0, "team": 0}
    assert overview["totals"]["indmoneyConnected"] == 1
    assert overview["authMode"] == "local"
    [row] = overview["profiles"]
    assert row["id"] == user["id"] and row["name"] == "Asha" and row["email"] is None
    assert row["plan"] == "free" and row["consentCurrent"] is True and row["indmoneyConnected"] is True
    assert row["questionsToday"] == 0 and row["dailyLimit"] is None and row["lockedUntil"] is None
    assert row["lastSeenAt"] and row["createdAt"]
    text = operator.get("/api/admin/overview").text
    for secret in ("secret beach house", "access-token", "client-secret", "scrypt", "sealed:"):
        assert secret not in text.lower()


def test_overview_shows_google_emails_question_counts_and_stale_consent(client, operator, monkeypatch):
    monkeypatch.setenv("DAILY_QUESTION_LIMIT", "5")
    other = users.create_user("Ravi", users.hash_pin("1357"))
    accounts.link(other["id"], accounts.GoogleIdentity(subject="google-ravi", email="ravi@example.com"))
    with main.connect_db() as connection:
        connection.execute(
            "INSERT INTO question_counts(user_id, day, questions) VALUES (?, ?, 3)",
            (other["id"], limits.local_now().date().isoformat()),
        )

    overview = operator.get("/api/admin/overview").json()

    [row] = overview["profiles"]
    assert row["email"] == "ravi@example.com"
    assert row["questionsToday"] == 3 and row["dailyLimit"] == 5
    assert row["consentCurrent"] is False and row["lastSeenAt"] is None
    assert overview["totals"]["questionsToday"] == 3 and overview["dailyLimit"] == 5


def test_paid_plans_show_no_daily_cap(operator, monkeypatch):
    monkeypatch.setenv("DAILY_QUESTION_LIMIT", "5")
    user = users.create_user("Ravi", users.hash_pin("1357"))
    main.plans.set_plan(user["id"], "individual")
    [row] = operator.get("/api/admin/overview").json()["profiles"]
    assert row["plan"] == "individual" and row["dailyLimit"] is None


def test_individual_profiles_show_their_credits(operator):
    user = users.create_user("Ravi", users.hash_pin("1357"))
    users.create_user("Asha", users.hash_pin("2468"))
    main.plans.set_plan(user["id"], "individual")
    main.credits.charge(user["id"], 3_000, 160, now=limits.local_now())
    main.credits.add_top_up(user["id"], 500)

    overview = operator.get("/api/admin/overview").json()

    rows = {row["name"]: row for row in overview["profiles"]}
    balance = rows["Ravi"]["credits"]
    assert (balance["monthly"], balance["used"], balance["monthlyRemaining"], balance["topUp"]) == (3_000, 160, 2_840, 500)
    assert balance["renewsOn"]
    assert rows["Asha"]["credits"] is None
    assert overview["totals"]["creditsUsedThisMonth"] == 160


def test_the_admin_page_reads_credits_without_starting_a_month(operator):
    user = users.create_user("Ravi", users.hash_pin("1357"))
    with main.connect_db() as connection:
        connection.execute("UPDATE users SET plan = 'individual' WHERE id = ?", (user["id"],))

    [row] = operator.get("/api/admin/overview").json()["profiles"]

    assert row["credits"]["remaining"] == 3_000
    with main.connect_db() as connection:
        stored = connection.execute("SELECT plan_started_at FROM users WHERE id = ?", (user["id"],)).fetchone()
    assert stored["plan_started_at"] is None


def test_a_failed_snapshot_attempt_is_flagged(operator):
    user = users.create_user("Ravi", users.hash_pin("1357"))
    with main.connect_db() as connection:
        connection.execute("UPDATE users SET snapshot_attempted_at = ? WHERE id = ?", (users.now_ms(), user["id"]))
    [row] = operator.get("/api/admin/overview").json()["profiles"]
    assert row["snapshotFailing"] is True and row["lastSnapshotAt"] is None


# ── Account actions ────────────────────────────────────────────────────────────

def test_admin_changes_a_plan(operator):
    user = users.create_user("Ravi", users.hash_pin("1357"))
    response = operator.put(f"/api/admin/profiles/{user['id']}/plan", json={"plan": "team"})
    assert response.status_code == 200
    assert users.get_user(user["id"])["plan"] == "team"


def test_plan_change_refuses_unknown_plans_and_profiles(operator):
    user = users.create_user("Ravi", users.hash_pin("1357"))
    assert operator.put(f"/api/admin/profiles/{user['id']}/plan", json={"plan": "gold"}).status_code == 422
    assert operator.put("/api/admin/profiles/nobody/plan", json={"plan": "team"}).status_code == 404


def test_actions_need_an_admin_session(client, admin_mode):
    user = users.create_user("Ravi", users.hash_pin("1357"))
    assert client.put(f"/api/admin/profiles/{user['id']}/plan", json={"plan": "team"}).status_code == 401
    assert client.post(f"/api/admin/profiles/{user['id']}/clear-lockout").status_code == 401
    assert client.post(f"/api/admin/profiles/{user['id']}/sign-out").status_code == 401
    assert client.request("DELETE", f"/api/admin/profiles/{user['id']}", json={"confirmName": "Ravi"}).status_code == 401
    assert client.post("/api/admin/backups").status_code == 401
    assert users.get_user(user["id"])["plan"] == "free"


def test_admin_clears_a_pin_lock(operator):
    user = users.create_user("Ravi", users.hash_pin("1357"))
    with main.connect_db() as connection:
        connection.execute(
            "UPDATE users SET failed_logins = 5, locked_until = ? WHERE id = ?", (users.now_ms() + 300_000, user["id"]),
        )
    [row] = operator.get("/api/admin/overview").json()["profiles"]
    assert row["lockedUntil"] and row["failedPins"] == 5

    assert operator.post(f"/api/admin/profiles/{user['id']}/clear-lockout").status_code == 200

    stored = users.get_user(user["id"])
    assert stored["failedLogins"] == 0 and stored["lockedUntil"] == 0
    assert operator.post("/api/admin/profiles/nobody/clear-lockout").status_code == 404


def test_admin_signs_a_profile_out_everywhere(client, admin_mode):
    user = sign_up(client)
    with TestClient(main.app, follow_redirects=False) as operator_client:
        admin_sign_in(operator_client, admin_mode)
        response = operator_client.post(f"/api/admin/profiles/{user['id']}/sign-out")
    assert response.status_code == 200 and response.json()["sessionsEnded"] == 1
    assert client.get("/api/session").json()["user"] is None


def test_admin_deletes_a_profile_only_with_its_name(operator, monkeypatch):
    user = users.create_user("Ravi", users.hash_pin("1357"))
    connect_user_to_indmoney(user["id"])
    revoked = []

    async def fake_revoke(stored_user):
        revoked.append(stored_user["id"])

    monkeypatch.setattr(main, "revoke_indmoney", fake_revoke)
    path = f"/api/admin/profiles/{user['id']}"

    assert operator.request("DELETE", path, json={"confirmName": "Asha"}).status_code == 422
    assert users.get_user(user["id"]) is not None

    response = operator.request("DELETE", path, json={"confirmName": " ravi "})

    assert response.status_code == 200
    assert users.get_user(user["id"]) is None and revoked == [user["id"]]
    assert operator.request("DELETE", path, json={"confirmName": "Ravi"}).status_code == 404


def test_admin_backs_up_the_database(operator):
    response = operator.post("/api/admin/backups")
    assert response.status_code == 201 and response.json()["count"] == 1


def test_notice_tells_profiles_what_the_admin_page_shows():
    titles = [item["title"] for item in privacy.NOTICE]
    assert "What whoever runs CA Mode can see" in titles

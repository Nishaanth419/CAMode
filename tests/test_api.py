from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import accounts, limits, main, privacy, snapshots, users
from app.agent import AgentRunError
from conftest import sign_up


def connect_user_to_indmoney(user_id: str, **overrides) -> None:
    users.save_indmoney(user_id, {
        "accessToken": "access-token", "refreshToken": "refresh", "expiresAt": users.now_ms() + 3_600_000,
        "clientId": "client-id", "clientSecret": "client-secret", **overrides,
    })


class OAuthResponse:
    is_success = True
    status_code = 200
    text = ""

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class FailingAsyncClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, *args, **kwargs):
        raise main.httpx.ConnectError("network down")

    async def post(self, *args, **kwargs):
        raise main.httpx.ConnectError("network down")


@pytest.fixture
def stored_user():
    """A profile created directly in the database (no browser session)."""
    return users.create_user("Ravi", users.hash_pin("1357"))["id"]


def test_health_and_frontend_routes(client):
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/").status_code == 200
    assert client.get("/js/app.js").status_code == 200


def test_provider_endpoint_exposes_ollama_openai_and_groq(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test")
    monkeypatch.setenv("GROQ_API_KEY", "groq-test")
    monkeypatch.setenv("AI_PROVIDER", "groq")

    response = client.get("/api/providers")

    assert response.status_code == 200
    assert [provider["id"] for provider in response.json()["providers"]] == ["ollama", "openai", "groq"]
    assert response.json()["defaultProvider"] == "groq"
    assert response.headers["cache-control"] == "no-store"


def test_session_initialization_sets_http_only_cookie(client):
    response = client.get("/api/session")

    assert response.json() == {
        "connected": False, "user": None, "locked": False, "consentRequired": False, "storageMode": None,
        "usage": None, "profiles": [], "authMode": "local", "account": None,
    }
    cookie = response.headers["set-cookie"].lower()
    assert "indmoney_session=" in cookie
    assert "httponly" in cookie


def test_session_cookie_is_secure_for_https_requests():
    with TestClient(main.app, base_url="https://testserver", follow_redirects=False) as secure_client:
        response = secure_client.get("/api/session")
    assert "secure" in response.headers["set-cookie"].lower()


def test_expired_local_session_is_removed():
    main.save_session("expired", {"state": "stale"})
    with main.connect_db() as connection:
        connection.execute("UPDATE oauth_sessions SET updated_at = ? WHERE id = ?", (0, "expired"))

    assert main.get_session("expired") == {}
    with main.connect_db() as connection:
        assert connection.execute("SELECT 1 FROM oauth_sessions WHERE id = ?", ("expired",)).fetchone() is None


def test_chat_crud_and_profile_isolation(client, signed_in):
    created = client.post("/api/chats", json={"agentType": "finance"})
    assert created.status_code == 201
    chat_id = created.json()["id"]
    assert client.get("/api/chats").json()["chats"][0]["id"] == chat_id
    assert client.get(f"/api/chats/{chat_id}").json()["messages"] == []

    other_browser = TestClient(main.app, follow_redirects=False)
    assert other_browser.get(f"/api/chats/{chat_id}").status_code == 401
    assert other_browser.delete(f"/api/chats/{chat_id}").status_code == 401

    deleted = client.delete(f"/api/chats/{chat_id}")
    assert deleted.json() == {"deleted": True}
    assert client.get(f"/api/chats/{chat_id}").status_code == 404


def test_create_chat_rejects_invalid_agent_type(client, signed_in):
    response = client.post("/api/chats", json={"agentType": "local"})
    assert response.status_code == 422


def test_chat_message_is_saved_and_agent_type_is_enforced(client, signed_in, monkeypatch):
    chat = client.post("/api/chats", json={"agentType": "finance"}).json()

    async def fake_run_agent(message, mcp_url, token, history, agent_type, provider, *, context=None, saved_context=None, plan_parts=()):
        assert message == "What is diversification?"
        assert history == []
        assert agent_type == "finance"
        assert provider == "openai"
        assert context is None
        return "Diversification spreads risk across investments."

    monkeypatch.setattr(main, "run_agent", fake_run_agent)
    response = client.post("/api/chat", json={
        "message": "What is diversification?", "chatId": chat["id"], "agentType": "finance"
    })
    assert response.status_code == 200
    assert response.text.startswith("Diversification")
    saved = client.get(f"/api/chats/{chat['id']}").json()
    assert saved["title"] == "What is diversification?"
    assert [item["role"] for item in saved["messages"]] == ["user", "assistant"]

    mismatch = client.post("/api/chat", json={
        "message": "my net worth?", "chatId": chat["id"], "agentType": "portfolio"
    })
    assert mismatch.status_code == 409


def test_chat_refreshes_indmoney_token_once_after_mcp_401(client, signed_in, monkeypatch):
    connect_user_to_indmoney(signed_in["id"])
    chat = client.post("/api/chats", json={"agentType": "portfolio"}).json()
    refreshed = []

    async def fake_user_access_token(user_id, *, force_refresh=False):
        if force_refresh:
            refreshed.append(user_id)
            return "fresh-token"
        return "stale-token"

    calls = []

    async def fake_run_agent(message, mcp_url, token, history, agent_type, provider, *, context=None, saved_context=None, plan_parts=()):
        calls.append(token)
        assert context is not None
        if token == "stale-token":
            raise AgentRunError('MCP tools/call failed for "get_indian_stocks_option_chain": 401 ', [], auth_failed=True)
        return "Option-chain data from refreshed INDmoney session."

    monkeypatch.setattr(users, "user_access_token", fake_user_access_token)
    monkeypatch.setattr(main, "run_agent", fake_run_agent)

    response = client.post("/api/chat", json={
        "message": "Show the Nifty option chain for the next monthly expiry.",
        "chatId": chat["id"], "agentType": "portfolio",
    })

    assert response.status_code == 200
    assert response.text == "Option-chain data from refreshed INDmoney session."
    assert refreshed == [signed_in["id"]]
    assert calls == ["stale-token", "fresh-token"]


def test_an_ai_provider_401_does_not_refresh_the_indmoney_token(client, signed_in, monkeypatch):
    connect_user_to_indmoney(signed_in["id"])
    chat = client.post("/api/chats", json={"agentType": "portfolio"}).json()
    refreshed = []

    async def fake_user_access_token(user_id, *, force_refresh=False):
        if force_refresh:
            refreshed.append(user_id)
        return "token"

    async def fake_run_agent(*args, **kwargs):
        # The words the old text check looked for, but from the AI provider rather than INDmoney.
        raise AgentRunError('OpenAI request failed: HTTP 401 {"error": "Unauthorized", "code": "invalid_token"}', [])

    monkeypatch.setattr(users, "user_access_token", fake_user_access_token)
    monkeypatch.setattr(main, "run_agent", fake_run_agent)

    response = client.post("/api/chat", json={"message": "Show my holdings", "chatId": chat["id"], "agentType": "portfolio"})

    assert response.status_code == 502
    assert refreshed == []


def test_portfolio_chat_passes_snapshot_context_and_only_relevant_saved_context(client, signed_in, monkeypatch):
    connect_user_to_indmoney(signed_in["id"])
    client.put("/api/profile", json={"riskAppetite": "moderate", "primaryGoal": "Buy a home"})
    snapshots.store_snapshot(
        signed_in["id"], datetime(2026, 10, 13, 9, 0, tzinfo=snapshots.SNAPSHOT_TIMEZONE),
        {"networth_snapshot": '{"net_worth": 4200000}'}, {},
    )
    chat = client.post("/api/chats", json={"agentType": "portfolio"}).json()
    seen = []

    async def fake_run_agent(message, mcp_url, token, history, agent_type, provider, *, context=None, saved_context=None, plan_parts=()):
        seen.append((context, saved_context))
        return "ok"

    monkeypatch.setattr(main, "run_agent", fake_run_agent)
    client.post("/api/chat", json={"message": "What is my net worth?", "chatId": chat["id"], "agentType": "portfolio"})
    client.post("/api/chat", json={"message": "Is my allocation right for my risk appetite?", "chatId": chat["id"], "agentType": "portfolio"})

    (context, unrelated), (_, relevant) = seen
    assert {tool.name for tool in context.local_tools} == {
        "get_portfolio_snapshot", "get_networth_history", "investigate_portfolio_change",
    }
    assert "2026-10-13T09:00" in context.profile_context
    assert "Risk appetite" not in context.profile_context
    # Preferences reach the prompt only when the request is about them.
    assert unrelated is None
    assert "risk appetite: moderate" in relevant.text and "primary goal: Buy a home" in relevant.text
    assert relevant.items == ("Profile preferences",)


def test_chat_rejects_empty_and_overlong_messages(client, signed_in):
    chat = client.post("/api/chats", json={"agentType": "finance"}).json()
    empty = client.post("/api/chat", json={"message": " ", "chatId": chat["id"]})
    long = client.post("/api/chat", json={"message": "x" * 8001, "chatId": chat["id"]})
    assert empty.status_code == 400
    assert long.status_code == 400


def test_chat_history_is_passed_to_agent_and_trimmed_to_limit(client, signed_in, monkeypatch):
    chat = client.post("/api/chats", json={"agentType": "finance"}).json()
    history = [{"role": "user", "content": f"question-{i}"} for i in range(main.MAX_HISTORY_MESSAGES)]
    with main.connect_db() as connection:
        connection.execute("UPDATE chats SET messages = ? WHERE id = ?", (json.dumps(history), chat["id"]))

    async def fake_run_agent(message, mcp_url, token, previous, agent_type, provider, *, context=None, saved_context=None, plan_parts=()):
        assert previous == history
        return "answer"

    monkeypatch.setattr(main, "run_agent", fake_run_agent)
    response = client.post("/api/chat", json={"message": "next", "chatId": chat["id"]})

    assert response.status_code == 200
    saved = client.get(f"/api/chats/{chat['id']}").json()["messages"]
    assert len(saved) == main.MAX_HISTORY_MESSAGES
    assert saved[-1] == {"role": "assistant", "content": "answer"}


def test_chat_limit_evicts_oldest_chat(client, signed_in):
    now = users.now_ms()
    with main.connect_db() as connection:
        for i in range(main.MAX_CHATS_PER_USER):
            connection.execute(
                "INSERT INTO chats(id, user_id, title, messages, updated_at, expires_at, agent_type) "
                "VALUES (?, ?, ?, '[]', ?, ?, 'finance')",
                (f"chat-{i}", signed_in["id"], f"chat {i}", now + i, now + main.SESSION_MAX_AGE_SECONDS * 1000),
            )

    created = client.post("/api/chats", json={"agentType": "finance"})

    assert created.status_code == 201
    chat_ids = {item["id"] for item in client.get("/api/chats").json()["chats"]}
    assert len(chat_ids) == main.MAX_CHATS_PER_USER
    assert "chat-0" not in chat_ids


def test_portfolio_chat_requires_indmoney_connection(client, signed_in):
    chat = client.post("/api/chats", json={"agentType": "portfolio"}).json()
    response = client.post("/api/chat", json={
        "message": "Summarize my allocation", "chatId": chat["id"], "agentType": "portfolio"
    })
    assert response.status_code == 401
    assert "Connect your INDmoney account" in response.json()["error"]


@pytest.mark.parametrize(("sent", "expected"), [("ollama", "ollama"), (None, "ollama")])
def test_chat_uses_the_local_provider(client, signed_in, monkeypatch, sent, expected):
    monkeypatch.setenv("OLLAMA_MODEL", "ca-mode")
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    chat = client.post("/api/chats", json={"agentType": "finance"}).json()
    used = []

    async def fake_run_agent(message, mcp_url, token, history, agent_type, provider, *, context=None, saved_context=None, plan_parts=()):
        used.append(provider)
        return "A budget tracks income and spending."

    monkeypatch.setattr(main, "run_agent", fake_run_agent)
    body = {"message": "What is a budget?", "chatId": chat["id"], "agentType": "finance"}
    if sent:
        body["aiProvider"] = sent
    response = client.post("/api/chat", json=body)

    assert response.status_code == 200
    assert used == [expected]


def test_chat_rejects_removed_provider(client, signed_in):
    chat = client.post("/api/chats", json={"agentType": "finance"}).json()
    response = client.post("/api/chat", json={
        "message": "hello", "chatId": chat["id"], "agentType": "finance", "aiProvider": "kimi"
    })
    assert response.status_code == 422


def test_tools_endpoint_returns_only_core_tools(client, signed_in, monkeypatch):
    connect_user_to_indmoney(signed_in["id"])

    async def fake_fetch(url, token):
        assert token == "access-token"
        return {"tools": [
            {"name": "get_indian_stocks_movers", "description": "market movers"},
            {"name": "networth_snapshot", "description": "portfolio value"},
            {"name": "mf_sips", "description": "mutual fund SIPs"},
        ]}

    monkeypatch.setattr(main, "fetch_mcp_session", fake_fetch)

    response = client.get("/api/tools")

    assert response.status_code == 200
    assert response.json()["toolCount"] == 2
    assert [item["name"] for item in response.json()["tools"]] == ["networth_snapshot", "mf_sips"]


def test_tools_endpoint_without_login_does_not_call_mcp(client, signed_in, monkeypatch):
    async def fail_if_called(url, token):
        raise AssertionError("MCP should not be called without a user token")

    monkeypatch.setattr(main, "fetch_mcp_session", fail_if_called)
    response = client.get("/api/tools")
    assert response.status_code == 200
    assert response.json()["connected"] is False
    assert response.json()["tools"] == []
    assert response.json()["loginUrl"] == "/auth/indmoney/connect"


def test_oauth_connect_requires_profile(client):
    client.get("/api/session")
    response = client.get("/auth/indmoney/connect")
    assert response.status_code == 302
    assert response.headers["location"] == "http://testserver/"


def _start_oauth(client) -> str:
    sid = client.cookies.get(main.SESSION_COOKIE)
    main.save_session(sid, {
        "state": "expected-state", "verifier": "pkce-verifier", "clientId": "client-id",
        "clientSecret": "client-secret", "redirectUri": "http://localhost/auth/indmoney/callback",
    })
    return sid


def test_oauth_callback_rejects_bad_state(client, signed_in):
    _start_oauth(client)

    response = client.get("/auth/indmoney/callback?code=auth-code&state=wrong")

    assert response.status_code == 400
    assert "could not be verified" in response.text


def test_oauth_connect_registers_client_and_uses_pkce(client, signed_in, monkeypatch):
    calls = {"gets": [], "posts": []}

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            calls["gets"].append(url)
            if url == main.MCP_RESOURCE_METADATA:
                return OAuthResponse({"authorization_servers": ["https://auth.example"]})
            return OAuthResponse({
                "registration_endpoint": "https://auth.example/register",
                "authorization_endpoint": "https://auth.example/authorize",
            })

        async def post(self, url, **kwargs):
            calls["posts"].append((url, kwargs))
            return OAuthResponse({"client_id": "client-id", "client_secret": "client-secret"})

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient())
    response = client.get("/auth/indmoney/connect")

    assert response.status_code == 302
    authorize = urlparse(response.headers["location"])
    query = parse_qs(authorize.query)
    assert authorize.netloc == "auth.example"
    assert query["redirect_uri"] == ["http://testserver/auth/indmoney/callback"]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["state"][0]) >= 32
    assert calls["posts"][0][1]["json"]["client_name"] == "CA Mode"

    stored = main.get_session(client.cookies.get(main.SESSION_COOKIE))
    expected_challenge = base64.urlsafe_b64encode(hashlib.sha256(stored["verifier"].encode()).digest()).decode().rstrip("=")
    assert query["code_challenge"] == [expected_challenge]
    assert stored["state"] == query["state"][0]


def test_oauth_callback_exchanges_code_and_saves_token_on_profile(client, signed_in, monkeypatch):
    sid = _start_oauth(client)
    requests = []
    background = []

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, data):
            requests.append((url, data))
            return OAuthResponse({"access_token": "access", "refresh_token": "refresh", "expires_in": 3600})

    async def fake_refresh(user_id):
        background.append(user_id)

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient())
    monkeypatch.setattr(main, "refresh_snapshot_in_background", fake_refresh)
    response = client.get("/auth/indmoney/callback?code=auth-code&state=expected-state")

    assert response.status_code == 302
    assert response.headers["location"] == "http://testserver/"
    assert requests[0][1]["code"] == "auth-code"
    assert requests[0][1]["code_verifier"] == "pkce-verifier"
    stored = users.get_indmoney(signed_in["id"])
    assert stored["accessToken"] == "access"
    assert stored["refreshToken"] == "refresh"
    assert stored["clientId"] == "client-id"
    session = main.get_session(sid)
    assert "state" not in session and "verifier" not in session
    assert background == [signed_in["id"]]


def test_user_access_token_refreshes_expired_access_token(stored_user, monkeypatch):
    connect_user_to_indmoney(stored_user, accessToken="expired", expiresAt=1)
    requests = []

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, data):
            requests.append((url, data))
            return OAuthResponse({"access_token": "fresh", "refresh_token": "new-refresh", "expires_in": 3600})

    monkeypatch.setattr(users.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient())

    token = asyncio.run(users.user_access_token(stored_user))

    assert token == "fresh"
    assert requests[0][1]["grant_type"] == "refresh_token"
    assert users.get_indmoney(stored_user)["refreshToken"] == "new-refresh"


def test_disconnect_keeps_chats_and_clears_snapshots(client, signed_in, monkeypatch):
    connect_user_to_indmoney(signed_in["id"], accessToken="token")
    users.cache_mcp_tools(signed_in["id"], [{"name": "networth_snapshot"}])
    snapshots.store_snapshot(signed_in["id"], datetime.now(snapshots.SNAPSHOT_TIMEZONE), {"networth_snapshot": "{}"}, {})
    chat = client.post("/api/chats", json={"agentType": "finance"}).json()
    revoked = []

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, data):
            revoked.append((url, data))
            return OAuthResponse({})

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient())
    response = client.post("/auth/indmoney/disconnect")

    assert response.json() == {"connected": False}
    assert revoked[0][1]["token"] == "token"
    user = users.get_user(signed_in["id"])
    assert user["indmoney"] == {} and user["mcpTools"] is None
    assert snapshots.latest_snapshot(signed_in["id"]) is None
    assert [item["id"] for item in client.get("/api/chats").json()["chats"]] == [chat["id"]]


def test_snapshots_endpoint_returns_latest_and_history(client, signed_in):
    today = datetime.now(snapshots.SNAPSHOT_TIMEZONE).replace(hour=9, minute=0, second=0, microsecond=0)
    for days_ago, value in ((2, 100.0), (1, 110.0), (0, 125.5)):
        snapshots.store_snapshot(
            signed_in["id"], today - timedelta(days=days_ago),
            {"networth_snapshot": json.dumps({"net_worth": value})}, {"networth_holdings": "timeout"},
        )

    response = client.get("/api/snapshots?days=7")

    assert response.status_code == 200
    body = response.json()
    assert body["latest"]["networthInr"] == 125.5
    assert body["latest"]["unavailable"] == {"networth_holdings": "timeout"}
    assert [point["networthInr"] for point in body["history"]] == [100.0, 110.0, 125.5]
    assert body["schedule"] == "09:00 Asia/Kolkata"


def test_snapshot_refresh_requires_indmoney(client, signed_in):
    response = client.post("/api/snapshots/refresh")
    assert response.status_code == 401
    assert "Connect your INDmoney account" in response.json()["error"]


def test_snapshot_refresh_runs_capture(client, signed_in, monkeypatch):
    connect_user_to_indmoney(signed_in["id"])

    async def fake_refresh_user(user_id, *, mcp_url, now=None):
        assert user_id == signed_in["id"]
        return {"asOfDate": "2026-10-13", "capturedAt": 1, "networthInr": 5.0, "errors": {}}

    monkeypatch.setattr(snapshots, "refresh_user", fake_refresh_user)
    response = client.post("/api/snapshots/refresh")

    assert response.status_code == 200
    assert response.json()["latest"]["networthInr"] == 5.0


def test_api_responses_disable_caching(client):
    response = client.get("/api/providers")
    assert response.headers["cache-control"] == "no-store"


def test_connect_db_closes_connection():
    with main.connect_db() as connection:
        connection.execute("SELECT 1")

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_session_endpoint_slides_session_expiry(client):
    client.get("/api/session")
    sid = client.cookies.get(main.SESSION_COOKIE)
    stale = int(time.time() * 1000) - 29 * 24 * 60 * 60 * 1000
    with main.connect_db() as connection:
        connection.execute("UPDATE oauth_sessions SET updated_at = ? WHERE id = ?", (stale, sid))

    response = client.get("/api/session")

    assert response.status_code == 200
    assert f"{main.SESSION_COOKIE}={sid}" in response.headers["set-cookie"]
    with main.connect_db() as connection:
        updated_at = connection.execute("SELECT updated_at FROM oauth_sessions WHERE id = ?", (sid,)).fetchone()[0]
    assert updated_at > stale


def test_expired_chats_are_hidden_and_purged(client, signed_in):
    chat_id = client.post("/api/chats", json={"agentType": "finance"}).json()["id"]
    with main.connect_db() as connection:
        connection.execute("UPDATE chats SET expires_at = 1 WHERE id = ?", (chat_id,))

    assert client.get("/api/chats").json()["chats"] == []
    assert client.get(f"/api/chats/{chat_id}").status_code == 404
    client.get("/api/session")
    with main.connect_db() as connection:
        assert connection.execute("SELECT 1 FROM chats WHERE id = ?", (chat_id,)).fetchone() is None


def test_user_access_token_refresh_network_error_returns_no_token(stored_user, monkeypatch):
    connect_user_to_indmoney(stored_user, accessToken="expired", expiresAt=1)
    monkeypatch.setattr(users.httpx, "AsyncClient", lambda **kwargs: FailingAsyncClient())

    assert asyncio.run(users.user_access_token(stored_user)) is None


def test_oauth_connect_upstream_failure_returns_502(client, signed_in, monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FailingAsyncClient())

    response = client.get("/auth/indmoney/connect")

    assert response.status_code == 502
    assert "Could not reach INDmoney" in response.json()["detail"]


def test_oauth_callback_without_access_token_returns_502(client, signed_in, monkeypatch):
    _start_oauth(client)

    class EmptyTokenClient(FailingAsyncClient):
        async def post(self, *args, **kwargs):
            return OAuthResponse({"token_type": "Bearer"})

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: EmptyTokenClient())

    response = client.get("/auth/indmoney/callback?code=auth-code&state=expected-state")

    assert response.status_code == 502
    assert "did not return an access token" in response.text


def test_portfolio_endpoint_returns_figures_and_every_holding(client, signed_in):
    from fixtures import indmoney_sample

    assert client.get("/api/portfolio").json() == {"available": False}
    snapshots.store_snapshot(
        signed_in["id"], datetime(2026, 10, 13, 9, 0, tzinfo=snapshots.SNAPSHOT_TIMEZONE),
        indmoney_sample.results(), {},
    )

    body = client.get("/api/portfolio").json()

    assert body["available"] is True
    assert body["asOfDate"] == "2026-10-13"
    assert body["figures"]["totals"]["net_worth_inr"] == 1200000
    assert [item["name"] for item in body["holdings"]][:2] == ["Parag Parikh Flexi Cap Fund", "Tata Consultancy Services"]
    assert len(body["holdings"]) == 6
    hdfc = next(item for item in body["holdings"] if item["name"] == "HDFC Bank")
    assert hdfc["broker"] == "Zerodha" and hdfc["pnl_inr"] is None


def test_portfolio_endpoint_requires_an_unlocked_profile(client):
    assert client.get("/api/portfolio").status_code == 401


# ── Google sign-in (AUTH_MODE=google) ──────────────────────────────────────────

GOOGLE_CLIENT_ID = "client-123.apps.googleusercontent.com"


def fake_id_token(claims: dict) -> str:
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")  # noqa: E731
    return f"{encode({'alg': 'RS256'})}.{encode(claims)}.signature"


@pytest.fixture
def google_mode(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "google")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", GOOGLE_CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "google-secret")
    return monkeypatch


def google_sign_in(client, monkeypatch, subject="google-asha", email="asha@example.com", *, params=None, **claims):
    """Run /auth/google/start and the callback against a fake Google token endpoint; return the callback response."""
    start = client.get("/auth/google/start")
    assert start.status_code == 302, start.text
    query = parse_qs(urlparse(start.headers["location"]).query)
    now = int(time.time())
    token_claims = {
        "iss": "https://accounts.google.com", "aud": GOOGLE_CLIENT_ID, "sub": subject, "email": email,
        "email_verified": True, "name": "Asha Kumar", "given_name": "Asha", "nonce": query["nonce"][0],
        "iat": now, "exp": now + 3600, **claims,
    }
    posted = {}

    class GoogleTokenClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, data):
            posted.update(url=url, **data)
            return OAuthResponse({"id_token": fake_id_token(token_claims), "access_token": "unused"})

    monkeypatch.setattr(accounts.httpx, "AsyncClient", GoogleTokenClient)
    callback = client.get("/auth/google/callback", params=params or {"state": query["state"][0], "code": "auth-code"})
    callback.posted = posted
    return callback


def create_account_profile(client, name="Asha", pin="2468", *, consent=True) -> dict:
    created = client.post("/api/users", json={"displayName": name, "pin": pin})
    assert created.status_code == 201, created.text
    if consent:
        client.post("/api/privacy/consent", json={"policyVersion": privacy.POLICY_VERSION, "storageMode": "device"})
    return created.json()


def test_google_mode_never_lists_profiles_or_signs_in_by_profile_id(client, google_mode):
    existing = users.create_user("Ravi", users.hash_pin("1357"))

    session = client.get("/api/session").json()

    assert session["profiles"] == [] and session["user"] is None
    assert (session["authMode"], session["account"]) == ("google", None)
    assert "Ravi" not in json.dumps(session)
    assert client.post("/api/session/login", json={"userId": existing["id"], "pin": "1357"}).status_code == 404
    refused = client.post("/api/users", json={"displayName": "Mallory", "pin": "2468"})
    assert refused.status_code == 401 and refused.json()["signInRequired"] is True


def test_google_mode_keeps_the_sign_up_check_and_sign_in_rate_limit(client, google_mode):
    google_mode.setenv("TURNSTILE_SITE_KEY", "site-key")
    google_mode.setenv("TURNSTILE_SECRET_KEY", "secret-key")
    google_mode.setenv("RATE_LIMITS_ENABLED", "true")
    google_mode.setitem(limits.RATE_RULES, "sign_in", limits.GroupRules(limits.Rule(1, 600), None))
    google_sign_in(client, google_mode)

    session = client.get("/api/session").json()
    assert (session["authMode"], session["turnstileSiteKey"]) == ("google", "site-key")
    unchecked = client.post("/api/users", json={"displayName": "Asha", "pin": "2468"})
    assert unchecked.status_code == 400 and unchecked.json()["turnstileFailed"] is True
    # Claiming a profile checks a PIN, so it shares the sign-in limit.
    assert client.post("/api/account/claim", json={"displayName": "Ravi", "pin": "1357"}).status_code == 401
    blocked = client.post("/api/account/claim", json={"displayName": "Ravi", "pin": "1357"})
    assert blocked.status_code == 429 and blocked.json()["rateLimited"] is True


def test_google_start_redirects_with_pkce_state_and_nonce(client, google_mode):
    client.get("/api/session")
    before = client.cookies.get(main.SESSION_COOKIE)

    start = client.get("/auth/google/start")

    location = urlparse(start.headers["location"])
    query = {key: values[0] for key, values in parse_qs(location.query).items()}
    assert f"{location.scheme}://{location.netloc}{location.path}" == accounts.GOOGLE_AUTHORIZE_URL
    assert query["client_id"] == GOOGLE_CLIENT_ID
    assert query["redirect_uri"] == "http://testserver/auth/google/callback"
    assert query["scope"] == "openid email profile"
    assert query["code_challenge_method"] == "S256" and query["code_challenge"]
    assert query["state"] and query["nonce"] and query["state"] != query["nonce"]
    assert client.cookies.get(main.SESSION_COOKIE) != before  # Rotated before leaving for Google.
    assert start.headers["cache-control"] == "no-store"


def test_new_google_account_creates_a_profile_and_unlocks_it(client, google_mode):
    callback = google_sign_in(client, google_mode)

    assert callback.status_code == 302 and callback.headers["location"] == "http://testserver/"
    assert callback.posted["code"] == "auth-code" and callback.posted["code_verifier"]
    assert callback.posted["client_secret"] == "google-secret"
    session = client.get("/api/session").json()
    assert session["user"] is None
    assert session["account"] == {"email": "asha@example.com", "name": "Asha", "pending": True, "canClaim": True}

    created = create_account_profile(client)

    assert created["recoveryCode"]
    session = client.get("/api/session").json()
    assert session["user"] == created["user"] and session["locked"] is False
    assert session["account"] == {"email": "asha@example.com"}
    assert client.get("/api/chats").status_code == 200
    with main.connect_db() as connection:
        row = connection.execute("SELECT provider, subject, email FROM accounts").fetchone()
    assert (row["provider"], row["subject"]) == ("google", "google-asha")
    assert "asha@example.com" not in row["email"]  # Encrypted at rest.


def test_returning_google_account_signs_in_locked_until_pin(client, google_mode):
    google_sign_in(client, google_mode)
    user = create_account_profile(client)["user"]
    chat = client.post("/api/chats", json={"agentType": "finance"}).json()
    client.post("/api/session/logout")
    assert client.get("/api/session").json()["account"] is None

    google_sign_in(client, google_mode)

    session = client.get("/api/session").json()
    assert session["user"] == user and session["locked"] is True
    assert client.get("/api/chats").status_code == 403
    assert client.post("/api/session/unlock", json={"pin": "0000"}).status_code == 401
    assert client.post("/api/session/unlock", json={"pin": "2468"}).status_code == 200
    assert [item["id"] for item in client.get("/api/chats").json()["chats"]] == [chat["id"]]


def test_google_accounts_may_share_a_profile_name(client, google_mode):
    google_sign_in(client, google_mode, "google-asha", "asha@example.com")
    first = create_account_profile(client, "Asha")["user"]
    client.post("/api/session/logout")

    google_sign_in(client, google_mode, "google-other", "other@example.com")
    second = create_account_profile(client, "asha", "1357")["user"]

    assert first["id"] != second["id"]
    assert client.get("/api/chats").json()["chats"] == []


def test_profile_from_the_picker_is_claimed_with_its_pin(client, google_mode):
    google_mode.setenv("AUTH_MODE", "local")
    ravi = sign_up(client, "Ravi", "1357")
    chat = client.post("/api/chats", json={"agentType": "finance"}).json()
    google_mode.setenv("AUTH_MODE", "google")
    # The picker session no longer counts: the profile has no account yet.
    assert client.get("/api/session").json()["user"] is None

    google_sign_in(client, google_mode, "google-ravi", "ravi@example.com")
    unknown = client.post("/api/account/claim", json={"displayName": "Nobody", "pin": "1357"})
    wrong = client.post("/api/account/claim", json={"displayName": "Ravi", "pin": "0000"})
    claimed = client.post("/api/account/claim", json={"displayName": " ravi ", "pin": "1357"})

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json() == {"error": main.CLAIM_FAILED}
    assert claimed.status_code == 200 and claimed.json()["user"] == ravi
    session = client.get("/api/session").json()
    assert session["user"] == ravi and session["locked"] is False
    assert [item["id"] for item in client.get("/api/chats").json()["chats"]] == [chat["id"]]

    # Another Google account cannot claim it again, even with the PIN.
    client.post("/api/session/logout")
    google_sign_in(client, google_mode, "google-mallory", "mallory@example.com")
    again = client.post("/api/account/claim", json={"displayName": "Ravi", "pin": "1357"})
    assert again.status_code == 401 and again.json() == {"error": main.CLAIM_FAILED}


def test_claim_shares_the_profile_pin_lockout_and_can_be_turned_off(client, google_mode):
    users.create_user("Ravi", users.hash_pin("1357"))
    google_sign_in(client, google_mode, "google-mallory", "mallory@example.com")

    for _ in range(users.MAX_FAILED_LOGINS - 1):
        assert client.post("/api/account/claim", json={"displayName": "Ravi", "pin": "0000"}).status_code == 401
    assert client.post("/api/account/claim", json={"displayName": "Ravi", "pin": "0000"}).status_code == 429
    assert client.post("/api/account/claim", json={"displayName": "Ravi", "pin": "1357"}).status_code == 429

    google_mode.setenv("ALLOW_PROFILE_CLAIMS", "false")
    off = client.post("/api/account/claim", json={"displayName": "Ravi", "pin": "1357"})
    assert off.status_code == 403
    assert client.get("/api/session").json()["account"]["canClaim"] is False


def test_claim_needs_a_google_account_waiting_and_google_mode(client, google_mode):
    users.create_user("Ravi", users.hash_pin("1357"))
    client.get("/api/session")
    no_account = client.post("/api/account/claim", json={"displayName": "Ravi", "pin": "1357"})
    assert no_account.status_code == 401 and no_account.json()["signInRequired"] is True

    google_mode.setenv("AUTH_MODE", "local")
    assert client.post("/api/account/claim", json={"displayName": "Ravi", "pin": "1357"}).status_code == 404
    assert client.get("/auth/google/start").status_code == 404
    assert client.get("/auth/google/callback").status_code == 404


def test_pending_google_account_expires(client, google_mode):
    google_sign_in(client, google_mode)
    sid = client.cookies.get(main.SESSION_COOKIE)
    stored = main.get_session(sid)
    stored[accounts.PENDING_KEY]["at"] -= accounts.PENDING_SECONDS * 1000 + 1
    main.save_session(sid, stored)

    assert client.get("/api/session").json()["account"] is None
    assert client.post("/api/users", json={"displayName": "Asha", "pin": "2468"}).status_code == 401


@pytest.mark.parametrize(("case", "expected"), [
    ("wrong_state", "failed"), ("denied", "cancelled"), ("other_audience", "failed"),
    ("unverified_email", "failed"), ("wrong_nonce", "failed"), ("expired_token", "expired"), ("not_google", "failed"),
])
def test_google_callback_failures_sign_nobody_in(client, google_mode, case, expected):
    now = int(time.time())
    options = {
        "wrong_state": {"params": {"state": "forged", "code": "auth-code"}},
        "denied": {"params": {"error": "access_denied"}},
        "other_audience": {"aud": "someone-else"},
        "unverified_email": {"email_verified": False},
        "wrong_nonce": {"nonce": "replayed"},
        "expired_token": {"exp": now - 3600, "iat": now - 7200},
        "not_google": {"iss": "https://evil.example"},
    }[case]

    callback = google_sign_in(client, google_mode, **options)

    assert callback.status_code == 302
    assert callback.headers["location"] == f"http://testserver/?signInError={expected}"
    session = client.get("/api/session").json()
    assert session["user"] is None and session["account"] is None


def test_google_callback_without_a_started_sign_in_is_refused(client, google_mode):
    client.get("/api/session")
    callback = client.get("/auth/google/callback", params={"state": "s", "code": "c"})
    assert callback.headers["location"] == "http://testserver/?signInError=expired"


def test_deleting_a_google_profile_removes_the_account_link(client, google_mode):
    google_sign_in(client, google_mode)
    user = create_account_profile(client)["user"]

    assert client.request("DELETE", "/api/users/me", json={"pin": "2468"}).json() == {"deleted": True}

    assert accounts.account_for_user(user["id"]) is None
    google_sign_in(client, google_mode)
    assert client.get("/api/session").json()["account"]["pending"] is True


def test_export_includes_the_account_email(client, google_mode):
    google_sign_in(client, google_mode)
    create_account_profile(client)

    exported = client.post("/api/users/me/export", json={"pin": "2468"}).json()

    assert exported["account"]["provider"] == "google"
    assert exported["account"]["email"] == "asha@example.com"
    assert "google-asha" not in json.dumps(exported)


def test_google_recovery_resets_the_signed_in_profile_only(client, google_mode):
    google_sign_in(client, google_mode, "google-other", "other@example.com")
    other = create_account_profile(client, "Other", "9999")["user"]
    client.post("/api/session/logout")
    google_sign_in(client, google_mode)
    asha = create_account_profile(client)
    client.post("/api/session/logout")
    google_sign_in(client, google_mode)  # Signed in again: locked.

    stolen = client.post("/api/session/recover", json={
        "userId": other["id"], "recoveryCode": asha["recoveryCode"], "newPin": "1111",
    })
    recovered = client.post("/api/session/recover", json={"recoveryCode": asha["recoveryCode"], "newPin": "1111"})

    assert stolen.status_code == 401
    assert recovered.status_code == 200 and recovered.json()["recoveryCode"]
    assert client.get("/api/session").json()["locked"] is False
    client.post("/api/session/logout")
    assert client.post("/api/session/recover", json={"recoveryCode": "x", "newPin": "1111"}).json()["signInRequired"]


def test_local_mode_still_lists_profiles_and_refuses_duplicate_names(client):
    asha = sign_up(client, "Asha")
    client.post("/api/session/logout")

    session = client.get("/api/session").json()

    assert (session["authMode"], session["account"], session["profiles"]) == ("local", None, [asha])
    assert client.post("/api/users", json={"displayName": "ASHA", "pin": "1111"}).status_code == 409
    assert client.post("/api/session/login", json={"userId": asha["id"], "pin": "2468"}).status_code == 200

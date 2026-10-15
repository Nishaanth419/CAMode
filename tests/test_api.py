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

from app import main, snapshots, users
from app.agent import AgentRunError


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
        "profiles": [],
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

    async def fake_run_agent(message, mcp_url, token, history, agent_type, provider, *, context=None):
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

    async def fake_run_agent(message, mcp_url, token, history, agent_type, provider, *, context=None):
        calls.append(token)
        assert context is not None
        if token == "stale-token":
            raise AgentRunError(
                'MCP tools/call failed for "get_indian_stocks_option_chain": 401 '
                '{"error":"invalid_token","error_description":"Authentication required"}',
                [],
            )
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


def test_portfolio_chat_passes_profile_and_snapshot_context(client, signed_in, monkeypatch):
    connect_user_to_indmoney(signed_in["id"])
    client.put("/api/profile", json={"riskAppetite": "moderate", "primaryGoal": "Buy a home"})
    snapshots.store_snapshot(
        signed_in["id"], datetime(2026, 10, 13, 9, 0, tzinfo=snapshots.SNAPSHOT_TIMEZONE),
        {"networth_snapshot": '{"net_worth": 4200000}'}, {},
    )
    chat = client.post("/api/chats", json={"agentType": "portfolio"}).json()
    seen = {}

    async def fake_run_agent(message, mcp_url, token, history, agent_type, provider, *, context=None):
        seen["context"] = context
        return "ok"

    monkeypatch.setattr(main, "run_agent", fake_run_agent)
    client.post("/api/chat", json={"message": "What is my net worth?", "chatId": chat["id"], "agentType": "portfolio"})

    context = seen["context"]
    assert {tool.name for tool in context.local_tools} == {"get_portfolio_snapshot", "get_networth_history"}
    assert "Risk appetite: moderate" in context.profile_context
    assert "Buy a home" in context.profile_context
    assert "2026-10-13T09:00" in context.profile_context


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

    async def fake_run_agent(message, mcp_url, token, previous, agent_type, provider, *, context=None):
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

    async def fake_run_agent(message, mcp_url, token, history, agent_type, provider, *, context=None):
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


GOAL = {"id": "home", "name": "Buy a home", "targetInr": 5_000_000, "targetYear": 2034,
        "savedInr": 800_000, "monthlyInr": 25_000, "expectedReturnPct": 10}


def test_goals_are_saved_with_the_profile_and_kept_by_profile_edits(client, signed_in):
    assert client.get("/api/goals").json() == {"goals": []}
    client.put("/api/profile", json={"riskAppetite": "high"})

    saved = client.put("/api/goals", json={"goals": [GOAL]})
    client.put("/api/profile", json={"riskAppetite": "low"})

    assert saved.status_code == 200
    assert client.get("/api/goals").json() == {"goals": [GOAL]}
    assert client.get("/api/profile").json()["profile"]["riskAppetite"] == "low"
    stored = users.get_user(signed_in["id"])["profileStored"]
    assert stored.startswith("sealed:v1:") and "Buy a home" not in stored


def test_goals_keep_their_kind_and_reject_unknown_kinds(client, signed_in):
    saved = client.put("/api/goals", json={"goals": [{**GOAL, "kind": "home"}]})

    assert saved.status_code == 200
    assert client.get("/api/goals").json()["goals"][0]["kind"] == "home"
    assert client.put("/api/goals", json={"goals": [{**GOAL, "kind": "yacht"}]}).status_code == 422


def test_goals_reject_invalid_values_and_duplicate_ids(client, signed_in):
    assert client.put("/api/goals", json={"goals": [{**GOAL, "targetInr": 0}]}).status_code == 422
    assert client.put("/api/goals", json={"goals": [GOAL, GOAL]}).status_code == 422
    assert client.put("/api/goals", json={"goals": [{**GOAL, "id": f"g{i}"} for i in range(11)]}).status_code == 422


def test_goals_reach_the_portfolio_profile_context():
    context = snapshots.profile_context({"goals": [GOAL]}, None)

    assert "Goal: Buy a home, target INR 5000000 by 2034, saved INR 800000, investing INR 25000 a month" in context

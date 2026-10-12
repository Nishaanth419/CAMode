from __future__ import annotations

import asyncio
import json
import base64
import hashlib
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app import main


def test_health_and_frontend_routes(client):
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/").status_code == 200
    assert client.get("/js/app.js").status_code == 200


def test_provider_endpoint_exposes_only_openai_and_groq(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test")
    monkeypatch.setenv("GROQ_API_KEY", "groq-test")
    monkeypatch.setenv("AI_PROVIDER", "groq")

    response = client.get("/api/providers")

    assert response.status_code == 200
    assert [provider["id"] for provider in response.json()["providers"]] == ["openai", "groq"]
    assert response.json()["defaultProvider"] == "groq"
    assert response.headers["cache-control"] == "no-store"


def test_session_initialization_sets_http_only_cookie(client):
    response = client.get("/api/session")

    assert response.json() == {"connected": False}
    cookie = response.headers["set-cookie"].lower()
    assert "indmoney_session=" in cookie
    assert "httponly" in cookie


def test_session_cookie_is_secure_for_https_requests():
    with TestClient(main.app, base_url="https://testserver", follow_redirects=False) as secure_client:
        response = secure_client.get("/api/session")
    assert "secure" in response.headers["set-cookie"].lower()


def test_expired_local_session_is_removed():
    main.save_session("expired", {"accessToken": "stale"})
    with main.connect_db() as connection:
        connection.execute("UPDATE oauth_sessions SET updated_at = ? WHERE id = ?", (0, "expired"))

    assert main.get_session("expired") == {}
    with main.connect_db() as connection:
        assert connection.execute("SELECT 1 FROM oauth_sessions WHERE id = ?", ("expired",)).fetchone() is None


def test_chat_endpoints_require_local_session(client):
    response = client.get("/api/chats")
    assert response.status_code == 401
    assert response.json()["loginUrl"] == "/auth/indmoney/connect"


def test_chat_crud_and_session_isolation(client):
    client.get("/api/session")
    created = client.post("/api/chats", json={"agentType": "finance"})
    assert created.status_code == 201
    chat_id = created.json()["id"]
    assert client.get("/api/chats").json()["chats"][0]["id"] == chat_id
    assert client.get(f"/api/chats/{chat_id}").json()["messages"] == []

    other_user = type(client)(main.app, follow_redirects=False)
    assert other_user.get(f"/api/chats/{chat_id}").status_code == 401
    assert other_user.delete(f"/api/chats/{chat_id}").status_code == 401

    deleted = client.delete(f"/api/chats/{chat_id}")
    assert deleted.json() == {"deleted": True}
    assert client.get(f"/api/chats/{chat_id}").status_code == 404


def test_create_chat_rejects_invalid_agent_type(client):
    client.get("/api/session")
    response = client.post("/api/chats", json={"agentType": "local"})
    assert response.status_code == 422


def test_chat_message_is_saved_and_agent_type_is_enforced(client, monkeypatch):
    client.get("/api/session")
    chat = client.post("/api/chats", json={"agentType": "finance"}).json()

    async def fake_run_agent(message, mcp_url, token, history, agent_type, provider):
        assert message == "What is diversification?"
        assert history == []
        assert agent_type == "finance"
        assert provider == "openai"
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


def test_chat_rejects_empty_and_overlong_messages(client):
    client.get("/api/session")
    chat = client.post("/api/chats", json={"agentType": "finance"}).json()
    empty = client.post("/api/chat", json={"message": " ", "chatId": chat["id"]})
    long = client.post("/api/chat", json={"message": "x" * 8001, "chatId": chat["id"]})
    assert empty.status_code == 400
    assert long.status_code == 400


def test_chat_history_is_passed_to_agent_and_trimmed_to_limit(client, monkeypatch):
    client.get("/api/session")
    chat = client.post("/api/chats", json={"agentType": "finance"}).json()
    history = [{"role": "user", "content": f"question-{i}"} for i in range(main.MAX_HISTORY_MESSAGES)]
    with main.connect_db() as connection:
        connection.execute("UPDATE chats SET messages = ? WHERE id = ?", (json.dumps(history), chat["id"]))

    async def fake_run_agent(message, mcp_url, token, previous, agent_type, provider):
        assert previous == history
        return "answer"

    monkeypatch.setattr(main, "run_agent", fake_run_agent)
    response = client.post("/api/chat", json={"message": "next", "chatId": chat["id"]})

    assert response.status_code == 200
    saved = client.get(f"/api/chats/{chat['id']}").json()["messages"]
    assert len(saved) == main.MAX_HISTORY_MESSAGES
    assert saved[-1] == {"role": "assistant", "content": "answer"}


def test_chat_limit_evicts_oldest_chat(client):
    client.get("/api/session")
    sid = client.cookies.get(main.SESSION_COOKIE)
    now = int(main.time.time() * 1000)
    with main.connect_db() as connection:
        for i in range(main.MAX_CHATS_PER_SESSION):
            connection.execute(
                "INSERT INTO chats(id, session_id, title, messages, updated_at, expires_at, agent_type) "
                "VALUES (?, ?, ?, '[]', ?, ?, 'finance')",
                (f"chat-{i}", sid, f"chat {i}", now + i, now + main.SESSION_MAX_AGE_SECONDS * 1000),
            )

    created = client.post("/api/chats", json={"agentType": "finance"})

    assert created.status_code == 201
    chat_ids = {item["id"] for item in client.get("/api/chats").json()["chats"]}
    assert len(chat_ids) == main.MAX_CHATS_PER_SESSION
    assert "chat-0" not in chat_ids


def test_portfolio_chat_requires_indmoney_connection(client):
    client.get("/api/session")
    chat = client.post("/api/chats", json={"agentType": "portfolio"}).json()
    response = client.post("/api/chat", json={
        "message": "Summarize my allocation", "chatId": chat["id"], "agentType": "portfolio"
    })
    assert response.status_code == 401
    assert "Connect your INDmoney account" in response.json()["error"]


def test_chat_rejects_removed_provider(client):
    client.get("/api/session")
    chat = client.post("/api/chats", json={"agentType": "finance"}).json()
    response = client.post("/api/chat", json={
        "message": "hello", "chatId": chat["id"], "agentType": "finance", "aiProvider": "kimi"
    })
    assert response.status_code == 422


def test_profile_review_requires_portfolio_agent(client):
    client.get("/api/session")
    chat = client.post("/api/chats", json={"agentType": "finance"}).json()
    response = client.post("/api/chat", json={
        "message": "Create my portfolio profile review.", "chatId": chat["id"],
        "agentType": "finance", "profileReview": True,
    })
    assert response.status_code == 400
    assert "requires Portfolio Analyzer" in response.json()["error"]


def test_profile_review_uses_extended_agent_mode(client, monkeypatch):
    client.get("/api/session")
    chat = client.post("/api/chats", json={"agentType": "portfolio"}).json()

    async def fake_access_token(request):
        return client.cookies.get(main.SESSION_COOKIE), "account-token"

    async def fake_run_agent(message, mcp_url, token, history, agent_type, provider, profile_review=False):
        assert token == "account-token"
        assert agent_type == "portfolio"
        assert profile_review is True
        return "## Portfolio review"

    monkeypatch.setattr(main, "access_token", fake_access_token)
    monkeypatch.setattr(main, "run_agent", fake_run_agent)
    response = client.post("/api/chat", json={
        "message": "Create my portfolio profile review.", "chatId": chat["id"],
        "agentType": "portfolio", "profileReview": True,
    })
    assert response.status_code == 200
    assert response.text == "## Portfolio review"


def test_tools_endpoint_returns_only_core_tools(client, monkeypatch):
    async def fake_access_token(request):
        return "session", "access-token"

    async def fake_fetch(url, token):
        return {"tools": [
            {"name": "get_indian_stocks_movers", "description": "market movers"},
            {"name": "networth_snapshot", "description": "portfolio value"},
            {"name": "mf_sips", "description": "mutual fund SIPs"},
        ]}

    monkeypatch.setattr(main, "access_token", fake_access_token)
    monkeypatch.setattr(main, "fetch_mcp_session", fake_fetch)

    response = client.get("/api/tools")

    assert response.status_code == 200
    assert response.json()["toolCount"] == 2
    assert [item["name"] for item in response.json()["tools"]] == ["networth_snapshot", "mf_sips"]


def test_tools_endpoint_without_login_does_not_call_mcp(client, monkeypatch):
    async def fail_if_called(url, token):
        raise AssertionError("MCP should not be called without a user token")

    monkeypatch.setattr(main, "fetch_mcp_session", fail_if_called)
    response = client.get("/api/tools")
    assert response.status_code == 200
    assert response.json()["connected"] is False
    assert response.json()["tools"] == []
    assert response.json()["loginUrl"] == "/auth/indmoney/connect"


def test_oauth_callback_rejects_bad_state(client):
    client.cookies.set(main.SESSION_COOKIE, "oauth-session")
    main.save_session("oauth-session", {
        "state": "expected", "verifier": "verifier", "clientId": "client", "clientSecret": "secret"
    })

    response = client.get("/auth/indmoney/callback?code=auth-code&state=wrong")

    assert response.status_code == 400
    assert "could not be verified" in response.text


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


def test_oauth_connect_registers_client_and_uses_pkce(client, monkeypatch):
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

    sid = response.cookies[main.SESSION_COOKIE]
    stored = main.get_session(sid)
    expected_challenge = base64.urlsafe_b64encode(hashlib.sha256(stored["verifier"].encode()).digest()).decode().rstrip("=")
    assert query["code_challenge"] == [expected_challenge]
    assert stored["state"] == query["state"][0]


def test_oauth_callback_exchanges_code_and_saves_token(client, monkeypatch):
    client.cookies.set(main.SESSION_COOKIE, "oauth-session")
    main.save_session("oauth-session", {
        "state": "expected-state", "verifier": "pkce-verifier", "clientId": "client-id",
        "clientSecret": "client-secret", "redirectUri": "http://localhost/auth/indmoney/callback",
    })
    requests = []

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, data):
            requests.append((url, data))
            return OAuthResponse({"access_token": "access", "refresh_token": "refresh", "expires_in": 3600})

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient())
    response = client.get("/auth/indmoney/callback?code=auth-code&state=expected-state")

    assert response.status_code == 302
    assert response.headers["location"] == "http://testserver/"
    assert requests[0][1]["code"] == "auth-code"
    assert requests[0][1]["code_verifier"] == "pkce-verifier"
    stored = main.get_session("oauth-session")
    assert stored["accessToken"] == "access"
    assert stored["refreshToken"] == "refresh"
    assert "state" not in stored and "verifier" not in stored


def test_access_token_refreshes_expired_access_token(monkeypatch):
    main.save_session("refresh-session", {
        "accessToken": "expired", "expiresAt": 1, "refreshToken": "refresh",
        "clientId": "client-id", "clientSecret": "client-secret",
    })
    requests = []

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, data):
            requests.append((url, data))
            return OAuthResponse({"access_token": "fresh", "refresh_token": "new-refresh", "expires_in": 3600})

    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient())
    request = type("Request", (), {"cookies": {main.SESSION_COOKIE: "refresh-session"}})()

    sid, token = asyncio.run(main.access_token(request))

    assert sid == "refresh-session"
    assert token == "fresh"
    assert requests[0][1]["grant_type"] == "refresh_token"
    assert main.get_session("refresh-session")["refreshToken"] == "new-refresh"


def test_disconnect_revokes_token_and_clears_session_and_chats(client, monkeypatch):
    client.cookies.set(main.SESSION_COOKIE, "signed-in")
    main.save_session("signed-in", {
        "accessToken": "token", "clientId": "client-id", "clientSecret": "client-secret"
    })
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
    assert main.get_session("signed-in") == {}
    client.get("/api/session")
    assert client.get("/api/chats").json()["chats"] == []


def test_api_responses_disable_caching(client):
    response = client.get("/api/providers")
    assert response.headers["cache-control"] == "no-store"

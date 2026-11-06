"""Every broker with an official hosted MCP server: registry, read-only rule, login flows, OAuth, tool lists, layouts."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app import broker_oauth, brokers, main, mcp_client, tool_results, users
from app.db import connect_db
from app.sealing import ProfileKeys

SIX = {"zerodha", "fyers", "kotak", "upstox", "dhan", "groww"}


def run(coro):
    return asyncio.run(coro)


def agent_user(user_id):
    return {"id": user_id, "keys": ProfileKeys(), "storageMode": "device"}


def tool(tools, name):
    return next(item for item in tools if item.name == name)


# ── Registry and the read-only rule ────────────────────────────────────────────

def test_registry_is_exactly_the_brokers_with_official_hosted_servers():
    assert set(brokers.BROKERS) == SIX
    assert {broker.auth for broker in brokers.BROKERS.values()} == {"mcp_login_tool", "mcp_login_qr", "oauth"}
    for broker in brokers.BROKERS.values():
        assert broker.mcp_url.startswith("https://") and broker.indmoney_labels
        assert not (brokers.WRITE_TOOLS & set(broker.read_tools))
        assert not any(brokers.is_write_name(name) for name in broker.read_tools)
        assert not any({"login", "logout", "validate"} & brokers._name_words(name) for name in broker.read_tools)
        assert set(broker.holdings_tools) <= set(broker.read_tools)
    assert brokers.BROKERS["kotak"].session_arg == "sessionid"


def test_the_curated_fyers_list_has_reads_only():
    fyers = brokers.BROKERS["fyers"]
    for name in ("create_order", "cancel_order", "modify_order", "exit_positions", "convert_position", "login",
                 "logout_user", "add_symbols_to_watchlist", "delete_watchlist", "create_price_alert"):
        assert name not in fyers.read_tools
        assert brokers.tool_access(fyers, name) in {"write", "not_enabled"}
    assert {"get_holdings", "get_funds", "calculate_span_margin", "get_mutual_fund_holdings"} <= set(fyers.read_tools)


@pytest.mark.parametrize("name", [
    "place_order", "modifyOrder", "cancel_all", "delete_watchlist", "exit_positions", "convert_position", "create_alert",
    "add_symbols", "remove_symbol", "update_profile", "set_alert", "login", "get_login", "logout_user", "validate_login",
    "transfer_funds", "withdraw", "buy_stock", "sell_stock", "get_and_place_order", "order_place", "fetch_then_cancel",
])
def test_write_looking_names_are_never_read(name):
    assert brokers.dynamic_read({"name": name}) is False
    assert brokers.dynamic_read({"name": name, "annotations": {"readOnlyHint": True}}) is False


@pytest.mark.parametrize("name", ["get_holdings", "search_instruments", "list_orders", "fetch_quotes", "get_asset_allocation",
                                  "get_settings_summary", "get_order_book"])
def test_read_looking_names_are_read(name):
    assert brokers.dynamic_read({"name": name}) is True


def test_a_read_only_hint_is_honoured_but_other_names_are_not_read():
    assert brokers.dynamic_read({"name": "portfolio_summary", "annotations": {"readOnlyHint": True}}) is True
    assert brokers.dynamic_read({"name": "portfolio_summary", "annotations": {"readOnlyHint": False}}) is False
    assert brokers.dynamic_read({"name": "portfolio_summary", "annotations": None}) is False
    assert brokers.dynamic_read({"name": "portfolio_summary"}) is False
    assert brokers.dynamic_read({"name": ""}) is False and brokers.dynamic_read({}) is False


def test_tool_access_uses_the_live_entry_for_oauth_brokers():
    upstox = brokers.BROKERS["upstox"]
    assert brokers.tool_access(upstox, "get_holdings", {"name": "get_holdings"}) == "read"
    assert brokers.tool_access(upstox, "get_holdings") == "not_enabled"  # no live entry: never read
    assert brokers.tool_access(upstox, "place_order", {"name": "place_order"}) == "write"
    assert brokers.tool_access(upstox, "login", {"name": "login"}) == "not_enabled"


# ── A fake MCP server for every broker ─────────────────────────────────────────

LIVE_TOOLS = [
    {"name": "get_holdings", "description": "Holdings."},
    {"name": "get_mf_holdings", "description": "MF holdings."},
    {"name": "get_positions", "description": "Positions."},
    {"name": "place_order", "description": "Place an order."},
    {"name": "delete_watchlist", "description": "x"},
    {"name": "portfolio_summary", "description": "Hinted.", "annotations": {"readOnlyHint": True}},
    {"name": "login", "description": "Log in."},
]


class FakeMcp:
    def __init__(self):
        self.sessions: list[tuple[str, str | None]] = []
        self.calls: list[tuple[str, str | None, str, dict]] = []
        self.responses: dict[str, str] = {}
        self.errors: dict[str, Exception] = {}
        self.tools = LIVE_TOOLS

    async def fetch_mcp_session(self, url, token, *, service="INDmoney"):
        self.sessions.append((url, token))
        if "initialize" in self.errors:
            raise self.errors["initialize"]
        return {"tools": self.tools, "sessionId": f"mcp-{len(self.sessions)}", "protocolVersion": "2025-03-26"}

    async def call_mcp_tool(self, url, token, protocol, session_id, name, arguments, *, client=None):
        self.calls.append((name, token, session_id, dict(arguments)))
        if name in self.errors:
            raise self.errors[name]
        return self.responses.get(name, "[]")


@pytest.fixture
def mcp(monkeypatch):
    fake = FakeMcp()
    monkeypatch.setattr(brokers, "_tools_cache", {})
    monkeypatch.setattr(brokers.mcp_client, "fetch_mcp_session", fake.fetch_mcp_session)
    monkeypatch.setattr(brokers.mcp_client, "call_mcp_tool", fake.call_mcp_tool)
    return fake


def store_oauth(user_id, broker_id="upstox", *, expires_in=3600, refresh="refresh-secret", token="access-secret"):
    brokers.save_oauth(user_id, broker_id, {
        "accessToken": token, "refreshToken": refresh, "expiresAt": users.now_ms() + expires_in * 1000,
        "client": {"clientId": "cid", "clientSecret": "csecret"}, "tokenEndpoint": "https://auth.example/token",
    })


def handler_for(user_id, name):
    return tool(brokers.broker_tools(agent_user(user_id)), name).handler


# ── call_broker_tool ───────────────────────────────────────────────────────────

def test_call_broker_tool_refuses_non_read_tools_for_every_broker_kind(signed_in, mcp):
    user_id = signed_in["id"]
    brokers.save_connection(user_id, "fyers", session_id="s1", protocol="2025-03-26", status="connected")
    store_oauth(user_id)
    handler = handler_for(user_id, "call_broker_tool")
    for broker, name in [("fyers", "create_order"), ("fyers", "login"), ("fyers", "logout_user"), ("fyers", "exit_positions"),
                         ("upstox", "place_order"), ("upstox", "delete_watchlist"), ("upstox", "login"), ("upstox", "nonexistent")]:
        result = run(handler({"broker": broker, "tool": name}))
        assert tool_results.reported_error(result), (broker, name)
    assert mcp.calls == []


def test_oauth_read_tools_are_called_with_the_bearer_token_on_a_fresh_session(signed_in, mcp):
    store_oauth(signed_in["id"])
    handler = handler_for(signed_in["id"], "call_broker_tool")
    run(handler({"broker": "upstox", "tool": "get_positions", "arguments": {"x": 1}}))
    run(handler({"broker": "upstox", "tool": "portfolio_summary"}))
    assert [call[0] for call in mcp.calls] == ["get_positions", "portfolio_summary"]
    assert all(call[1] == "access-secret" for call in mcp.calls)
    assert [call[2] for call in mcp.calls] == ["mcp-1", "mcp-2"]
    schema = tool(brokers.broker_tools(agent_user(signed_in["id"])), "call_broker_tool").input_schema
    assert "enum" not in schema["properties"]["tool"]


def test_a_401_for_an_oauth_broker_means_needs_login(signed_in, mcp):
    store_oauth(signed_in["id"])
    mcp.errors["get_positions"] = mcp_client.McpAuthError("MCP tools/call failed: 401")
    result = json.loads(run(handler_for(signed_in["id"], "call_broker_tool")({"broker": "upstox", "tool": "get_positions"})))
    assert result["errorKind"] == "unavailable" and "reconnect Upstox" in result["next_step"]
    assert brokers.get_connection(signed_in["id"], "upstox")["status"] == "needs_login"


def test_kotak_calls_carry_our_session_never_the_models(signed_in, mcp):
    brokers.save_connection(signed_in["id"], "kotak", session_id="mcp-k", protocol="2025-03-26", status="connected",
                            extra={"loginSession": "kotak-secret-session"})
    handler = handler_for(signed_in["id"], "call_broker_tool")
    run(handler({"broker": "kotak", "tool": "get_positions", "arguments": {"sessionid": "attacker"}}))
    assert mcp.calls == [("get_positions", None, "mcp-k", {"sessionid": "kotak-secret-session"})]
    assert tool_results.reported_error(run(handler({"broker": "kotak", "tool": "get_logout"})))
    assert tool_results.reported_error(run(handler({"broker": "kotak", "tool": "validate_login"})))


# ── FYERS login link ───────────────────────────────────────────────────────────

FYERS = brokers.BROKERS["fyers"]


@pytest.mark.parametrize("text", [
    "Open https://api-t1.fyers.in/api/v3/generate-authcode?x=1", "https://fyers.in/login", "https://mcp.fyers.in/auth?s=1",
])
def test_fyers_link_accepts_its_own_domain_and_subdomains(text):
    assert brokers.login_url(text, FYERS)


@pytest.mark.parametrize("text", [
    "https://evilfyers.in/login", "https://fyers.in.evil.com/login", "http://api-t1.fyers.in/x",
    "https://user:pw@api-t1.fyers.in/x", "https://api-t1.fyers.in@evil.com/x", "https://notfyers.in/x",
])
def test_fyers_link_refuses_lookalikes_http_and_userinfo(text):
    assert brokers.login_url(text, FYERS) is None


# ── Kotak: UCC, link, session id, QR verification ──────────────────────────────

KOTAK_LINK = "https://neo.kotaksecurities.com/web-login?qr=abc"
KOTAK_TEXT = f"Open {KOTAK_LINK} and scan the QR code in the Neo app. {{\"sessionid\": \"sess-9f8e7d6c\"}}"


@pytest.fixture
def kotak(client, signed_in, mcp):
    mcp.responses["get_login"] = KOTAK_TEXT
    mcp.responses["validate_login"] = "Login validated successfully."
    return mcp


def connect_kotak(client, ucc="AB123"):
    return client.post("/api/brokers/kotak/connect", json={"ucc": ucc})


@pytest.mark.parametrize("body", [{}, {"ucc": ""}, {"ucc": "ABCD"}, {"ucc": "ABCDEF"}, {"ucc": "AB 12"}, {"ucc": 12345}, {"ucc": "AB-12"}])
def test_kotak_needs_a_five_character_ucc(client, kotak, body):
    assert client.post("/api/brokers/kotak/connect", json=body).status_code == 422
    assert kotak.calls == [] and brokers.connections(client.get("/api/session").json()["user"]["id"]) == []


def test_kotak_connect_returns_the_link_and_keeps_the_session_secret(client, kotak, signed_in):
    response = connect_kotak(client)
    body = response.json()
    assert response.status_code == 200 and body["loginUrl"] == KOTAK_LINK and body["status"] == "pending" and body["needsScan"]
    assert "sess-9f8e7d6c" not in response.text and "AB123" not in response.text
    assert kotak.calls == [("get_login", None, "mcp-1", {"ucc": "AB123"})]
    with connect_db() as connection:
        raw = connection.execute("SELECT session FROM broker_connections").fetchone()["session"]
    assert raw.startswith("enc:v1:") and "sess-9f8e7d6c" not in raw and "AB123" not in raw
    assert brokers.get_connection(signed_in["id"], "kotak")["loginSession"] == "sess-9f8e7d6c"
    assert "sess-9f8e7d6c" not in client.get("/api/brokers").text


@pytest.mark.parametrize("text, expected", [
    ('{"sessionid": "abc12345"}', "abc12345"), ('{"sessionId": "abc12345"}', "abc12345"),
    ('{"data": {"session_id": "abc12345"}}', "abc12345"), ("Login link ready.\nsessionid: abc12345\nThanks", "abc12345"),
    ("session_id = \"abc-12345\"", "abc-12345"), ("no id here", None), ("", None), ('{"sessionid": ""}', None),
])
def test_session_id_parsing(text, expected):
    assert brokers.login_session_id(text) == expected


@pytest.mark.parametrize("text", [
    "https://evil.example/web-login sessionid: abc12345",  # link not Kotak's
    f"{KOTAK_LINK} but no session id",  # no session id
    "http://neo.kotaksecurities.com/x sessionid: abc12345",
    "https://neo.kotaksecurities.com.evil.com/x sessionid: abc12345",
])
def test_a_kotak_login_without_a_trusted_link_and_session_stores_nothing(client, kotak, signed_in, text):
    kotak.responses["get_login"] = text
    response = connect_kotak(client)
    assert response.status_code == 502 and "abc12345" not in response.text
    assert brokers.connections(signed_in["id"]) == []


def test_kotak_verify_connects_on_success_and_stays_pending_on_failure(client, kotak, signed_in):
    connect_kotak(client)
    assert client.get("/api/brokers/kotak/status").json()["status"] == "pending"
    kotak.responses["validate_login"] = "Login invalid: QR code not scanned."
    failed = client.post("/api/brokers/kotak/verify")
    assert failed.status_code == 409 and failed.json()["status"] == "pending" and "sess-9f8e7d6c" not in failed.text
    kotak.errors["validate_login"] = mcp_client.McpToolError('MCP tool "validate_login" failed: sessionid sess-9f8e7d6c rejected')
    failed = client.post("/api/brokers/kotak/verify")
    assert failed.status_code == 409 and "sess-9f8e7d6c" not in failed.text
    assert brokers.get_connection(signed_in["id"], "kotak")["status"] == "pending"
    del kotak.errors["validate_login"]
    kotak.responses["validate_login"] = "Login validated successfully."
    ok = client.post("/api/brokers/kotak/verify")
    assert ok.status_code == 200 and ok.json()["status"] == "connected" and "sess-9f8e7d6c" not in ok.text
    assert ("validate_login", None, "mcp-1", {"sessionid": "sess-9f8e7d6c"}) in kotak.calls
    kotak.responses["get_limits"] = "{}"
    assert client.get("/api/brokers/kotak/status").json()["status"] == "connected"
    assert kotak.calls[-1] == ("get_limits", None, "mcp-1", {"sessionid": "sess-9f8e7d6c"})


def test_kotak_verify_without_a_started_login_is_refused(client, kotak):
    assert client.post("/api/brokers/kotak/verify").status_code == 409
    assert client.post("/api/brokers/zerodha/verify").status_code == 409


def test_kotak_connect_and_verify_need_consent_and_unlock(client):
    assert client.post("/api/brokers/kotak/connect", json={"ucc": "AB123"}).status_code == 401
    assert client.post("/api/brokers/kotak/verify").status_code == 401


def test_kotak_holdings_use_the_generic_layout(client, kotak, signed_in):
    connect_kotak(client)
    client.post("/api/brokers/kotak/verify")
    kotak.responses["get_holdings"] = json.dumps({"data": [
        {"symbol": "ACME", "isin": "INE000A01010", "quantity": "10", "average_price": "100", "ltp": "120"}]})
    body = client.get("/api/brokers/kotak/holdings?refresh=1").json()
    assert body["available"] and body["status"] == "connected"
    assert body["holdings"][0]["broker"] == "Kotak Neo" and float(body["holdings"][0]["current_value_inr"]) == 1200.0


# ── OAuth brokers ──────────────────────────────────────────────────────────────

@pytest.fixture
def oauth(monkeypatch):
    seen = {"exchange": [], "refresh": []}

    async def discover(mcp_url, client):
        return {"authorization_endpoint": "https://auth.example/authorize", "token_endpoint": "https://auth.example/token",
                "registration_endpoint": "https://auth.example/register"}

    async def register(metadata, redirect_uri, client):
        seen["redirect_uri"] = redirect_uri
        return {"clientId": "cid", "clientSecret": "csecret"}

    async def exchange_code(token_endpoint, client_info, code, verifier, redirect_uri, resource, client):
        seen["exchange"].append((code, verifier, redirect_uri, resource))
        return {"access_token": "access-secret", "refresh_token": "refresh-secret", "expires_in": 3600}

    async def refresh(token_endpoint, client_info, refresh_token, resource, client):
        seen["refresh"].append(refresh_token)
        if seen.get("refresh_fails"):
            raise broker_oauth.OAuthError("refused")
        return {"access_token": "access-2", "expires_in": 600}

    monkeypatch.setattr(broker_oauth, "discover", discover)
    monkeypatch.setattr(broker_oauth, "register", register)
    monkeypatch.setattr(broker_oauth, "exchange_code", exchange_code)
    monkeypatch.setattr(broker_oauth, "refresh", refresh)
    return seen


def start_oauth(client, broker="upstox"):
    response = client.get(f"/auth/brokers/{broker}/connect", follow_redirects=False)
    assert response.status_code == 302
    return response


def test_oauth_connect_redirects_to_the_broker_with_pkce_and_state(client, signed_in, oauth):
    response = start_oauth(client)
    target = urlsplit(response.headers["location"])
    query = parse_qs(target.query)
    assert f"{target.scheme}://{target.netloc}{target.path}" == "https://auth.example/authorize"
    assert query["code_challenge_method"] == ["S256"] and query["client_id"] == ["cid"] and query["state"][0]
    assert query["redirect_uri"][0].endswith("/auth/brokers/upstox/callback")
    assert query["resource"] == [brokers.BROKERS["upstox"].mcp_url] and "csecret" not in response.headers["location"]
    assert oauth["redirect_uri"].endswith("/auth/brokers/upstox/callback")


def finish_oauth(client, broker="upstox", **override):
    query = parse_qs(urlsplit(start_oauth(client, broker).headers["location"]).query)
    params = {"code": "the-code", "state": query["state"][0], **override}
    return client.get(f"/auth/brokers/{broker}/callback", params={k: v for k, v in params.items() if v is not None}, follow_redirects=False)


def test_oauth_callback_stores_encrypted_tokens_and_redirects_to_holdings(client, signed_in, oauth):
    response = finish_oauth(client)
    assert response.status_code == 302 and response.headers["location"].endswith("/#/holdings/upstox")
    code, verifier, redirect_uri, resource = oauth["exchange"][0]
    assert code == "the-code" and verifier and redirect_uri.endswith("/auth/brokers/upstox/callback")
    assert resource == brokers.BROKERS["upstox"].mcp_url
    with connect_db() as connection:
        raw = connection.execute("SELECT session FROM broker_connections").fetchone()["session"]
    assert raw.startswith("enc:v1:") and "access-secret" not in raw and "refresh-secret" not in raw and "csecret" not in raw
    found = brokers.get_connection(signed_in["id"], "upstox")
    assert found["status"] == "connected" and found["oauth"]["accessToken"] == "access-secret"
    listed = {item["broker"]: item for item in client.get("/api/brokers").json()["brokers"]}
    assert listed["upstox"]["status"] == "connected" and listed["upstox"]["auth"] == "oauth"
    assert "access-secret" not in client.get("/api/brokers").text


def test_oauth_callback_refuses_a_wrong_state_and_replays(client, signed_in, oauth):
    assert finish_oauth(client, state="wrong").headers["location"].endswith("/?brokerError=upstox")
    assert oauth["exchange"] == [] and brokers.connections(signed_in["id"]) == []
    good = finish_oauth(client)
    assert good.headers["location"].endswith("#/holdings/upstox")
    replay = client.get("/auth/brokers/upstox/callback", params={"code": "c", "state": "anything"}, follow_redirects=False)
    assert replay.headers["location"].endswith("/?brokerError=upstox")


@pytest.mark.parametrize("override", [{"code": None}, {"state": None}, {"error": "access_denied"}])
def test_oauth_callback_failures_redirect_with_an_error(client, signed_in, oauth, override):
    response = finish_oauth(client, **override)
    assert response.headers["location"].endswith("/?brokerError=upstox")
    assert brokers.connections(signed_in["id"]) == []


def test_oauth_callback_with_a_failed_exchange_stores_nothing(client, signed_in, oauth, monkeypatch):
    async def fail(*args, **kwargs):
        raise broker_oauth.OAuthError("refused")

    monkeypatch.setattr(broker_oauth, "exchange_code", fail)
    assert finish_oauth(client).headers["location"].endswith("/?brokerError=upstox")
    assert brokers.connections(signed_in["id"]) == []


def test_oauth_connect_gates(client, oauth):
    assert client.get("/auth/brokers/upstox/connect", follow_redirects=False).headers["location"].endswith("/")
    assert client.get("/auth/brokers/zerodha/connect", follow_redirects=False).status_code == 404
    assert client.get("/auth/brokers/nope/connect", follow_redirects=False).status_code == 404
    assert client.get("/auth/brokers/zerodha/callback", follow_redirects=False).status_code == 404
    from tests.conftest import sign_up
    sign_up(client, storage_mode=None)  # no consent yet
    response = client.get("/auth/brokers/upstox/connect", follow_redirects=False)
    assert "auth.example" not in response.headers["location"]
    assert oauth["exchange"] == []


def test_oauth_discovery_failure_redirects_with_an_error(client, signed_in, monkeypatch):
    async def fail(mcp_url, http):
        raise broker_oauth.OAuthError("no")

    monkeypatch.setattr(broker_oauth, "discover", fail)
    assert client.get("/auth/brokers/upstox/connect", follow_redirects=False).headers["location"].endswith("/?brokerError=upstox")


def test_connect_post_for_an_oauth_broker_points_at_the_browser_flow(client, signed_in):
    body = client.post("/api/brokers/dhan/connect").json()
    assert body["authorizeUrl"] == "/auth/brokers/dhan/connect" and body["auth"] == "oauth"


def test_oauth_status_uses_the_token_and_refreshes_when_expired(client, signed_in, oauth, mcp):
    store_oauth(signed_in["id"], expires_in=-10)
    assert client.get("/api/brokers/upstox/status").json()["status"] == "connected"
    assert oauth["refresh"] == ["refresh-secret"]
    found = brokers.get_connection(signed_in["id"], "upstox")
    assert found["oauth"]["accessToken"] == "access-2" and found["oauth"]["refreshToken"] == "refresh-secret"
    assert mcp.calls == [] and mcp.sessions == []


def test_oauth_without_a_working_refresh_needs_login(client, signed_in, oauth, mcp):
    store_oauth(signed_in["id"], expires_in=-10, refresh=None)
    assert client.get("/api/brokers/upstox/status").json()["status"] == "needs_login"
    assert oauth["refresh"] == []
    store_oauth(signed_in["id"], broker_id="dhan", expires_in=-10)
    oauth["refresh_fails"] = True
    assert client.get("/api/brokers/dhan/status").json()["status"] == "needs_login"


def test_oauth_holdings_use_only_read_tools_named_for_holdings(client, signed_in, oauth, mcp):
    store_oauth(signed_in["id"])
    mcp.responses["get_holdings"] = json.dumps({"holdings": [{"trading_symbol": "ACME", "qty": 5, "avg_price": 10, "last_price": 12}]})
    mcp.responses["get_mf_holdings"] = json.dumps([{"scheme_name": "Fund X", "units": "2.5", "isin": "INF1", "current_value": "300"}])
    body = client.get("/api/brokers/upstox/holdings?refresh=1").json()
    assert body["available"] and len(body["holdings"]) == 2
    assert sorted(call[0] for call in mcp.calls) == ["get_holdings", "get_mf_holdings"]
    assert all(call[1] == "access-secret" for call in mcp.calls)
    types = {item["asset_type"]: item for item in body["holdings"]}
    assert set(types) == {"IND_STOCK", "MF"} and all(item["broker"] == "Upstox" for item in body["holdings"])


def test_oauth_holdings_with_a_401_need_login_and_keep_the_stored_copy_out(client, signed_in, oauth, mcp):
    store_oauth(signed_in["id"])
    mcp.errors["initialize"] = mcp_client.McpAuthError("MCP initialize failed: 401")
    body = client.get("/api/brokers/upstox/holdings?refresh=1").json()
    assert body["available"] is False and body["status"] == "needs_login"
    assert brokers.get_connection(signed_in["id"], "upstox")["status"] == "needs_login"


def test_oauth_without_a_holdings_tool_says_so(client, signed_in, oauth, mcp):
    store_oauth(signed_in["id"])
    mcp.tools = [{"name": "get_positions"}, {"name": "place_holding_order"}]
    body = client.get("/api/brokers/upstox/holdings?refresh=1").json()
    assert body["available"] is False and "no holdings tool" in body["problem"]


# ── Tool list route ────────────────────────────────────────────────────────────

def test_oauth_tool_list_needs_the_connection(client, signed_in, mcp):
    body = client.get("/api/brokers/upstox/tools").json()
    assert body == {"broker": "upstox", "name": "Upstox", "tools": [], "needsConnection": True}
    assert mcp.sessions == []


def test_oauth_tool_list_with_a_connection_marks_access_and_hides_secrets(client, signed_in, mcp):
    store_oauth(signed_in["id"])
    response = client.get("/api/brokers/upstox/tools")
    body = response.json()
    access = {item["name"]: item["access"] for item in body["tools"]}
    assert access == {"get_holdings": "read", "get_mf_holdings": "read", "get_positions": "read", "portfolio_summary": "read",
                      "login": "not_enabled", "place_order": "write", "delete_watchlist": "write"}
    assert "access-secret" not in response.text and "refresh-secret" not in response.text and "mcp-1" not in response.text
    assert mcp.sessions == [(brokers.BROKERS["upstox"].mcp_url, "access-secret")]
    client.get("/api/brokers/upstox/tools")
    assert len(mcp.sessions) == 1  # cached


def test_oauth_tool_cache_is_per_profile_and_cleared_on_disconnect(client, signed_in, mcp):
    store_oauth(signed_in["id"])
    client.get("/api/brokers/upstox/tools")
    assert (signed_in["id"], "upstox") in brokers._tools_cache
    assert client.post("/api/brokers/upstox/disconnect").json() == {"connected": False}
    assert brokers._tools_cache == {}
    assert client.get("/api/brokers/upstox/tools").json()["needsConnection"] is True


def test_login_tool_brokers_list_tools_without_a_connection(client, signed_in, mcp):
    body = client.get("/api/brokers/fyers/tools").json()
    assert body["broker"] == "fyers" and {item["name"] for item in body["tools"]} == {item["name"] for item in LIVE_TOOLS}
    assert mcp.sessions == [(brokers.BROKERS["fyers"].mcp_url, None)]


def test_every_registry_broker_is_listed_with_its_auth(client, signed_in):
    listed = client.get("/api/brokers").json()["brokers"]
    assert {item["broker"]: item["auth"] for item in listed} == {
        "zerodha": "mcp_login_tool", "fyers": "mcp_login_tool", "kotak": "mcp_login_qr",
        "upstox": "oauth", "dhan": "oauth", "groww": "oauth"}


def test_fyers_connects_through_the_login_tool_flow(client, signed_in, mcp):
    mcp.responses["login"] = "Login here: https://api-t1.fyers.in/api/v3/generate-authcode?client_id=x"
    body = client.post("/api/brokers/fyers/connect").json()
    assert body["loginUrl"].startswith("https://api-t1.fyers.in/") and body["status"] == "pending"
    mcp.responses["login"] = "Login here: https://evilfyers.in/x"
    assert client.post("/api/brokers/fyers/connect").status_code == 502


# ── Generic normaliser ─────────────────────────────────────────────────────────

NORMALISE = brokers.normalise_generic("Acme Broker")


def test_generic_normaliser_reads_common_layouts_in_decimal():
    rows = NORMALISE("get_holdings", [
        {"tradingsymbol": "ACME", "isin": "INE1", "quantity": "10", "average_price": "100.5", "last_price": "120"},
        {"symbol": "BETA", "qty": 3, "avg_price": 50, "ltp": 60.5, "market_value": "181.5"},
        {"name": "GAMMA", "holding_qty": "2", "costPrice": "10", "lastTradedPrice": "11", "security_id": "99"},
        {"scheme_name": "ignored name", "symbol": "DELTA", "units": "1.5", "average_cost": "200", "close_price": "210"},
    ])
    assert [row["investment"] for row in rows] == ["ACME", "BETA", "GAMMA", "DELTA"]
    assert rows[0]["total_units"] == "10" and rows[0]["unit_price"] == "120" and rows[0]["market_value"] == "1200"
    assert rows[0]["invested_amount"] == "1005.0" and rows[0]["investment_code"] == "INE1"
    assert rows[1]["market_value"] == "181.5" and rows[1]["investment_code"] == "BETA"
    assert rows[2]["market_value"] == "22" and rows[2]["investment_code"] == "99"
    assert all(row["broker"] == "Acme Broker" and row["asset_type"] == "IND_STOCK" for row in rows)


def test_generic_normaliser_marks_mutual_fund_tools_and_keeps_missing_prices_missing():
    rows = NORMALISE("get_mutual_fund_holdings", [{"fund": "Fund X", "units": "4", "isin": "INF1"}])
    assert rows[0]["asset_type"] == "MF" and rows[0]["unit_price"] is None and rows[0]["market_value"] is None
    assert NORMALISE("get_mf_holdings", [{"symbol": "F", "quantity": 1, "ltp": 5}])[0]["asset_type"] == "MF"


def test_generic_normaliser_drops_rows_without_a_quantity_and_empty_positions():
    rows = NORMALISE("get_holdings", [{"symbol": "A", "quantity": 0, "ltp": 5}, {"symbol": "B", "ltp": 5},
                                      "junk", {"symbol": "C", "quantity": "abc"}, {"symbol": "D", "quantity": 2, "ltp": 5}])
    assert [row["investment"] for row in rows] == ["D"]
    assert NORMALISE("get_holdings", []) == []
    assert NORMALISE("get_holdings", [{"symbol": "A", "quantity": 0}]) == []  # recognised, just nothing held


def test_generic_normaliser_refuses_a_layout_it_does_not_recognise():
    with pytest.raises(brokers.BrokerDataError, match="not recognised"):
        NORMALISE("get_holdings", [{"scrip": "A", "shares": 5, "price": 10}])


@pytest.mark.parametrize("text, count", [
    ('[{"quantity": 1}]', 1), ('{"data": [{"quantity": 1}]}', 1), ('{"holdings": [{"quantity": 1}]}', 1),
    ('{"result": [{"quantity": 1}]}', 1), ('{"data": {"holdings": [{"quantity": 1}, {"quantity": 2}]}}', 2),
])
def test_holdings_text_layouts(text, count):
    assert len(brokers.parse_items(text)) == count


@pytest.mark.parametrize("text", ["not json", '{"data": 5}', '{"other": []}', "7"])
def test_unreadable_holdings_text_is_refused(text):
    with pytest.raises(brokers.BrokerDataError):
        brokers.parse_items(text)


def test_an_unrecognised_layout_is_said_plainly_not_shown_as_zero(client, signed_in, mcp):
    brokers.save_connection(signed_in["id"], "fyers", session_id="s1", protocol="2025-03-26", status="connected")
    mcp.responses["get_holdings"] = json.dumps([{"scrip": "A", "shares": 5}])
    mcp.responses["get_mutual_fund_holdings"] = "[]"
    body = client.get("/api/brokers/fyers/holdings?refresh=1").json()
    assert body["available"] is False and "not recognised" in body["problem"]
    combined = client.get("/api/holdings/combined").json()
    fyers = next(source for source in combined["sources"] if source["id"] == "fyers")
    assert fyers["available"] is False and "not recognised" in fyers["problem"]


# ── Combined view and agent wiring with a new broker ───────────────────────────

def test_a_new_brokers_rows_replace_indmoneys_rows_for_that_broker(client, signed_in, mcp):
    from datetime import datetime, timezone
    from app import snapshots
    from tests.fixtures import indmoney_sample
    snapshots.store_snapshot(signed_in["id"], datetime.now(timezone.utc), indmoney_sample.results(), {})
    before = client.get("/api/holdings/combined").json()
    labels = {str(item["broker"]).lower() for item in before["sources"][0]["holdings"]}
    assert not any("fyers" in label for label in labels)
    brokers.save_connection(signed_in["id"], "fyers", session_id="s1", protocol="2025-03-26", status="connected")
    mcp.responses["get_holdings"] = json.dumps([{"symbol": "ACME", "isin": "INE1", "quantity": 4, "average_price": 10, "ltp": 12}])
    mcp.responses["get_mutual_fund_holdings"] = "[]"
    assert client.get("/api/brokers/fyers/holdings?refresh=1").json()["available"]
    body = client.get("/api/holdings/combined").json()
    assert [source["id"] for source in body["sources"]] == ["indmoney", "fyers"]
    fyers = body["sources"][1]
    assert fyers["kind"] == "broker" and fyers["holdings"][0]["broker"] == "FYERS"
    assert any(item["broker"] == "FYERS" for item in body["combined"]["holdings"])


def test_agent_tools_name_every_connected_broker(signed_in, mcp):
    brokers.save_connection(signed_in["id"], "fyers", session_id="s1", protocol="2025-03-26", status="connected")
    store_oauth(signed_in["id"], "groww")
    tools = brokers.broker_tools(agent_user(signed_in["id"]))
    assert tool(tools, "get_broker_holdings").input_schema["properties"]["broker"]["enum"] == ["fyers", "groww", "all"]
    assert "FYERS, Groww" in tool(tools, "call_broker_tool").description or "Groww" in tool(tools, "call_broker_tool").description
    assert "never add the two" in brokers.context_line(signed_in["id"])


def test_export_never_holds_oauth_tokens(client, signed_in, mcp):
    store_oauth(signed_in["id"])
    exported = client.post("/api/users/me/export", json={"pin": "2468"}).text
    assert "access-secret" not in exported and "refresh-secret" not in exported and "csecret" not in exported
    assert '"upstox"' in exported


def test_the_notice_explains_stored_tokens_and_the_version_is_bumped():
    from app import privacy
    assert privacy.POLICY_VERSION == "2026-11-06.2"
    body = next(item for item in privacy.NOTICE if item["title"] == "Broker connections")["body"]
    for word in ("refresh token", "encrypted", "never exported", "read-only", "Upstox", "Kotak"):
        assert word in body


# ── broker_oauth against a fake authorization server ───────────────────────────

def mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_discovery_follows_the_401_challenge_then_registers_with_pkce():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url == "https://mcp.example/mcp":
            return httpx.Response(401, headers={"www-authenticate": 'Bearer resource_metadata="https://mcp.example/.well-known/oauth-protected-resource/mcp"'})
        if url.endswith("/oauth-protected-resource/mcp"):
            return httpx.Response(200, json={"authorization_servers": ["https://auth.example/"]})
        if url == "https://auth.example/.well-known/oauth-authorization-server":
            return httpx.Response(200, json={"authorization_endpoint": "https://auth.example/a", "token_endpoint": "https://auth.example/t",
                                             "registration_endpoint": "https://auth.example/r", "token_endpoint_auth_methods_supported": ["none"]})
        if url == "https://auth.example/r":
            body = json.loads(request.content)
            assert body["redirect_uris"] == ["https://app.example/cb"] and body["token_endpoint_auth_method"] == "none"
            return httpx.Response(201, json={"client_id": "abc"})
        return httpx.Response(404)

    async def go():
        async with mock_client(handler) as http:
            metadata = await broker_oauth.discover("https://mcp.example/mcp", http)
            info = await broker_oauth.register(metadata, "https://app.example/cb", http)
            return metadata, info

    metadata, info = run(go())
    assert info == {"clientId": "abc", "clientSecret": None}
    verifier, challenge = broker_oauth.pkce()
    assert verifier and challenge and "=" not in challenge
    link = broker_oauth.authorize_url(metadata, "abc", "https://app.example/cb", "st", challenge, "https://mcp.example/mcp")
    assert parse_qs(urlsplit(link).query)["code_challenge_method"] == ["S256"]


def test_discovery_falls_back_to_the_well_known_document_and_refuses_http():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://mcp.example/.well-known/oauth-protected-resource":
            return httpx.Response(200, json={"authorization_servers": ["http://auth.example"]})
        return httpx.Response(401)

    async def go():
        async with mock_client(handler) as http:
            await broker_oauth.discover("https://mcp.example/mcp", http)

    with pytest.raises(broker_oauth.OAuthError):
        run(go())


def test_token_exchange_sends_pkce_and_refuses_errors():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        seen.append(form)
        if form["grant_type"] == ["refresh_token"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "r", "expires_in": 60})

    async def go():
        async with mock_client(handler) as http:
            token = await broker_oauth.exchange_code("https://auth.example/t", {"clientId": "c", "clientSecret": "s"}, "code", "ver",
                                                     "https://app.example/cb", "https://mcp.example/mcp", http)
            with pytest.raises(broker_oauth.OAuthError):
                await broker_oauth.refresh("https://auth.example/t", {"clientId": "c"}, "r", "https://mcp.example/mcp", http)
            return token

    assert run(go())["access_token"] == "a"
    assert seen[0]["code_verifier"] == ["ver"] and seen[0]["client_secret"] == ["s"] and "client_secret" not in seen[1]

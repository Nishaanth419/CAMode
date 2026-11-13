from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app import brokers, portfolio_math, users
from app.db import connect_db
from tests.fixtures import kite_sample as kite

ZERODHA = brokers.BROKERS["zerodha"]


def test_registry_is_complete_and_never_allows_writes():
    for broker in brokers.BROKERS.values():
        assert broker.mcp_url.startswith("https://")
        assert not (brokers.WRITE_TOOLS & set(broker.read_tools))
        assert "login" not in broker.read_tools
        assert set(broker.holdings_tools) <= set(broker.read_tools)
    assert ZERODHA.login_hosts == ("mcp.kite.trade", "kite.zerodha.com")


@pytest.mark.parametrize("text", [
    kite.LOGIN_TEXT,
    "Open https://kite.zerodha.com/connect/login?v=3&api_key=x to continue",
    "<https://mcp.kite.trade/authorize?session_id=a>",
])
def test_login_url_accepts_the_brokers_own_hosts(text):
    assert brokers.login_url(text, ZERODHA).startswith("https://")


@pytest.mark.parametrize("text", [
    "go to http://mcp.kite.trade/authorize?x=1",
    "go to https://evil.example/authorize",
    "go to https://mcp.kite.trade.evil.com/authorize",
    "go to https://mcp.kite.trade@evil.com/authorize",
    "go to https://user:pw@mcp.kite.trade/authorize",
    "no link at all",
    "",
])
def test_login_url_refuses_everything_else(text):
    assert brokers.login_url(text, ZERODHA) is None


def test_connection_session_is_stored_encrypted(signed_in):
    user_id = signed_in["id"]
    brokers.save_connection(user_id, "zerodha", session_id="kitemcp-secret-123", protocol="2025-03-26", status="pending")
    with connect_db() as connection:
        raw = connection.execute("SELECT session FROM broker_connections").fetchone()["session"]
    assert raw.startswith("enc:v1:") and "kitemcp-secret-123" not in raw
    found = brokers.get_connection(user_id, "zerodha")
    assert found["sessionId"] == "kitemcp-secret-123" and found["status"] == "pending"
    assert brokers.has_connection(user_id)
    assert brokers.delete_connection(user_id, "zerodha") and not brokers.has_connection(user_id)


def test_normaliser_uses_decimals_t1_and_skips_empty_rows():
    rows = ZERODHA.normalise("get_holdings", kite.HOLDINGS)
    assert [row["investment"] for row in rows] == ["ACMEBANK", "SAMPLEIT"]
    first = rows[0]
    assert first["total_units"] == "12" and first["broker"] == "Zerodha" and first["asset_type"] == "IND_STOCK"
    assert Decimal(first["market_value"]) == Decimal("12") * Decimal("120.25")
    assert Decimal(first["invested_amount"]) == Decimal("12") * Decimal("100.5")
    assert first["investment_code"] == "INE000A01011"
    funds = ZERODHA.normalise("get_mf_holdings", kite.MF_HOLDINGS)
    assert funds[0]["asset_type"] == "MF" and funds[0]["investment"].startswith("Sample Flexi Cap")


def test_a_missing_price_stays_missing_not_zero():
    rows = ZERODHA.normalise("get_holdings", [{"tradingsymbol": "X", "quantity": 3, "average_price": "n/a"}])
    assert rows[0]["market_value"] is None and rows[0]["unit_price"] is None
    assert rows[0]["invested_amount"] == "0"


def test_metrics_over_normalised_rows_group_by_zerodha():
    rows = ZERODHA.normalise("get_holdings", kite.HOLDINGS) + ZERODHA.normalise("get_mf_holdings", kite.MF_HOLDINGS)
    by_type: dict = {}
    for row in rows:
        by_type.setdefault(row["asset_type"], []).append(row)
    results = {portfolio_math.HOLDINGS_PREFIX + t: json.dumps({"holdings": r}) for t, r in by_type.items()}
    figures = portfolio_math.compute_metrics(results)
    assert [row["broker"] for row in figures["by_broker"]] == ["Zerodha"]
    assert figures["concentration"]["holdings_count"] == 3


# ── Fake Kite MCP server ───────────────────────────────────────────────────────

class FakeKite:
    """Stands in for the hosted Kite MCP server: issues a session id, needs a login, serves holdings."""

    def __init__(self):
        self.logged_in = False
        self.calls: list[tuple[str, str | None, dict]] = []
        self.sessions = 0
        self.login_text = kite.LOGIN_TEXT
        self.error: Exception | None = None
        self.holdings_text = kite.text(kite.HOLDINGS)
        self.mf_text = kite.text(kite.MF_HOLDINGS)
        self.gone = False

    async def fetch_mcp_session(self, url, token, *, service="INDmoney"):
        assert token is None
        self.sessions += 1
        return {"tools": [], "sessionId": f"kitemcp-{self.sessions}", "protocolVersion": "2025-03-26"}

    async def call_mcp_tool(self, url, token, protocol, session_id, name, arguments, *, client=None):
        from app import mcp_client
        assert token is None
        self.calls.append((name, session_id, arguments))
        if self.error:
            raise self.error
        if self.gone:
            raise mcp_client.McpHttpError("gone", 404)
        if name == "login":
            return self.login_text
        if not self.logged_in:
            raise mcp_client.McpToolError(f'MCP tool "{name}" failed: {kite.LOGIN_FIRST}')
        if name == "get_profile":
            return kite.text(kite.PROFILE)
        if name == "get_holdings":
            return self.holdings_text
        if name == "get_mf_holdings":
            return self.mf_text
        return kite.text([])

    @property
    def called(self):
        return [name for name, _, _ in self.calls]


@pytest.fixture
def fake_kite(monkeypatch):
    fake = FakeKite()
    monkeypatch.setattr(brokers.mcp_client, "fetch_mcp_session", fake.fetch_mcp_session)
    monkeypatch.setattr(brokers.mcp_client, "call_mcp_tool", fake.call_mcp_tool)
    return fake


def connect(client):
    return client.post("/api/brokers/zerodha/connect")


def test_broker_routes_need_a_profile_and_consent(client):
    assert client.get("/api/brokers").status_code == 401
    assert connect(client).status_code == 401
    assert client.get("/api/brokers/zerodha/status").status_code == 401
    from tests.conftest import sign_up
    sign_up(client, storage_mode=None)
    assert connect(client).status_code == 403
    assert client.get("/api/brokers/zerodha/status").status_code == 403


def test_connect_returns_the_validated_link_and_stores_pending(client, signed_in, fake_kite):
    response = connect(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "pending" and body["loginUrl"].startswith("https://mcp.kite.trade/authorize")
    assert body["expiresInSeconds"] == 300
    assert brokers.get_connection(signed_in["id"], "zerodha")["status"] == "pending"
    assert client.get("/api/brokers").json()["brokers"][1]["status"] == "pending"


@pytest.mark.parametrize("text", ["http://mcp.kite.trade/authorize", "https://evil.example/x", "nothing to see"])
def test_a_bad_login_link_gives_502_and_stores_nothing(client, signed_in, fake_kite, text):
    fake_kite.login_text = text
    assert connect(client).status_code == 502
    assert not brokers.has_connection(signed_in["id"])


def test_an_already_logged_in_session_is_connected(client, signed_in, fake_kite):
    fake_kite.login_text = kite.LOGGED_IN_TEXT
    assert connect(client).json()["status"] == "connected"


def test_status_moves_from_pending_to_connected_to_needs_login(client, signed_in, fake_kite, monkeypatch):
    connect(client)
    assert client.get("/api/brokers/zerodha/status").json()["status"] == "pending"
    fake_kite.logged_in = True
    assert client.get("/api/brokers/zerodha/status").json()["status"] == "connected"
    fake_kite.logged_in = False
    assert client.get("/api/brokers/zerodha/status").json()["status"] == "needs_login"


def test_status_after_the_login_window_without_login_needs_login(client, signed_in, fake_kite, monkeypatch):
    connect(client)
    real = users.now_ms()
    monkeypatch.setattr(users, "now_ms", lambda: real + brokers.LOGIN_WAIT_MS + 1000)
    assert client.get("/api/brokers/zerodha/status").json()["status"] == "needs_login"


def test_a_session_older_than_12_hours_needs_login_without_a_call(client, signed_in, fake_kite, monkeypatch):
    fake_kite.logged_in = True
    connect(client)
    fake_kite.calls.clear()
    real = users.now_ms()
    monkeypatch.setattr(users, "now_ms", lambda: real + brokers.SESSION_MAX_MS + 1000)
    assert client.get("/api/brokers/zerodha/status").json()["status"] == "needs_login"
    assert fake_kite.calls == []


def test_a_gone_session_needs_login(client, signed_in, fake_kite):
    connect(client)
    fake_kite.gone = True
    assert client.get("/api/brokers/zerodha/status").json()["status"] == "needs_login"


@pytest.mark.parametrize("error", [
    "timeout", "server",
])
def test_an_unreachable_broker_is_unavailable_and_changes_nothing(client, signed_in, fake_kite, error):
    import httpx
    from app import mcp_client
    connect(client)
    fake_kite.error = httpx.ReadTimeout("slow") if error == "timeout" else mcp_client.McpHttpError("boom", 503)
    response = client.get("/api/brokers/zerodha/status")
    assert response.status_code == 200 and response.json()["status"] == "unavailable"
    assert brokers.get_connection(signed_in["id"], "zerodha")["status"] == "pending"


def test_disconnect_deletes_the_connection(client, signed_in, fake_kite):
    connect(client)
    assert client.post("/api/brokers/zerodha/disconnect").json() == {"connected": False}
    assert not brokers.has_connection(signed_in["id"])
    assert client.get("/api/brokers/zerodha/status").json()["status"] == "disconnected"


def test_an_unknown_broker_is_404(client, signed_in):
    assert client.post("/api/brokers/nope/connect").status_code == 404
    assert client.get("/api/brokers/nope/status").status_code == 404
    assert client.post("/api/brokers/nope/disconnect").status_code == 404


def test_another_profile_never_sees_a_connection(client, signed_in, fake_kite):
    from tests.conftest import sign_up
    connect(client)
    client.post("/api/session/logout")
    sign_up(client, name="Ben", pin="1357")
    assert client.get("/api/brokers").json()["brokers"][1]["status"] == "disconnected"
    assert client.get("/api/brokers/zerodha/status").json()["status"] == "disconnected"


def test_broker_rate_limit_is_per_profile(client, signed_in, fake_kite, monkeypatch):
    from app import limits
    monkeypatch.setenv("RATE_LIMITS_ENABLED", "true")
    monkeypatch.setitem(limits.RATE_RULES, "broker", limits.GroupRules(limits.Rule(100, 60), limits.Rule(2, 60)))
    assert connect(client).status_code == 200
    assert client.get("/api/brokers/zerodha/status").status_code == 200
    blocked = client.get("/api/brokers/zerodha/status")
    assert blocked.status_code == 429 and blocked.json()["rateLimited"] is True


# ── Holdings and the combined view ─────────────────────────────────────────────

from datetime import datetime, timezone  # noqa: E402

from app import snapshots  # noqa: E402
from tests.fixtures import indmoney_sample  # noqa: E402


def logged_in(client, fake_kite):
    connect(client)
    fake_kite.logged_in = True
    assert client.get("/api/brokers/zerodha/status").json()["status"] == "connected"


def store_indmoney_snapshot(user_id):
    snapshots.store_snapshot(user_id, datetime.now(timezone.utc), indmoney_sample.results(), {})


def value_of(holdings):
    return sum(Decimal(str(item["current_value_inr"])) for item in holdings)


def test_a_live_fetch_returns_figures_and_stores_sealed_holdings(client, signed_in, fake_kite):
    logged_in(client, fake_kite)
    body = client.get("/api/brokers/zerodha/holdings?refresh=1").json()
    assert body["available"] and body["stale"] is False and body["status"] == "connected"
    assert [row["broker"] for row in body["figures"]["by_broker"]] == ["Zerodha"]
    assert len(body["holdings"]) == 3 and "_rows" not in body
    with connect_db() as connection:
        raw = connection.execute("SELECT holdings FROM broker_connections").fetchone()["holdings"]
    assert raw.startswith("sealed:v1:") and "ACMEBANK" not in raw
    assert set(fake_kite.called) >= {"get_holdings", "get_mf_holdings"}


def test_paged_payloads_are_read_too(client, signed_in, fake_kite):
    fake_kite.holdings_text = kite.paged(kite.HOLDINGS, limit=10)
    logged_in(client, fake_kite)
    assert len(client.get("/api/brokers/zerodha/holdings?refresh=1").json()["holdings"]) == 3


def test_an_unreadable_payload_falls_back_to_nothing_not_an_error(client, signed_in, fake_kite):
    fake_kite.holdings_text = "this is not json"
    logged_in(client, fake_kite)
    response = client.get("/api/brokers/zerodha/holdings?refresh=1")
    assert response.status_code == 200 and response.json()["available"] is False


def test_an_expired_session_returns_the_stored_copy_as_stale(client, signed_in, fake_kite):
    logged_in(client, fake_kite)
    first = client.get("/api/brokers/zerodha/holdings?refresh=1").json()
    fake_kite.logged_in = False
    body = client.get("/api/brokers/zerodha/holdings?refresh=1").json()
    assert body["available"] and body["stale"] is True and body["status"] == "needs_login"
    assert body["asOf"] == first["asOf"] and len(body["holdings"]) == 3
    assert brokers.get_connection(signed_in["id"], "zerodha")["status"] == "needs_login"


def test_an_expired_session_with_nothing_stored_is_unavailable_not_500(client, signed_in, fake_kite):
    connect(client)
    body = client.get("/api/brokers/zerodha/holdings?refresh=1")
    assert body.status_code == 200 and body.json() == {
        "available": False, "broker": "zerodha", "name": "Zerodha", "status": "needs_login"}


def test_a_plain_load_shows_the_stored_copy_without_calling_the_broker(client, signed_in, fake_kite):
    logged_in(client, fake_kite)
    client.get("/api/brokers/zerodha/holdings?refresh=1")
    fake_kite.calls.clear()
    body = client.get("/api/brokers/zerodha/holdings").json()
    assert body["available"] and body["stale"] is True and fake_kite.calls == []


def test_private_mode_never_stores_holdings(client, fake_kite):
    from tests.conftest import sign_up
    user = sign_up(client, storage_mode="private")
    logged_in(client, fake_kite)
    body = client.get("/api/brokers/zerodha/holdings?refresh=1").json()
    assert body["available"] is True
    assert brokers.get_connection(user["id"], "zerodha")["holdingsSealed"] is None


def test_a_locked_session_gets_unlock_required(client, signed_in, fake_kite):
    from app import unlock
    logged_in(client, fake_kite)
    unlock._unlocked.clear()
    assert client.get("/api/brokers/zerodha/holdings").status_code == 403
    assert client.get("/api/brokers/zerodha/holdings").json()["unlockRequired"] is True
    assert client.get("/api/holdings/combined").status_code == 403


def test_combined_drops_indmoneys_zerodha_rows_and_reports_it(client, signed_in, fake_kite):
    store_indmoney_snapshot(signed_in["id"])
    logged_in(client, fake_kite)
    direct = client.get("/api/brokers/zerodha/holdings?refresh=1").json()
    body = client.get("/api/holdings/combined").json()
    assert [source["id"] for source in body["sources"]] == ["indmoney", "zerodha"]
    assert body["combined"]["deduplicated"] == {"Zerodha": 1}
    indmoney = next(source for source in body["sources"] if source["id"] == "indmoney")
    assert indmoney["movedTo"] == {"Zerodha": 1}
    assert len(indmoney["holdings"]) == 6 - 1
    assert all("zerodha" not in str(item["broker"]).lower() for item in indmoney["holdings"])
    expected = value_of(indmoney["holdings"]) + value_of(direct["holdings"])
    assert value_of(body["combined"]["holdings"]) == expected
    brokers_seen = {item["broker"] for item in body["combined"]["holdings"]}
    assert "Zerodha" in brokers_seen and len(body["combined"]["holdings"]) == 6 - 1 + 3
    assert all(item["name"] != "HDFC Bank" for item in body["combined"]["holdings"])


def test_indmoney_keeps_its_zerodha_rows_until_zerodha_has_holdings(client, signed_in, fake_kite):
    store_indmoney_snapshot(signed_in["id"])
    client.post("/api/brokers/zerodha/connect")
    body = client.get("/api/holdings/combined").json()
    indmoney = next(source for source in body["sources"] if source["id"] == "indmoney")
    assert indmoney["movedTo"] == {}
    assert len(indmoney["holdings"]) == 6


def test_combined_without_brokers_equals_indmoney_only(client, signed_in):
    store_indmoney_snapshot(signed_in["id"])
    body = client.get("/api/holdings/combined").json()
    assert [source["id"] for source in body["sources"]] == ["indmoney"]
    assert body["combined"]["deduplicated"] == {}
    assert body["combined"]["holdings"] == body["sources"][0]["holdings"]


def test_combined_with_nothing_is_unavailable_not_an_error(client, signed_in):
    response = client.get("/api/holdings/combined")
    assert response.status_code == 200
    assert response.json() == {"sources": [], "combined": {"available": False, "deduplicated": {}, "onlyInIndmoney": {}}}


# ── Needs attention ────────────────────────────────────────────────────────────

def test_needs_attention_for_an_ended_login_or_day_old_holdings():
    day = brokers.HOLDINGS_STALE_MS
    assert brokers.needs_attention("needs_login", None) is True
    assert brokers.needs_attention("connected", 1000, now=1000 + day) is False
    assert brokers.needs_attention("connected", 1000, now=1001 + day) is True
    assert brokers.needs_attention("connected", None) is False  # nothing fetched yet is not an old copy
    for status in ("pending", "unavailable", "disconnected"):
        assert brokers.needs_attention(status, 1, now=10 * day) is False


def test_broker_list_carries_checked_at_and_needs_attention(client, signed_in, fake_kite, monkeypatch):
    first = client.get("/api/brokers").json()["brokers"][1]
    assert first["needsAttention"] is False and first["checkedAt"] is None
    logged_in(client, fake_kite)
    client.get("/api/brokers/zerodha/holdings?refresh=1")
    item = client.get("/api/brokers").json()["brokers"][1]
    assert item["status"] == "connected" and item["checkedAt"] and item["needsAttention"] is False
    real = users.now_ms()
    monkeypatch.setattr(users, "now_ms", lambda: real + brokers.HOLDINGS_STALE_MS + 1000)
    assert client.get("/api/brokers").json()["brokers"][1]["needsAttention"] is True
    monkeypatch.setattr(users, "now_ms", lambda: real)
    fake_kite.logged_in = False
    client.get("/api/brokers/zerodha/status")
    ended = client.get("/api/brokers").json()["brokers"][1]
    assert ended["status"] == "needs_login" and ended["needsAttention"] is True


def test_the_holdings_tool_tells_the_model_to_say_when_and_suggest_reconnecting(signed_in, fake_kite):
    brokers.save_connection(signed_in["id"], "zerodha", session_id="s", protocol="2025-03-26", status="connected")
    tools = brokers.broker_tools(agent_user(signed_in["id"]))
    description = tool(tools, "get_broker_holdings").description
    assert "stale" in description and "reconnect" in description


# ── ISIN matching in the combined view ─────────────────────────────────────────

ACME, SAMPLE = "INE000A01011", "INE000B01012"


def im_row(code, broker, asset_type="IND_STOCK", name="Some Name"):
    return {"investment_code": code, "investment": name, "asset_type": asset_type, "broker": broker,
            "market_value": "100", "total_units": "1", "invested_amount": "0"}


def split(rows, broker_rows=None):
    broker_rows = broker_rows if broker_rows is not None else ZERODHA.normalise("get_holdings", kite.HOLDINGS)
    return brokers.split_indmoney_rows(rows, [(ZERODHA, broker_rows)])


def test_isin_of_accepts_only_isin_shaped_codes():
    assert brokers.isin_of({"investment_code": " ine000a01011 "}) == ACME
    for code in ["INFY", "120503", "", None, "INE000A0101", "XX000A010111", "INE000A0101X"]:
        assert brokers.isin_of({"investment_code": code}) is None


def test_label_rule_is_unchanged_and_counts_unconfirmed_isins():
    kept, dedup, only_in = split([im_row("HDFCBANK", "Zerodha"), im_row(ACME, "Zerodha"), im_row("INE999Z01019", "Zerodha"),
                                  im_row("TCS", "INDmoney")])
    assert [row["investment_code"] for row in kept] == ["TCS"]
    assert dedup == {"Zerodha": 3}
    assert only_in == {"Zerodha": 1}  # only the ISIN-shaped code Zerodha does not list


@pytest.mark.parametrize("label", [None, "", "others", "Other", "unknown", "NA"])
def test_a_generic_label_with_a_matching_isin_is_the_same_holding(label):
    kept, dedup, only_in = split([im_row(ACME, label), im_row("INE999Z01019", label)])
    assert [row["investment_code"] for row in kept] == ["INE999Z01019"]
    assert dedup == {"Zerodha": 1} and only_in == {}


def test_a_generic_label_needs_the_same_asset_type():
    kept, dedup, _ = split([im_row(ACME, None, asset_type="MF")])
    assert len(kept) == 1 and dedup == {}


def test_a_holding_at_two_named_brokers_is_never_dropped():
    kept, dedup, only_in = split([im_row(ACME, "INDstocks")])
    assert len(kept) == 1 and dedup == {} and only_in == {}


def test_names_never_match_and_non_isin_codes_stay():
    kept, dedup, _ = split([im_row("ACMEBANK", None, name="ACMEBANK"), im_row("", "others", name="ACMEBANK")])
    assert len(kept) == 2 and dedup == {}


def test_combined_reports_isin_matches_and_only_in_indmoney():
    snapshot = {"capturedAt": 1, "results": {"networth_holdings:IND_STOCK": json.dumps({"holdings": [
        im_row(ACME, "others"), im_row("INE999Z01019", "Zerodha"), im_row(SAMPLE, "INDstocks"), im_row("TCS", "INDmoney")]})}}
    rows = ZERODHA.normalise("get_holdings", kite.HOLDINGS)
    payload = {"broker": "zerodha", "available": True, "status": "connected", "asOf": 5, "stale": False,
               "figures": None, "holdings": [], "_rows": rows}
    result = brokers.combined(snapshot, [payload])
    assert result["combined"]["deduplicated"] == {"Zerodha": 2}
    assert result["combined"]["onlyInIndmoney"] == {"Zerodha": 1}
    indmoney = next(source for source in result["sources"] if source["id"] == "indmoney")
    assert indmoney["movedTo"] == {"Zerodha": 2}
    assert len(indmoney["holdings"]) == 2  # INDstocks' and INDmoney's own rows stay


# ── Privacy paths ──────────────────────────────────────────────────────────────

def holdings_stored(client, fake_kite):
    logged_in(client, fake_kite)
    assert client.get("/api/brokers/zerodha/holdings?refresh=1").json()["available"]


def test_the_notice_and_policy_version_cover_brokers():
    from app import privacy
    assert privacy.POLICY_VERSION == "2026-11-13.1"
    section = next(item for item in privacy.NOTICE if item["title"] == "Broker connections")
    assert "Zerodha" in section["body"] and "read-only" in section["body"]


def test_export_has_the_holdings_and_never_the_session(client, signed_in, fake_kite):
    holdings_stored(client, fake_kite)
    exported = client.post("/api/users/me/export", json={"pin": "2468"})
    body = exported.json()
    assert body["brokers"][0]["broker"] == "zerodha" and len(body["brokers"][0]["holdings"]) == 3
    text = exported.text.lower()
    assert "sessionid" not in text and "session" not in body["brokers"][0] and "kitemcp-" not in text


def test_clearing_history_keeps_the_connection_and_drops_the_holdings(client, signed_in, fake_kite):
    holdings_stored(client, fake_kite)
    assert client.delete("/api/users/me/history").json() == {"cleared": True}
    found = brokers.get_connection(signed_in["id"], "zerodha")
    assert found is not None and found["holdingsSealed"] is None and found["holdingsAt"] is None


def test_switching_to_private_mode_drops_the_holdings(client, signed_in, fake_kite):
    from app import privacy
    holdings_stored(client, fake_kite)
    accepted = client.post("/api/privacy/consent", json={"policyVersion": privacy.POLICY_VERSION, "storageMode": "private"})
    assert accepted.status_code == 200
    found = brokers.get_connection(signed_in["id"], "zerodha")
    assert found is not None and found["holdingsSealed"] is None


def test_deleting_the_profile_leaves_no_connection(client, signed_in, fake_kite):
    holdings_stored(client, fake_kite)
    assert client.request("DELETE", "/api/users/me", json={"pin": "2468"}).status_code == 200
    with connect_db() as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM broker_connections").fetchone()["n"] == 0


# ── Agent wiring ───────────────────────────────────────────────────────────────

import asyncio  # noqa: E402

from app import agent, main, tasks, tool_results  # noqa: E402
from app.sealing import ProfileKeys  # noqa: E402
from tests.test_agent import FakeAsyncClient, _completion  # noqa: E402


def agent_user(user_id):
    return {"id": user_id, "keys": ProfileKeys(), "storageMode": "device"}


def tool(tools, name):
    return next(item for item in tools if item.name == name)


def run(coro):
    return asyncio.run(coro)


def connect_directly(user_id, status="connected"):
    brokers.save_connection(user_id, "zerodha", session_id="kitemcp-1", protocol="2025-03-26", status=status)


def test_broker_tools_are_offered_only_with_a_connection(signed_in):
    assert brokers.broker_tools(agent_user(signed_in["id"])) == []
    assert brokers.context_line(signed_in["id"]) is None
    connect_directly(signed_in["id"])
    tools = brokers.broker_tools(agent_user(signed_in["id"]))
    assert [item.name for item in tools] == ["get_broker_holdings", "call_broker_tool"]
    holdings_schema = tool(tools, "get_broker_holdings").input_schema
    assert holdings_schema["properties"]["broker"]["enum"] == ["zerodha", "all"]
    call_schema = tool(tools, "call_broker_tool").input_schema
    assert call_schema["properties"]["broker"]["enum"] == ["zerodha"]
    assert "login" not in call_schema["properties"]["tool"]["enum"]
    assert not brokers.WRITE_TOOLS & set(call_schema["properties"]["tool"]["enum"])
    assert "never add the two" in brokers.context_line(signed_in["id"])


def test_a_write_tool_is_refused_and_never_reaches_the_broker(signed_in, fake_kite):
    connect_directly(signed_in["id"])
    handler = tool(brokers.broker_tools(agent_user(signed_in["id"])), "call_broker_tool").handler
    result = run(handler({"broker": "zerodha", "tool": "place_order", "arguments": {"quantity": 1}}))
    assert tool_results.reported_error(result) and "place_order" in result
    assert fake_kite.calls == []
    run(handler({"broker": "zerodha", "tool": "login"}))
    assert fake_kite.calls == []


def test_a_read_tool_passes_through_with_the_stored_session(signed_in, fake_kite):
    connect_directly(signed_in["id"])
    fake_kite.logged_in = True
    handler = tool(brokers.broker_tools(agent_user(signed_in["id"])), "call_broker_tool").handler
    assert json.loads(run(handler({"broker": "zerodha", "tool": "get_profile"})))["user_id"] == "AB1234"
    assert fake_kite.calls == [("get_profile", "kitemcp-1", {})]


def test_a_login_needed_error_tells_the_model_to_reconnect_and_never_raises(signed_in, fake_kite):
    connect_directly(signed_in["id"])
    tools = brokers.broker_tools(agent_user(signed_in["id"]))
    result = json.loads(run(tool(tools, "call_broker_tool").handler({"broker": "zerodha", "tool": "get_holdings"})))
    assert result["errorKind"] == "unavailable" and "reconnect Zerodha" in result["next_step"]
    assert brokers.get_connection(signed_in["id"], "zerodha")["status"] == "needs_login"
    fake_kite.error = main.McpAuthError("401 unauthorized")
    result = json.loads(run(tool(tools, "call_broker_tool").handler({"broker": "zerodha", "tool": "get_holdings"})))
    assert "error" in result
    fake_kite.error = RuntimeError("boom")
    assert json.loads(run(tool(tools, "get_broker_holdings").handler({"broker": "zerodha"})))["available"] is False


def test_broker_holdings_tool_returns_figures_with_the_time_and_staleness(signed_in, fake_kite):
    connect_directly(signed_in["id"])
    fake_kite.logged_in = True
    handler = tool(brokers.broker_tools(agent_user(signed_in["id"])), "get_broker_holdings").handler
    live = json.loads(run(handler({"broker": "zerodha"})))
    assert live["stale"] is False and live["asOf"] and live["figures"]["by_broker"][0]["broker"] == "Zerodha"
    fake_kite.logged_in = False
    stale = json.loads(run(handler({"broker": "zerodha"})))
    assert stale["stale"] is True and stale["asOf"] == live["asOf"]
    everything = json.loads(run(handler({"broker": "all"})))
    assert everything["broker"] == "all" and everything["sources"][0]["id"] == "zerodha"


def test_get_broker_holdings_counts_as_reading_account_data():
    assert tasks.is_account_tool("get_broker_holdings", {"get_broker_holdings"})
    assert tasks.is_account_tool("call_broker_tool", {"call_broker_tool"})


def test_keywords_route_broker_questions_to_account_data():
    assert agent._keyword_category("what do I hold in zerodha", []) == "account_data"
    assert agent._keyword_category("show my kite holdings", []) == "account_data"


def _chat(client, monkeypatch, message="what do I hold in zerodha"):
    seen = []

    async def fake_run_agent(message, mcp_url, token, history, agent_type, provider, *, context=None, saved_context=None, plan_parts=()):
        seen.append((token, context))
        return "ok"

    monkeypatch.setattr(main, "run_agent", fake_run_agent)
    chat = client.post("/api/chats", json={"agentType": "portfolio"}).json()
    return client.post("/api/chat", json={"message": message, "chatId": chat["id"], "agentType": "portfolio"}), seen


def test_portfolio_chat_works_with_only_a_broker_connected(client, signed_in, monkeypatch):
    connect_directly(signed_in["id"])
    response, seen = _chat(client, monkeypatch)
    assert response.status_code == 200, response.text
    token, context = seen[0]
    assert token is None
    assert {"get_broker_holdings", "call_broker_tool"} <= {item.name for item in context.local_tools}
    assert "Connected brokers: Zerodha" in context.profile_context


def test_portfolio_chat_with_no_indmoney_and_no_broker_is_still_refused(client, signed_in, monkeypatch):
    response, seen = _chat(client, monkeypatch)
    assert response.status_code == 401 and seen == []
    assert response.json()["error"] == "Connect your INDmoney account to use Portfolio Analyzer."


def test_tools_listing_names_the_brokers(client, signed_in):
    assert client.get("/api/tools").json()["brokers"][0]["broker"] == "zerodha"


@pytest.mark.asyncio
async def test_stream_agent_answers_from_broker_tools_without_an_indmoney_session(signed_in, fake_kite, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    connect_directly(signed_in["id"])
    fake_kite.logged_in = True
    tools = brokers.broker_tools(agent_user(signed_in["id"]))
    context = agent.PortfolioContext(local_tools=tuple(tools))

    async def refuse(*args, **kwargs):
        raise AssertionError("no INDmoney session may be opened")

    monkeypatch.setattr(agent, "fetch_mcp_session", refuse)
    figure = json.loads(await tool(tools, "get_broker_holdings").handler({"broker": "zerodha"}))["figures"]
    assert figure["totals"]["current_value_inr"] == 12470.5
    value = "12,470.50"
    call = {"id": "c1", "function": {"name": "get_broker_holdings", "arguments": json.dumps({"broker": "zerodha"})}}
    captured = []
    client = FakeAsyncClient([_completion(tool_calls=[call]), _completion(f"Your Zerodha holdings are worth ₹{value}.")], captured)
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: client)

    answer = await agent.run_agent("what do I hold in zerodha", "https://mcp", None, [], "portfolio", "openai", context=context)

    assert answer == f"Your Zerodha holdings are worth ₹{value}."
    assert "get_broker_holdings" in [item["function"]["name"] for item in captured[0]["payload"]["tools"]]


@pytest.mark.asyncio
async def test_stream_agent_without_token_or_broker_tools_still_errors(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    with pytest.raises(agent.AgentRunError):
        await agent.run_agent("what is in my portfolio", "https://mcp", None, [], "portfolio", "openai")


# ── Tools the broker's server exposes ─────────────────────────────────────────

KITE_TOOLS = [
    {"name": "place_order", "description": "Place an order."},
    {"name": "get_holdings", "description": "Get   holdings\n for the user."},
    {"name": "login", "description": "Log in to Kite."},
    {"name": "exit_position", "description": "Exit a position."},
    {"name": "get_profile", "description": "x" * 1000},
    {"description": "no name"},
]


@pytest.fixture
def kite_tools(monkeypatch):
    seen = []

    async def fetch(url, token, *, service="INDmoney"):
        seen.append((url, token))
        return {"tools": KITE_TOOLS, "sessionId": "kitemcp-tools", "protocolVersion": "2025-03-26"}

    monkeypatch.setattr(brokers, "_tools_cache", {})
    monkeypatch.setattr(brokers.mcp_client, "fetch_mcp_session", fetch)
    return seen


def test_tool_list_marks_what_ca_mode_may_call(client, signed_in, kite_tools):
    body = client.get("/api/brokers/zerodha/tools").json()
    tools = {tool["name"]: tool for tool in body["tools"]}
    assert set(tools) == {"place_order", "get_holdings", "login", "exit_position", "get_profile"}
    assert tools["get_holdings"] == {"name": "get_holdings", "description": "Get holdings for the user.", "access": "read"}
    assert tools["place_order"]["access"] == "write" and tools["exit_position"]["access"] == "write"
    assert tools["login"]["access"] == "not_enabled"
    assert len(tools["get_profile"]["description"]) == brokers.MAX_TOOL_DESCRIPTION
    assert [tool["access"] for tool in body["tools"]] == ["read", "read", "not_enabled", "write", "write"]
    assert kite_tools == [(brokers.BROKERS["zerodha"].mcp_url, None)]
    assert "sessionId" not in body and "kitemcp-tools" not in json.dumps(body)


def test_tool_list_is_cached_and_needs_no_broker_connection(client, signed_in, kite_tools):
    client.get("/api/brokers/zerodha/tools")
    client.get("/api/brokers/zerodha/tools")
    assert len(kite_tools) == 1
    assert brokers.connections(signed_in["id"]) == []


def test_tool_list_needs_a_profile_and_a_known_broker(client, kite_tools):
    assert client.get("/api/brokers/zerodha/tools").status_code == 401
    from tests.conftest import sign_up
    sign_up(client)
    assert client.get("/api/brokers/nope/tools").status_code == 404


def test_tool_list_failure_is_a_safe_message(client, signed_in, monkeypatch):
    async def fail(url, token, *, service="INDmoney"):
        raise RuntimeError("secret internals")

    monkeypatch.setattr(brokers, "_tools_cache", {})
    monkeypatch.setattr(brokers.mcp_client, "fetch_mcp_session", fail)
    response = client.get("/api/brokers/zerodha/tools")
    assert response.status_code == 502
    assert "secret" not in response.text and "Zerodha" in response.json()["error"]


def test_broker_fetch_schedules_fill(client, signed_in, fake_kite, monkeypatch):
    from app import logos

    scheduled = []
    monkeypatch.setattr(logos, "schedule_fill", lambda holdings, **kwargs: scheduled.append((holdings, kwargs)))
    logged_in(client, fake_kite)
    body = client.get("/api/brokers/zerodha/holdings?refresh=1").json()
    assert len(scheduled) == 1 and scheduled[0][1] == {}
    assert scheduled[0][0] == body["holdings"] and len(body["holdings"]) == 3
    assert body["available"] and "_rows" not in body

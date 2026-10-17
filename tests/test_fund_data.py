"""Fund figures from INDmoney for a goal's suitable categories: parsing, missing fields, dates, mapping, fetching, API, tool."""

from __future__ import annotations

import json
from datetime import date

import pytest

from app import fund_data, goal_suitability, main, memory, number_check, unlock, users
from app.mcp_client import McpToolError
from tests.fixtures import indmoney_funds as fx

TODAY = date(2026, 10, 16)
RETRIEVED = "2026-10-16T04:00:00Z"
# The get_mf_by_category ``categories`` enum as INDmoney served it on 2026-10-16.
INDMONEY_SLUGS = {
    "index-funds", "equity-other", "liquid", "index-funds-fixed-income", "fund-of-funds", "low-duration", "overnight",
    "ultra-short-duration", "nifty-index-funds", "money-market", "arbitrage-fund", "short-duration", "other-bond",
    "corporate-bond", "dynamic-bond", "fixed-maturity-intermediate-term-bond", "large-cap", "global-funds",
    "aggressive-allocation", "flexi-cap", "banking-psu", "elss-tax-savings", "large-cap-index-funds", "gilt-funds",
    "dynamic-asset-allocation", "equity-savings", "sector-precious-metals", "small-cap", "large-mid-cap", "mid-cap",
    "conservative-allocation", "sector-financial-services", "multi-asset-allocation", "multi-cap", "focused-fund",
    "fixed-maturity-short-term-bond", "floating-rate", "medium-duration", "equity-consumption", "credit-risk",
    "medium-to-long-duration", "value", "mid-cap-index-funds", "nifty-50-index-funds", "other-index-funds",
    "equity-infrastructure", "small-cap-index-funds", "retirement", "sector-healthcare", "long-duration",
    "sector-technology", "fixed-maturity-ultrashort-bond", "nifty-next-50-index-funds", "10-yr-government-bond",
    "dividend-yield", "nifty-midcap-index-funds", "equity-esg", "children", "balanced-allocation", "global-index-funds",
    "nifty-smallcap-index-funds", "sector-energy", "contra", "sector-fmcg",
}


def flexi(today=TODAY, rows=None, benchmarks=None):
    return fund_data.parse_category(
        "large_flexi_equity", fx.listing(rows or fx.FLEXI_ROWS), [fx.details(benchmarks or fx.FLEXI_BENCHMARKS)],
        today, RETRIEVED,
    )


def by_name(parsed):
    return {fund["name"].split()[0]: fund for fund in parsed["funds"]}


def missing(fund):
    return {item["field"]: item["reason"] for item in fund["missing"]}


# ── Parsing ────────────────────────────────────────────────────────────────────

def test_a_listing_row_becomes_sourced_dated_decimal_metrics():
    zeta = by_name(flexi())["Zeta"]

    assert zeta["schemeId"] == "2101" and zeta["category"] == "flexi cap" and zeta["navDate"] == "2026-10-15"
    metrics = zeta["metrics"]
    assert metrics["return1yPct"] == {"value": "14.20", "asOf": "2026-10-15", "status": "current", "source": fund_data.SOURCE_LISTING}
    assert metrics["return3yPct"]["value"] == "17.85" and metrics["return5yPct"]["value"] == "21.30"
    assert metrics["expenseRatioPct"] == {
        "value": "0.62", "asOf": None, "status": "undated", "source": fund_data.SOURCE_LISTING, "retrievedAt": RETRIEVED,
    }
    assert metrics["risk"]["value"] == "Very High" and metrics["risk"]["level"] == 6
    assert metrics["benchmark"]["value"] == "NIFTY 500 TRI" and metrics["benchmark"]["source"] == fund_data.SOURCE_DETAILS
    assert zeta["missing"] == []


def test_numbers_are_read_as_decimals_without_float_noise():
    row = fx.listing_row(1, "X Flexi Cap Fund", "flexi cap", r1=0.1 + 0.2, expense="0.565")
    fund = flexi(rows=[row], benchmarks={1: "B"})["funds"][0]
    assert fund["metrics"]["return1yPct"]["value"] == "0.30"
    assert fund["metrics"]["expenseRatioPct"]["value"] == "0.57"
    assert fund_data.to_decimal("1,234.5%") == fund_data.to_decimal(1234.5)
    assert fund_data.to_decimal(True) is None and fund_data.to_decimal("n/a") is None


def test_schemes_are_listed_by_name_and_rankings_are_dropped():
    parsed = flexi()
    assert [fund["name"].split()[0] for fund in parsed["funds"]] == ["Alpha", "Beta", "Nova", "Zeta"]
    text = json.dumps(parsed)
    assert "rank" not in text and "rating" not in text and "total_score" not in text


def test_a_zero_return_is_missing_not_zero():
    beta = by_name(flexi())["Beta"]
    assert "return5yPct" not in beta["metrics"]
    assert "reported 0" in missing(beta)["return5yPct"]


def test_returns_for_periods_longer_than_the_scheme_has_existed_are_missing():
    nova = by_name(flexi())["Nova"]
    assert nova["metrics"]["return1yPct"]["value"] == "9.60"
    assert "return3yPct" not in nova["metrics"] and "return5yPct" not in nova["metrics"]
    assert "younger than 3 years (launched 2024-06-12)" in missing(nova)["return3yPct"]
    assert "younger than 5 years" in missing(nova)["return5yPct"]


def test_absent_risk_and_benchmark_are_listed_as_missing():
    beta = by_name(flexi())["Beta"]
    assert "risk" not in beta["metrics"] and "benchmark" not in beta["metrics"]
    assert set(missing(beta)) == {"return5yPct", "risk", "benchmark"}
    assert missing(beta)["risk"] == "INDmoney did not give it."


@pytest.mark.parametrize(("value", "shown"), [(None, False), (0, False), (45, False), (0.004, False), (2.25, True)])
def test_an_implausible_expense_ratio_is_not_shown(value, shown):
    row = fx.listing_row(1, "X Flexi Cap Fund", "flexi cap", expense=value)
    fund = flexi(rows=[row], benchmarks={1: "B"})["funds"][0]
    assert ("expenseRatioPct" in fund["metrics"]) is shown
    assert ("expenseRatioPct" in missing(fund)) is not shown


def test_failed_details_say_so_for_the_benchmark():
    parsed = fund_data.parse_category(
        "large_flexi_equity", fx.listing(fx.FLEXI_ROWS[:1]), ["not json"], TODAY, RETRIEVED,
    )
    assert "could not be fetched" in missing(parsed["funds"][0])["benchmark"]


def test_an_unreadable_listing_raises_a_safe_error():
    with pytest.raises(fund_data.FundDataError, match="not in a readable form"):
        fund_data.parse_category("liquid_overnight", "<html>oops", [], TODAY, RETRIEVED)
    with pytest.raises(fund_data.FundDataError, match="reported no data"):
        fund_data.parse_category("liquid_overnight", json.dumps({"success": False}), [], TODAY, RETRIEVED)


def test_a_listing_wrapped_in_a_result_string_is_unwrapped():
    wrapped = json.dumps({"result": fx.listing(fx.LIQUID_ROWS)})
    parsed = fund_data.parse_category("liquid_overnight", wrapped, [fx.details(fx.LIQUID_BENCHMARKS)], TODAY, RETRIEVED)
    assert len(parsed["funds"]) == 2


# ── Dates ──────────────────────────────────────────────────────────────────────

def test_returns_older_than_the_stale_limit_are_stale_not_current(monkeypatch):
    monkeypatch.setenv("FUND_STALE_DAYS", "5")
    later = by_name(flexi(today=date(2026, 10, 21)))["Zeta"]["metrics"]["return1yPct"]
    assert later["status"] == "stale" and later["asOf"] == "2026-10-15"
    edge = by_name(flexi(today=date(2026, 10, 20)))["Zeta"]["metrics"]["return1yPct"]
    assert edge["status"] == "current"


@pytest.mark.parametrize("nav_date", ["", "sometime", "15/10/2026", None])
def test_returns_without_a_readable_date_are_undated_and_never_current(nav_date):
    row = fx.listing_row(1, "X Flexi Cap Fund", "flexi cap", nav_date=nav_date)
    fund = flexi(rows=[row], benchmarks={1: "B"})["funds"][0]
    for name in ("return1yPct", "return3yPct", "return5yPct"):
        assert fund["metrics"][name]["status"] == "undated" and fund["metrics"][name]["asOf"] is None
        assert fund["metrics"][name]["retrievedAt"] == RETRIEVED
    assert fund["navDate"] is None and "no readable NAV date" in missing(fund)["navDate"]
    assert all(item["status"] != "current" for item in fund["metrics"].values())


def test_a_nav_date_in_the_future_is_treated_as_undated():
    row = fx.listing_row(1, "X Flexi Cap Fund", "flexi cap", nav_date="15 Oct 2027")
    metric = flexi(rows=[row], benchmarks={1: "B"})["funds"][0]["metrics"]["return1yPct"]
    assert metric["status"] == "undated" and metric["asOf"] is None


@pytest.mark.parametrize(("text", "expected"), [
    ("15 Oct 2026", date(2026, 10, 15)), ("12 June 2023", date(2023, 6, 12)), ("01 Sept 2025", date(2025, 9, 1)),
    ("2026-10-15", date(2026, 10, 15)), ("", None), ("31 Feb 2026", None), (20261015, None),
])
def test_dates_are_read_in_indmoney_formats_only(text, expected):
    assert fund_data.parse_date(text) == expected


# ── Category mapping ───────────────────────────────────────────────────────────

def test_every_goal_category_maps_to_real_indmoney_slugs_and_none_twice():
    assert set(fund_data.INDMONEY_CATEGORIES) == {item["id"] for item in goal_suitability.CATEGORIES}
    slugs = [slug for group in fund_data.INDMONEY_CATEGORIES.values() for slug in group]
    assert len(slugs) == len(set(slugs)) and set(slugs) <= INDMONEY_SLUGS


@pytest.mark.parametrize(("text", "category"), [
    ("flexi cap", "large_flexi_equity"), ("Large Cap", "large_flexi_equity"), ("nifty 50 index funds", "large_flexi_equity"),
    ("liquid", "liquid_overnight"), ("money market", "short_duration_debt"), ("conservative allocation", "conservative_hybrid"),
    ("dynamic asset allocation", "balanced_multi_asset"), ("small cap", "mid_small_equity"),
    ("aggressive allocation", None), ("multi cap", None), ("sector technology", None), ("credit risk", None),
    ("index funds", None), ("", None), (None, None),
])
def test_indmoney_categories_map_only_where_they_clearly_fit(text, category):
    assert fund_data.category_for(text) == category


def test_rows_filed_under_another_or_unmapped_category_are_excluded_with_the_reason():
    parsed = flexi()
    assert [item["name"] for item in parsed["excluded"]] == ["Gamma Aggressive Hybrid Fund Direct Growth"]
    assert 'lists its category as "aggressive allocation"' in parsed["excluded"][0]["reason"]

    wrong = fund_data.parse_category("liquid_overnight", fx.listing(fx.FLEXI_ROWS[:2]), [], TODAY, RETRIEVED)
    assert wrong["funds"] == [] and all("another goal category" in item["reason"] for item in wrong["excluded"])


@pytest.mark.parametrize(("text", "expected"), [
    ("Very High Risk", ("Very High", 6)), ("Low to Moderate Risk", ("Low to Moderate", 2)), ("moderately high", ("Moderately High", 4)),
    ("Unrated", ("Unrated", None)), ("", None), (3, None),
])
def test_risk_labels_follow_the_riskometer(text, expected):
    assert fund_data.risk_label(text) == expected


# ── Comparison for a goal ──────────────────────────────────────────────────────

def suitability(**goal):
    base = {"targetDate": "2036-10-16", "flexibility": "flexible", "liquidityNeed": "low", "riskTolerance": "high",
            "lossCapacity": "high"}
    return goal_suitability.evaluate({**base, **goal}, TODAY)


def fetched(**listings):
    return {cid: fund_data.CategoryFetch(text, details, retrieved_at=1_792_130_400) for cid, (text, details) in listings.items()}


def test_only_categories_the_goal_does_not_rule_out_are_compared():
    short = suitability(targetDate="2027-10-16", riskTolerance="low", lossCapacity="low", flexibility="fixed")
    result = fund_data.build_comparison(short, fetched(liquid_overnight=(fx.listing(fx.LIQUID_ROWS), [])), TODAY)

    ids = [entry["id"] for entry in result["categories"]]
    assert "large_flexi_equity" not in ids and "mid_small_equity" not in ids
    assert "Large-cap, flexi-cap and index equity" in result["unsuitableCategories"]
    assert result["available"] is True and "Not advice" in result["note"]


def test_categories_not_fetched_or_unreadable_say_exactly_what_is_missing():
    result = fund_data.build_comparison(
        suitability(), {**fetched(large_flexi_equity=("<html>", [])),
                        "mid_small_equity": fund_data.CategoryFetch(None, error="INDmoney's fund listing failed for this category.")},
        TODAY,
    )
    by_id = {entry["id"]: entry for entry in result["categories"]}
    assert by_id["large_flexi_equity"]["available"] is False and "readable form" in by_id["large_flexi_equity"]["reason"]
    assert by_id["mid_small_equity"]["reason"] == "INDmoney's fund listing failed for this category."
    assert by_id["liquid_overnight"]["reason"] == "Not fetched."
    assert result["available"] is False and "no fund data" in result["reason"]


def test_without_a_connection_the_reason_is_given_and_nothing_is_shown():
    result = fund_data.build_comparison(suitability(), "INDmoney is not connected.", TODAY)
    assert result == {**result, "available": False, "reason": "INDmoney is not connected.", "categories": []}


def test_the_tool_payload_keeps_dates_with_figures_and_fits_groq():
    many = [fx.listing_row(3000 + n, f"Scheme {n:02d} Flexi Cap Fund Direct Growth", "flexi cap") for n in range(20)]
    listings = {cid: (fx.listing(many if cid == "large_flexi_equity" else fx.LIQUID_ROWS), []) for cid in fund_data.INDMONEY_CATEGORIES}
    result = {"goalName": "Retire", **fund_data.build_comparison(suitability(), fetched(**listings), TODAY)}

    payload = fund_data.tool_payload(result)

    assert len(json.dumps(payload, ensure_ascii=False)) <= fund_data.MAX_TOOL_PAYLOAD_CHARS
    flexi_entry = next(item for item in payload["categories"] if item["category"].startswith("Large-cap"))
    assert flexi_entry["truncated"] and flexi_entry["funds"]
    fund = flexi_entry["funds"][0]
    assert fund["return1yPct"] == "14.20" and fund["returnsAsOf"] == "2026-10-15" and fund["returnsStatus"] == "current"
    assert payload["datedBy"] == fund_data.DATED_BY and "rank" not in json.dumps(payload["categories"])


# ── Fetching ───────────────────────────────────────────────────────────────────

class FakeIndmoney:
    def __init__(self, monkeypatch, *, token="token", fail=()):
        self.calls = []
        self.fail = set(fail)

        async def access_token(user_id, *, force_refresh=False):
            return token

        async def session(url, token):
            return {"protocolVersion": "2025-03-26", "sessionId": "s", "tools": []}

        async def call(url, token, protocol, session_id, name, arguments, *, client=None):
            self.calls.append((name, arguments))
            if name in self.fail:
                raise McpToolError(f'MCP tool "{name}" failed: bad argument')
            if name == "get_mf_by_category":
                rows = fx.LIQUID_ROWS if "liquid" in arguments["categories"] else fx.FLEXI_ROWS
                return fx.listing(rows)
            return fx.details({**fx.FLEXI_BENCHMARKS, **fx.LIQUID_BENCHMARKS})

        monkeypatch.setattr(fund_data.users, "user_access_token", access_token)
        monkeypatch.setattr(fund_data, "fetch_mcp_session", session)
        monkeypatch.setattr(fund_data, "call_mcp_tool", call)
        fund_data.clear_cache()


@pytest.mark.asyncio
async def test_fetching_asks_for_the_largest_schemes_and_caches_them(monkeypatch):
    fake = FakeIndmoney(monkeypatch)

    first = await fund_data.fetch_categories("u1", ["liquid_overnight", "large_flexi_equity"])
    again = await fund_data.fetch_categories("u1", ["liquid_overnight", "large_flexi_equity"])

    listing_calls = [arguments for name, arguments in fake.calls if name == "get_mf_by_category"]
    assert {tuple(item["categories"]) for item in listing_calls} == {("liquid", "overnight"), fund_data.INDMONEY_CATEGORIES["large_flexi_equity"]}
    assert all(item["sort_key"] == "aum" and item["sort_asc"] is False and item["size"] == 5 for item in listing_calls)
    assert ("get_mf_funds_details", {"fund_ids": "1302,1303"}) in fake.calls
    assert len(fake.calls) == 4 and again.keys() == first.keys()
    fund_data.clear_cache("u1")
    await fund_data.fetch_categories("u1", ["liquid_overnight"])
    assert len(fake.calls) == 6


@pytest.mark.asyncio
async def test_a_failed_listing_is_reported_per_category_and_not_cached(monkeypatch):
    fake = FakeIndmoney(monkeypatch, fail={"get_mf_by_category"})
    result = await fund_data.fetch_categories("u1", ["liquid_overnight"])
    assert result["liquid_overnight"].error and result["liquid_overnight"].listing is None
    await fund_data.fetch_categories("u1", ["liquid_overnight"])
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_failed_details_keep_the_listing_and_flag_the_benchmark(monkeypatch):
    fake = FakeIndmoney(monkeypatch, fail={"get_mf_funds_details"})
    result = await fund_data.fetch_categories("u1", ["liquid_overnight"])
    await fund_data.fetch_categories("u1", ["liquid_overnight"])
    assert len(fake.calls) == 4  # not cached, so the next request tries again
    comparison = fund_data.build_comparison(suitability(), result, TODAY)
    liquid = next(entry for entry in comparison["categories"] if entry["id"] == "liquid_overnight")
    assert liquid["available"] and all("could not be fetched" in missing(fund)["benchmark"] for fund in liquid["funds"])


@pytest.mark.asyncio
async def test_a_slow_indmoney_gives_a_reason_instead_of_hanging(monkeypatch):
    import asyncio
    fake = FakeIndmoney(monkeypatch)

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(fund_data, "call_mcp_tool", slow)
    monkeypatch.setattr(fund_data, "FETCH_DEADLINE_SECONDS", 0.05)
    result = await fund_data.fetch_categories("u1", ["liquid_overnight"])
    assert result == "INDmoney's fund data took too long to arrive. Try again later." and not fake.calls


@pytest.mark.asyncio
async def test_no_token_means_no_calls_and_a_reason(monkeypatch):
    fake = FakeIndmoney(monkeypatch, token=None)
    result = await fund_data.fetch_categories("u1", ["liquid_overnight"])
    assert isinstance(result, str) and "not connected" in result and fake.calls == []


# ── API ────────────────────────────────────────────────────────────────────────

GOAL = {"name": "Retirement", "kind": "retirement", "currency": "INR", "targetAmount": 20_000_000,
        "targetDate": "2046-10-16", "savedAmount": 0, "monthlyContribution": 20_000, "flexibility": "flexible",
        "liquidityNeed": "low", "riskTolerance": "high", "lossCapacity": "high"}


def test_the_goal_funds_route_returns_sourced_dated_figures(client, signed_in, monkeypatch):
    FakeIndmoney(monkeypatch)
    goal = client.post("/api/goals", json=GOAL).json()

    body = client.get(f"/api/goals/{goal['id']}/funds").json()

    assert body["goalName"] == "Retirement" and body["available"] is True
    flexi_entry = next(entry for entry in body["categories"] if entry["id"] == "large_flexi_equity")
    assert flexi_entry["verdict"] == "allowed" and flexi_entry["retrievedAt"]
    for fund in flexi_entry["funds"]:
        for metric in fund["metrics"].values():
            assert metric["source"].startswith("INDmoney") and metric["status"] in {"current", "stale", "undated"}
            assert metric["asOf"] or metric["retrievedAt"]


def test_the_goal_funds_route_limits_to_one_category_and_refuses_unsuitable_ones(client, signed_in, monkeypatch):
    fake = FakeIndmoney(monkeypatch)
    goal = client.post("/api/goals", json=GOAL).json()
    near = client.post("/api/goals", json={**GOAL, "name": "Car", "targetDate": "2027-06-30"}).json()

    one = client.get(f"/api/goals/{goal['id']}/funds", params={"category": "liquid_overnight"}).json()
    assert [entry["id"] for entry in one["categories"]] == ["liquid_overnight"]
    assert client.get(f"/api/goals/{near['id']}/funds", params={"category": "mid_small_equity"}).status_code == 422
    assert client.get(f"/api/goals/{goal['id']}/funds", params={"category": "crypto"}).status_code == 422
    assert client.get("/api/goals/nope/funds").status_code == 404
    assert all(name != "get_mf_by_category" or "mid-cap" not in arguments["categories"]
               for name, arguments in fake.calls[2:])


def test_the_goal_funds_route_needs_consent_and_reports_a_missing_connection(client, monkeypatch):
    from conftest import sign_up
    sign_up(client, storage_mode=None)
    assert client.get("/api/goals/x/funds").status_code == 403

    signed = sign_up(client, name="Ravi")  # never connected to INDmoney
    goal = client.post("/api/goals", json=GOAL).json()
    fund_data.clear_cache()
    body = client.get(f"/api/goals/{goal['id']}/funds").json()
    assert body["available"] is False and "not connected" in body["reason"] and signed["id"]


# ── Agent tool and number check ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_agent_tool_returns_the_comparison_and_quoted_figures_pass_the_number_check(client, signed_in, monkeypatch):
    FakeIndmoney(monkeypatch)
    client.post("/api/goals", json=GOAL)
    keys = unlock.keys_for_session(users.get_user(signed_in["id"]), client.cookies.get(main.SESSION_COOKIE))
    user = {"id": signed_in["id"], "keys": keys}
    tool = fund_data.fund_comparison_tool(user, today=TODAY)

    text = await tool.handler({"goalName": "retirement", "categoryId": "large_flexi_equity"})
    payload = json.loads(text)

    assert payload["goalName"] == "Retirement" and payload["categories"][0]["funds"][0]["name"].startswith("Alpha")
    sourced = "Alpha Large Cap Fund returned 11.05% over 1 year (as of 15 Oct 2026) with a 0.48% expense ratio."
    assert number_check.unverified_numbers(sourced, [text]) == []
    assert number_check.unverified_numbers("Alpha returned 19.4% over 1 year.", [text]) == ["19.4%"]
    assert "error" in json.loads(await tool.handler({"goalName": "nothing"}))
    assert memory.count_goals(signed_in["id"]) == 1


# ── Chat wiring and prompt rules ───────────────────────────────────────────────

def test_only_portfolio_analyzer_with_a_goal_gets_the_fund_tool(client, signed_in, monkeypatch):
    from tests.test_api import connect_user_to_indmoney
    from tests.test_goal_portfolio import _chat_with
    connect_user_to_indmoney(signed_in["id"])

    context, _ = _chat_with(client, "portfolio", monkeypatch)
    assert "get_goal_fund_comparison" not in {tool.name for tool in context.local_tools}

    client.post("/api/goals", json=GOAL)
    context, _ = _chat_with(client, "portfolio", monkeypatch)
    assert "get_goal_fund_comparison" in {tool.name for tool in context.local_tools}
    finance, _ = _chat_with(client, "finance", monkeypatch)
    assert "get_goal_fund_comparison" not in {tool.name for tool in finance.local_tools}


@pytest.mark.asyncio
async def test_the_fund_rules_replace_the_no_fund_rule_only_when_the_tool_is_offered(monkeypatch):
    from app import agent, goal_portfolio
    from tests.test_agent import FakeAsyncClient, _completion
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    user = {"id": "u", "keys": None}
    captured = []
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient([_completion("ok"), _completion("ok")], captured))

    with_funds = agent.PortfolioContext(local_tools=(*goal_portfolio.goal_tools(user, include_holdings=False),
                                                     fund_data.fund_comparison_tool(user)))
    await agent.run_agent("Explain ELSS", "https://unused", None, [], "finance", "openai",
                          context=agent.PortfolioContext(local_tools=tuple(goal_portfolio.goal_tools(user, include_holdings=False))))
    await agent.run_agent("Which funds fit my retirement goal?", "https://unused", "token", [], "portfolio", "openai",
                          context=with_funds)

    finance_prompt = captured[0]["payload"]["messages"][0]["content"]
    portfolio_prompt = captured[-1]["payload"]["messages"][0]["content"]
    assert agent.NO_FUND_DATA_RULE in finance_prompt and agent.FUND_COMPARISON_RULES not in finance_prompt
    assert agent.FUND_COMPARISON_RULES in portfolio_prompt and agent.NO_FUND_DATA_RULE not in portfolio_prompt

from __future__ import annotations

import base64
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pytest

from app import amfi, benchmarks, cas, mf_ledger, mf_math, mf_returns, unlock, users
from app.amfi import NavRow
from mf_fixtures import scheme, statement

TODAY = date(2026, 11, 11)
PDF = b"%PDF-1.7 synthetic"


def ledger(*schemes, period=("01-Jan-2020", "30-Sep-2026")):
    data = statement(*schemes, period=period)
    return mf_ledger.build([{
        "uploadedAt": 1, "period_from": data.statement_period.from_, "period_to": data.statement_period.to,
        "ledger": mf_ledger.extract(data, mf_ledger.folio_key_for(b"s" * 32)),
    }])


def nav(isin="INF179K01VK6", value="130", day=date(2026, 11, 10), category="Other Scheme - Index Funds"):
    return {isin: NavRow(isin, "120716", "HDFC Index Fund", category, Decimal(value), day)}


def pct(rate: Decimal) -> float:
    return float((rate * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def test_scheme_figures_from_lots_and_amfi_nav():
    (item,) = ledger(scheme())

    result = mf_returns.scheme_returns(item, nav(), TODAY)

    # 20 units left at 130; open lot cost 2500.125; realised 699.375 (see test_mf_math).
    assert result["currentValue"] == 2600.0
    assert result["invested"] == 2500.13 and result["unrealisedGain"] == 99.88 and result["realisedGain"] == 699.38
    assert (result["navSource"], result["navDate"], result["nav"], result["units"]) == ("amfi", "2026-11-10", 130.0, 20.0)
    assert (result["status"], result["reason"], result["amfiCategory"]) == ("ok", None, "Other Scheme - Index Funds")
    flows = mf_math.fifo(item.transactions).cashflows + [(date(2026, 11, 10), Decimal("2600"))]
    assert result["xirr"] == pct(mf_math.xirr(flows).rate)
    assert result["absoluteReturnPct"] is None and result["returnReason"] is None
    assert result["heldSince"] == "2024-01-10"


def test_fully_redeemed_scheme_counts():
    (item,) = ledger(scheme(close="0", transactions=[
        ("2024-01-10", "PURCHASE", "10000", "100", "100"), ("2025-03-10", "REDEMPTION", "-11000", "-100", "110"),
    ]))

    result = mf_returns.scheme_returns(item, nav(), TODAY)

    assert result["status"] == "ok"
    assert result["currentValue"] == 0.0 and result["invested"] == 0.0 and result["realisedGain"] == 1000.0
    assert result["xirr"] == pct(mf_math.xirr([(date(2024, 1, 10), Decimal("-10000")), (date(2025, 3, 10), Decimal("11000"))]).rate)


def test_short_holding_reports_absolute_return():
    (item,) = ledger(scheme(close="10", transactions=[("2026-06-01", "PURCHASE", "1000", "10", "100")]))

    result = mf_returns.scheme_returns(item, nav(value="110"), TODAY)

    assert result["xirr"] is None and result["returnReason"] == "under_one_year"
    assert result["absoluteReturnPct"] == 10.0


def test_statement_nav_used_when_newer_or_missing():
    (item,) = ledger(scheme())

    older_amfi = mf_returns.scheme_returns(item, nav(day=date(2026, 9, 1)), date(2026, 10, 2))
    no_amfi = mf_returns.scheme_returns(item, {}, date(2026, 10, 2))

    for result in (older_amfi, no_amfi):
        assert (result["navSource"], result["navDate"], result["currentValue"]) == ("statement", "2026-09-30", 2500.0)
        assert result["status"] == "ok"
    assert no_amfi["amfiCategory"] is None


def test_old_nav_is_stale_but_counted():
    (item,) = ledger(scheme())

    result = mf_returns.scheme_returns(item, nav(day=date(2026, 11, 1)), TODAY)

    assert result["status"] == "nav_stale" and "01 Nov 2026" in result["reason"]
    assert result["currentValue"] == 2600.0 and result["xirr"] is not None


def test_lots_that_do_not_match_the_closing_units_are_unreconciled():
    (item,) = ledger(scheme(close="50", transactions=[
        ("2024-01-01", "PURCHASE", "1000", "100", "10"), ("2024-02-03", "REVERSAL", "-500", "-50", "10"),
    ]))

    result = mf_returns.scheme_returns(item, nav(value="11"), TODAY)

    assert result["status"] == "unreconciled" and result["invested"] is None and result["xirr"] is None
    assert result["problems"]


def test_excluded_schemes_have_reasons_and_coverage():
    schemes = ledger(scheme(), scheme("Axis Bluechip", "INF846K01DP8", close="25"))
    navs = {**nav(), **nav("INF846K01DP8", "40")}

    result = mf_returns.returns_from(schemes, navs, TODAY)

    by_name = {item["name"]: item for item in result["schemes"]}
    excluded = by_name["Axis Bluechip"]
    assert excluded["status"] == "unreconciled" and excluded["currentValue"] == 1000.0
    assert excluded["invested"] is None and excluded["realisedGain"] is None and excluded["returnReason"] == "unreconciled"
    portfolio = result["portfolio"]
    assert portfolio["totalValue"] == 3600.0 and portfolio["includedValue"] == 2600.0
    assert portfolio["invested"] == 2500.13 and portfolio["realisedGain"] == 699.38
    assert result["coverage"]["pct"] == 72.22
    assert result["coverage"]["excluded"] == [{"name": "Axis Bluechip", "status": "unreconciled", "reason": excluded["reason"]}]
    assert result["navDate"] == "2026-11-10"
    assert result["statementPeriod"] == {"from": "2020-01-01", "to": "2026-09-30"}


def test_switch_between_two_schemes_cancels_in_portfolio_flows():
    schemes = ledger(
        scheme("Fund A", "INF000000001", close="0", transactions=[
            ("2023-01-02", "PURCHASE", "10000", "100", "100"), ("2024-01-02", "SWITCH_OUT", "-11000", "-100", "110"),
        ]),
        scheme("Fund B", "INF000000002", close="110", transactions=[("2024-01-02", "SWITCH_IN", "11000", "110", "100")]),
    )
    navs = nav("INF000000002", "120", category=None)

    result = mf_returns.returns_from(schemes, navs, TODAY)

    # Pooled, the switch nets out: 10000 in on 2 Jan 2023, 13200 (110 x 120) on 10 Nov 2026.
    expected = mf_math.xirr([(date(2023, 1, 2), Decimal("-10000")), (date(2026, 11, 10), Decimal("13200"))])
    assert result["portfolio"]["xirr"] == pct(expected.rate)
    assert result["coverage"] == {"pct": 100.0, "excluded": [], "valueUnknown": 0}


def test_no_ledger_gives_the_hint():
    assert mf_returns.returns_from([], {}, TODAY) == {"available": False, "hint": mf_ledger.HINT}


# ── API ────────────────────────────────────────────────────────────────────────

@pytest.fixture
def detailed_pdf(monkeypatch):
    monkeypatch.setattr(cas.casparser, "read_cas_pdf", lambda file, password: statement())


def upload(client):
    return client.post("/api/cas", json={"password": "x", "pdfBase64": base64.b64encode(PDF).decode()})


def test_returns_requires_profile_and_unlock(client, signed_in, detailed_pdf):
    upload(client)
    unlock._unlocked.clear()
    assert client.get("/api/returns").json()["unlockRequired"] is True
    client.cookies.clear()
    assert client.get("/api/returns").status_code == 401


def test_returns_without_ledger_gives_hint(client, signed_in):
    assert client.get("/api/returns").json() == {"available": False, "hint": mf_ledger.HINT}


def test_returns_after_a_detailed_upload(client, signed_in, detailed_pdf):
    upload(client)
    amfi.store_navs(list(nav().values()))

    body = client.get("/api/returns").json()

    assert body["available"] is True
    assert [item["name"] for item in body["schemes"]] == ["HDFC Index Fund Nifty 50 Plan - Direct"]
    assert body["schemes"][0]["navSource"] == "amfi"


def test_returns_are_per_profile(client, detailed_pdf):
    from conftest import sign_up

    sign_up(client, "Asha")
    upload(client)
    sign_up(client, "Ravi")
    assert client.get("/api/returns").json()["available"] is False


def test_returns_need_no_consent(client, detailed_pdf):
    from conftest import sign_up

    from app import privacy
    from app.db import connect_db

    user = sign_up(client, "Asha")
    upload(client)
    with connect_db() as connection:
        connection.execute("UPDATE consents SET policy_version = 'old'")
    assert not privacy.has_current_consent(user["id"])
    assert client.get("/api/returns").json()["available"] is True


# ── Agent tool ─────────────────────────────────────────────────────────────────

import asyncio  # noqa: E402
import json  # noqa: E402

from app import agent, sealing, snapshots, tasks  # noqa: E402


def tool_names(client, user_id):
    user = users.get_user(user_id)
    user["keys"] = unlock.keys_for_session(user, client.cookies.get("indmoney_session"))
    return [tool.name for tool in snapshots.portfolio_context(user).local_tools], user


def test_tool_is_offered_only_with_a_ledger(client, signed_in, monkeypatch):
    monkeypatch.setattr(cas.casparser, "read_cas_pdf", lambda file, password: statement(cas_type="SUMMARY"))
    upload(client)
    names, _ = tool_names(client, signed_in["id"])
    assert "get_cas_holdings" in names and "get_mf_returns" not in names

    monkeypatch.setattr(cas.casparser, "read_cas_pdf", lambda file, password: statement())
    upload(client)
    names, user = tool_names(client, signed_in["id"])
    assert "get_mf_returns" in names

    user["keys"] = sealing.ProfileKeys()  # Locked: the ledger cannot be read, so no tool.
    assert "get_mf_returns" not in [tool.name for tool in snapshots.portfolio_context(user).local_tools]


def test_tool_payload_and_scheme_filter(client, signed_in, detailed_pdf):
    upload(client)
    amfi.store_navs(list(nav().values()))
    _, user = tool_names(client, signed_in["id"])
    tool = next(t for t in snapshots.portfolio_context(user).local_tools if t.name == "get_mf_returns")

    everything = json.loads(asyncio.run(tool.handler({})))
    one = json.loads(asyncio.run(tool.handler({"scheme": "nifty 50"})))
    none = json.loads(asyncio.run(tool.handler({"scheme": "INF000NOPE"})))

    assert everything["available"] is True and everything["portfolio"]["totalValue"] == 2600.0
    assert everything["schemes"][0]["name"] == "HDFC Index Fund Nifty 50 Plan - Direct"
    assert "folioKey" not in everything["schemes"][0]
    assert one["scheme"]["isin"] == "INF179K01VK6" and one["scheme"]["invested"] == 2500.13
    assert "error" in none


def test_tool_payload_stays_small_with_many_schemes(monkeypatch):
    many = ledger(*[scheme(f"Fund number {i} with a fairly long scheme name - Direct Growth", f"INF00000{i:04d}")
                    for i in range(60)])
    monkeypatch.setattr(mf_returns, "returns_for", lambda user_id, keys, today, **_: mf_returns.returns_from(many, {}, TODAY))

    payload = asyncio.run(snapshots.mf_returns_tool("u", None).handler({}))

    body = json.loads(payload)
    assert len(payload) < 5500
    assert body["omittedSchemes"] == 60 - len(body["schemes"]) > 0


def test_tool_counts_as_reading_account_data():
    assert tasks.is_account_tool("get_mf_returns", {"get_mf_returns"})


def test_the_agent_quotes_the_tool_figures(monkeypatch):
    from tests.test_agent_graph import reply, run
    from scripts import eval_workflows as ev

    figures = mf_returns.returns_from(ledger(scheme()), nav(), TODAY, benchmark_navs=NIFTY)
    monkeypatch.setattr(mf_returns, "returns_for", lambda user_id, keys, today, **_: figures)
    monkeypatch.setattr(ev, "build_context", lambda case: agent.PortfolioContext(
        local_tools=(snapshots.mf_returns_tool("u", None),), cached_mcp_tools=ev.TOOLS))
    xirr = figures["portfolio"]["xirr"]

    gap = figures["portfolio"]["benchmark"]["differenceInr"]
    assert gap is not None

    answer, events, payloads, _, _ = run(monkeypatch, "What is my mutual fund XIRR?", [
        reply(calls=[["get_mf_returns", {}]]),
        reply(f"Your mutual fund XIRR is {xirr}% on ₹2,600 (AMFI NAV of 10 Nov 2026), ₹{gap:,.2f} against the "
              "same flows in the Nifty 50 index fund. This is information, not advice.")])

    assert agent.MF_RETURNS_RULES in payloads[0]["messages"][0]["content"]
    assert next(e for e in events if e["kind"] == "validation" and e["check"] == "numbers")["outcome"] == "passed"
    assert str(xirr) in answer


# ── Final review fixes ─────────────────────────────────────────────────────────

def test_fully_redeemed_scheme_needs_no_nav():
    (item,) = ledger(scheme(close="0", isin=None, valuation=("30-Sep-2026", "0"), transactions=[
        ("2024-01-10", "PURCHASE", "10000", "100", "100"), ("2025-03-10", "REDEMPTION", "-15000", "-100", "150"),
    ]))

    result = mf_returns.returns_from([item], {}, TODAY)

    (row,) = result["schemes"]
    assert (row["status"], row["currentValue"], row["realisedGain"]) == ("ok", 0.0, 5000.0)
    assert result["portfolio"]["realisedGain"] == 5000.0 and result["portfolio"]["xirr"] is not None


def test_held_units_without_a_nav_are_no_nav_not_unreconciled():
    (item,) = ledger(scheme(isin=None, valuation=("30-Sep-2026", "0")))

    row = mf_returns.scheme_returns(item, {}, TODAY)

    assert row["status"] == "no_nav" and row["currentValue"] is None and "NAV" in row["reason"]


def test_unknown_values_make_coverage_unknown():
    schemes = ledger(scheme(), scheme("No NAV Fund", None, amfi="999", valuation=("30-Sep-2026", "0")))

    result = mf_returns.returns_from(schemes, nav(), TODAY)

    assert result["coverage"]["pct"] is None and result["coverage"]["valueUnknown"] == 1
    assert result["portfolio"]["totalValue"] == 2600.0


def test_tool_payload_stays_small_with_many_excluded_funds(monkeypatch):
    many = ledger(*[scheme(f"Fund number {i} with a fairly long scheme name - Direct Growth", f"INF00000{i:04d}", close="25")
                    for i in range(60)])
    monkeypatch.setattr(mf_returns, "returns_for", lambda user_id, keys, today, **_: mf_returns.returns_from(many, {}, TODAY))

    payload = asyncio.run(snapshots.mf_returns_tool("u", None).handler({}))

    body = json.loads(payload)
    assert len(payload) < 5500
    assert body["coverage"]["excludedCount"] == 60 and len(body["coverage"]["excluded"]) < 60


# ── Benchmarks ─────────────────────────────────────────────────────────────────

# Benchmark history covering the example scheme's flows (10 Jan 2024, 10 Jun 2024, 10 Mar 2025) and its NAV date.
HISTORY = [(date(2024, 1, 10), Decimal("100")), (date(2024, 6, 10), Decimal("120")),
           (date(2025, 3, 10), Decimal("110")), (date(2026, 11, 10), Decimal("130"))]
NIFTY = {"nifty50": HISTORY}


def round2(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def test_scheme_is_compared_with_its_category_benchmark():
    (item,) = ledger(scheme())

    result = mf_returns.returns_from([item], nav(), TODAY, benchmark_navs=NIFTY)

    expected = mf_math.replay(mf_math.fifo(item.transactions).cashflows, HISTORY, date(2026, 11, 10), Decimal("2600"))
    bench = result["schemes"][0]["benchmark"]
    assert (bench["id"], bench["label"], bench["status"], bench["reason"]) == ("nifty50", "Nifty 50", "ok", None)
    assert bench["value"] == round2(expected.value) and bench["differenceInr"] == round2(expected.difference)
    assert bench["xirr"] == pct(mf_math.xirr(expected.flows).rate) and bench["navDate"] == "2026-11-10"
    assert bench["absoluteReturnPct"] is None and bench["returnReason"] is None


def test_portfolio_uses_the_chosen_benchmark():
    schemes = ledger(scheme())
    histories = {**NIFTY, "midcap150": HISTORY}

    default = mf_returns.returns_from(schemes, nav(), TODAY, benchmark_navs=histories)
    midcap = mf_returns.returns_from(schemes, nav(), TODAY, benchmark="midcap150", benchmark_navs=histories)

    assert default["portfolio"]["benchmark"]["id"] == "nifty50" and default["benchmark"]["id"] == "nifty50"
    assert midcap["portfolio"]["benchmark"]["id"] == "midcap150" and midcap["portfolio"]["benchmark"]["status"] == "ok"
    assert midcap["benchmark"]["label"] == "Nifty Midcap 150"
    assert midcap["benchmark"]["schemeName"] == "Motilal Oswal Nifty Midcap 150 Index Fund - Direct Plan - Growth"
    assert midcap["benchmark"]["schemeCode"] == "147622" and midcap["benchmark"]["navDate"] == "2026-11-10"
    assert midcap["benchmark"]["note"] == benchmarks.INDEX_NOTE
    assert [o["id"] for o in midcap["benchmark"]["options"]] == list(benchmarks.BENCHMARK_IDS)
    assert midcap["schemes"][0]["benchmark"]["id"] == "nifty50"


def test_unknown_or_unseeded_benchmarks():
    schemes = ledger(scheme())
    plain = mf_returns.returns_from(schemes, nav(), TODAY)

    result = mf_returns.returns_from(schemes, nav(), TODAY, benchmark_navs={})

    for bench in (result["schemes"][0]["benchmark"], result["portfolio"]["benchmark"]):
        assert bench["status"] == "unavailable"
        assert bench["reason"] == "Benchmark NAV history has not been downloaded yet."
        assert bench["xirr"] is None and bench["differenceInr"] is None
    assert result["benchmark"]["navDate"] is None
    assert result["portfolio"]["xirr"] == plain["portfolio"]["xirr"] is not None
    with pytest.raises(ValueError):
        mf_returns.returns_from(schemes, nav(), TODAY, benchmark="sensex")


def test_excluded_and_unmapped_schemes_say_why():
    schemes = ledger(scheme(), scheme("Axis Bluechip", "INF846K01DP8", close="25"),
                     scheme("Some Hybrid Fund", "INF000000009"))
    navs = {**nav(), **nav("INF846K01DP8", "40"), **nav("INF000000009", "130", category="Hybrid Scheme - Aggressive Hybrid Fund")}

    result = mf_returns.returns_from(schemes, navs, TODAY, benchmark_navs=NIFTY)

    by_name = {item["name"]: item["benchmark"] for item in result["schemes"]}
    assert by_name["Axis Bluechip"]["status"] == "excluded"
    assert by_name["Axis Bluechip"]["reason"] == "Left out of the totals, so not compared."
    hybrid = by_name["Some Hybrid Fund"]
    assert (hybrid["id"], hybrid["label"], hybrid["status"]) == (None, None, "none")
    assert hybrid["reason"] == "Hybrid funds mix asset classes, so no single index fits."
    assert hybrid["xirr"] is None and hybrid["differenceInr"] is None


def test_fully_redeemed_scheme_ends_at_its_last_flow():
    (item,) = ledger(scheme(close="0", isin=None, valuation=("30-Sep-2026", "0"), transactions=[
        ("2024-01-10", "PURCHASE", "10000", "100", "100"), ("2025-03-10", "REDEMPTION", "-10500", "-100", "105"),
    ]))
    navs_by_isin = {}

    # No NAV row, so no category: compare with the portfolio benchmark only. 100 units at 100, 10500 at 110
    # sells 95.4545...; 4.5454... units x 110 = 500 left in the benchmark, against 0 in the fund.
    result = mf_returns.returns_from([item], navs_by_isin, TODAY, benchmark_navs=NIFTY)

    bench = result["portfolio"]["benchmark"]
    assert bench["status"] == "ok" and bench["navDate"] == "2025-03-10"
    assert bench["value"] == 500.0 and bench["differenceInr"] == -500.0


def test_returns_route_takes_a_benchmark(client, signed_in, detailed_pdf):
    upload(client)
    amfi.store_navs(list(nav().values()))
    benchmarks.store_navs("liquid", HISTORY)

    assert client.get("/api/returns").json()["benchmark"]["id"] == "nifty50"
    body = client.get("/api/returns?benchmark=liquid").json()
    assert body["benchmark"]["id"] == "liquid" and body["portfolio"]["benchmark"]["status"] == "ok"
    wrong = client.get("/api/returns?benchmark=sensex")
    assert wrong.status_code == 422
    assert wrong.json() == {"error": "Unknown benchmark.", "benchmarks": list(benchmarks.BENCHMARK_IDS)}


def test_tool_takes_a_benchmark(client, signed_in, detailed_pdf):
    upload(client)
    amfi.store_navs(list(nav().values()))
    benchmarks.store_navs("nifty50", HISTORY)
    benchmarks.store_navs("smallcap250", HISTORY)
    _, user = tool_names(client, signed_in["id"])
    tool = next(t for t in snapshots.portfolio_context(user).local_tools if t.name == "get_mf_returns")

    assert tool.input_schema["properties"]["benchmark"]["enum"] == list(benchmarks.BENCHMARK_IDS)
    default = json.loads(asyncio.run(tool.handler({})))
    small = json.loads(asyncio.run(tool.handler({"benchmark": "smallcap250"})))
    one = json.loads(asyncio.run(tool.handler({"scheme": "nifty 50", "benchmark": "smallcap250"})))
    wrong = json.loads(asyncio.run(tool.handler({"benchmark": "sensex"})))

    assert default["benchmark"]["id"] == "nifty50" and "options" not in default["benchmark"]
    assert default["schemes"][0]["benchmark"]["status"] == "ok"
    assert set(default["schemes"][0]["benchmark"]) == {"id", "xirr", "absoluteReturnPct", "differenceInr", "status", "reason"}
    assert small["portfolio"]["benchmark"]["id"] == "smallcap250" and small["benchmark"]["label"] == "Nifty Smallcap 250"
    assert one["scheme"]["benchmark"]["id"] == "nifty50" and one["benchmark"]["id"] == "smallcap250"
    assert wrong == {"error": "Unknown benchmark 'sensex'.", "benchmarks": list(benchmarks.BENCHMARK_IDS)}


def test_benchmark_payload_stays_small_with_many_schemes(monkeypatch):
    many = ledger(*[scheme(f"Fund number {i} with a fairly long scheme name - Direct Growth", f"INF00000{i:04d}")
                    for i in range(60)])
    navs = {k: v for i in range(60)
            for k, v in nav(f"INF00000{i:04d}", category="Equity Scheme - Large Cap Fund").items()}
    monkeypatch.setattr(mf_returns, "returns_for",
                        lambda user_id, keys, today, **_: mf_returns.returns_from(many, navs, TODAY, benchmark_navs=NIFTY))

    payload = asyncio.run(snapshots.mf_returns_tool("u", None).handler({}))

    body = json.loads(payload)
    assert len(payload) < snapshots.MF_RETURNS_MAX_CHARS
    assert body["schemes"] and body["schemes"][0]["benchmark"]["status"] == "ok"

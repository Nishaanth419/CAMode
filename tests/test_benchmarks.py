from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app import amfi, benchmarks, db, snapshots
from app.amfi import NavRow
from app.snapshots import SNAPSHOT_TIMEZONE


@pytest.fixture(autouse=True)
def fresh_state():
    benchmarks.reset_state()
    yield
    benchmarks.reset_state()


# ── The funds and the category mapping ─────────────────────────────────────────

def test_benchmarks_are_the_verified_funds():
    assert benchmarks.BENCHMARK_IDS == ("nifty50", "nifty500", "midcap150", "smallcap250", "liquid")
    assert [b.scheme_code for b in benchmarks.BENCHMARKS] == ["120716", "147625", "147622", "147623", "119800"]
    assert [b.isin for b in benchmarks.BENCHMARKS] == [
        "INF789F01XA0", "INF247L01957", "INF247L01916", "INF247L01932", "INF200K01UT4",
    ]
    assert all(b.scheme_name.endswith("Direct Plan - Growth") for b in benchmarks.BENCHMARKS)
    assert benchmarks.DEFAULT_BENCHMARK == "nifty50"
    assert benchmarks.get("midcap150").label == "Nifty Midcap 150"
    assert benchmarks.get("x") is None


@pytest.mark.parametrize(("category", "name", "expected"), [
    ("Equity Scheme - Large Cap Fund", "Some Bluechip Fund", "nifty50"),
    ("Equity Schemes - Large Cap Fund", "Some Bluechip Fund", "nifty50"),
    ("Equity Schemes - Flexi Cap Fund", "Some Flexi Cap Fund", "nifty50"),
    ("ELSS", "Some Tax Saver", "nifty50"),
    ("Equity Scheme - ELSS", "Some Tax Saver", "nifty50"),
    ("Equity Schemes - ELSS- Tax Saver Fund", "Some Tax Saver", "nifty50"),
    ("Equity Scheme - Focused Fund", "Some Focused Fund", "nifty50"),
    ("Equity Scheme - Value Fund", "Some Value Fund", "nifty50"),
    ("Equity Schemes - Contra Fund", "Some Contra Fund", "nifty50"),
    ("Equity Scheme - Dividend Yield Fund", "Some Dividend Yield Fund", "nifty50"),
    ("Equity Schemes - Multi Cap Fund", "Some Multi Cap Fund", "nifty500"),
    ("Equity Scheme - Large & Mid Cap Fund", "Some Emerging Bluechip", "nifty500"),
    ("Equity Scheme - Mid Cap Fund", "Some Mid Cap Fund", "midcap150"),
    ("Equity Schemes - Small Cap Fund", "Some Small Cap Fund", "smallcap250"),
    ("Debt Scheme - Liquid Fund", "Some Liquid Fund", "liquid"),
    ("Income/Debt Oriented Schemes - Overnight Fund", "Some Overnight Fund", "liquid"),
    ("Debt Scheme - Money Market Fund", "Some Money Market Fund", "liquid"),
    ("Other Scheme - Index Funds", "UTI Nifty 50 Index Fund - Direct Plan - Growth", "nifty50"),
    ("Other Scheme - Index Funds", "NAVI ELSS TAX SAVER NIFTY50 INDEX FUND - Direct Plan - GROWTH", "nifty50"),
    ("Index Funds - Equity Funds", "Some S&P BSE Sensex Index Fund - Direct Plan - Growth", "nifty50"),
    ("Other Scheme - Index Funds", "Axis Nifty 500 Index Fund - Direct Plan - Growth Option", "nifty500"),
    ("Other Scheme - Index Funds", "DSP Nifty Smallcap 250 Index Fund - Direct Plan - Growth", "smallcap250"),
    ("Other Scheme - Index Funds", "Kotak Nifty Midcap 150 Index Fund - Direct Plan - GROWTH", "midcap150"),
])
def test_category_mapping(category, name, expected):
    assert benchmarks.benchmark_for(category, name) == (expected, None)


@pytest.mark.parametrize(("category", "name", "reason"), [
    ("Hybrid Scheme - Aggressive Hybrid Fund", "Some Hybrid", "Hybrid funds mix asset classes, so no single index fits."),
    ("Debt Scheme - Gilt Fund", "Some Gilt",
     "Only liquid, overnight and money market debt funds have a benchmark here (a liquid fund)."),
    ("Income/Debt Oriented Schemes - Credit Risk Fund", "Some Credit Risk",
     "Only liquid, overnight and money market debt funds have a benchmark here (a liquid fund)."),
    ("Equity Scheme - Sectoral/ Thematic", "Some Banking Fund",
     "Sectoral and thematic funds have no broad index benchmark here."),
    ("Equity Schemes - Thematic Fund", "Some Theme Fund", "Sectoral and thematic funds have no broad index benchmark here."),
    ("Other Scheme - FoF Overseas", "Some US Fund", "Funds of funds, international and gold funds have no benchmark here."),
    ("Other Scheme - Gold ETF", "Some Gold ETF", "Funds of funds, international and gold funds have no benchmark here."),
    ("Other Scheme - FoF Domestic", "Some FoF", "Funds of funds, international and gold funds have no benchmark here."),
    (None, "Some Fund", "The fund's AMFI category is not known."),
    ("Solution Oriented Scheme - Retirement Fund", "Some Retirement Fund",
     "No benchmark is set for the AMFI category Solution Oriented Scheme - Retirement Fund."),
    ("Other Scheme - Index Funds", "Some Nifty Next 50 Index Fund", "This index fund tracks an index that has no benchmark here."),
    ("Other Scheme - Index Funds", "Some Nifty 50 Equal Weight Index Fund",
     "This index fund tracks an index that has no benchmark here."),
    ("Other Scheme - Index Funds", "Some Nifty Midcap 150 Momentum 50 Index Fund",
     "This index fund tracks an index that has no benchmark here."),
    ("Other Scheme - Index Funds", "Some Nifty Bank Index Fund", "This index fund tracks an index that has no benchmark here."),
])
def test_categories_without_a_benchmark_say_why(category, name, reason):
    assert benchmarks.benchmark_for(category, name) == (None, reason)


# ── NAV history ────────────────────────────────────────────────────────────────

def mfapi(code=120716, rows=None, status="SUCCESS"):
    return {"status": status, "meta": {"scheme_code": code}, "data": rows if rows is not None else [
        {"date": "12-11-2026", "nav": "178.25610"},
        {"date": "11-11-2026", "nav": "177.00000"},
    ]}


def test_parse_mfapi():
    payload = mfapi(rows=[
        {"date": "12-11-2026", "nav": "178.25610"},
        {"date": "bad", "nav": "1"},
        {"date": "11-11-2026", "nav": "0"},
        {"date": "10-11-2026", "nav": "N.A."},
        {"date": "02-01-2013", "nav": "37.40400"},
    ])

    assert benchmarks.parse_mfapi(payload, "120716") == [
        (date(2013, 1, 2), Decimal("37.40400")), (date(2026, 11, 12), Decimal("178.25610")),
    ]


def test_parse_mfapi_refuses_another_scheme_or_a_failure():
    with pytest.raises(ValueError):
        benchmarks.parse_mfapi(mfapi(code=147625), "120716")
    with pytest.raises(ValueError):
        benchmarks.parse_mfapi(mfapi(status="ERROR"), "120716")


def test_store_and_read_history():
    benchmarks.store_navs("nifty50", [(date(2026, 11, 12), Decimal("178")), (date(2026, 11, 10), Decimal("175"))])
    benchmarks.store_navs("nifty50", [(date(2026, 11, 12), Decimal("178.2561"))])

    assert benchmarks.nav_history("nifty50") == [
        (date(2026, 11, 10), Decimal("175")), (date(2026, 11, 12), Decimal("178.2561")),
    ]
    assert benchmarks.latest_nav_date("nifty50") == date(2026, 11, 12)
    assert benchmarks.nav_history("liquid") == [] and benchmarks.latest_nav_date("liquid") is None


NOW = datetime(2026, 11, 13, 10, 0, tzinfo=SNAPSHOT_TIMEZONE)


def fake_fetch(calls, failing=()):
    async def fetch(code):
        calls.append(code)
        if code in failing:
            raise RuntimeError("mfapi down")
        return mfapi(code=int(code), rows=[{"date": "12-11-2026", "nav": "100"}])
    return fetch


def test_seed_fetches_only_missing_or_stale_benchmarks(monkeypatch):
    monkeypatch.setenv("MF_DATA_ENABLED", "true")
    calls = []

    stored = asyncio.run(benchmarks.seed_if_needed(NOW, fetch=fake_fetch(calls)))

    assert stored == list(benchmarks.BENCHMARK_IDS)
    assert calls == ["120716", "147625", "147622", "147623", "119800"]
    # Seeded rows are dated 12 Nov (one day old); make the liquid fund's history 8 days old.
    with db.connect_db() as connection:
        connection.execute("DELETE FROM benchmark_navs WHERE benchmark_id = 'liquid'")
    benchmarks.store_navs("liquid", [(NOW.date() - timedelta(days=8), Decimal("101"))])
    calls.clear()

    assert asyncio.run(benchmarks.seed_if_needed(NOW, fetch=fake_fetch(calls))) == ["liquid"]
    assert calls == ["119800"]


def test_seed_failure_waits_an_hour_per_benchmark(monkeypatch):
    monkeypatch.setenv("MF_DATA_ENABLED", "true")
    calls = []
    fetch = fake_fetch(calls, failing={"147625"})

    assert asyncio.run(benchmarks.seed_if_needed(NOW, fetch=fetch)) == ["nifty50", "midcap150", "smallcap250", "liquid"]
    calls.clear()
    assert asyncio.run(benchmarks.seed_if_needed(NOW + timedelta(minutes=59), fetch=fetch)) == []
    assert calls == []
    asyncio.run(benchmarks.seed_if_needed(NOW + timedelta(minutes=60), fetch=fetch))
    assert calls == ["147625"]


def test_seed_is_off_when_disabled():
    async def fetch(code):
        raise AssertionError("must not download")

    assert asyncio.run(benchmarks.seed_if_needed(NOW, fetch=fetch)) == []


def test_default_fetch_asks_mfapi_for_the_scheme_code_only(monkeypatch):
    import httpx

    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers["User-Agent"]))
        return httpx.Response(200, json=mfapi(code=147623))

    payload = asyncio.run(benchmarks._download("147623", transport=httpx.MockTransport(handler)))

    assert payload["meta"]["scheme_code"] == 147623
    assert seen == [("https://api.mfapi.in/mf/147623", amfi.USER_AGENT)]
    monkeypatch.setenv("MFAPI_URL", "https://mirror.example/")
    asyncio.run(benchmarks._download("147623", transport=httpx.MockTransport(handler)))
    assert seen[-1][0] == "https://mirror.example/mf/147623"


def test_append_from_amfi_adds_todays_nav_to_seeded_benchmarks():
    amfi.store_navs([
        NavRow("INF789F01XA0", "120716", "UTI Nifty 50 Index Fund - Direct Plan - Growth", "Other Scheme - Index Funds",
               Decimal("178.2561"), date(2026, 11, 12)),
        NavRow("INF247L01957", "147625", "Motilal Oswal Nifty 500 Index Fund - Direct Plan - Growth",
               "Other Scheme - Index Funds", Decimal("27.9307"), date(2026, 11, 12)),
    ])
    benchmarks.store_navs("nifty50", [(date(2026, 11, 11), Decimal("177"))])

    assert benchmarks.append_from_amfi() == 1
    assert benchmarks.nav_history("nifty50")[-1] == (date(2026, 11, 12), Decimal("178.2561"))
    assert benchmarks.nav_history("nifty500") == []


def test_refresh_seeds_then_appends(monkeypatch):
    monkeypatch.setenv("MF_DATA_ENABLED", "true")
    amfi.store_navs([NavRow("INF789F01XA0", "120716", "UTI", "Other Scheme - Index Funds", Decimal("180"), date(2026, 11, 13))])

    asyncio.run(benchmarks.refresh(NOW, fetch=fake_fetch([])))

    assert benchmarks.nav_history("nifty50") == [(date(2026, 11, 12), Decimal("100")), (date(2026, 11, 13), Decimal("180"))]


def test_scheduler_refreshes_benchmarks(monkeypatch):
    seen = []
    moment = datetime(2026, 11, 13, 9, 0, tzinfo=SNAPSHOT_TIMEZONE)

    async def fake_refresh_due_users(*, mcp_url, now=None):
        return 0

    async def fake_refresh(now):
        seen.append(now)
        stop.set()
        raise RuntimeError("logged, not raised")

    monkeypatch.setattr(snapshots, "refresh_due_users", fake_refresh_due_users)
    monkeypatch.setattr(benchmarks, "refresh", fake_refresh)
    stop = asyncio.Event()

    async def run():
        await asyncio.wait_for(
            snapshots.run_scheduler(stop, mcp_url="https://mcp", poll_seconds=0.01, clock=lambda: moment), timeout=2,
        )

    asyncio.run(run())
    assert seen == [moment]

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app import amfi
from app.snapshots import SNAPSHOT_TIMEZONE

# AMFI's NAVAll.txt layout: a header, section headings naming the category, AMC lines, data lines.
NAV_FILE = """Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date

Open Ended Schemes(Equity Scheme - Large Cap Fund)

HDFC Mutual Fund

120716;INF179K01VK6;-;HDFC Index Fund Nifty 50 Plan - Direct;250.1234;10-Nov-2026
120717;INF179K01AA1;INF179K01AB9;HDFC Top 100 Fund - IDCW;45.5;10-Nov-2026
120718;INF179K01NA1;-;HDFC Closed Plan;N.A.;10-Nov-2026
120719;INF179K01NA2;-;HDFC Empty Plan;;10-Nov-2026
120720;INF179K01NA3;-;HDFC Odd Plan;abc;10-Nov-2026

Open Ended Schemes(Debt Scheme - Liquid Fund)

Axis Mutual Fund

119800;INF846K01CH7;-;Axis Liquid Fund - Direct Growth;2800.5521;09-Nov-2026
"""


@pytest.fixture(autouse=True)
def fresh_state():
    amfi.reset_state()
    yield
    amfi.reset_state()


def by_isin(rows):
    return {row.isin: row for row in rows}


def test_parse_reads_category_from_the_section_heading():
    rows = by_isin(amfi.parse_nav_file(NAV_FILE))

    assert rows["INF179K01VK6"].amfi_category == "Equity Scheme - Large Cap Fund"
    assert rows["INF846K01CH7"].amfi_category == "Debt Scheme - Liquid Fund"
    assert rows["INF846K01CH7"].scheme_code == "119800"
    assert rows["INF846K01CH7"].nav_date == date(2026, 11, 9)
    assert all("Mutual Fund" not in row.scheme_name for row in rows.values())


def test_both_isin_columns_map_to_the_row():
    rows = by_isin(amfi.parse_nav_file(NAV_FILE))

    assert rows["INF179K01AA1"].nav == rows["INF179K01AB9"].nav == Decimal("45.5")
    assert "-" not in rows


def test_na_and_non_numeric_navs_are_skipped():
    rows = by_isin(amfi.parse_nav_file(NAV_FILE))

    assert not {"INF179K01NA1", "INF179K01NA2", "INF179K01NA3"} & rows.keys()
    assert len(rows) == 4


def test_store_and_lookup():
    assert amfi.store_navs(amfi.parse_nav_file(NAV_FILE)) == 4

    found = amfi.navs_for(["INF179K01VK6", "UNKNOWN"])

    assert list(found) == ["INF179K01VK6"]
    assert found["INF179K01VK6"].nav == Decimal("250.1234")
    assert found["INF179K01VK6"].nav_date == date(2026, 11, 10)
    assert amfi.navs_for([]) == {}


def test_a_newer_download_replaces_the_row():
    amfi.store_navs(amfi.parse_nav_file(NAV_FILE))
    amfi.store_navs(amfi.parse_nav_file(NAV_FILE.replace("250.1234;10-Nov-2026", "251;11-Nov-2026")))

    assert amfi.navs_for(["INF179K01VK6"])["INF179K01VK6"].nav == Decimal("251")


def test_refresh_runs_once_a_day_and_retries_hourly(monkeypatch):
    monkeypatch.setenv("MF_DATA_ENABLED", "true")
    calls = []

    async def fetch():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("AMFI down")
        return NAV_FILE

    start = datetime(2026, 11, 11, 10, 0, tzinfo=SNAPSHOT_TIMEZONE)
    run = lambda now: asyncio.run(amfi.refresh_if_due(now, fetch=fetch))  # noqa: E731

    assert run(start) is False
    assert run(start + timedelta(minutes=30)) is False and len(calls) == 1
    assert run(start + timedelta(hours=1)) is True and len(calls) == 2
    assert run(start + timedelta(hours=5)) is False and len(calls) == 2
    assert run(start + timedelta(days=1)) is True and len(calls) == 3
    assert amfi.navs_for(["INF179K01VK6"])


def test_refresh_is_off_when_disabled():
    async def fetch():
        raise AssertionError("must not download")

    assert asyncio.run(amfi.refresh_if_due(datetime.now(SNAPSHOT_TIMEZONE), fetch=fetch)) is False


# The live layout since 2026: Plan and Option columns before the NAV.
EIGHT_COLUMNS = """Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date

Open Ended Schemes(Equity Scheme - Small Cap Fund)

Axis Mutual Fund

135762;INF846K01WO1;-;Axis Small Cap Fund;Direct Plan;Growth Option;31.0915;11-Nov-2026
135763;INF846K01WS2;INF846K01WQ6;Axis Small Cap Fund;Direct Plan;IDCW Option;N.A.;11-Nov-2026
"""


def test_parse_reads_the_eight_column_layout():
    rows = by_isin(amfi.parse_nav_file(EIGHT_COLUMNS))

    assert list(rows) == ["INF846K01WO1"]
    row = rows["INF846K01WO1"]
    assert (row.nav, row.nav_date, row.amfi_category) == (Decimal("31.0915"), date(2026, 11, 11), "Equity Scheme - Small Cap Fund")
    assert row.scheme_name == "Axis Small Cap Fund - Direct Plan - Growth Option"


def test_download_follows_amfis_redirect_to_its_portal():
    import httpx

    def handler(request):
        if request.url.host == "www.amfiindia.com":
            return httpx.Response(302, headers={"Location": "https://portal.amfiindia.com/spages/NAVAll.txt"})
        return httpx.Response(200, text=EIGHT_COLUMNS)

    assert asyncio.run(amfi._download(transport=httpx.MockTransport(handler))) == EIGHT_COLUMNS


def test_download_refuses_a_redirect_away_from_amfi():
    import httpx

    def handler(request):
        if request.url.host == "www.amfiindia.com":
            return httpx.Response(302, headers={"Location": "https://example.com/NAVAll.txt"})
        return httpx.Response(200, text=EIGHT_COLUMNS)

    with pytest.raises(ValueError, match="amfiindia.com"):
        asyncio.run(amfi._download(transport=httpx.MockTransport(handler)))

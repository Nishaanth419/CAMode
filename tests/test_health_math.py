from __future__ import annotations

import json
from decimal import Decimal

from app import health_math, portfolio_math
from tests.fixtures import indmoney_market as market
from tests.fixtures import indmoney_sample


def metrics():
    return portfolio_math.compute_metrics(indmoney_sample.results())


def test_concentration_uses_total_current_value_and_names_the_denominator():
    result = health_math.concentration(metrics())
    assert result["denominatorInr"] == 1250000.0 and "net worth before liabilities" in result["denominator"]
    assert [(h["name"], h["sharePct"]) for h in result["holdingsAboveThreshold"]] == [
        ("Parag Parikh Flexi Cap Fund", 30.0), ("Tata Consultancy Services", 15.2), ("Infosys", 14.4), ("HDFC Bank", 10.4)]
    # Exactly 10% is not above the threshold.
    assert "HDFC Index Fund Nifty 50" not in {h["name"] for h in result["holdingsAboveThreshold"]}
    assert health_math.concentration(metrics(), Decimal(20))["holdingsAboveThreshold"][0]["sharePct"] == 30.0


def test_fund_details_layout_and_benchmark_rule():
    funds = health_math.parse_fund_details(json.dumps(market.fund_details(market.FUNDS)))
    lagging = health_math.fund_check("120503", 375000.0, funds["120503"])
    assert (lagging["status"], lagging["period"], lagging["gapVsBenchmarkPp"]) == ("underperforming", "3Y", -2.1)
    assert lagging["benchmark"] == "Nifty 500 TR INR" and lagging["gapVsCategoryPp"] == -0.01
    index = health_math.fund_check("118989", 125000.0, funds["118989"])
    assert index["status"] == "in_line" and index["gapVsBenchmarkPp"] == -0.3


def test_a_fund_is_never_judged_without_a_shared_period():
    spec = {**market.FUNDS["120503"], "returns": {"5Y": 10.0}, "benchmarkReturns": {"3Y": 12.0}}
    funds = health_math.parse_fund_details(json.dumps(market.fund_details({"120503": spec})))
    result = health_math.fund_check("120503", 1.0, funds["120503"])
    assert result["status"] == "not_checked" and "same period" in result["reason"]
    assert health_math.fund_check("999", 1.0, None)["status"] == "not_checked"


def test_one_year_is_used_when_three_years_are_missing():
    spec = {**market.FUNDS["120503"], "returns": {"1Y": 5.0}, "benchmarkReturns": {"1Y": 8.0, "3Y": 12.0}}
    funds = health_math.parse_fund_details(json.dumps(market.fund_details({"120503": spec})))
    result = health_math.fund_check("120503", 1.0, funds["120503"])
    assert (result["period"], result["status"], result["gapVsBenchmarkPp"]) == ("1Y", "underperforming", -3.0)


def test_sector_lookthrough_reads_the_displayed_percentages():
    funds = health_math.parse_fund_details(json.dumps(market.fund_details(market.FUNDS)))
    holdings = portfolio_math.all_holdings(indmoney_sample.results())
    result = health_math.sector_lookthrough(holdings, funds, 1250000.0)
    by_name = {item["sector"]: item for item in result["sectors"]}
    # 375000 × 90% × 30% + 125000 × 100% × 35%
    assert by_name["Financial Services"]["exposureInr"] == 145000.0 and by_name["Financial Services"]["sharePct"] == 11.6
    assert by_name["Tech"]["sharePct"] == 4.78
    assert result["directStocksUnclassifiedInr"] == 600000.0 and len(result["caveats"]) == 2
    # The fixture's percValue is rounded (12.5 → 12); the displayed "perc" text is what counts.
    assert funds["120503"]["sectors"]["Tech"] == Decimal("12.5")


def test_unrealised_is_labelled_as_today_not_a_trend():
    result = health_math.unrealised(metrics())
    assert (result["gainInr"], result["gainPct"], result["holdingsWithoutCost"]) == (140000.0, 16.87, 1)
    assert "not a trend" in result["label"]


def test_trend_comes_only_from_saved_snapshots():
    trend = health_math.history_trend(json.dumps(market.history([("2026-01-01", None), ("2026-02-01", 1000000), ("2026-10-27", 1150000)])))
    assert (trend["from"], trend["changeInr"], trend["changePct"]) == ("2026-02-01", 150000.0, 15.0)
    assert health_math.history_trend(json.dumps(market.history([("2026-10-27", 1)])))["available"] is False
    assert health_math.history_trend(None)["available"] is False

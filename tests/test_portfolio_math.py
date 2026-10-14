from __future__ import annotations

import json

import pytest

from app import portfolio_math, snapshots
from fixtures import indmoney_sample


@pytest.fixture
def metrics():
    return portfolio_math.compute_metrics(indmoney_sample.results())


def test_totals_come_from_the_snapshot(metrics):
    assert metrics["totals"] == {
        "net_worth_inr": 1200000.0, "current_value_inr": 1250000.0, "invested_inr": 980000.0, "liabilities_inr": 50000.0,
    }


def test_pnl_leaves_out_holdings_without_a_cost_basis(metrics):
    # HDFC Bank (linked broker, invested_amount 0) would otherwise show a 100% gain.
    assert metrics["pnl"] == {
        "basis": "holdings with a reported invested amount", "holdings_counted": 5,
        "invested_inr": 830000.0, "current_value_inr": 970000.0, "pnl_inr": 140000.0, "pnl_pct": 16.87,
        "holdings_excluded": 1,
    }
    hdfc = next(item for item in metrics["top_holdings"] if item["code"] == "HDFCBANK")
    assert hdfc["invested_inr"] is None and hdfc["pnl_inr"] is None and hdfc["pnl_pct"] is None
    assert any("₹1,30,000.00 have no invested amount" in note for note in metrics["data_notes"])


def test_broker_split_and_portfolio_shares(metrics):
    assert [(row["broker"], row["holdings"], row["current_value_inr"], row["share_of_listed_holdings_pct"])
            for row in metrics["by_broker"]] == [
        ("Mutual funds (no broker listed)", 2, 500000.0, 45.45),
        ("INDmoney", 3, 470000.0, 42.73),
        ("Zerodha", 1, 130000.0, 11.82),
    ]
    infosys = next(item for item in metrics["top_holdings"] if item["code"] == "INFY")
    assert infosys["share_of_portfolio_pct"] == 14.4  # 1,80,000 of 12,50,000, not INDmoney's holding_percent


def test_concentration(metrics):
    assert metrics["concentration"] == {
        "holdings_count": 6, "largest_holding": "Parag Parikh Flexi Cap Fund", "largest_holding_pct": 30.0,
        "top_5_pct": 80.0, "top_10_pct": 88.0,
    }


def test_usd_is_reported_separately(metrics):
    assert metrics["usd"] == {"US_STOCK": {"current_value_usd": 1200.0, "invested_usd": 960.0}}
    apple = next(item for item in metrics["top_holdings"] if item["code"] == "AAPL")
    assert apple["current_value_inr"] == 100000.0 and apple["current_value_usd"] == 1200.0
    assert metrics["totals"]["current_value_inr"] == 1250000.0  # USD never added in


def test_asset_type_and_class_splits(metrics):
    assert [(row["asset_type"], row["share_of_portfolio_pct"]) for row in metrics["by_asset_type"]] == [
        ("MF", 40.0), ("IND_STOCK", 40.0), ("EPF", 12.0), ("US_STOCK", 8.0),
    ]
    assert metrics["by_asset_class"] == [{"assetclass_l2": "Equity", "current_value_inr": 1100000.0, "share_pct": 88.0}]
    assert metrics["by_market_cap_direct_stocks"][0]["share_pct"] == 100.0


def test_notes_flag_missing_types_and_mismatched_totals():
    results = indmoney_sample.results()
    mismatched = indmoney_sample.holdings(indmoney_sample.MF)
    mismatched["asset_summary"]["total_value"] = 600000
    results["networth_holdings:MF"] = json.dumps(mismatched)

    notes = portfolio_math.compute_metrics(results)["data_notes"]

    assert any(note.startswith("Holdings were not fetched for: EPF") for note in notes)
    assert "MF holdings add up to ₹5,00,000.00 but INDmoney reports ₹6,00,000.00." in notes


def test_top_holdings_and_all_holdings():
    rows = [indmoney_sample._row(f"S{i}", f"Stock {i}", "IND_STOCK", 1000, 1000 + i, 1, "INDmoney") for i in range(15)]
    results = {"networth_holdings:IND_STOCK": json.dumps(indmoney_sample.holdings(rows))}

    top = portfolio_math.compute_metrics(results)
    every = portfolio_math.compute_metrics(results, include_all_holdings=True)

    assert len(top["top_holdings"]) == 10 and top["top_holdings"][0]["code"] == "S14"
    assert "all_holdings" not in top and len(every["all_holdings"]) == 15
    assert "top_holdings" not in every and "by_asset_class" not in every
    assert set(every["all_holdings"][0]) <= set(portfolio_math.COMPACT_HOLDING_FIELDS.values())


@pytest.mark.parametrize("results", [{}, {"networth_snapshot": "not json"}, {"networth_holdings:MF": "{}"}])
def test_missing_or_malformed_data_yields_empty_figures(results):
    metrics = portfolio_math.compute_metrics(results)
    assert metrics["totals"]["net_worth_inr"] is None
    assert metrics["top_holdings"] == [] and metrics["pnl"]["pnl_pct"] is None


@pytest.mark.parametrize(("value", "text"), [
    (0, "₹0.00"), (999, "₹999.00"), (1000, "₹1,000.00"), (130000, "₹1,30,000.00"),
    (1234567.5, "₹12,34,567.50"), (123456789, "₹12,34,56,789.00"), (-50000.456, "-₹50,000.46"),
])
def test_inr_uses_indian_grouping(value, text):
    assert portfolio_math.inr(value) == text


def test_holdings_types_to_fetch():
    assert snapshots.holdings_types_to_fetch(json.dumps(indmoney_sample.SNAPSHOT)) == ["MF", "IND_STOCK", "US_STOCK", "EPF"]
    unknown_names = json.dumps({"investments": [{"asset_type": "Stocks", "current_value": 10}]})
    assert snapshots.holdings_types_to_fetch(unknown_names) == list(snapshots.HOLDINGS_ASSET_TYPES)
    assert snapshots.holdings_types_to_fetch(None) == list(snapshots.HOLDINGS_ASSET_TYPES)


def test_old_snapshots_fall_back_to_raw_text():
    snapshot = {"asOfDate": "2026-10-01", "capturedAt": 0, "results": {"networth_snapshot": "Net worth ₹10"}, "errors": {}}

    payload = snapshots._snapshot_payload(snapshot)

    assert payload["raw"] == {"networth_snapshot": "Net worth ₹10"}

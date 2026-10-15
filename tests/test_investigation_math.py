from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app import investigation_math as im
from tests.fixtures import indmoney_sample as sample
from tests.fixtures.snapshot_pairs import baseline_rows, build_results, later_rows, row, stored


def _pair():
    previous = im.normalize_snapshot(stored("2026-10-13", build_results(baseline_rows())))
    current = im.normalize_snapshot(stored("2026-10-14", build_results(later_rows())))
    return previous, current


# ── calculate_change ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(("previous", "current", "absolute", "pct", "direction"), [
    ("100", "110", "10", "10", "up"),
    ("100", "90", "-10", "-10", "down"),
    ("100", "100", "0", "0", "unchanged"),
    ("0", "50", "50", None, "up"),                 # no percentage against a zero base
    ("-100", "-50", "50", "50", "up"),             # a rise from a negative base is a positive percentage
    ("0.1", "0.3", "0.2", "200", "up"),            # exact decimals, no float drift
])
def test_calculate_change_values_and_direction(previous, current, absolute, pct, direction):
    change = im.calculate_change(Decimal(previous), Decimal(current))

    assert change.absolute == Decimal(absolute)
    assert change.percentage == (Decimal(pct) if pct is not None else None)
    assert change.direction == direction


@pytest.mark.parametrize(("previous", "current"), [(None, Decimal(5)), (Decimal(5), None), (None, None)])
def test_calculate_change_with_missing_side_is_unknown_not_zero(previous, current):
    change = im.calculate_change(previous, current)

    assert change.absolute is None
    assert change.percentage is None
    assert change.direction == "unknown"


def test_money_rounds_half_up_only_at_output():
    assert im.money(Decimal("1.005")) == 1.01
    assert im.money(Decimal("-2.675")) == -2.68
    assert im.money(None) is None


@pytest.mark.parametrize(("raw", "expected"), [
    (1250.5, Decimal("1250.5")), ("1,25,000.75", Decimal("125000.75")), ("₹ 99", Decimal(99)),
    (0, Decimal(0)), (None, None), ("", None), ("n/a", None), (True, None), ("NaN", None),
])
def test_to_decimal_distinguishes_zero_from_missing(raw, expected):
    assert im.to_decimal(raw) == expected


# ── normalize_snapshot ─────────────────────────────────────────────────────────

def test_normalize_sample_keeps_provenance_totals_and_coverage():
    snapshot = im.normalize_snapshot(stored("2026-10-14", sample.results(), networth=1200000))

    assert snapshot.snapshot_id.startswith("snapshot:2026-10-14:")
    assert snapshot.account_scope == im.ACCOUNT_SCOPE
    assert snapshot.currency == "INR"
    assert snapshot.captured_at.tzinfo is not None
    assert snapshot.total_current_value_inr == Decimal(1250000)
    assert snapshot.total_networth_inr == Decimal(1200000)
    assert snapshot.liabilities_inr == Decimal(50000)
    assert snapshot.holdings_fetched == {"IND_STOCK", "MF", "US_STOCK"}
    assert snapshot.held_asset_types == {"IND_STOCK", "MF", "US_STOCK", "EPF"}
    assert len(snapshot.holdings) == 6


def test_normalize_keeps_zero_invested_as_zero_but_without_cost_basis():
    snapshot = im.normalize_snapshot(stored("2026-10-14", sample.results()))
    hdfc = next(item for item in snapshot.holdings if item.code == "HDFCBANK")

    assert hdfc.invested_inr == Decimal(0)
    assert hdfc.has_cost_basis is False
    assert hdfc.broker == "Zerodha"


def test_normalize_keeps_usd_separate_from_inr():
    snapshot = im.normalize_snapshot(stored("2026-10-14", sample.results()))
    apple = next(item for item in snapshot.holdings if item.code == "AAPL")

    assert apple.market_value_inr == Decimal(100000)
    assert apple.market_value_usd == Decimal("1200.0")


def test_normalize_treats_missing_market_value_as_unknown():
    results = build_results({"IND_STOCK": [row("INFY", "Infosys", "IND_STOCK", 100, 120, 1)]})
    parsed = json.loads(results["networth_holdings:IND_STOCK"])
    del parsed["holdings"][0]["market_value"]
    results["networth_holdings:IND_STOCK"] = json.dumps(parsed)

    holding = im.normalize_snapshot(stored("2026-10-14", results)).holdings[0]

    assert holding.market_value_inr is None


def test_normalize_merges_duplicate_rows_and_reports_them():
    rows = [row("INFY", "Infosys", "IND_STOCK", 100, 120, 1), row("INFY", "Infosys", "IND_STOCK", 200, 240, 2)]
    snapshot = im.normalize_snapshot(stored("2026-10-14", build_results({"IND_STOCK": rows})))

    assert len(snapshot.holdings) == 1
    merged = snapshot.holdings[0]
    assert merged.market_value_inr == Decimal(360)
    assert merged.units == Decimal(3)
    assert merged.unit_price_inr == Decimal(120)
    assert merged.merged_rows == 2
    assert snapshot.duplicate_holding_ids == ("IND_STOCK|INFY|INDmoney",)


def test_same_code_at_two_brokers_stays_two_holdings():
    rows = [row("INFY", "Infosys", "IND_STOCK", 100, 120, 1),
            row("INFY", "Infosys", "IND_STOCK", 100, 120, 1, "Zerodha")]

    snapshot = im.normalize_snapshot(stored("2026-10-14", build_results({"IND_STOCK": rows})))

    assert len(snapshot.holdings) == 2
    assert snapshot.duplicate_holding_ids == ()


def test_unreadable_holdings_response_is_unavailable_not_empty():
    results = {**build_results(baseline_rows()), "networth_holdings:MF": "<html>gateway error</html>"}

    snapshot = im.normalize_snapshot(stored("2026-10-14", results, errors={"networth_holdings:BOND": "timeout"}))

    assert "MF" not in snapshot.holdings_fetched
    assert snapshot.holdings_unavailable["MF"].startswith("INDmoney's holdings response")
    assert snapshot.holdings_unavailable["BOND"] == "timeout"
    assert snapshot.unreadable == ("networth_holdings:MF",)


def test_unreadable_summary_is_flagged():
    snapshot = im.normalize_snapshot(stored("2026-10-14", {"networth_snapshot": "not json"}))

    assert snapshot.has_summary is False
    assert snapshot.total_networth_inr is None
    assert snapshot.unreadable == ("networth_snapshot",)


def test_normalize_requires_identity_fields():
    with pytest.raises(ValueError, match="asOfDate"):
        im.normalize_snapshot({"results": {}})


# ── calculate_portfolio_change ────────────────────────────────────────────────

def test_portfolio_change_totals_and_market_effect():
    change = im.calculate_portfolio_change(*_pair())

    assert change.net_worth.previous == Decimal(810000)
    assert change.net_worth.current == Decimal(889000)
    assert change.net_worth.absolute == Decimal(79000)
    assert round(change.net_worth.percentage, 2) == Decimal("9.75")
    assert change.invested.absolute == Decimal(70000)
    assert change.market_effect_estimate_inr == Decimal(9000)
    assert change.comparable_asset_types == ("IND_STOCK", "MF")
    assert change.not_comparable_asset_types == ()


def test_holding_changes_split_into_price_and_unit_effects():
    change = im.calculate_portfolio_change(*_pair())
    by_name = {item.name: item for item in change.holdings}

    flexi = by_name["Parag Parikh Flexi Cap Fund"]
    assert flexi.status == "held"
    assert flexi.value.absolute == Decimal(54000)
    assert flexi.price_effect_inr == Decimal(8000)
    assert flexi.quantity_effect_inr == Decimal(46000)
    assert flexi.price_effect_inr + flexi.quantity_effect_inr == flexi.value.absolute
    assert by_name["Infosys"].value.absolute == Decimal(10000)
    assert by_name["Tata Consultancy Services"].value.direction == "down"
    assert by_name["HDFC Bank"].value.direction == "unchanged"


def test_new_holding_has_zero_previous_value_and_no_percentage():
    change = im.calculate_portfolio_change(*_pair())
    added = next(item for item in change.holdings if item.name == "HDFC Index Fund Nifty 50")

    assert added.status == "new"
    assert added.value.previous == Decimal(0)
    assert added.value.absolute == Decimal(25000)
    assert added.value.percentage is None


def test_holdings_are_sorted_by_size_of_move():
    change = im.calculate_portfolio_change(*_pair())

    assert [item.name for item in change.holdings][:2] == ["Parag Parikh Flexi Cap Fund", "HDFC Index Fund Nifty 50"]


def test_exited_holding_is_reported_as_exited():
    later = later_rows()
    later["IND_STOCK"] = [item for item in later["IND_STOCK"] if item["investment_code"] != "TCS"]
    previous = im.normalize_snapshot(stored("2026-10-13", build_results(baseline_rows())))
    current = im.normalize_snapshot(stored("2026-10-14", build_results(later)))

    exited = next(item for item in im.compare_holdings(previous, current) if item.name == "Tata Consultancy Services")

    assert exited.status == "exited"
    assert exited.value.current == Decimal(0)
    assert exited.value.absolute == Decimal(-200000)


def test_changed_identifier_is_matched_by_name_and_flagged():
    later = later_rows()
    later["IND_STOCK"][0] = row("INFY.NS", "Infosys", "IND_STOCK", 150000, 180000, 100)
    previous = im.normalize_snapshot(stored("2026-10-13", build_results(baseline_rows())))
    current = im.normalize_snapshot(stored("2026-10-14", build_results(later)))

    infosys = [item for item in im.compare_holdings(previous, current) if item.name == "Infosys"]

    assert len(infosys) == 1
    assert infosys[0].status == "held"
    assert infosys[0].identifier_changed is True
    assert infosys[0].value.absolute == Decimal(10000)


def test_asset_type_missing_from_one_snapshot_is_not_comparable_not_sold():
    earlier = build_results(baseline_rows())
    del earlier["networth_holdings:MF"]
    previous = im.normalize_snapshot(stored("2026-10-13", earlier, errors={"networth_holdings:MF": "HTTP 500"}))
    current = im.normalize_snapshot(stored("2026-10-14", build_results(later_rows())))

    change = im.calculate_portfolio_change(previous, current)
    mf = [item for item in change.holdings if item.asset_type == "MF"]

    assert change.not_comparable_asset_types == ("MF",)
    assert {item.status for item in mf} == {"not_comparable"}
    assert all(item.value.absolute is None for item in mf)
    assert next(item for item in change.asset_types if item.asset_type == "MF").value.absolute == Decimal(79000)


# ── calculate_allocation ───────────────────────────────────────────────────────

def test_allocation_uses_same_snapshot_total_current_value():
    snapshot = im.normalize_snapshot(stored("2026-10-14", sample.results()))

    allocation = im.calculate_allocation(snapshot)

    assert allocation.basis == "total_current_value"
    assert allocation.denominator_inr == Decimal(1250000)
    assert allocation.net_worth_inr == Decimal(1200000)
    shares = {item.label: item.share_pct for item in allocation.by_asset_type}
    assert shares["MF"] == Decimal(40)
    assert sum(shares.values()) == Decimal(100)
    assert allocation.unclassified_inr == Decimal(0)


def test_allocation_falls_back_to_holdings_and_names_the_basis():
    results = build_results(baseline_rows())
    results["networth_snapshot"] = json.dumps({})

    allocation = im.calculate_allocation(im.normalize_snapshot(stored("2026-10-14", results)))

    assert allocation.basis == "sum_of_holdings"
    assert allocation.denominator_inr == Decimal(860000)


def test_allocation_without_any_total_is_unavailable():
    allocation = im.calculate_allocation(im.normalize_snapshot(stored("2026-10-14", {})))

    assert allocation.basis == "unavailable"
    assert allocation.denominator_inr is None
    assert allocation.by_asset_type == ()


def test_asset_type_new_in_latest_summary_changes_from_zero():
    later = later_rows()
    later["BOND"] = [row("GOI2030", "GOI 2030", "BOND", 50000, 50000, 50, None)]
    previous = im.normalize_snapshot(stored("2026-10-13", build_results(baseline_rows())))
    current = im.normalize_snapshot(stored("2026-10-14", build_results(later)))

    bond = next(item for item in im.calculate_portfolio_change(previous, current).asset_types if item.asset_type == "BOND")

    assert bond.value.previous == Decimal(0)
    assert bond.value.absolute == Decimal(50000)
    assert bond.value.percentage is None


def test_row_asset_type_label_does_not_override_response_type():
    rows = [row("NIFTYBEES", "Nippon Nifty BeES", "ETF", 100, 120, 1)]
    snapshot = im.normalize_snapshot(stored("2026-10-14", build_results({"IND_STOCK": rows})))

    assert snapshot.holdings[0].asset_type == "IND_STOCK"
    assert snapshot.holdings_of("IND_STOCK") == [snapshot.holdings[0]]

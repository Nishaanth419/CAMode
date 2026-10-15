from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from app import investigation_checks as ic
from app import investigation_math as im
from tests.fixtures.snapshot_pairs import (
    baseline_rows,
    build_results,
    captured_at,
    later_rows,
    mutate_summary,
    row,
    stored,
)

SETTINGS = ic.CheckSettings()
NOW = captured_at("2026-10-14") + timedelta(hours=2)


def _normalize(as_of, results, **kwargs):
    return im.normalize_snapshot(stored(as_of, results, **kwargs))


def _run(previous, current, *, now=NOW, settings=SETTINGS):
    change = im.calculate_portfolio_change(previous, current)
    allocation = im.calculate_allocation(current)
    return {check.check: check for check in ic.run_checks(previous, current, change, allocation, now=now, settings=settings)}


def _consistent_pair():
    return (_normalize("2026-10-13", build_results(baseline_rows())),
            _normalize("2026-10-14", build_results(later_rows())))


def test_consistent_pair_passes_every_check():
    checks = _run(*_consistent_pair())

    assert {name: check.status for name, check in checks.items()} == {
        "snapshot_identity": "passed", "freshness": "passed", "totals_consistency": "passed",
        "holdings_reconciliation": "passed", "holdings_coverage": "passed", "allocation_denominator": "passed",
        "percentage_validity": "passed", "change_arithmetic": "passed", "currency_consistency": "passed",
    }


def test_check_results_serialise_with_evidence_and_next_step():
    previous, current = _consistent_pair()
    stale = _run(previous, current, now=NOW + timedelta(days=3))["freshness"].to_dict()

    assert stale["status"] == "warning"
    assert stale["evidence"]["staleAfterHours"] == 36
    assert stale["nextStep"]


# ── Identity and freshness ─────────────────────────────────────────────────────

def test_different_account_scopes_are_not_compared():
    previous, current = _consistent_pair()

    check = ic.snapshot_identity(replace(previous, account_scope="cas:nsdl"), current)

    assert check.status == "failed"
    assert "different account scopes" in check.summary


def test_baseline_not_older_than_latest_fails_identity():
    previous, current = _consistent_pair()

    check = ic.snapshot_identity(current, current)

    assert check.status == "failed"
    assert "not older" in check.summary


def test_snapshot_without_readable_summary_is_not_comparable():
    previous = _normalize("2026-10-13", {"networth_snapshot": "garbage"})
    _, current = _consistent_pair()

    assert ic.snapshot_identity(previous, current).status == "failed"


@pytest.mark.parametrize(("age_hours", "status"), [(1, "passed"), (36, "passed"), (37, "warning")])
def test_freshness_flags_stale_latest_snapshot(age_hours, status):
    previous, current = _consistent_pair()

    check = ic.freshness(previous, current, current.captured_at + timedelta(hours=age_hours), SETTINGS)

    assert check.status == status
    assert check.evidence["daysBetweenSnapshots"] == 1.0


def test_missing_history_is_a_failure_not_no_change():
    _, current = _consistent_pair()

    check = ic.history_unavailable(current, None)

    assert check.status == "failed"
    assert "nothing earlier" in check.summary
    assert "no change" not in check.summary.lower()


# ── Totals and holdings ────────────────────────────────────────────────────────

def test_conflicting_net_worth_is_reported_with_both_values():
    previous, _ = _consistent_pair()
    current = _normalize("2026-10-14", mutate_summary(build_results(later_rows()), total_networth=950000))

    check = _run(previous, current)["totals_consistency"]

    assert check.status == "failed"
    conflict = check.evidence["conflicts"][0]
    assert conflict["what"] == "current value minus liabilities vs net worth"
    assert (conflict["left"], conflict["right"]) == (889000.0, 950000.0)


def test_saved_net_worth_disagreeing_with_snapshot_is_a_conflict():
    previous, _ = _consistent_pair()
    current = _normalize("2026-10-14", build_results(later_rows()), networth=123.0)

    assert _run(previous, current)["totals_consistency"].status == "failed"


def test_missing_totals_are_not_checked_rather_than_passed():
    results = build_results(later_rows())
    summary = json.loads(results["networth_snapshot"])
    del summary["liabilities"]
    previous, _ = _consistent_pair()
    current = _normalize("2026-10-14", {**results, "networth_snapshot": json.dumps(summary)})

    assert _run(previous, current)["totals_consistency"].status == "not_checked"


def test_holdings_not_adding_up_to_asset_summary_is_a_warning():
    results = build_results(later_rows())
    parsed = json.loads(results["networth_holdings:MF"])
    parsed["asset_summary"]["total_value"] = 500000
    previous, _ = _consistent_pair()
    current = _normalize("2026-10-14", {**results, "networth_holdings:MF": json.dumps(parsed)})

    check = _run(previous, current)["holdings_reconciliation"]

    assert check.status == "warning"
    assert check.evidence["mismatches"][0]["what"] == "MF holdings vs asset summary"


@pytest.mark.parametrize(("left", "right", "agrees"), [
    ("1000000", "1000999", True),     # ₹999 on ₹10 lakh is within 0.5%
    ("1000000", "1006000", False),    # ₹6,000 is not
    ("100", "100.99", True),          # within the ₹1 floor
    ("100", "101.50", False),
])
def test_tolerance_uses_larger_of_rupee_floor_and_percentage(left, right, agrees):
    assert SETTINGS.agrees(Decimal(left), Decimal(right)) is agrees


def test_settings_from_env_reads_and_rejects_bad_values(monkeypatch):
    monkeypatch.setenv("INVESTIGATION_TOLERANCE_PCT", "0.1")
    monkeypatch.setenv("INVESTIGATION_TOLERANCE_INR", "-5")
    monkeypatch.setenv("INVESTIGATION_STALE_HOURS", "soon")

    settings = ic.settings_from_env()

    assert settings.tolerance_pct == Decimal("0.1")
    assert settings.tolerance_inr == Decimal("1")
    assert settings.stale_after_hours == 36


# ── Coverage, denominators, percentages ────────────────────────────────────────

def test_asset_type_not_saved_in_one_snapshot_is_a_coverage_warning():
    earlier = build_results(baseline_rows())
    del earlier["networth_holdings:MF"]
    previous = _normalize("2026-10-13", earlier, errors={"networth_holdings:MF": "HTTP 500"})
    _, current = _consistent_pair()

    check = _run(previous, current)["holdings_coverage"]

    assert check.status == "warning"
    assert check.evidence["notComparableAssetTypes"] == ["MF"]
    assert check.evidence["unavailable"]["previous"] == {"MF": "HTTP 500"}


def test_holding_percent_against_asset_type_total_is_flagged():
    """The plan's case: 1.22% matches ₹5,531.29 of a ₹4,54,764.74 MF total, not the ₹9,76,403 portfolio."""
    mf = [row("100", "Small fund", "MF", 5000, 5531.29, 100, None, holding_percent=1.22),
          row("200", "Large fund", "MF", 400000, 449233.45, 1000, None, holding_percent=98.78)]
    stocks = [row("INFY", "Infosys", "IND_STOCK", 400000, 571638.26, 300)]
    current = _normalize("2026-10-14", build_results({"MF": mf, "IND_STOCK": stocks}, liabilities=50000))

    allocation = im.calculate_allocation(current)
    check = ic.allocation_denominator(current, allocation)

    assert allocation.denominator_inr == Decimal("1026403.00")
    assert check.status == "warning"
    assert check.evidence["indmoneyHoldingPercentMatches"]["assetType"] == 2
    assert "not a share of the whole portfolio" in check.summary
    assert check.evidence["netWorthInr"] == 976403.0


def test_holding_percent_matching_portfolio_share_passes():
    rows = [row("INFY", "Infosys", "IND_STOCK", 100, 600, 1, holding_percent=60),
            row("TCS", "TCS", "IND_STOCK", 100, 400, 1, holding_percent=40)]
    current = _normalize("2026-10-14", build_results({"IND_STOCK": rows}, liabilities=0))

    check = ic.allocation_denominator(current, im.calculate_allocation(current))

    assert check.status == "passed"
    assert check.evidence["indmoneyHoldingPercentMatches"]["portfolio"] == 2


def test_negative_asset_value_fails_percentage_validity():
    results = mutate_summary(build_results(later_rows()), investments=[
        {"asset_type": "MF", "current_value": 1000000}, {"asset_type": "IND_STOCK", "current_value": -70000}])
    current = _normalize("2026-10-14", results)

    check = ic.percentage_validity(im.calculate_allocation(current), SETTINGS)

    assert check.status == "failed"


def test_value_outside_any_asset_type_is_reported_as_unclassified():
    results = mutate_summary(build_results(later_rows()), total_current_value=1039000, total_networth=989000)
    current = _normalize("2026-10-14", results)

    check = ic.percentage_validity(im.calculate_allocation(current), SETTINGS)

    assert check.status == "warning"
    assert check.evidence["unclassifiedInr"] == 100000.0


# ── Change arithmetic and currency ─────────────────────────────────────────────

def test_holding_changes_that_do_not_explain_type_change_are_a_warning():
    later = mutate_summary(build_results(later_rows()), investments=[
        {"asset_type": "IND_STOCK", "invested_value": 350000, "current_value": 500000},
        {"asset_type": "MF", "invested_value": 370000, "current_value": 489000}],
        total_current_value=989000, total_networth=939000)
    previous, _ = _consistent_pair()
    current = _normalize("2026-10-14", later)

    check = _run(previous, current)["change_arithmetic"]

    assert check.status == "warning"
    assert check.evidence["residuals"][0]["what"] == "MF holding changes vs asset-type change"


def test_usd_without_inr_value_fails_currency_check():
    us_row = row("AAPL", "Apple", "US_STOCK", 80000, 100000, 5, current_value_usd=1200.0, invested_value_usd=960.0)
    results = build_results({"US_STOCK": [us_row]})
    parsed = json.loads(results["networth_holdings:US_STOCK"])
    del parsed["holdings"][0]["market_value"]
    results["networth_holdings:US_STOCK"] = json.dumps(parsed)
    snapshot = _normalize("2026-10-14", results)

    check = ic.currency_consistency(snapshot, snapshot)

    assert check.status == "failed"
    assert check.evidence["usdWithoutInr"] == ["Apple", "Apple"]


def test_usd_alongside_inr_passes_and_says_it_is_kept_separate():
    us_row = row("AAPL", "Apple", "US_STOCK", 80000, 100000, 5, current_value_usd=1200.0, invested_value_usd=960.0)
    snapshot = _normalize("2026-10-14", build_results({"US_STOCK": [us_row]}))

    check = ic.currency_consistency(snapshot, snapshot)

    assert check.status == "passed"
    assert "never added to INR" in check.summary

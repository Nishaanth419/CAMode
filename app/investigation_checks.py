"""Consistency checks that decide whether two snapshots can be compared and trusted.

Each check returns a ``CheckResult`` naming the check, its status, the evidence it used
and what to do next. Statuses: ``passed``; ``warning`` (usable, but the user must be
told); ``failed`` (conflicting or unusable data; findings that depend on it are not
verified); ``not_checked`` (the data needed for the check is missing).

Tolerance. INDmoney reports amounts to the paisa, but its summaries are computed on
its side and can differ from the sum of the rows it returns by rounding. Two amounts
agree when they differ by at most ``max(INVESTIGATION_TOLERANCE_INR,
INVESTIGATION_TOLERANCE_PCT% of the larger amount)``, by default ₹1 or 0.5%.
Percentages agree within ``PERCENT_POINT_TOLERANCE`` percentage points, since INDmoney
rounds them to two decimals.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal

from .investigation_math import (
    Allocation,
    PortfolioChange,
    PortfolioSnapshot,
    money,
    percent,
)

logger = logging.getLogger("ca_mode.investigation_checks")

CheckStatus = Literal["passed", "warning", "failed", "not_checked"]
PERCENT_POINT_TOLERANCE: Final = Decimal("0.05")
_ZERO: Final = Decimal(0)


@dataclass(frozen=True, slots=True)
class CheckSettings:
    """Tolerances for the checks; see the module docstring for their meaning."""

    tolerance_pct: Decimal = Decimal("0.5")
    tolerance_inr: Decimal = Decimal("1")
    stale_after_hours: int = 36

    def allowed_gap(self, scale: Decimal) -> Decimal:
        """The largest difference tolerated between amounts of about ``scale`` rupees."""
        return max(self.tolerance_inr, abs(scale) * self.tolerance_pct / 100)

    def agrees(self, left: Decimal, right: Decimal) -> bool:
        """Whether two amounts match within the configured tolerance."""
        return abs(left - right) <= self.allowed_gap(max(abs(left), abs(right)))

    def describe(self) -> dict[str, Any]:
        return {
            "amountTolerance": f"max(₹{self.tolerance_inr}, {self.tolerance_pct}% of the larger amount)",
            "percentPointTolerance": str(PERCENT_POINT_TOLERANCE),
            "staleAfterHours": self.stale_after_hours,
        }


def _env_decimal(name: str, default: str) -> Decimal:
    raw = os.getenv(name, default)
    try:
        value = Decimal(raw)
    except InvalidOperation:
        value = Decimal(-1)
    if not value.is_finite() or value < 0:
        logger.warning("invalid_investigation_setting", extra={"setting": name})
        return Decimal(default)
    return value


def settings_from_env() -> CheckSettings:
    """Read ``INVESTIGATION_TOLERANCE_PCT``, ``INVESTIGATION_TOLERANCE_INR`` and ``INVESTIGATION_STALE_HOURS``."""
    try:
        stale = max(1, int(os.getenv("INVESTIGATION_STALE_HOURS", "36")))
    except ValueError:
        logger.warning("invalid_investigation_setting", extra={"setting": "INVESTIGATION_STALE_HOURS"})
        stale = 36
    return CheckSettings(
        tolerance_pct=_env_decimal("INVESTIGATION_TOLERANCE_PCT", "0.5"),
        tolerance_inr=_env_decimal("INVESTIGATION_TOLERANCE_INR", "1"),
        stale_after_hours=stale,
    )


@dataclass(frozen=True, slots=True)
class CheckResult:
    check: str
    status: CheckStatus
    summary: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    next_step: str | None = None

    def to_dict(self) -> dict[str, Any]:
        item: dict[str, Any] = {"check": self.check, "status": self.status, "summary": self.summary,
                                "evidence": dict(self.evidence)}
        if self.next_step:
            item["nextStep"] = self.next_step
        return item


def _ref(snapshot: PortfolioSnapshot) -> dict[str, Any]:
    return {"snapshotId": snapshot.snapshot_id, "asOfDate": snapshot.as_of_date,
            "capturedAt": snapshot.captured_at.isoformat(timespec="minutes")}


def _gap(label: str, left: Decimal, right: Decimal, snapshot: PortfolioSnapshot) -> dict[str, Any]:
    return {"snapshotId": snapshot.snapshot_id, "what": label, "left": money(left), "right": money(right),
            "differenceInr": money(left - right)}


# ── Individual checks ──────────────────────────────────────────────────────────

def history_unavailable(current: PortfolioSnapshot | None, baseline_date: str | None) -> CheckResult:
    """The result when there is no earlier snapshot to compare with (never reported as "no change")."""
    if current is None:
        summary = "No saved snapshot exists for this profile yet."
    elif baseline_date:
        summary = f"No saved snapshot exists on or before {baseline_date} that is older than the latest one."
    else:
        summary = "Only one snapshot is saved, so there is nothing earlier to compare it with."
    return CheckResult(
        "history_available", "failed", summary,
        {"latest": _ref(current) if current else None, "baselineDate": baseline_date},
        "Snapshots are saved each morning at the scheduled time; refresh the snapshot or try again after the next one.",
    )


def snapshot_identity(previous: PortfolioSnapshot, current: PortfolioSnapshot) -> CheckResult:
    """Same account scope, same valuation currency, both summaries readable, and ordered in time."""
    evidence = {"previous": _ref(previous), "current": _ref(current),
                "scopes": [previous.account_scope, current.account_scope],
                "currencies": [previous.currency, current.currency]}
    problems = []
    if previous.account_scope != current.account_scope:
        problems.append("the snapshots come from different account scopes")
    if previous.currency != current.currency:
        problems.append("the snapshots use different valuation currencies")
    if previous.as_of_date >= current.as_of_date:
        problems.append("the baseline is not older than the latest snapshot")
    for snapshot in (previous, current):
        if not snapshot.has_summary:
            problems.append(f"the {snapshot.as_of_date} snapshot has no readable net-worth summary")
    if problems:
        return CheckResult("snapshot_identity", "failed", "Not comparable: " + "; ".join(problems) + ".", evidence,
                           "Refresh the snapshot, or choose a baseline date with a complete snapshot.")
    return CheckResult("snapshot_identity", "passed",
                       f"Both snapshots are INDmoney net-worth snapshots in {current.currency} for this profile.", evidence)


def freshness(previous: PortfolioSnapshot, current: PortfolioSnapshot, now: datetime, settings: CheckSettings) -> CheckResult:
    age_hours = Decimal((now - current.captured_at).total_seconds()) / 3600
    gap_days = (current.captured_at - previous.captured_at).total_seconds() / 86400
    evidence = {"latestCapturedAt": current.captured_at.isoformat(timespec="minutes"),
                "latestAgeHours": float(round(age_hours, 1)), "daysBetweenSnapshots": round(gap_days, 2),
                "staleAfterHours": settings.stale_after_hours}
    if age_hours > settings.stale_after_hours:
        return CheckResult("freshness", "warning",
                           f"The latest snapshot is {round(age_hours)} hours old, so it may not reflect today's values.",
                           evidence, "Refresh the snapshot for current figures.")
    return CheckResult("freshness", "passed", f"The latest snapshot is {round(age_hours, 1)} hours old.", evidence)


def _totals_gaps(snapshot: PortfolioSnapshot, settings: CheckSettings) -> tuple[list[dict[str, Any]], list[str]]:
    gaps: list[dict[str, Any]] = []
    missing: list[str] = []
    total = snapshot.total_current_value_inr
    summed = sum(snapshot.asset_type_values_inr.values(), _ZERO)
    if total is None or not snapshot.asset_type_values_inr:
        missing.append(f"{snapshot.as_of_date}: total or per-asset-type values")
    elif not settings.agrees(summed, total):
        gaps.append(_gap("sum of asset types vs total current value", summed, total, snapshot))
    if snapshot.total_networth_inr is None or total is None or snapshot.liabilities_inr is None:
        missing.append(f"{snapshot.as_of_date}: net worth, current value or liabilities")
    elif not settings.agrees(total - snapshot.liabilities_inr, snapshot.total_networth_inr):
        gaps.append(_gap("current value minus liabilities vs net worth",
                         total - snapshot.liabilities_inr, snapshot.total_networth_inr, snapshot))
    stored = snapshot.stored_networth_inr
    if stored is not None and snapshot.total_networth_inr is not None and not settings.agrees(stored, snapshot.total_networth_inr):
        gaps.append(_gap("saved net worth vs net worth in the snapshot", stored, snapshot.total_networth_inr, snapshot))
    return gaps, missing


def totals_consistency(previous: PortfolioSnapshot, current: PortfolioSnapshot, settings: CheckSettings) -> CheckResult:
    """Conflicting totals inside a snapshot are reported with both values; neither is chosen silently."""
    gaps: list[dict[str, Any]] = []
    missing: list[str] = []
    for snapshot in (previous, current):
        snapshot_gaps, snapshot_missing = _totals_gaps(snapshot, settings)
        gaps += snapshot_gaps
        missing += snapshot_missing
    evidence = {"conflicts": gaps, "missing": missing, "tolerance": settings.describe()["amountTolerance"]}
    if gaps:
        return CheckResult("totals_consistency", "failed",
                           f"{len(gaps)} total(s) in the snapshots disagree with each other.", evidence,
                           "Treat the totals as unverified; refresh the snapshot and compare again.")
    if missing:
        return CheckResult("totals_consistency", "not_checked",
                           "Some totals were not reported, so they could not be cross-checked.", evidence)
    return CheckResult("totals_consistency", "passed",
                       "In both snapshots, asset types add up to the total and net worth equals value minus liabilities.",
                       evidence)


def _holdings_gaps(snapshot: PortfolioSnapshot, settings: CheckSettings) -> tuple[list[dict[str, Any]], int]:
    gaps: list[dict[str, Any]] = []
    missing_values = 0
    for asset_type in sorted(snapshot.holdings_fetched):
        rows = snapshot.holdings_of(asset_type)
        missing_values += sum(1 for row in rows if row.market_value_inr is None)
        summed = sum((row.market_value_inr or _ZERO for row in rows), _ZERO)
        for label, reported in (("asset summary", snapshot.asset_type_reported_totals_inr.get(asset_type)),
                                ("net-worth summary", snapshot.asset_type_values_inr.get(asset_type))):
            if reported is not None and not settings.agrees(summed, reported):
                gaps.append(_gap(f"{asset_type} holdings vs {label}", summed, reported, snapshot))
    return gaps, missing_values


def holdings_reconciliation(previous: PortfolioSnapshot, current: PortfolioSnapshot, settings: CheckSettings) -> CheckResult:
    """Holdings of each fetched asset type add up to INDmoney's reported totals for that type."""
    if not previous.holdings_fetched and not current.holdings_fetched:
        return CheckResult("holdings_reconciliation", "not_checked", "No holdings were saved in either snapshot.", {})
    gaps: list[dict[str, Any]] = []
    missing_values = 0
    for snapshot in (previous, current):
        snapshot_gaps, snapshot_missing = _holdings_gaps(snapshot, settings)
        gaps += snapshot_gaps
        missing_values += snapshot_missing
    evidence = {"mismatches": gaps, "holdingsWithoutValue": missing_values,
                "tolerance": settings.describe()["amountTolerance"]}
    if gaps or missing_values:
        parts = [f"{len(gaps)} asset-type total(s) do not match their holdings"] if gaps else []
        parts += [f"{missing_values} holding(s) have no reported value"] if missing_values else []
        return CheckResult("holdings_reconciliation", "warning", "; ".join(parts).capitalize() + ".", evidence,
                           "Per-holding contributions may not add up to the asset-type change.")
    return CheckResult("holdings_reconciliation", "passed",
                       "Holdings add up to INDmoney's totals for every fetched asset type.", evidence)


def _asset_type_total(snapshot: PortfolioSnapshot, asset_type: str) -> Decimal | None:
    reported = snapshot.asset_type_reported_totals_inr.get(asset_type)
    return reported if reported is not None else sum(
        (row.market_value_inr or _ZERO for row in snapshot.holdings_of(asset_type)), _ZERO) or None


def _holding_percent_bases(snapshot: PortfolioSnapshot, denominator: Decimal | None) -> dict[str, int]:
    """Count which total INDmoney's ``holding_percent`` matches for each holding that reports one."""
    counts = {"assetType": 0, "portfolio": 0, "neither": 0, "zeroForNonZeroHolding": 0}
    for row in snapshot.holdings:
        value, reported = row.market_value_inr, row.holding_percent_reported
        if value is None or reported is None:
            continue
        if reported == 0:
            counts["zeroForNonZeroHolding"] += int(value > 0)
            continue
        type_share = percent(value, _asset_type_total(snapshot, row.asset_type))
        portfolio_share = percent(value, denominator)
        if portfolio_share is not None and abs(portfolio_share - reported) <= PERCENT_POINT_TOLERANCE:
            counts["portfolio"] += 1
        elif type_share is not None and abs(type_share - reported) <= PERCENT_POINT_TOLERANCE:
            counts["assetType"] += 1
        else:
            counts["neither"] += 1
    return counts


def allocation_denominator(current: PortfolioSnapshot, allocation: Allocation) -> CheckResult:
    """Shares use the same snapshot's gross total, and INDmoney's own percentages are labelled by basis."""
    bases = _holding_percent_bases(current, allocation.denominator_inr)
    evidence = {
        "snapshotId": current.snapshot_id,
        "denominatorInr": money(allocation.denominator_inr), "denominatorBasis": allocation.basis,
        "netWorthInr": money(allocation.net_worth_inr), "liabilitiesInr": money(current.liabilities_inr),
        "explanation": "Shares are of gross current value (before liabilities); net worth is current value minus "
                       "liabilities, so it is a different, smaller total.",
        "indmoneyHoldingPercentMatches": bases,
    }
    if allocation.basis == "unavailable":
        return CheckResult("allocation_denominator", "not_checked", "No total is available to measure shares against.", evidence)
    if bases["assetType"] or bases["neither"]:
        return CheckResult(
            "allocation_denominator", "warning",
            f"INDmoney's holding_percent for {bases['assetType'] + bases['neither']} holding(s) is not a share of the "
            "whole portfolio (it matches the holding's asset-type total or neither). Do not present it as a portfolio share.",
            evidence, "Use the shares computed here against the snapshot's own total.")
    if allocation.basis != "total_current_value":
        return CheckResult("allocation_denominator", "warning",
                           f"INDmoney reported no total current value; shares use the {allocation.basis.replace('_', ' ')}.",
                           evidence)
    return CheckResult("allocation_denominator", "passed",
                       "Shares are measured against the same snapshot's total current value.", evidence)


def percentage_validity(allocation: Allocation, settings: CheckSettings) -> CheckResult:
    shares = [row.share_pct for row in allocation.by_asset_type if row.share_pct is not None]
    unclassified_share = percent(allocation.unclassified_inr, allocation.denominator_inr) or _ZERO
    total_share = sum(shares, _ZERO) + unclassified_share
    evidence = {"sumOfSharesPct": float(round(total_share, 2)), "unclassifiedInr": money(allocation.unclassified_inr),
                "unclassifiedPct": float(round(unclassified_share, 2))}
    if not shares:
        return CheckResult("percentage_validity", "not_checked", "No asset-type shares could be computed.", evidence)
    if any(share < 0 or share > 100 for share in shares) or any(row.value_inr < 0 for row in allocation.by_asset_type):
        return CheckResult("percentage_validity", "failed", "An asset-type value or share is negative or above 100%.",
                           evidence, "Treat the allocation as unverified.")
    if allocation.denominator_inr and abs(allocation.unclassified_inr) > settings.allowed_gap(allocation.denominator_inr):
        return CheckResult("percentage_validity", "warning",
                           f"{float(round(unclassified_share, 2))}% of the total is not assigned to any asset type.",
                           evidence)
    return CheckResult("percentage_validity", "passed", "Asset-type shares are between 0% and 100% and add up to 100%.",
                       evidence)


def change_arithmetic(change: PortfolioChange, settings: CheckSettings) -> CheckResult:
    """The total change agrees with the asset-type changes, and holdings explain each comparable type's change."""
    residuals: list[dict[str, Any]] = []
    type_changes = [item.value.absolute for item in change.asset_types]
    if change.current_value.absolute is not None and type_changes and None not in type_changes:
        summed = sum((value for value in type_changes if value is not None), _ZERO)
        if not settings.agrees(summed, change.current_value.absolute):
            residuals.append({"what": "asset-type changes vs total change", "left": money(summed),
                              "right": money(change.current_value.absolute)})
    for item in change.asset_types:
        if not item.holdings_comparable or item.value.absolute is None:
            continue
        held = [h.value.absolute for h in change.holdings if h.asset_type == item.asset_type and h.status != "not_comparable"]
        if None in held:
            continue
        summed = sum((value for value in held if value is not None), _ZERO)
        if not settings.agrees(summed, item.value.absolute):
            residuals.append({"what": f"{item.asset_type} holding changes vs asset-type change",
                              "left": money(summed), "right": money(item.value.absolute)})
    evidence = {"residuals": residuals, "percentageDenominator": "each value in the earlier snapshot",
                "netWorthPrevious": money(change.net_worth.previous), "currentValuePrevious": money(change.current_value.previous)}
    if change.current_value.absolute is None and change.net_worth.absolute is None:
        return CheckResult("change_arithmetic", "not_checked", "Neither snapshot pair reports comparable totals.", evidence)
    if residuals:
        return CheckResult("change_arithmetic", "warning",
                           f"{len(residuals)} change(s) are not fully explained by their parts.", evidence,
                           "Contributions are shown, but part of the change is unexplained by the saved holdings.")
    return CheckResult("change_arithmetic", "passed",
                       "The total change equals the sum of asset-type changes, and holdings explain each comparable type.",
                       evidence)


def currency_consistency(previous: PortfolioSnapshot, current: PortfolioSnapshot) -> CheckResult:
    usd_rows = [row for snapshot in (previous, current) for row in snapshot.holdings if row.market_value_usd is not None]
    usd_only = [row.name for row in usd_rows if row.market_value_inr is None]
    evidence = {"valuationCurrency": current.currency, "holdingsWithUsd": len(usd_rows), "usdWithoutInr": usd_only[:10]}
    if usd_only:
        return CheckResult("currency_consistency", "failed",
                           f"{len(usd_only)} holding(s) report USD but no INR value, so they cannot be included in INR totals.",
                           evidence, "Their change is shown as unavailable.")
    summary = "All amounts are in INR." if not usd_rows else (
        "All sums use INR; USD amounts on US holdings are kept separate and never added to INR.")
    return CheckResult("currency_consistency", "passed", summary, evidence)


def holdings_coverage(previous: PortfolioSnapshot, current: PortfolioSnapshot, change: PortfolioChange) -> CheckResult:
    """Asset types fetched in only one snapshot, duplicate rows, and changed identifiers."""
    renamed = [item.name for item in change.holdings if item.identifier_changed]
    evidence = {
        "comparableAssetTypes": list(change.comparable_asset_types),
        "notComparableAssetTypes": list(change.not_comparable_asset_types),
        "unavailable": {"previous": dict(previous.holdings_unavailable), "current": dict(current.holdings_unavailable)},
        "duplicateHoldingIds": sorted(set(previous.duplicate_holding_ids) | set(current.duplicate_holding_ids)),
        "identifierChanged": renamed,
    }
    notes = []
    if change.not_comparable_asset_types:
        notes.append("holdings for " + ", ".join(change.not_comparable_asset_types)
                     + " were not saved in both snapshots, so their per-holding changes are not compared")
    if evidence["duplicateHoldingIds"]:
        notes.append(f"{len(evidence['duplicateHoldingIds'])} holding(s) were reported more than once and were merged")
    if renamed:
        notes.append(f"{len(renamed)} holding(s) changed identifier and were matched by name")
    if notes:
        return CheckResult("holdings_coverage", "warning", "; ".join(notes).capitalize() + ".", evidence,
                           "Refresh the snapshot to fetch every asset type's holdings." if change.not_comparable_asset_types else None)
    return CheckResult("holdings_coverage", "passed", "Every held asset type's holdings were saved in both snapshots.", evidence)


def run_checks(
    previous: PortfolioSnapshot,
    current: PortfolioSnapshot,
    change: PortfolioChange,
    allocation: Allocation,
    *,
    now: datetime,
    settings: CheckSettings,
) -> list[CheckResult]:
    """Run every check for a comparison, in a stable order."""
    return [
        snapshot_identity(previous, current),
        freshness(previous, current, now, settings),
        totals_consistency(previous, current, settings),
        holdings_reconciliation(previous, current, settings),
        holdings_coverage(previous, current, change),
        allocation_denominator(current, allocation),
        percentage_validity(allocation, settings),
        change_arithmetic(change, settings),
        currency_consistency(previous, current),
    ]

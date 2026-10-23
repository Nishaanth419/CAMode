"""Normalised portfolio snapshots and deterministic change and allocation calculations.

No model, database or network access: every function takes stored snapshot data and
returns exact ``Decimal`` results. Rounding happens only when results are serialised
(``money()`` / ``percent()``), never between steps.

Conventions, decided here once so every caller agrees:

- Missing is not zero. A value INDmoney did not report is ``None``; ``0`` means it
  reported zero. A holding whose asset type was not fetched in one snapshot is
  ``not_comparable``, never "sold".
- A holding is identified by asset type, INDmoney code (the normalised name when the
  code is missing) and broker, so the same stock held at two brokers stays two
  holdings. Rows sharing an identity within one snapshot are merged and reported as
  duplicates. A holding whose code changed between snapshots is matched by asset
  type, name and broker and flagged ``identifier_changed``.
- Percentages are ``None`` when their denominator is zero or missing.
- INR and USD are never added together; INR is the valuation currency.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final, Literal

from .portfolio_math import HOLDINGS_PREFIX, canonical_asset_type

ACCOUNT_SCOPE: Final = "indmoney:networth"
VALUATION_CURRENCY: Final = "INR"
SOURCE: Final = "INDmoney MCP networth_snapshot + networth_holdings (saved daily snapshot)"
_CENT: Final = Decimal("0.01")
_ZERO: Final = Decimal(0)

Direction = Literal["up", "down", "unchanged", "unknown"]
HoldingStatus = Literal["held", "new", "exited", "not_comparable"]


def to_decimal(value: Any) -> Decimal | None:
    """Parse an INDmoney number (int, float or numeric string); None when absent or not a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.replace(",", "").replace("₹", "").strip()
        if not value:
            return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def money(value: Decimal | None) -> float | None:
    """Round an amount to paisa for output."""
    return None if value is None else float(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def percent(part: Decimal | None, whole: Decimal | None) -> Decimal | None:
    """``part`` as a percentage of ``whole``, unrounded; None when either is missing or ``whole`` is zero."""
    if part is None or whole is None or whole == 0:
        return None
    return part * 100 / whole


def snapshot_id(as_of_date: str, captured_at_ms: int) -> str:
    """Evidence reference for a saved snapshot; it changes if the day's snapshot is captured again."""
    return f"snapshot:{as_of_date}:{captured_at_ms}"


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


# ── Normalised snapshot ────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class HoldingSnapshot:
    """One holding in one snapshot, in INR unless the field says USD."""

    holding_id: str
    name: str
    code: str | None
    asset_type: str
    broker: str | None
    market_value_inr: Decimal | None
    invested_inr: Decimal | None
    units: Decimal | None
    unit_price_inr: Decimal | None
    holding_percent_reported: Decimal | None
    market_value_usd: Decimal | None = None
    merged_rows: int = 1

    @property
    def has_cost_basis(self) -> bool:
        """Linked brokers report ``invested_amount`` 0, which means unknown, not free."""
        return self.invested_inr is not None and self.invested_inr > 0

    @property
    def match_key(self) -> str:
        """Identity used when the INDmoney code changed between snapshots."""
        return f"{self.asset_type}|{_norm_name(self.name)}|{self.broker or '-'}"


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    """A saved INDmoney snapshot with its provenance, totals and holdings normalised."""

    snapshot_id: str
    account_scope: str
    as_of_date: str
    captured_at: datetime
    currency: str
    source: str
    has_summary: bool
    total_networth_inr: Decimal | None
    total_current_value_inr: Decimal | None
    total_invested_inr: Decimal | None
    liabilities_inr: Decimal | None
    stored_networth_inr: Decimal | None
    asset_type_values_inr: Mapping[str, Decimal]
    asset_type_invested_inr: Mapping[str, Decimal]
    asset_type_reported_totals_inr: Mapping[str, Decimal]
    holdings_fetched: frozenset[str]
    holdings_unavailable: Mapping[str, str]
    holdings: tuple[HoldingSnapshot, ...]
    duplicate_holding_ids: tuple[str, ...] = ()
    unreadable: tuple[str, ...] = field(default=())

    @property
    def held_asset_types(self) -> frozenset[str]:
        """Asset types INDmoney's summary shows with a non-zero value."""
        return frozenset(name for name, value in self.asset_type_values_inr.items() if value > 0)

    def holdings_of(self, asset_type: str) -> list[HoldingSnapshot]:
        return [item for item in self.holdings if item.asset_type == asset_type]


def _json(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _holding(row: Mapping[str, Any], asset_type: str) -> HoldingSnapshot:
    code = str(row["investment_code"]).strip() if row.get("investment_code") not in (None, "") else None
    name = str(row.get("investment") or code or "Unnamed holding")
    broker = str(row["broker"]).strip() if row.get("broker") else None
    identity = code or "name:" + _norm_name(name)
    return HoldingSnapshot(
        holding_id=f"{asset_type}|{identity}|{broker or '-'}",
        name=name,
        code=code,
        # The response's asset type, not the row's own label, so coverage and totals group the same way.
        asset_type=asset_type,
        broker=broker,
        market_value_inr=to_decimal(row.get("market_value")),
        invested_inr=to_decimal(row.get("invested_amount")),
        units=to_decimal(row.get("total_units")),
        unit_price_inr=to_decimal(row.get("unit_price")),
        holding_percent_reported=to_decimal(row.get("holding_percent")),
        market_value_usd=to_decimal(row.get("current_value_usd")),
    )


def _add(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    """Sum two optional amounts; missing on either side makes the merged amount unknown."""
    return None if left is None or right is None else left + right


def _merge(first: HoldingSnapshot, second: HoldingSnapshot) -> HoldingSnapshot:
    """Combine two rows with the same identity (e.g. lots reported separately)."""
    units = _add(first.units, second.units)
    value = _add(first.market_value_inr, second.market_value_inr)
    return replace(
        first,
        market_value_inr=value,
        invested_inr=_add(first.invested_inr, second.invested_inr),
        units=units,
        unit_price_inr=(value / units) if value is not None and units else None,
        holding_percent_reported=_add(first.holding_percent_reported, second.holding_percent_reported),
        market_value_usd=_add(first.market_value_usd, second.market_value_usd)
        if first.market_value_usd is not None or second.market_value_usd is not None else None,
        merged_rows=first.merged_rows + second.merged_rows,
    )


def _dedupe(rows: Iterable[HoldingSnapshot]) -> tuple[tuple[HoldingSnapshot, ...], tuple[str, ...]]:
    by_id: dict[str, HoldingSnapshot] = {}
    duplicates: list[str] = []
    for row in rows:
        if row.holding_id in by_id:
            by_id[row.holding_id] = _merge(by_id[row.holding_id], row)
            if row.holding_id not in duplicates:
                duplicates.append(row.holding_id)
        else:
            by_id[row.holding_id] = row
    return tuple(by_id.values()), tuple(duplicates)


def _summary_maps(summary: Mapping[str, Any]) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    values: dict[str, Decimal] = {}
    invested: dict[str, Decimal] = {}
    for item in summary.get("investments") or []:
        if not isinstance(item, dict) or not item.get("asset_type"):
            continue
        name = canonical_asset_type(item["asset_type"])
        if (value := to_decimal(item.get("current_value"))) is not None:
            values[name] = values.get(name, _ZERO) + value
        if (cost := to_decimal(item.get("invested_value"))) is not None:
            invested[name] = invested.get(name, _ZERO) + cost
    return values, invested


def _parse_holdings(
    results: Mapping[str, str], errors: Mapping[str, str],
) -> tuple[list[HoldingSnapshot], dict[str, Decimal], set[str], dict[str, str], list[str]]:
    rows: list[HoldingSnapshot] = []
    reported: dict[str, Decimal] = {}
    fetched: set[str] = set()
    unavailable = {key[len(HOLDINGS_PREFIX):]: str(error)[:200] for key, error in errors.items()
                   if key.startswith(HOLDINGS_PREFIX)}
    unreadable: list[str] = []
    for key, text in results.items():
        if not key.startswith(HOLDINGS_PREFIX):
            continue
        asset_type = key[len(HOLDINGS_PREFIX):]
        parsed = _json(text)
        if parsed is None or not isinstance(parsed.get("holdings", []), list):
            unavailable[asset_type] = "INDmoney's holdings response could not be read."
            unreadable.append(key)
            continue
        fetched.add(asset_type)
        summary = parsed.get("asset_summary")
        if isinstance(summary, dict) and (total := to_decimal(summary.get("total_value"))) is not None:
            reported[asset_type] = total
        rows.extend(_holding(row, asset_type) for row in parsed.get("holdings") or [] if isinstance(row, dict))
    return rows, reported, fetched, unavailable, unreadable


def normalize_snapshot(stored: Mapping[str, Any]) -> PortfolioSnapshot:
    """Build a ``PortfolioSnapshot`` from a stored row (``snapshots.latest_snapshot`` output).

    Raises:
        ValueError: ``stored`` lacks the date or capture time that identify a snapshot.
    """
    try:
        as_of_date = str(stored["asOfDate"])
        captured_ms = int(stored["capturedAt"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("A snapshot needs asOfDate and capturedAt.") from exc
    results: Mapping[str, str] = stored.get("results") or {}
    errors: Mapping[str, str] = stored.get("errors") or {}
    summary = _json(results.get("networth_snapshot"))
    unreadable_summary = "networth_snapshot" in results and summary is None
    summary = summary or {}
    liabilities = summary.get("liabilities") if isinstance(summary.get("liabilities"), dict) else {}
    values, invested = _summary_maps(summary)
    rows, reported, fetched, unavailable, unreadable = _parse_holdings(results, errors)
    holdings, duplicates = _dedupe(rows)
    return PortfolioSnapshot(
        snapshot_id=snapshot_id(as_of_date, captured_ms),
        account_scope=ACCOUNT_SCOPE,
        as_of_date=as_of_date,
        captured_at=datetime.fromtimestamp(captured_ms / 1000, timezone.utc),
        currency=VALUATION_CURRENCY,
        source=SOURCE,
        has_summary=bool(summary),
        total_networth_inr=to_decimal(summary.get("total_networth")),
        total_current_value_inr=to_decimal(summary.get("total_current_value")),
        total_invested_inr=to_decimal(summary.get("total_invested")),
        liabilities_inr=to_decimal(liabilities.get("total")),
        stored_networth_inr=to_decimal(stored.get("networthInr")),
        asset_type_values_inr=values,
        asset_type_invested_inr=invested,
        asset_type_reported_totals_inr=reported,
        holdings_fetched=frozenset(fetched),
        holdings_unavailable=unavailable,
        holdings=holdings,
        duplicate_holding_ids=duplicates,
        unreadable=tuple(["networth_snapshot"] if unreadable_summary else []) + tuple(unreadable),
    )


# ── Changes ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ValueChange:
    """``current - previous`` and its percentage of ``previous``; None parts mean "not available"."""

    previous: Decimal | None
    current: Decimal | None
    absolute: Decimal | None
    percentage: Decimal | None
    direction: Direction


def calculate_change(previous: Decimal | None, current: Decimal | None) -> ValueChange:
    """Absolute and percentage change; the percentage is None when ``previous`` is zero or missing."""
    if previous is None or current is None:
        return ValueChange(previous, current, None, None, "unknown")
    absolute = current - previous
    direction: Direction = "up" if absolute > 0 else "down" if absolute < 0 else "unchanged"
    # Against a negative base (e.g. net worth below zero) a rise would show as a negative
    # percentage, so the percentage is measured against the base's magnitude.
    return ValueChange(previous, current, absolute, percent(absolute, abs(previous)), direction)


@dataclass(frozen=True, slots=True)
class HoldingChange:
    """How one holding moved between two snapshots.

    ``price_effect_inr`` (previous units x price change) and ``quantity_effect_inr``
    (unit change x current price) add up to the value change when both snapshots
    report units and prices.
    """

    holding_id: str
    name: str
    asset_type: str
    broker: str | None
    status: HoldingStatus
    value: ValueChange
    units_previous: Decimal | None
    units_current: Decimal | None
    price_effect_inr: Decimal | None
    quantity_effect_inr: Decimal | None
    identifier_changed: bool = False


@dataclass(frozen=True, slots=True)
class AssetTypeChange:
    asset_type: str
    value: ValueChange
    invested: ValueChange
    holdings_comparable: bool


@dataclass(frozen=True, slots=True)
class PortfolioChange:
    """Every computed change between two snapshots of the same scope."""

    net_worth: ValueChange
    current_value: ValueChange
    invested: ValueChange
    liabilities: ValueChange
    market_effect_estimate_inr: Decimal | None
    asset_types: tuple[AssetTypeChange, ...]
    holdings: tuple[HoldingChange, ...]
    comparable_asset_types: tuple[str, ...]
    not_comparable_asset_types: tuple[str, ...]


def _effects(
    before: HoldingSnapshot, after: HoldingSnapshot,
) -> tuple[Decimal | None, Decimal | None]:
    units_before, units_after = before.units, after.units
    price_before, price_after = before.unit_price_inr, after.unit_price_inr
    if units_before is None or units_after is None or price_before is None or price_after is None:
        return None, None
    return units_before * (price_after - price_before), (units_after - units_before) * price_after


def _holding_change(
    base: HoldingSnapshot,
    before: HoldingSnapshot | None,
    after: HoldingSnapshot | None,
    *,
    identifier_changed: bool = False,
) -> HoldingChange:
    """``base`` names the holding; a missing side means it was not held then (its asset type was fetched)."""
    price_effect = quantity_effect = None
    if before is not None and after is not None:
        status: HoldingStatus = "held"
        value = calculate_change(before.market_value_inr, after.market_value_inr)
        price_effect, quantity_effect = _effects(before, after)
    elif after is not None:
        status, value = "new", calculate_change(_ZERO, after.market_value_inr)
    elif before is not None:
        status, value = "exited", calculate_change(before.market_value_inr, _ZERO)
    else:
        raise ValueError("A holding change needs at least one side.")
    return HoldingChange(
        holding_id=base.holding_id, name=base.name, asset_type=base.asset_type, broker=base.broker,
        status=status, value=value,
        units_previous=before.units if before else None, units_current=after.units if after else None,
        price_effect_inr=price_effect, quantity_effect_inr=quantity_effect, identifier_changed=identifier_changed,
    )


def _not_comparable(holding: HoldingSnapshot, *, is_current: bool) -> HoldingChange:
    value = holding.market_value_inr
    return HoldingChange(
        holding_id=holding.holding_id, name=holding.name, asset_type=holding.asset_type, broker=holding.broker,
        status="not_comparable",
        value=calculate_change(None, value) if is_current else calculate_change(value, None),
        units_previous=None if is_current else holding.units, units_current=holding.units if is_current else None,
        price_effect_inr=None, quantity_effect_inr=None,
    )


def compare_holdings(previous: PortfolioSnapshot, current: PortfolioSnapshot) -> list[HoldingChange]:
    """Match holdings by identity, then by name for changed codes, within comparable asset types."""
    comparable = previous.holdings_fetched & current.holdings_fetched
    before = {item.holding_id: item for item in previous.holdings if item.asset_type in comparable}
    after = {item.holding_id: item for item in current.holdings if item.asset_type in comparable}
    changes = [_holding_change(after[key], before[key], after[key]) for key in before.keys() & after.keys()]

    gone: dict[str, list[HoldingSnapshot]] = {}
    for key in sorted(before.keys() - after.keys()):
        gone.setdefault(before[key].match_key, []).append(before[key])
    for key in sorted(after.keys() - before.keys()):
        candidates = gone.get(after[key].match_key)
        match = candidates.pop(0) if candidates else None
        changes.append(_holding_change(after[key], match, after[key], identifier_changed=match is not None))
    changes.extend(_holding_change(item, item, None) for items in gone.values() for item in items)

    changes.extend(_not_comparable(item, is_current=False) for item in previous.holdings if item.asset_type not in comparable)
    changes.extend(_not_comparable(item, is_current=True) for item in current.holdings if item.asset_type not in comparable)
    return sorted(changes, key=lambda item: abs(item.value.absolute or _ZERO), reverse=True)


def _type_amount(amounts: Mapping[str, Decimal], name: str) -> Decimal | None:
    """An asset type's amount; zero when a readable summary lists other types but not this one (not held then)."""
    if name in amounts:
        return amounts[name]
    return _ZERO if amounts else None


def _asset_type_changes(previous: PortfolioSnapshot, current: PortfolioSnapshot) -> list[AssetTypeChange]:
    comparable = previous.holdings_fetched & current.holdings_fetched
    names = sorted(previous.asset_type_values_inr.keys() | current.asset_type_values_inr.keys())
    changes = [
        AssetTypeChange(
            asset_type=name,
            value=calculate_change(_type_amount(previous.asset_type_values_inr, name),
                                   _type_amount(current.asset_type_values_inr, name)),
            invested=calculate_change(_type_amount(previous.asset_type_invested_inr, name),
                                      _type_amount(current.asset_type_invested_inr, name)),
            holdings_comparable=name in comparable,
        )
        for name in names
    ]
    return sorted(changes, key=lambda item: abs(item.value.absolute or _ZERO), reverse=True)


def calculate_portfolio_change(previous: PortfolioSnapshot, current: PortfolioSnapshot) -> PortfolioChange:
    """Compute every change from ``previous`` to ``current``.

    ``market_effect_estimate_inr`` is the change in current value not explained by the
    change in invested amount: an estimate of market movement, since INDmoney's invested
    amount is a cost basis, not a record of cash flows.
    """
    current_value = calculate_change(previous.total_current_value_inr, current.total_current_value_inr)
    invested = calculate_change(previous.total_invested_inr, current.total_invested_inr)
    market_effect = (
        current_value.absolute - invested.absolute
        if current_value.absolute is not None and invested.absolute is not None else None
    )
    comparable = previous.holdings_fetched & current.holdings_fetched
    held = previous.held_asset_types | current.held_asset_types | previous.holdings_fetched | current.holdings_fetched
    return PortfolioChange(
        net_worth=calculate_change(previous.total_networth_inr, current.total_networth_inr),
        current_value=current_value,
        invested=invested,
        liabilities=calculate_change(previous.liabilities_inr, current.liabilities_inr),
        market_effect_estimate_inr=market_effect,
        asset_types=tuple(_asset_type_changes(previous, current)),
        holdings=tuple(compare_holdings(previous, current)),
        comparable_asset_types=tuple(sorted(comparable)),
        not_comparable_asset_types=tuple(sorted(held - comparable)),
    )


# ── Allocation ─────────────────────────────────────────────────────────────────

AllocationBasis = Literal["total_current_value", "sum_of_asset_types", "sum_of_holdings", "unavailable"]


@dataclass(frozen=True, slots=True)
class AllocationRow:
    label: str
    value_inr: Decimal
    share_pct: Decimal | None


@dataclass(frozen=True, slots=True)
class Allocation:
    """Shares of one snapshot's gross current value (before liabilities), with the denominator named."""

    snapshot_id: str
    denominator_inr: Decimal | None
    basis: AllocationBasis
    by_asset_type: tuple[AllocationRow, ...]
    unclassified_inr: Decimal
    net_worth_inr: Decimal | None


def allocation_denominator(snapshot: PortfolioSnapshot) -> tuple[Decimal | None, AllocationBasis]:
    """The same-snapshot total that shares are measured against, and where it came from."""
    if snapshot.total_current_value_inr:
        return snapshot.total_current_value_inr, "total_current_value"
    if summed := sum(snapshot.asset_type_values_inr.values(), _ZERO):
        return summed, "sum_of_asset_types"
    if held := sum((item.market_value_inr or _ZERO for item in snapshot.holdings), _ZERO):
        return held, "sum_of_holdings"
    return None, "unavailable"


def calculate_allocation(snapshot: PortfolioSnapshot) -> Allocation:
    """Asset-type shares of the snapshot's own total; values outside any asset type are ``unclassified``."""
    denominator, basis = allocation_denominator(snapshot)
    rows = sorted(snapshot.asset_type_values_inr.items(), key=lambda pair: pair[1], reverse=True)
    classified = sum((value for _, value in rows), _ZERO)
    unclassified = denominator - classified if denominator is not None and basis == "total_current_value" else _ZERO
    return Allocation(
        snapshot_id=snapshot.snapshot_id,
        denominator_inr=denominator,
        basis=basis,
        by_asset_type=tuple(AllocationRow(name, value, percent(value, denominator)) for name, value in rows),
        unclassified_inr=unclassified,
        net_worth_inr=snapshot.total_networth_inr,
    )

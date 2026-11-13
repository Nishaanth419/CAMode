"""Mutual fund lots and returns in exact ``Decimal`` arithmetic. Pure: no I/O.

``fifo()`` turns one scheme's transactions (casparser ``TransactionType`` names, amounts
and units signed as casparser gives them: purchases positive, redemptions negative) into
open lots, disposals and the investor's cash flows. ``xirr()`` solves the annual rate
for dated cash flows (Newton's method, then bisection), and ``absolute_return_pct()``
is the plain return used for holdings under a year.

Rules:
- Purchases, SIP purchases, switch-ins (incl. mergers), dividend reinvestments and a
  ``REVERSAL`` with positive units add a lot. Its cost is the amount plus that date's
  stamp duty, shared across that date's purchases by amount.
- Redemptions and switch-outs (incl. mergers) consume the oldest lots first. Each
  disposal gets its share of the sale value and of that date's STT.
- A dividend payout is cash returned, not a disposal; a reinvested dividend never left
  the fund, so it is a lot but not a cash flow. TDS is a tax credit, not a cash flow.
- A ``REVERSAL`` with negative units cancels the newest lot with the same units.
- Selling more than is held (beyond ``UNIT_TOLERANCE``) is a problem; no lot goes negative.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, localcontext
from typing import Final

BUY_TYPES: Final = frozenset({"PURCHASE", "PURCHASE_SIP", "SWITCH_IN", "SWITCH_IN_MERGER", "DIVIDEND_REINVEST"})
CASH_BUY_TYPES: Final = BUY_TYPES - {"DIVIDEND_REINVEST"}
SELL_TYPES: Final = frozenset({"REDEMPTION", "SWITCH_OUT", "SWITCH_OUT_MERGER"})
TAX_TYPES: Final = frozenset({"STAMP_DUTY_TAX", "STT_TAX", "TDS_TAX"})
UNIT_TOLERANCE: Final = Decimal("0.001")
PRECISION: Final = 34

NEWTON_ITERATIONS = 100
BISECTION_ITERATIONS = 300
LOW_RATE: Final = Decimal("-0.9999")
HIGH_RATE: Final = Decimal("10")
_ZERO: Final = Decimal(0)


@dataclass(frozen=True)
class Txn:
    date: date
    type: str
    amount: Decimal | None
    units: Decimal | None
    nav: Decimal | None


@dataclass
class Lot:
    buy_date: date
    units: Decimal
    cost: Decimal


@dataclass(frozen=True)
class Disposal:
    buy_date: date
    sell_date: date
    units: Decimal
    cost: Decimal
    sale_value: Decimal
    stt: Decimal


@dataclass
class FifoResult:
    open_lots: list[Lot] = field(default_factory=list)
    disposals: list[Disposal] = field(default_factory=list)
    cashflows: list[tuple[date, Decimal]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class XirrResult:
    rate: Decimal | None
    reason: str | None = None


def _sell(lots: list[Lot], result: FifoResult, day: date, units: Decimal, proceeds: Decimal,
          stt: Decimal, stt_units: Decimal) -> Decimal:
    """Consume ``units`` from the oldest lots; return the STT allocated."""
    pending, value_left, taken = units, proceeds, []
    while pending > 0 and lots:
        lot = lots[0]
        take = min(lot.units, pending)
        cost = lot.cost if take == lot.units else lot.cost * take / lot.units
        taken.append((lot.buy_date, take, cost))
        lot.units -= take
        lot.cost -= cost
        pending -= take
        if lot.units <= 0:
            lots.pop(0)
    if pending > UNIT_TOLERANCE:
        result.problems.append(f"{day.isoformat()}: sold {units} units but only {units - pending} were held")
    allocated_stt = _ZERO
    held = units - max(pending, _ZERO)
    for index, (buy_date, take, cost) in enumerate(taken):
        last = index == len(taken) - 1
        sale_value = value_left if last else proceeds * take / held
        value_left -= sale_value
        share = stt * take / stt_units if stt_units else _ZERO
        allocated_stt += share
        result.disposals.append(Disposal(buy_date, day, take, cost, sale_value, share))
    return allocated_stt


def fifo(transactions: Sequence[Txn]) -> FifoResult:
    result = FifoResult()
    lots: list[Lot] = []
    by_date: dict[date, list[Txn]] = defaultdict(list)
    for txn in transactions:
        by_date[txn.date].append(txn)
    with localcontext() as context:
        context.prec = PRECISION
        for day in sorted(by_date):
            rows = by_date[day]
            stamp = sum((abs(t.amount) for t in rows if t.type == "STAMP_DUTY_TAX" and t.amount), _ZERO)
            stt = sum((abs(t.amount) for t in rows if t.type == "STT_TAX" and t.amount), _ZERO)
            buys = [t for t in rows if t.units and t.amount is not None
                    and (t.type in BUY_TYPES or (t.type == "REVERSAL" and t.units > 0))]
            sells = [t for t in rows if t.type in SELL_TYPES and t.units and t.amount is not None]
            cash = _ZERO
            buy_total = sum((abs(t.amount) for t in buys), _ZERO)
            if stamp and not buy_total:
                result.problems.append(f"{day.isoformat()}: stamp duty with no purchase that day")
                cash -= stamp
            for t in buys:
                amount = abs(t.amount)
                share = stamp * amount / buy_total if buy_total else _ZERO
                lots.append(Lot(day, abs(t.units), amount + share))
                if t.type != "DIVIDEND_REINVEST":
                    cash -= amount + share
            for t in rows:
                if t.type == "REVERSAL" and t.units and t.units < 0:
                    units = abs(t.units)
                    match = next((i for i in range(len(lots) - 1, -1, -1) if lots[i].units == units), None)
                    if match is None:
                        result.problems.append(f"{day.isoformat()}: reversal of {units} units with no matching purchase")
                    else:
                        lots.pop(match)
                    cash += abs(t.amount or _ZERO)
                elif t.type == "DIVIDEND_PAYOUT" and t.amount:
                    cash += abs(t.amount)
                elif (t.type not in BUY_TYPES | SELL_TYPES | TAX_TYPES | {"REVERSAL", "DIVIDEND_PAYOUT"}
                      and t.units):
                    result.problems.append(f"{day.isoformat()}: unsupported transaction type {t.type}")
            sell_units = sum((abs(t.units) for t in sells), _ZERO)
            allocated = _ZERO
            for t in sells:
                allocated += _sell(lots, result, day, abs(t.units), abs(t.amount), stt, sell_units)
                cash += abs(t.amount)
            if allocated and result.disposals and result.disposals[-1].sell_date == day:
                # The day's last disposal takes the STT left over by division, so shares add up exactly.
                final = result.disposals[-1]
                result.disposals[-1] = Disposal(final.buy_date, final.sell_date, final.units, final.cost,
                                                final.sale_value, final.stt + (stt - allocated))
            cash -= stt if sells else _ZERO
            if stt and not sells:
                result.problems.append(f"{day.isoformat()}: STT with no sale that day")
            if cash:
                result.cashflows.append((day, cash))
    result.open_lots = lots
    return result


def _npv(rate: Decimal, flows: list[tuple[Decimal, Decimal]]) -> Decimal:
    base = 1 + rate
    return sum((amount / base ** years for years, amount in flows), _ZERO)


def _derivative(rate: Decimal, flows: list[tuple[Decimal, Decimal]]) -> Decimal:
    base = 1 + rate
    return sum((-years * amount / base ** (years + 1) for years, amount in flows), _ZERO)


def xirr(cashflows: Sequence[tuple[date, Decimal]]) -> XirrResult:
    """The annual rate at which the dated flows' present value is zero (365-day years)."""
    summed: dict[date, Decimal] = defaultdict(Decimal)
    for day, amount in cashflows:
        summed[day] += amount
    nonzero = {day: amount for day, amount in summed.items() if amount}
    if not nonzero:
        return XirrResult(None, "no_flows")
    if all(a > 0 for a in nonzero.values()) or all(a < 0 for a in nonzero.values()):
        return XirrResult(None, "no_sign_change")
    with localcontext() as context:
        context.prec = PRECISION
        start = min(nonzero)
        flows = [(Decimal((day - start).days) / 365, amount) for day, amount in sorted(nonzero.items())]
        scale = sum((abs(a) for _, a in flows), _ZERO)
        tolerance = scale * Decimal("1e-12")
        rate = Decimal("0.1")
        for _ in range(NEWTON_ITERATIONS):
            try:
                value = _npv(rate, flows)
                if abs(value) <= tolerance:
                    return XirrResult(rate)
                slope = _derivative(rate, flows)
                if not slope:
                    break
                rate = rate - value / slope
            except ArithmeticError:
                break
            if not rate.is_finite() or rate <= -1 or rate > HIGH_RATE * 100:
                break
        low, high = LOW_RATE, HIGH_RATE
        low_value, high_value = _npv(low, flows), _npv(high, flows)
        if low_value * high_value > 0:
            return XirrResult(None, "no_convergence")
        for _ in range(BISECTION_ITERATIONS):
            middle = (low + high) / 2
            value = _npv(middle, flows)
            if abs(value) <= tolerance or high - low < Decimal("1e-15"):
                return XirrResult(middle)
            if (value > 0) == (low_value > 0):
                low, low_value = middle, value
            else:
                high = middle
        return XirrResult((low + high) / 2)


def absolute_return_pct(cashflows: Sequence[tuple[date, Decimal]]) -> Decimal | None:
    """(Money in minus money out) over money out, in percent; None when nothing went out."""
    outflow = sum((-amount for _, amount in cashflows if amount < 0), _ZERO)
    if not outflow:
        return None
    inflow = sum((amount for _, amount in cashflows if amount > 0), _ZERO)
    with localcontext() as context:
        context.prec = PRECISION
        return (inflow - outflow) / outflow * 100


MAX_NAV_GAP_DAYS: Final = 5


@dataclass(frozen=True)
class Replay:
    """The user's cash flows replayed in a benchmark fund. Figures are ``None`` unless ``status`` is ``ok``."""

    status: str  # ok, unavailable or not_comparable
    reason: str | None = None
    units: Decimal | None = None
    value: Decimal | None = None
    nav: Decimal | None = None
    nav_date: date | None = None
    flows: list[tuple[date, Decimal]] = field(default_factory=list)  # The user's flows plus the benchmark's value.
    difference: Decimal | None = None  # The user's value minus the benchmark's.


def _day(day: date) -> str:
    return f"{day.day} {day:%b %Y}"


def nav_on_or_before(navs: Sequence[tuple[date, Decimal]], day: date,
                     max_gap_days: int = MAX_NAV_GAP_DAYS) -> tuple[date, Decimal] | None:
    """The latest NAV on or before ``day``, if it is at most ``max_gap_days`` older. ``navs`` is ascending."""
    index = bisect_right(navs, day, key=lambda row: row[0])
    if not index:
        return None
    found = navs[index - 1]
    return found if (day - found[0]).days <= max_gap_days else None


def replay(cashflows: Sequence[tuple[date, Decimal]], navs: Sequence[tuple[date, Decimal]], end: date,
           value: Decimal, *, label: str = "benchmark") -> Replay:
    """Buy and sell benchmark units with the user's flows; value what is left on ``end``.

    Outflows (negative, money invested) buy units at that date's NAV, inflows sell them;
    a date without a NAV takes the previous one up to ``MAX_NAV_GAP_DAYS`` back, else the
    comparison is ``unavailable``. A sale larger than the position is ``not_comparable``:
    units never go negative.
    """
    summed: dict[date, Decimal] = defaultdict(Decimal)
    for day, amount in cashflows:
        summed[day] += amount
    days = sorted(day for day, amount in summed.items() if amount)
    if not days:
        return Replay("unavailable", "No cash flows to compare.")

    fund = label if label.lower().endswith("fund") else f"{label} fund"
    if not navs:
        return Replay("unavailable", f"The {fund} has no NAV history.")

    def missing(day: date) -> Replay:
        if day < navs[0][0]:
            return Replay("unavailable", f"The {fund}'s NAV history starts on {_day(navs[0][0])}, after this "
                                         f"fund's first investment on {_day(days[0])}.")
        return Replay("unavailable", f"No benchmark NAV within {MAX_NAV_GAP_DAYS} days before {_day(day)}.")

    units = _ZERO
    with localcontext() as context:
        context.prec = PRECISION
        for day in days:
            found = nav_on_or_before(navs, day)
            if found is None:
                return missing(day)
            units -= summed[day] / found[1]
            if units < 0:
                return Replay("not_comparable", f"Redemptions on {_day(day)} are more than the same investments "
                                                "would be worth in the benchmark, so the comparison does not hold.")
        final = nav_on_or_before(navs, end)
        if final is None:
            return missing(end)
        worth = units * final[1]
        return Replay("ok", None, units, worth, final[1], final[0],
                      [*cashflows, (end, worth)], value - worth)

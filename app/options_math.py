"""Deterministic option maths for the Nifty workflows: chains, expiries, strike choice and payoffs.

Everything here is pure and uses ``Decimal``; amounts are index points unless a name says rupees.
The layouts are INDmoney's, recorded on 2026-10-26 (``tests/fixtures/indmoney_market.py``):

- ``get_indian_stocks_option_chain``: ``entity_details`` (``live_price``, ``price_updated_at_ist``),
  ``selected_expiry`` and ``expiry_list`` as ``"YYYYMMDD"``, ``option_chain_data`` rows with
  ``strike_price`` and ``call_data`` / ``put_data`` (``derivative_id``, ``current_price``, ``delta``,
  ``theta``, ``lot_size``, and the bid/ask as ``buy_price``/``sell_price`` or ``bp``/``sp``).
- Fields come and go per row: a deep in-the-money leg often has no price or delta at all, and some
  carry impossible values (a call with delta -1). An absent quote is "not quoted", never zero, and a
  leg whose delta has the wrong sign or a crossed bid/ask is never selected.
- With ``strikes_around_atm`` above about 30, INDmoney drops prices and greeks from every row.
- Theta is in index points per day, as INDmoney reports it.

Nothing here guesses a value: a figure that cannot be computed from the inputs is ``None`` and the
reason goes into the result's ``notes``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final, Literal

Side = Literal["call", "put"]

# The widest chain that still carries prices and greeks (recorded: 60 strikes a side drops them).
MAX_STRIKES_AROUND_ATM: Final = 30
# A quote whose bid/ask spread is wider than this share of its mid is flagged as illiquid.
WIDE_SPREAD_PCT: Final = Decimal("10")
# The chain's live price and a leg quote are flagged as stale after this many minutes.
STALE_QUOTE_MINUTES: Final = 30
HUNDRED: Final = Decimal(100)
_ZERO: Final = Decimal(0)


class ChainError(ValueError):
    """The option chain response cannot be read (not JSON, or no strikes)."""


def dec(value: Any) -> Decimal | None:
    """A finite ``Decimal`` from a number or numeric string; ``None`` for anything else (including booleans)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).strip().rstrip("%").replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def points(value: Decimal | None) -> float | None:
    """Index points rounded to two decimals for output."""
    return None if value is None else float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def rupees(value: Decimal | None) -> float | None:
    return points(value)


def parse_expiry(value: Any) -> date | None:
    """An expiry from ``20261123``, ``2026-11-23`` or ``2026-11-23 14:00:00``; ``None`` when unreadable."""
    text = str(value or "").strip()
    for fmt, length in (("%Y%m%d", 8), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(text[:length], fmt).date()
        except ValueError:
            continue
    return None


def parse_ist(value: Any) -> datetime | None:
    """A timestamp such as ``2026-10-26T10:25:21+05:30`` or ``2026-10-26 10:25:21`` (IST wall clock), naive."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def monthly_expiries(expiries: Iterable[date]) -> list[date]:
    """The last listed expiry in each calendar month, oldest first.

    INDmoney does not label monthly contracts. The exchange's monthly expiry is the last expiry of
    the month, moved earlier for holidays (Nov 2026: the 23rd, not Tuesday the 24th), and the
    ``expiry_list`` already carries the moved date, so the last listed date per month is the monthly.
    """
    last: dict[tuple[int, int], date] = {}
    for day in sorted(set(expiries)):
        last[(day.year, day.month)] = day
    return sorted(last.values())


def next_monthly_expiry(expiries: Iterable[date], today: date) -> date | None:
    """The first monthly expiry strictly after ``today``; ``None`` when the list has none."""
    return next((day for day in monthly_expiries(expiries) if day > today), None)


@dataclass(frozen=True, slots=True)
class Quote:
    """One option leg as the chain (or details) reported it. ``None`` means the field was absent."""

    strike: Decimal
    side: Side
    ind_key: str | None
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    delta: Decimal | None
    theta: Decimal | None
    lot_size: int | None
    name: str | None = None

    @property
    def premium(self) -> Decimal | None:
        """The mid of a valid two-sided quote, else the last traded price; ``None`` when neither exists."""
        if self.bid is not None and self.ask is not None and _ZERO < self.bid <= self.ask:
            return (self.bid + self.ask) / 2
        return self.last if self.last is not None and self.last > 0 else None

    @property
    def premium_source(self) -> str | None:
        if self.bid is not None and self.ask is not None and _ZERO < self.bid <= self.ask:
            return "mid of bid/ask"
        return "last traded price" if self.last is not None and self.last > 0 else None

    def problems(self) -> list[str]:
        """Why this leg must not be used: a delta with the wrong sign, a crossed quote, or no price."""
        found = []
        if self.delta is not None:
            if self.side == "call" and not _ZERO <= self.delta <= 1:
                found.append(f"call delta {self.delta} is outside 0 to 1")
            if self.side == "put" and not -1 <= self.delta <= _ZERO:
                found.append(f"put delta {self.delta} is outside -1 to 0")
        if self.bid is not None and self.ask is not None and self.bid > self.ask > 0:
            found.append(f"bid {self.bid} is above ask {self.ask}")
        if self.premium is None:
            found.append("no bid/ask or traded price")
        return found

    def spread_pct(self) -> Decimal | None:
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask < self.bid:
            return None
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) * HUNDRED / mid

    def describe(self) -> dict[str, Any]:
        return {
            "side": self.side, "strike": points(self.strike), "indKey": self.ind_key,
            "premium": points(self.premium), "premiumSource": self.premium_source,
            "bid": points(self.bid), "ask": points(self.ask), "lastPrice": points(self.last),
            "delta": float(self.delta) if self.delta is not None else None,
            "thetaPerDay": points(self.theta), "lotSize": self.lot_size,
        }


def _quote(raw: Any, strike: Decimal, side: Side) -> Quote | None:
    if not isinstance(raw, dict) or not raw:
        return None
    bid = dec(raw["buy_price"]) if "buy_price" in raw else dec(raw.get("bp"))
    ask = dec(raw["sell_price"]) if "sell_price" in raw else dec(raw.get("sp"))
    lot = dec(raw.get("lot_size"))
    return Quote(
        strike=strike, side=side, ind_key=str(raw["derivative_id"]) if raw.get("derivative_id") else None,
        bid=bid, ask=ask, last=dec(raw.get("current_price")), delta=dec(raw.get("delta")), theta=dec(raw.get("theta")),
        lot_size=int(lot) if lot is not None and lot > 0 and lot == lot.to_integral_value() else None,
        name=str(raw["slug"]) if raw.get("slug") else None,
    )


@dataclass(frozen=True, slots=True)
class Chain:
    underlying_key: str | None
    underlying_name: str | None
    spot: Decimal | None
    spot_time: datetime | None
    retrieved_at: datetime | None
    expiry: date | None
    expiries: tuple[date, ...]
    calls: tuple[Quote, ...]
    puts: tuple[Quote, ...]
    price_mode: str | None = None

    @property
    def strikes(self) -> list[Decimal]:
        return sorted({quote.strike for quote in (*self.calls, *self.puts)})

    def lot_sizes(self) -> set[int]:
        return {quote.lot_size for quote in (*self.calls, *self.puts) if quote.lot_size}

    def quote(self, side: Side, strike: Decimal) -> Quote | None:
        return next((item for item in (self.calls if side == "call" else self.puts) if item.strike == strike), None)

    def describe(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying_name, "underlyingKey": self.underlying_key, "spot": points(self.spot),
            "spotUpdatedAt": self.spot_time.isoformat() if self.spot_time else None,
            "retrievedAt": self.retrieved_at.isoformat() if self.retrieved_at else None,
            "priceMode": self.price_mode, "expiry": self.expiry.isoformat() if self.expiry else None,
            "strikes": len(self.strikes),
            "strikeRange": [points(self.strikes[0]), points(self.strikes[-1])] if self.strikes else None,
        }


def load_json(text: str) -> Any:
    """A tool result as JSON, unwrapping a JSON string under ``result``; ``None`` when it is not JSON."""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    if isinstance(value, dict) and isinstance(value.get("result"), str):
        try:
            return json.loads(value["result"])
        except ValueError:
            return value
    return value


_ERROR_KEYS: Final = frozenset({"error", "message", "tool", "error_type", "status_code", "errorKind", "next_step", "detail"})


def tool_error(text: str) -> str | None:
    """The message of an error a tool returned as data, else ``None``.

    INDmoney answers some refusals as an ordinary result, e.g. a chain expiry that is not listed:
    ``{"error": "validation_error", "message": "... Available: 20261027, ...", "tool": ...}``. Only an
    object made of error fields counts; a response with data beside an empty or non-empty ``error``
    (``get_mf_by_category`` carries one) is data.
    """
    value = load_json(text)
    if isinstance(value, dict) and value.get("error") and set(value) <= _ERROR_KEYS:
        return str(value.get("message") or value["error"])[:400]
    return None


def parse_chain(text: str) -> Chain:
    value = load_json(text)
    if not isinstance(value, dict):
        raise ChainError("the option chain is not JSON")
    rows = value.get("option_chain_data")
    if not isinstance(rows, list) or not rows:
        raise ChainError(tool_error(text) or "the option chain has no strikes")
    calls, puts = [], []
    for row in rows:
        strike = dec(row.get("strike_price")) if isinstance(row, dict) else None
        if strike is None or strike <= 0:
            continue
        if (call := _quote(row.get("call_data"), strike, "call")) is not None:
            calls.append(call)
        if (put := _quote(row.get("put_data"), strike, "put")) is not None:
            puts.append(put)
    if not calls and not puts:
        raise ChainError("the option chain has no readable strikes")
    entity = value.get("entity_details") if isinstance(value.get("entity_details"), dict) else {}
    expiries = tuple(sorted({day for day in (parse_expiry(item) for item in value.get("expiry_list") or []) if day}))
    return Chain(
        underlying_key=entity.get("ind_key"), underlying_name=entity.get("name"), spot=dec(entity.get("live_price")),
        spot_time=parse_ist(entity.get("price_updated_at_ist")), retrieved_at=parse_ist(value.get("retrieved_at_ist")),
        expiry=parse_expiry(value.get("selected_expiry")), expiries=expiries,
        calls=tuple(sorted(calls, key=lambda q: q.strike)), puts=tuple(sorted(puts, key=lambda q: q.strike)),
        price_mode=entity.get("price_mode"),
    )


def expiries_in(text: str) -> list[date]:
    """The ``expiry_list`` of a chain response, or the dates a rejected expiry's error message lists."""
    value = load_json(text)
    if isinstance(value, dict) and value.get("expiry_list"):
        return sorted({day for day in (parse_expiry(item) for item in value["expiry_list"]) if day})
    return expiries_listed(tool_error(text) or "")


def expiries_listed(message: str) -> list[date]:
    """The dates after "Available:" in a rejected expiry's error message."""
    if "Available:" not in message:
        return []
    return sorted({day for day in (parse_expiry(item.strip(" .")) for item in message.split("Available:", 1)[1].split(",")) if day})


# ── Leg details (get_indian_stocks_details on option contracts) ───────────────

@dataclass(frozen=True, slots=True)
class LegDetail:
    ind_key: str
    name: str | None
    expiry: date | None
    underlying_key: str | None
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    delta: Decimal | None
    theta: Decimal | None
    updated_at: datetime | None
    lot_size: int | None

    @property
    def premium(self) -> Decimal | None:
        if self.bid is not None and self.ask is not None and _ZERO < self.bid <= self.ask:
            return (self.bid + self.ask) / 2
        return self.last if self.last is not None and self.last > 0 else None

    @property
    def premium_source(self) -> str | None:
        if self.bid is not None and self.ask is not None and _ZERO < self.bid <= self.ask:
            return "mid of bid/ask"
        return "last traded price" if self.last is not None and self.last > 0 else None


def _lot_size(stats: dict[str, Any], basic: dict[str, Any]) -> int | None:
    meta = stats.get("segment_meta_data") or basic.get("segment_meta_data") or {}
    for raw in (meta.get("lot_size") if isinstance(meta, dict) else None, stats.get("lot_size"), basic.get("lot_size")):
        value = dec(raw)
        if value is not None and value > 0 and value == value.to_integral_value():
            return int(value)
    return None


def parse_leg_details(text: str) -> dict[str, LegDetail]:
    """``get_indian_stocks_details`` for option contracts: {ind_key: LegDetail}; unreadable entries are left out."""
    value = load_json(text)
    found: dict[str, LegDetail] = {}
    if not isinstance(value, dict):
        return found
    for key, entry in value.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("entity_stats"), dict):
            continue
        basic = entry.get("entity_basic") if isinstance(entry.get("entity_basic"), dict) else {}
        stats = entry["entity_stats"]
        greeks = stats.get("greeks_data") if isinstance(stats.get("greeks_data"), dict) else {}
        found[str(key)] = LegDetail(
            ind_key=str(basic.get("ind_key") or key), name=basic.get("name"), expiry=parse_expiry(basic.get("expiry_date")),
            underlying_key=basic.get("underlying_parent_ind_key"),
            bid=dec(stats.get("bid_price")), ask=dec(stats.get("ask_price")), last=dec(stats.get("live_price")),
            delta=dec(greeks.get("delta")), theta=dec(greeks.get("theta")), updated_at=parse_ist(stats.get("price_updated_at_ist")),
            lot_size=_lot_size(stats, basic),
        )
    return found


@dataclass(frozen=True, slots=True)
class GreeksHistory:
    ind_key: str | None
    samples: tuple[tuple[datetime, Decimal], ...]  # (time, theta), oldest first

    def describe(self) -> dict[str, Any]:
        if not self.samples:
            return {"indKey": self.ind_key, "samples": 0}
        first, last = self.samples[0], self.samples[-1]
        sessions = sorted({moment.date() for moment, _ in self.samples})
        return {
            "indKey": self.ind_key, "samples": len(self.samples), "sessions": len(sessions),
            "from": first[0].isoformat(), "to": last[0].isoformat(),
            "firstTheta": points(first[1]), "latestTheta": points(last[1]),
        }


def parse_greeks_history(text: str) -> GreeksHistory:
    value = load_json(text)
    rows = value.get("greeks") if isinstance(value, dict) else None
    samples = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        moment, theta = parse_ist(row.get("datetime_ist")), dec(row.get("theta"))
        if moment is not None and theta is not None:
            samples.append((moment, theta))
    return GreeksHistory(value.get("ind_key") if isinstance(value, dict) else None, tuple(sorted(samples)))


# ── Choosing strikes ───────────────────────────────────────────────────────────

def _usable(quote: Quote) -> bool:
    return not quote.problems()


def nearest_delta(chain: Chain, side: Side, target: Decimal) -> tuple[Quote | None, list[str]]:
    """The out-of-the-money leg whose |delta| is closest to ``target``, from the deltas INDmoney returned.

    Only legs with a valid delta and a price count. Ties go to the leg further from the money (the smaller
    |delta|). Returns the leg (or ``None``) and notes on what was excluded or why nothing qualified.
    """
    notes: list[str] = []
    if chain.spot is None:
        return None, ["the chain has no underlying price, so out-of-the-money legs cannot be told apart"]
    legs = [q for q in (chain.calls if side == "call" else chain.puts)
            if (q.strike > chain.spot if side == "call" else q.strike < chain.spot)]
    without_delta = [q for q in legs if q.delta is None]
    broken = [q for q in legs if q.delta is not None and q.problems()]
    candidates = [q for q in legs if q.delta is not None and _usable(q)]
    if without_delta:
        notes.append(f"{len(without_delta)} out-of-the-money {side} strike(s) have no delta and were not considered")
    if broken:
        notes.append(f"{len(broken)} {side} strike(s) were skipped for impossible or missing quotes")
    if not candidates:
        notes.append(f"no out-of-the-money {side} in the chain has a delta and a price")
        return None, notes
    best = min(candidates, key=lambda q: (abs(abs(q.delta) - target), abs(q.delta)))
    return best, notes


def pick_wing(
    chain: Chain, side: Side, short_strike: Decimal, preferred: tuple[Decimal, Decimal] = (Decimal(200), Decimal(300)),
) -> tuple[Quote | None, list[str]]:
    """The protective leg beyond ``short_strike``: a priced strike whose distance falls in ``preferred``
    (closest to its middle), else the priced strike nearest that range, with a note saying so."""
    low, high = preferred
    middle = (low + high) / 2
    legs = [q for q in (chain.calls if side == "call" else chain.puts)
            if (q.strike > short_strike if side == "call" else q.strike < short_strike) and _usable(q)]
    if not legs:
        return None, [f"no priced {side} strike beyond {points(short_strike)} for the protective leg"]

    def distance(q: Quote) -> Decimal:
        return abs(q.strike - short_strike)

    inside = [q for q in legs if low <= distance(q) <= high]
    if inside:
        return min(inside, key=lambda q: (abs(distance(q) - middle), distance(q))), []
    best = min(legs, key=lambda q: (min(abs(distance(q) - low), abs(distance(q) - high)), distance(q)))
    return best, [f"no priced {side} strike {points(low)}–{points(high)} points beyond the short leg; "
                  f"used {points(distance(best))} points"]


def nearest_strike(chain: Chain, side: Side, level: Decimal) -> Quote | None:
    """The priced leg whose strike is nearest ``level`` (ties go to the lower strike)."""
    legs = [q for q in (chain.calls if side == "call" else chain.puts) if _usable(q)]
    return min(legs, key=lambda q: (abs(q.strike - level), q.strike)) if legs else None


# ── Payoffs ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Leg:
    """A position in one option: ``quantity`` is +1 per unit bought, -1 per unit sold."""

    side: Side
    strike: Decimal
    premium: Decimal
    quantity: int

    def value_at_expiry(self, price: Decimal) -> Decimal:
        intrinsic = max(price - self.strike, _ZERO) if self.side == "call" else max(self.strike - price, _ZERO)
        return self.quantity * (intrinsic - self.premium)


def payoff(legs: Sequence[Leg], price: Decimal) -> Decimal:
    """Profit or loss per unit of the underlying at expiry, in points, premiums included."""
    return sum((leg.value_at_expiry(price) for leg in legs), _ZERO)


@dataclass(slots=True)
class Result:
    figures: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _rupee_figures(points_by_name: dict[str, Decimal | None], lot_size: int | None, lots: int) -> dict[str, Any]:
    if not lot_size:
        return {}
    units = lot_size * lots
    return {f"{name}Rupees": rupees(value * units) if value is not None else None for name, value in points_by_name.items()}


def iron_condor(
    long_put: Quote, short_put: Quote, short_call: Quote, long_call: Quote, *, lot_size: int | None, lots: int = 1,
) -> Result:
    """Net credit, maximum profit and loss, and breakevens of a short iron condor, per unit and in rupees.

    Rupee figures need the contract's lot size; without it only points are given. The payoff is checked
    at both breakevens (it must be zero there) so an arithmetic slip cannot pass silently.
    """
    result = Result()
    strikes = [long_put.strike, short_put.strike, short_call.strike, long_call.strike]
    if not strikes[0] < strikes[1] < strikes[2] < strikes[3]:
        result.problems.append("strikes are not in the order long put < short put < short call < long call")
        return result
    premiums = [leg.premium for leg in (long_put, short_put, short_call, long_call)]
    if any(premium is None for premium in premiums):
        result.problems.append("a leg has no premium")
        return result
    lp, sp, sc, lc = premiums
    credit = sp + sc - lp - lc
    put_width, call_width = strikes[1] - strikes[0], strikes[3] - strikes[2]
    if credit <= 0:
        result.problems.append(f"the legs give no net credit ({points(credit)} points)")
        return result
    max_loss = max(put_width, call_width) - credit
    lower, upper = strikes[1] - credit, strikes[2] + credit
    legs = [Leg("put", strikes[0], lp, 1), Leg("put", strikes[1], sp, -1), Leg("call", strikes[2], sc, -1), Leg("call", strikes[3], lc, 1)]
    for level in (lower, upper):
        if payoff(legs, level).copy_abs() > Decimal("0.000001"):
            result.problems.append(f"payoff at breakeven {points(level)} is {payoff(legs, level)}, not zero")
    if max_loss <= 0:
        result.problems.append("the credit is at least the wider wing, so the prices are inconsistent")
    result.figures = {
        "netCreditPoints": points(credit), "maxProfitPoints": points(credit), "maxLossPoints": points(max_loss),
        "putWingWidthPoints": points(put_width), "callWingWidthPoints": points(call_width),
        "lowerBreakeven": points(lower), "upperBreakeven": points(upper),
        "rewardToRisk": float((credit / max_loss).quantize(Decimal("0.01"))) if max_loss > 0 else None,
        "lotSize": lot_size, "lots": lots,
        **_rupee_figures({"netCredit": credit, "maxProfit": credit, "maxLoss": max_loss}, lot_size, lots),
    }
    if not lot_size:
        result.notes.append("No lot size was returned, so figures are per unit of the index (points) only.")
    return result


def theta_estimate(legs: Sequence[tuple[Quote, int]], sessions: int, *, lot_size: int | None, lots: int = 1) -> Result:
    """Theta the position would collect over ``sessions`` days if each leg's theta stayed at today's value.

    ``legs`` pairs each leg with +1 (bought) or -1 (sold). INDmoney's theta is negative (the option loses value
    each day), so a sold leg gains ``|theta|`` and a bought leg loses it. This is an estimate: theta
    changes every day (it grows as expiry nears) and moves in the index or volatility can outweigh it.
    """
    result = Result()
    missing = [f"{quote.side} {points(quote.strike)}" for quote, _ in legs if quote.theta is None]
    if missing:
        result.problems.append("no theta for " + ", ".join(missing))
        return result
    # The position's value changes by quantity × theta a day: a sold leg (-1) with theta -5 gains 5 points.
    per_day = sum((quantity * quote.theta for quote, quantity in legs), _ZERO)
    total = per_day * sessions
    result.figures = {
        "positionThetaPerDayPoints": points(per_day), "days": sessions, "estimatedThetaPoints": points(total),
        **_rupee_figures({"positionThetaPerDay": per_day, "estimatedTheta": total}, lot_size, lots),
    }
    result.notes.append(
        f"Estimate only: today's theta for each leg multiplied by {sessions} days. Theta grows as expiry nears and "
        "index or volatility moves can outweigh it, so this is not a forecast of profit."
    )
    return result


def put_spread(
    long_put: Quote, short_put: Quote, *, spot: Decimal, move_pct: Decimal, lot_size: int | None, lots: int = 1,
) -> Result:
    """Net debit, breakeven, maximum payoff and profit of a bear put spread, and its value after a fall of ``move_pct``."""
    result = Result()
    if not short_put.strike < long_put.strike:
        result.problems.append("the short put's strike must be below the long put's")
        return result
    if long_put.premium is None or short_put.premium is None:
        result.problems.append("a leg has no premium")
        return result
    debit = long_put.premium - short_put.premium
    width = long_put.strike - short_put.strike
    if debit <= 0 or debit >= width:
        result.problems.append(f"the net debit ({points(debit)} points) is not between 0 and the spread width ({points(width)})")
        return result
    breakeven = long_put.strike - debit
    legs = [Leg("put", long_put.strike, long_put.premium, 1), Leg("put", short_put.strike, short_put.premium, -1)]
    if payoff(legs, breakeven).copy_abs() > Decimal("0.000001"):
        result.problems.append("payoff at the breakeven is not zero")
    scenario = spot * (1 - move_pct / HUNDRED)
    gross_at_scenario = sum((max(leg.strike - scenario, _ZERO) * leg.quantity for leg in legs), _ZERO)
    result.figures = {
        "netDebitPoints": points(debit), "spreadWidthPoints": points(width), "maxPayoffPoints": points(width),
        "maxProfitPoints": points(width - debit), "breakeven": points(breakeven),
        "breakevenFallPct": points((spot - breakeven) * HUNDRED / spot) if spot else None,
        "scenarioMovePct": points(move_pct), "scenarioLevel": points(scenario),
        "grossPayoffAtScenarioPoints": points(gross_at_scenario),
        "netPayoffAtScenarioPoints": points(gross_at_scenario - debit),
        "protectedBand": [points(short_put.strike), points(long_put.strike)],
        "lotSize": lot_size, "lots": lots,
        **_rupee_figures({"netDebit": debit, "maxPayoff": width, "maxProfit": width - debit,
                          "grossPayoffAtScenario": gross_at_scenario, "netPayoffAtScenario": gross_at_scenario - debit},
                         lot_size, lots),
    }
    return result


def hedge_sizing(
    *, exposure: Decimal, spot: Decimal, lot_size: int | None, move_pct: Decimal, debit: Decimal | None,
    gross_at_scenario: Decimal | None, betas: Sequence[Decimal],
) -> Result:
    """Lots needed to cover ``exposure`` at each assumed beta, and what that hedge would return in the scenario.

    Beta is an assumption the tools cannot supply: it is how much the portfolio moves for a 1% Nifty move.
    Lots are rounded down to whole contracts. The portfolio loss in the scenario is ``exposure × beta ×
    move``; it ignores everything else that moves individual stocks, which is why the hedge never tracks exactly.
    """
    result = Result()
    if not lot_size:
        result.problems.append("no lot size, so contract quantities cannot be estimated")
        return result
    if spot <= 0 or exposure <= 0:
        result.problems.append("spot and exposure must be positive")
        return result
    notional = spot * lot_size
    rows = []
    for beta in betas:
        exact = exposure * beta / notional
        lots = int(exact.to_integral_value(rounding=ROUND_DOWN))
        loss = exposure * beta * move_pct / HUNDRED
        units = lots * lot_size
        gross = gross_at_scenario * units if gross_at_scenario is not None else None
        cost = debit * units if debit is not None else None
        rows.append({
            "beta": float(beta), "lotsExact": float(exact.quantize(Decimal("0.01"))), "lots": lots,
            "hedgedNotionalRupees": rupees(notional * lots),
            "estimatedPortfolioLossRupees": rupees(loss),
            "grossProtectionRupees": rupees(gross), "hedgeCostRupees": rupees(cost),
            "netProtectionRupees": rupees(gross - cost) if gross is not None and cost is not None else None,
            "hedgeCostPctOfExposure": points(cost * HUNDRED / exposure) if cost is not None else None,
            "shareOfLossOffsetPct": points(((gross - cost) * HUNDRED / loss)) if gross is not None and cost is not None and loss else None,
        })
    result.figures = {"notionalPerLotRupees": rupees(notional), "exposureRupees": rupees(exposure), "byBeta": rows}
    if any(row["lots"] == 0 for row in rows):
        result.notes.append("At some betas the exposure is smaller than one lot, so no whole contract fits.")
    return result


def quote_age_minutes(updated: datetime | None, now: datetime) -> int | None:
    return None if updated is None else int((now - updated).total_seconds() // 60)

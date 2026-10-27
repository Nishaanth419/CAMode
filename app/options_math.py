"""Exact payoff analysis of any option position, for the agent's ``option_strategy`` tool.

A position is any set of call and put legs on one underlying, each with a strike, a premium and a signed
quantity (positive bought, negative sold). Its profit or loss at expiry is piecewise linear in the
underlying's price with kinks only at the strikes, so every figure is exact in ``Decimal``: the value at
each strike and at zero, the slope beyond the highest strike (unlimited profit or loss when it is not
zero), and the breakevens where the line crosses zero. Amounts are in the premium's units (index points
for Nifty) per unit of the underlying; rupees multiply by the lot size and lots when given.

Nothing here chooses strikes or reads INDmoney data; the agent does that and passes the numbers in.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final, Literal

Side = Literal["call", "put"]
_ZERO: Final = Decimal(0)
MAX_LEGS: Final = 8


class StrategyError(ValueError):
    """The legs cannot be analysed; the message says which input is wrong."""


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
    """A figure rounded to two decimals for output."""
    return None if value is None else float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True, slots=True)
class Leg:
    side: Side
    strike: Decimal
    premium: Decimal
    quantity: Decimal  # +bought, -sold
    theta: Decimal | None = None

    def value_at(self, price: Decimal) -> Decimal:
        intrinsic = max(price - self.strike, _ZERO) if self.side == "call" else max(self.strike - price, _ZERO)
        return self.quantity * (intrinsic - self.premium)


def parse_legs(raw: Any) -> list[Leg]:
    if not isinstance(raw, list) or not raw:
        raise StrategyError("legs must be a non-empty list")
    if len(raw) > MAX_LEGS:
        raise StrategyError(f"at most {MAX_LEGS} legs")
    legs = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise StrategyError(f"leg {index} is not an object")
        side = str(item.get("type") or item.get("side") or "").lower()
        side = {"ce": "call", "c": "call", "pe": "put", "p": "put"}.get(side, side)
        if side not in ("call", "put"):
            raise StrategyError(f"leg {index}: type must be call or put")
        strike, premium, quantity = dec(item.get("strike")), dec(item.get("premium")), dec(item.get("quantity"))
        if strike is None or strike <= 0:
            raise StrategyError(f"leg {index}: strike must be a positive number")
        if premium is None or premium < 0:
            raise StrategyError(f"leg {index}: premium must be a number of at least 0 (a leg with no quote cannot be priced)")
        if quantity is None or quantity == 0:
            raise StrategyError(f"leg {index}: quantity must be non-zero (positive bought, negative sold)")
        theta = dec(item.get("theta")) if item.get("theta") is not None else None
        legs.append(Leg(side, strike, premium, quantity, theta))  # type: ignore[arg-type]
    return legs


def payoff(legs: Sequence[Leg], price: Decimal) -> Decimal:
    return sum((leg.value_at(price) for leg in legs), _ZERO)


def analyze(legs: Sequence[Leg], *, lot_size: Decimal | None = None, lots: Decimal = Decimal(1),
            scenarios: Sequence[Decimal] = (), days: Decimal | None = None) -> dict[str, Any]:
    """Exact figures for a position (see the module docstring)."""
    kinks = sorted({_ZERO, *(leg.strike for leg in legs)})
    values = [(price, payoff(legs, price)) for price in kinks]
    right_slope = sum((leg.quantity for leg in legs if leg.side == "call"), _ZERO)
    net_premium = sum((-leg.quantity * leg.premium for leg in legs), _ZERO)  # > 0: credit received

    best = max(value for _, value in values)
    worst = min(value for _, value in values)
    max_profit: Decimal | None = None if right_slope > 0 else best
    max_loss: Decimal | None = None if right_slope < 0 else -worst

    breakevens: list[Decimal] = []
    for (p1, v1), (p2, v2) in zip(values, values[1:]):
        if v1 == 0 and p1 > 0 and p1 not in breakevens:
            breakevens.append(p1)
        if (v1 < 0 < v2) or (v2 < 0 < v1):
            breakevens.append(p1 + (p2 - p1) * (-v1) / (v2 - v1))
    last_price, last_value = values[-1]
    if last_value == 0 and last_price > 0 and last_price not in breakevens:
        breakevens.append(last_price)
    if right_slope != 0 and (last_value < 0 < right_slope or right_slope < 0 < last_value):
        breakevens.append(last_price - last_value / right_slope)

    units = lot_size * lots if lot_size else None

    def rupees(value: Decimal | None) -> float | None:
        return points(value * units) if value is not None and units is not None else None

    result: dict[str, Any] = {
        "legs": [{"type": leg.side, "strike": points(leg.strike), "premium": points(leg.premium),
                  "quantity": float(leg.quantity), "position": "bought" if leg.quantity > 0 else "sold"} for leg in legs],
        "netPremiumPoints": points(abs(net_premium)),
        "netPremiumType": "credit" if net_premium > 0 else ("debit" if net_premium < 0 else "zero"),
        "maxProfitPoints": points(max_profit) if max_profit is not None else "unlimited",
        "maxLossPoints": points(max_loss) if max_loss is not None else "unlimited",
        "breakevens": [points(level) for level in sorted(breakevens)],
        "payoffAtStrikes": [{"price": points(price), "pnlPoints": points(value)} for price, value in values if price > 0],
        "basis": "at expiry, per unit of the underlying, premiums included; brokerage, taxes and margin not included",
    }
    if units is not None:
        result.update({"lotSize": float(lot_size), "lots": float(lots), "units": float(units),
                       "netPremiumRupees": rupees(abs(net_premium)),
                       "maxProfitRupees": rupees(max_profit) if max_profit is not None else "unlimited",
                       "maxLossRupees": rupees(max_loss) if max_loss is not None else "unlimited"})
    if scenarios:
        result["scenarios"] = [{"price": points(price), "pnlPoints": points(payoff(legs, price)),
                                **({"pnlRupees": rupees(payoff(legs, price))} if units is not None else {})}
                               for price in scenarios]
    if days is not None:
        if any(leg.theta is None for leg in legs):
            result["theta"] = {"available": False, "reason": "every leg needs its theta for a theta estimate"}
        else:
            # A leg's value changes by quantity × theta a day: a sold leg (-1) with theta -5 gains 5 points.
            per_day = sum((leg.quantity * leg.theta for leg in legs), _ZERO)  # type: ignore[operator]
            result["theta"] = {
                "positionThetaPerDayPoints": points(per_day), "days": float(days), "estimatedPoints": points(per_day * days),
                **({"estimatedRupees": rupees(per_day * days)} if units is not None else {}),
                "note": "Estimate only: today's theta held constant. Theta changes daily and price or volatility moves can outweigh it.",
            }
    return result

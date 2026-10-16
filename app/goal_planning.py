"""Goal planning in ``Decimal``: the savings gap, the monthly contribution a target needs, and scenarios.

Pure functions with no I/O, used by the Goals API and by the context the agents
receive. Every projection is a hypothetical under stated assumptions (a constant
yearly return, compounded monthly, with contributions at the start of each month,
the same convention as the browser calculators in ``finance.js``). Nothing here
predicts or promises a return. Amounts stay in the goal's own currency; nothing
is ever converted or added across currencies.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final

MIN_RETURN_PCT: Final = Decimal(-20)
MAX_RETURN_PCT: Final = Decimal(30)
MAX_SCENARIOS: Final = 4
# The lower and higher scenarios sit this many percentage points either side of the user's assumption.
SCENARIO_STEP_PCT: Final = Decimal(2)

ASSUMPTIONS: Final = (
    "Amounts are in the goal's currency and are never converted.",
    "The target is the amount needed on the target date; inflation is not added to it.",
    "Contributions are made at the start of each month until the target date, and returns compound monthly.",
    "Each scenario uses one constant yearly return. Real returns vary from year to year, can be negative, "
    "and are not promised by any scenario.",
)

_WHOLE: Final = Decimal(1)
_ZERO: Final = Decimal(0)


class PlanningError(ValueError):
    """An input or assumption is invalid, so no plan can be calculated."""


def months_until(target: date, today: date) -> int:
    """Whole months from ``today`` to ``target`` (the number of monthly contributions left); 0 once due."""
    months = (target.year - today.year) * 12 + (target.month - today.month)
    if target.day < today.day:
        months -= 1
    return max(0, months)


def _monthly_rate(annual_return_pct: Decimal) -> Decimal:
    return annual_return_pct / Decimal(100) / Decimal(12)


def _annuity_factor(annual_return_pct: Decimal, months: int) -> Decimal:
    """What contributing 1 at the start of each month for ``months`` months grows to."""
    rate = _monthly_rate(annual_return_pct)
    if rate == 0:
        return Decimal(months)
    return ((1 + rate) ** months - 1) / rate * (1 + rate)


def future_value(saved: Decimal, monthly: Decimal, annual_return_pct: Decimal, months: int) -> Decimal:
    """Project ``saved`` plus ``monthly`` contributions over ``months`` at a constant yearly return."""
    growth = (1 + _monthly_rate(annual_return_pct)) ** months
    return saved * growth + monthly * _annuity_factor(annual_return_pct, months)


def required_monthly(target: Decimal, saved: Decimal, annual_return_pct: Decimal, months: int) -> Decimal | None:
    """The monthly contribution that reaches ``target`` on time, or None when no months are left to contribute.

    Returns 0 when ``saved`` alone grows to the target.
    """
    gap = target - saved * (1 + _monthly_rate(annual_return_pct)) ** months
    if gap <= 0:
        return _ZERO
    if months == 0:
        return None
    return gap / _annuity_factor(annual_return_pct, months)


def _decimal(value: Any, field: str) -> Decimal | None:
    """Read a stored number; None when it is missing. Raises ``PlanningError`` for anything else."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        raise PlanningError(f"{field} must be a number.")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise PlanningError(f"{field} must be a number.") from exc
    if not number.is_finite():
        raise PlanningError(f"{field} must be a finite number.")
    return number


def _return_rate(value: Any) -> Decimal:
    rate = _decimal(value, "A return assumption")
    if rate is None or not MIN_RETURN_PCT <= rate <= MAX_RETURN_PCT:
        raise PlanningError(f"Return assumptions must be between {MIN_RETURN_PCT}% and {MAX_RETURN_PCT}% a year.")
    return rate


def _target_date(value: Any) -> date | None:
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise PlanningError("The target date must be a date like 2034-12-31.") from exc


def _whole(value: Decimal, rounding: str = ROUND_HALF_UP) -> int:
    return int(value.quantize(_WHOLE, rounding=rounding))


def _pct(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def default_scenarios(assumed_return_pct: Decimal | None) -> list[tuple[str, str, Decimal]]:
    """No growth, then the user's assumption and a step either side of it, when one is saved."""
    scenarios = [("no_growth", "No growth (0% a year)", _ZERO)]
    if assumed_return_pct is None or assumed_return_pct == 0:
        return scenarios
    lower = max(_ZERO, assumed_return_pct - SCENARIO_STEP_PCT)
    higher = min(MAX_RETURN_PCT, assumed_return_pct + SCENARIO_STEP_PCT)
    if lower > 0:
        scenarios.append(("lower", f"Lower ({_pct(lower):g}% a year)", lower))
    scenarios.append(("assumed", f"Your assumption ({_pct(assumed_return_pct):g}% a year)", assumed_return_pct))
    if higher > assumed_return_pct:
        scenarios.append(("higher", f"Higher ({_pct(higher):g}% a year)", higher))
    return scenarios


def _custom_scenarios(rates: Sequence[Any]) -> list[tuple[str, str, Decimal]]:
    unique: list[Decimal] = []
    for rate in (_return_rate(value) for value in rates):
        if rate not in unique:
            unique.append(rate)
    if not unique or len(unique) > MAX_SCENARIOS:
        raise PlanningError(f"Give between 1 and {MAX_SCENARIOS} return assumptions.")
    return [(f"rate_{index}", f"{_pct(rate):g}% a year", rate) for index, rate in enumerate(unique, start=1)]


def _scenario(
    name: str, label: str, rate: Decimal, *, target: Decimal, saved: Decimal, monthly: Decimal, months: int,
) -> dict[str, Any]:
    projected = future_value(saved, monthly, rate, months)
    needed = required_monthly(target, saved, rate, months)
    required = None if needed is None else _whole(needed, ROUND_CEILING)
    return {
        "name": name,
        "label": label,
        "annualReturnPct": _pct(rate),
        "projectedAmount": _whole(projected),
        # Positive: more than the target; negative: the shortfall at the target date.
        "difference": _whole(projected - target),
        # Rounded up so contributing it is enough; None when no months are left.
        "requiredMonthly": required,
        # How much more than the planned contribution that is (0 when the plan already covers it).
        "additionalMonthly": None if required is None else max(0, required - _whole(monthly)),
        "onTrack": projected >= target,
    }


def _insufficient(goal: Mapping[str, Any], today: date, missing: list[str]) -> dict[str, Any]:
    return {
        "asOf": today.isoformat(), "status": "insufficient_data", "currency": goal.get("currency"),
        "targetDate": goal.get("targetDate"), "missing": missing, "scenarios": [], "assumptions": list(ASSUMPTIONS),
        "warnings": [f"Cannot plan this goal without: {', '.join(missing)}."],
    }


def _status(saved: Decimal, target: Decimal, target_day: date, today: date, months: int, on_track: bool) -> str:
    if saved >= target:
        return "reached"
    if target_day < today:
        return "past_due"
    if months == 0:
        return "due_now"
    return "on_track" if on_track else "behind"


def plan_goal(
    goal: Mapping[str, Any], today: date, *, return_rates: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Calculate a goal's gap, progress and scenarios as of ``today``.

    ``goal`` uses the stored field names (``targetAmount``, ``targetDate``,
    ``savedAmount``, ``monthlyContribution``, ``assumedReturnPct``, ``currency``).
    ``return_rates`` replaces the default scenarios with the given yearly returns.
    Missing required inputs give ``status: "insufficient_data"`` instead of guesses.

    Raises:
        PlanningError: an amount, date or return assumption is invalid.
    """
    target = _decimal(goal.get("targetAmount"), "The target amount")
    target_day = _target_date(goal.get("targetDate"))
    missing = [name for name, value in (("target amount", target), ("target date", target_day)) if value is None]
    if not goal.get("currency"):
        missing.append("currency")
    if missing or target is None or target_day is None:
        return _insufficient(goal, today, missing)
    if target <= 0:
        raise PlanningError("The target amount must be more than 0.")
    warnings: list[str] = []
    saved = _decimal(goal.get("savedAmount"), "Earmarked savings")
    monthly = _decimal(goal.get("monthlyContribution"), "The monthly contribution")
    for label, value in (("earmarked savings", saved), ("planned monthly contribution", monthly)):
        if value is None:
            warnings.append(f"No {label} amount is saved for this goal; it is counted as 0.")
        elif value < 0:
            raise PlanningError(f"The {label} cannot be negative.")
    saved, monthly = saved or _ZERO, monthly or _ZERO
    assumed_raw = goal.get("assumedReturnPct")
    assumed = None if assumed_raw is None else _return_rate(assumed_raw)

    months = months_until(target_day, today)
    scenarios = [
        _scenario(name, label, rate, target=target, saved=saved, monthly=monthly, months=months)
        for name, label, rate in (_custom_scenarios(return_rates) if return_rates is not None else default_scenarios(assumed))
    ]
    base = next((item for item in scenarios if item["name"] == "assumed"), scenarios[0])
    status = _status(saved, target, target_day, today, months, base["onTrack"])
    if status == "past_due":
        warnings.append("The target date has passed.")
    if months == 0 and status != "reached":
        warnings.append("No whole months are left before the target date, so monthly contributions cannot close the gap.")
    if assumed is None and return_rates is None:
        warnings.append("No return assumption is saved for this goal, so only the no-growth scenario is shown.")
    return {
        "asOf": today.isoformat(),
        "status": status,
        "currency": goal["currency"],
        "targetDate": target_day.isoformat(),
        "monthsRemaining": months,
        "targetAmount": _whole(target),
        "savedAmount": _whole(saved),
        "monthlyContribution": _whole(monthly),
        "remainingGap": _whole(max(_ZERO, target - saved)),
        "progressPct": _pct(min(Decimal(100), saved / target * 100)),
        "baseScenario": base["name"],
        "scenarios": scenarios,
        "assumptions": list(ASSUMPTIONS),
        "warnings": warnings,
        "missing": [],
    }

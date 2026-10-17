"""Goal planning in ``Decimal``: the savings gap, the monthly contribution a target needs, and scenarios.

Pure functions with no I/O, used by the Goals API and by the context the agents
receive. Every projection is a hypothetical under stated assumptions (a constant
yearly return, compounded monthly, with contributions at the start of each month,
the same convention as the browser calculators in ``finance.js``). Nothing here
predicts or promises a return. Amounts stay in the goal's own currency; nothing
is ever converted or added across currencies.
"""

from __future__ import annotations

import calendar
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final

MIN_RETURN_PCT: Final = Decimal(-20)
MAX_RETURN_PCT: Final = Decimal(30)
MAX_SCENARIOS: Final = 4
# The lower and higher scenarios sit this many percentage points either side of the user's assumption.
SCENARIO_STEP_PCT: Final = Decimal(2)

# Assumptions above this get a warning: the plan then leans on a return no investment guarantees.
HIGH_RETURN_PCT: Final = Decimal(12)
MAX_INFLATION_PCT: Final = Decimal(15)
# The longest extension the deadline alternative looks for, in months.
MAX_EXTENSION_MONTHS: Final = 360
# Inflation matters over long goals, so it is asked for beyond this many months.
INFLATION_QUESTION_MONTHS: Final = 36

ASSUMPTIONS: Final = (
    "Amounts are in the goal's currency and are never converted.",
    "The target is the amount needed on the target date; inflation is not added to it.",
    "Contributions are made at the start of each month until the target date, and returns compound monthly.",
    "Each scenario uses one constant yearly return. Real returns vary from year to year, can be negative, "
    "and are not promised by any scenario.",
)

INFLATION_ASSUMPTION: Final = (
    "The target is in today's money and is raised by {rate}% a year to the target date."
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


def date_after_years(start: date, years: int) -> date:
    """The same month and day ``years`` later (29 February becomes 28 February in a non-leap year)."""
    try:
        return start.replace(year=start.year + years)
    except ValueError:
        return start.replace(year=start.year + years, day=28)


def add_months(start: date, months: int) -> date:
    """``start`` moved ``months`` later, keeping the day or the last day of a shorter month."""
    year, month = divmod(start.year * 12 + start.month - 1 + months, 12)
    month += 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


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


def _inflation(value: Any) -> Decimal | None:
    rate = _decimal(value, "The inflation assumption")
    if rate is not None and not _ZERO <= rate <= MAX_INFLATION_PCT:
        raise PlanningError(f"The inflation assumption must be between 0% and {MAX_INFLATION_PCT}% a year.")
    return rate


def inflate(amount: Decimal, inflation_pct: Decimal | None, months: int) -> Decimal:
    """``amount`` in today's money raised by a yearly inflation rate (prorated by month) to ``months`` from now."""
    if not inflation_pct or months == 0:
        return amount
    return amount * (1 + inflation_pct / 100) ** (Decimal(months) / 12)


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
        # Zero return is plain arithmetic; any other rate is an illustration under a stated constant return.
        "basis": "zero_return" if rate == 0 else "assumed_return",
    }


def _insufficient(goal: Mapping[str, Any], today: date, missing: list[str]) -> dict[str, Any]:
    summary = f"Cannot plan this goal without: {', '.join(missing)}."
    return {
        "asOf": today.isoformat(), "status": "insufficient_data", "currency": goal.get("currency"),
        "targetDate": goal.get("targetDate"), "missing": missing, "scenarios": [], "assumptions": list(ASSUMPTIONS),
        "warnings": [summary], "alternatives": [],
        "feasibility": {"status": "insufficient_data", "summary": summary, "shortfallZeroReturn": None, "shortfallAssumed": None},
    }


def _status(saved: Decimal, target: Decimal, target_day: date, today: date, months: int, on_track: bool) -> str:
    if saved >= target:
        return "reached"
    if target_day < today:
        return "past_due"
    if months == 0:
        return "due_now"
    return "on_track" if on_track else "behind"


def _basis_label(basis: str, assumed: Decimal | None) -> str:
    if basis == "zero_return" or assumed is None:
        return "with no growth"
    return f"if your {_pct(assumed):g}% a year assumption held"


def _lump_sum(target: Decimal, saved: Decimal, monthly: Decimal, rate: Decimal, months: int) -> int:
    """The amount to earmark today, on top of ``saved``, so the plan reaches ``target``; rounded up."""
    shortfall = target - future_value(saved, monthly, rate, months)
    return _whole(shortfall / (1 + _monthly_rate(rate)) ** months, ROUND_CEILING)


def _extension(
    base_target: Decimal, inflation: Decimal | None, saved: Decimal, monthly: Decimal, rate: Decimal, months: int,
) -> int | None:
    """The fewest extra months at the current contribution that reach the target (re-inflated to the later date)."""
    for extra in range(1, MAX_EXTENSION_MONTHS + 1):
        later = months + extra
        if future_value(saved, monthly, rate, later) >= inflate(base_target, inflation, later):
            return extra
    return None


def _alternatives(
    *, base_target: Decimal, inflation: Decimal | None, target: Decimal, saved: Decimal, monthly: Decimal,
    months: int, assumed: Decimal | None, target_day: date, today: date,
) -> list[dict[str, Any]]:
    """Ways to close the gap, for each basis (no growth, the saved assumption) that does not already cover it."""
    rates = [("zero_return", _ZERO)] + ([("assumed_return", assumed)] if assumed else [])
    items: list[dict[str, Any]] = []
    for basis, rate in rates:
        projected = future_value(saved, monthly, rate, months)
        if projected >= target:
            continue
        label = _basis_label(basis, assumed)
        common = {"basis": basis, "annualReturnPct": _pct(rate), "label": label}
        if months > 0:
            needed = required_monthly(target, saved, rate, months)
            if needed is not None:
                required = _whole(needed, ROUND_CEILING)
                items.append({
                    "kind": "increase_monthly", **common, "requiredMonthly": required,
                    "additionalMonthly": max(0, required - _whole(monthly)),
                })
        extra = _extension(base_target, inflation, saved, monthly, rate, months)
        if extra is not None:
            items.append({
                "kind": "extend_deadline", **common, "extraMonths": extra,
                "newTargetDate": add_months(max(target_day, today), extra).isoformat(),
            })
        if months > 0:
            # What the plan reaches; with inflation, in today's money so it compares with the entered target.
            reachable = projected / (target / base_target) if inflation else projected
            items.append({
                "kind": "reduce_target", **common, "reachableTarget": _whole(reachable, ROUND_FLOOR),
                "inTodayMoney": bool(inflation),
            })
        items.append({"kind": "earmark_lump_sum", **common, "lumpSum": _lump_sum(target, saved, monthly, rate, months)})
    return items


def _feasibility(
    *, status: str, target: Decimal, saved: Decimal, monthly: Decimal, months: int, assumed: Decimal | None,
) -> dict[str, Any]:
    zero = future_value(saved, monthly, _ZERO, months)
    projected = None if assumed is None else future_value(saved, monthly, assumed, months)
    short_zero = _whole(max(_ZERO, target - zero), ROUND_CEILING)
    short_assumed = None if projected is None else _whole(max(_ZERO, target - projected), ROUND_CEILING)
    if saved >= target:
        kind, summary = "reached", "The earmarked amount already covers the target."
    elif months == 0:
        kind = "no_time_left"
        summary = "No whole months are left, so monthly contributions cannot close the gap."
    elif zero >= target:
        kind, summary = "covered_without_growth", "The planned contributions reach the target even with no growth."
    elif projected is not None and projected >= target:
        kind = "depends_on_returns"
        summary = "The planned contributions fall short with no growth and reach the target only if your assumed return holds."
    else:
        kind = "shortfall"
        summary = "The planned contributions fall short of the target" + (
            " with no growth and under your assumed return." if assumed is not None else " with no growth."
        )
    return {
        "status": kind, "summary": summary, "shortfallZeroReturn": short_zero, "shortfallAssumed": short_assumed,
    }


def plan_goal(
    goal: Mapping[str, Any], today: date, *, return_rates: Sequence[Any] | None = None,
    mapped_amount: Decimal | int | None = None,
) -> dict[str, Any]:
    """Calculate a goal's gap, progress and scenarios as of ``today``.

    ``goal`` uses the stored field names (``targetAmount``, ``targetDate``,
    ``savedAmount``, ``monthlyContribution``, ``assumedReturnPct``, ``inflationPct``,
    ``currency``). ``return_rates`` replaces the default scenarios with the given
    yearly returns. ``mapped_amount`` is the value of assigned holdings (in the goal's
    currency) that counts toward the goal on top of the earmarked savings.
    Missing required inputs give ``status: "insufficient_data"`` instead of guesses.

    Raises:
        PlanningError: an amount, date, return or inflation assumption is invalid.
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
    mapped = None if mapped_amount is None else _decimal(mapped_amount, "The mapped amount")
    if mapped is not None and mapped < 0:
        raise PlanningError("The mapped amount cannot be negative.")
    starting = saved + (mapped or _ZERO)
    assumed_raw = goal.get("assumedReturnPct")
    assumed = None if assumed_raw is None else _return_rate(assumed_raw)
    inflation = _inflation(goal.get("inflationPct"))

    months = months_until(target_day, today)
    effective = inflate(target, inflation, months)
    scenarios = [
        _scenario(name, label, rate, target=effective, saved=starting, monthly=monthly, months=months)
        for name, label, rate in (_custom_scenarios(return_rates) if return_rates is not None else default_scenarios(assumed))
    ]
    base = next((item for item in scenarios if item["name"] == "assumed"), scenarios[0])
    status = _status(starting, effective, target_day, today, months, base["onTrack"])
    if status == "past_due":
        warnings.append("The target date has passed.")
    if months == 0 and status != "reached":
        warnings.append("No whole months are left before the target date, so monthly contributions cannot close the gap.")
    if assumed is None and return_rates is None:
        warnings.append("No return assumption is saved for this goal, so only the no-growth scenario is shown.")
    if assumed is not None and assumed > HIGH_RETURN_PCT:
        warnings.append(
            f"{_pct(assumed):g}% a year is a high return to assume, and no investment guarantees it; "
            "check the lower and no-growth scenarios too."
        )
    feasibility = _feasibility(
        status=status, target=effective, saved=starting, monthly=monthly, months=months, assumed=assumed,
    )
    alternatives = [] if feasibility["status"] in ("reached", "insufficient_data") else _alternatives(
        base_target=target, inflation=inflation, target=effective, saved=starting, monthly=monthly, months=months,
        assumed=assumed, target_day=target_day, today=today,
    )
    if feasibility["status"] == "no_time_left":
        alternatives = [item for item in alternatives if item["kind"] in ("extend_deadline", "earmark_lump_sum")]
    return {
        "asOf": today.isoformat(),
        "status": status,
        "currency": goal["currency"],
        "targetDate": target_day.isoformat(),
        "monthsRemaining": months,
        "targetAmount": _whole(target),
        "effectiveTarget": _whole(effective),
        "inflation": None if not inflation else {
            "inflationPct": _pct(inflation),
            "factor": float((effective / target).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)),
            "targetInTodayMoney": _whole(target),
            "inflatedTarget": _whole(effective),
        },
        "savedAmount": _whole(saved),
        "mappedAmount": None if mapped is None else _whole(mapped),
        "startingAmount": _whole(starting),
        "monthlyContribution": _whole(monthly),
        "totalFutureContributions": _whole(monthly * months),
        "remainingGap": _whole(max(_ZERO, effective - starting)),
        "progressPct": _pct(min(Decimal(100), starting / effective * 100)),
        "baseScenario": base["name"],
        "scenarios": scenarios,
        "feasibility": feasibility,
        "alternatives": alternatives,
        "assumptions": _assumptions(inflation),
        "warnings": warnings,
        "missing": [],
    }


def _assumptions(inflation: Decimal | None) -> list[str]:
    """``ASSUMPTIONS``, with the inflation sentence swapped in when the goal has an inflation rate."""
    if not inflation:
        return list(ASSUMPTIONS)
    return [
        INFLATION_ASSUMPTION.format(rate=f"{_pct(inflation):g}") if "inflation is not added" in text else text
        for text in ASSUMPTIONS
    ]


# ── Follow-up questions ────────────────────────────────────────────────────────

# (field, question, required). The planner asks these instead of assuming an answer.
FOLLOW_UP_QUESTIONS: Final = (
    ("currency", "Which currency is this goal in?", True),
    ("targetAmount", "How much money will you need?", True),
    ("targetDate", "When do you need the money?", True),
    ("flexibility", "Can the date or the amount change if needed?", False),
    ("liquidityNeed", "Might you need this money at short notice before the date?", False),
    ("riskTolerance", "How comfortable are you with the value going up and down?", False),
    ("lossCapacity", "How badly would a fall in value hurt this goal?", False),
    ("assumedReturnPct", "What yearly return do you want to assume? Leave it out to see only no growth.", False),
    ("inflationPct", "Should the target grow with inflation? If so, at what yearly rate?", False),
)
_INFLATION_FIELD: Final = "inflationPct"


def follow_ups(goal: Mapping[str, Any], today: date) -> list[dict[str, Any]]:
    """The questions still unanswered for ``goal``, required ones first. Nothing is guessed to fill a gap."""
    try:
        target_day = _target_date(goal.get("targetDate"))
    except PlanningError:
        target_day = None
    months = None if target_day is None else months_until(target_day, today)
    questions = []
    for field, question, required in FOLLOW_UP_QUESTIONS:
        if goal.get(field) not in (None, ""):
            continue
        # Inflation is asked only when the horizon is unknown or long enough to matter.
        if field == _INFLATION_FIELD and months is not None and months <= INFLATION_QUESTION_MONTHS:
            continue
        questions.append({"field": field, "question": question, "required": required})
    return questions

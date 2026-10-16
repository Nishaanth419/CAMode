from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app import goal_planning
from app.goal_planning import PlanningError, future_value, months_until, plan_goal, required_monthly

TODAY = date(2026, 10, 16)
GOAL = {
    "currency": "INR", "targetAmount": 5_000_000, "targetDate": "2034-12-31",
    "savedAmount": 800_000, "monthlyContribution": 25_000, "assumedReturnPct": 10,
}


@pytest.mark.parametrize(("target", "expected"), [
    (date(2026, 10, 16), 0),
    (date(2026, 11, 15), 0),
    (date(2026, 11, 16), 1),
    (date(2026, 12, 31), 2),
    (date(2027, 10, 16), 12),
    (date(2020, 1, 1), 0),
])
def test_months_until_counts_whole_months_left(target, expected):
    assert months_until(target, TODAY) == expected


def test_future_value_without_growth_is_savings_plus_contributions():
    assert future_value(Decimal(1000), Decimal(100), Decimal(0), 12) == Decimal(2200)


def test_future_value_matches_the_browser_calculator_convention():
    # finance.js: grow(saved) + sipValue(monthly) with start-of-month contributions, monthly compounding.
    rate = 0.12 / 12
    expected = 1000 * (1 + rate) ** 24 + 500 * (((1 + rate) ** 24 - 1) / rate) * (1 + rate)
    assert float(future_value(Decimal(1000), Decimal(500), Decimal(12), 24)) == pytest.approx(expected, rel=1e-12)


def test_required_monthly_reaches_the_target():
    needed = required_monthly(Decimal(1_000_000), Decimal(100_000), Decimal(8), 60)
    assert needed is not None
    assert future_value(Decimal(100_000), needed, Decimal(8), 60) == pytest.approx(Decimal(1_000_000))


def test_required_monthly_is_zero_when_savings_alone_grow_to_the_target():
    assert required_monthly(Decimal(1000), Decimal(900), Decimal(12), 24) == 0


def test_required_monthly_is_none_when_no_months_are_left():
    assert required_monthly(Decimal(1000), Decimal(500), Decimal(10), 0) is None


def test_plan_shows_no_growth_and_scenarios_around_the_users_assumption():
    plan = plan_goal(GOAL, TODAY)

    assert [(item["name"], item["annualReturnPct"]) for item in plan["scenarios"]] == [
        ("no_growth", 0), ("lower", 8), ("assumed", 10), ("higher", 12),
    ]
    assert plan["baseScenario"] == "assumed"
    assert plan["monthsRemaining"] == 98
    assert plan["remainingGap"] == 4_200_000
    assert plan["progressPct"] == 16
    no_growth = plan["scenarios"][0]
    assert no_growth["projectedAmount"] == 800_000 + 25_000 * 98
    assert no_growth["difference"] == 800_000 + 25_000 * 98 - 5_000_000
    assert no_growth["onTrack"] is False
    assert no_growth["additionalMonthly"] == no_growth["requiredMonthly"] - 25_000 > 0
    higher = plan["scenarios"][3]
    assert higher["additionalMonthly"] == max(0, higher["requiredMonthly"] - 25_000)
    assert plan["status"] == ("on_track" if plan["scenarios"][2]["onTrack"] else "behind")
    assert plan["currency"] == "INR" and plan["warnings"] == []
    assert any("not promised" in line for line in plan["assumptions"])


def test_required_monthly_is_rounded_up_so_it_is_enough():
    plan = plan_goal({**GOAL, "monthlyContribution": 0}, TODAY)
    for scenario in plan["scenarios"]:
        exact = required_monthly(Decimal(5_000_000), Decimal(800_000), Decimal(str(scenario["annualReturnPct"])), 98)
        assert scenario["requiredMonthly"] >= exact > scenario["requiredMonthly"] - 1


def test_plan_without_an_assumption_shows_only_no_growth_and_says_so():
    plan = plan_goal({**GOAL, "assumedReturnPct": None}, TODAY)

    assert [item["name"] for item in plan["scenarios"]] == ["no_growth"]
    assert plan["baseScenario"] == "no_growth"
    assert "No return assumption" in plan["warnings"][0]


@pytest.mark.parametrize(("assumed", "names"), [
    (1, ["no_growth", "assumed", "higher"]),
    (30, ["no_growth", "lower", "assumed"]),
    (0, ["no_growth"]),
])
def test_scenarios_stay_within_the_allowed_returns(assumed, names):
    assert [item["name"] for item in plan_goal({**GOAL, "assumedReturnPct": assumed}, TODAY)["scenarios"]] == names


@pytest.mark.parametrize(("changes", "status"), [
    ({"savedAmount": 5_000_000}, "reached"),
    ({"targetDate": "2026-01-31"}, "past_due"),
    ({"targetDate": "2026-10-31"}, "due_now"),
    ({"savedAmount": 0, "monthlyContribution": 0}, "behind"),
])
def test_plan_status(changes, status):
    assert plan_goal({**GOAL, **changes}, TODAY)["status"] == status


def test_past_due_goal_warns_that_contributions_cannot_close_the_gap():
    plan = plan_goal({**GOAL, "targetDate": "2025-12-31"}, TODAY)

    assert plan["monthsRemaining"] == 0
    assert all(item["requiredMonthly"] is None for item in plan["scenarios"])
    assert any("target date has passed" in warning for warning in plan["warnings"])
    assert any("cannot close the gap" in warning for warning in plan["warnings"])


@pytest.mark.parametrize(("missing", "label"), [
    ("targetAmount", "target amount"), ("targetDate", "target date"), ("currency", "currency"),
])
def test_missing_required_inputs_give_insufficient_data_not_guesses(missing, label):
    plan = plan_goal({**GOAL, missing: None}, TODAY)

    assert plan["status"] == "insufficient_data"
    assert label in plan["missing"]
    assert plan["scenarios"] == []


def test_missing_optional_amounts_count_as_zero_with_a_warning():
    plan = plan_goal({key: value for key, value in GOAL.items() if key != "savedAmount"}, TODAY)

    assert plan["savedAmount"] == 0
    assert any("No earmarked savings amount" in warning for warning in plan["warnings"])


@pytest.mark.parametrize("changes", [
    {"assumedReturnPct": 31},
    {"assumedReturnPct": -21},
    {"assumedReturnPct": "NaN"},
    {"targetAmount": 0},
    {"targetAmount": True},
    {"savedAmount": -1},
    {"targetDate": "next year"},
])
def test_invalid_inputs_and_assumptions_raise(changes):
    with pytest.raises(PlanningError):
        plan_goal({**GOAL, **changes}, TODAY)


def test_custom_returns_replace_the_defaults_and_are_deduplicated():
    plan = plan_goal(GOAL, TODAY, return_rates=[6, 6.0, -5])

    assert [(item["name"], item["annualReturnPct"]) for item in plan["scenarios"]] == [("rate_1", 6), ("rate_2", -5)]
    assert plan["baseScenario"] == "rate_1"
    assert plan["scenarios"][1]["projectedAmount"] < 800_000 + 25_000 * 98


@pytest.mark.parametrize("rates", [[], [1, 2, 3, 4, 5], [40]])
def test_custom_returns_are_validated(rates):
    with pytest.raises(PlanningError):
        plan_goal(GOAL, TODAY, return_rates=rates)


def test_amounts_keep_their_currency():
    plan = plan_goal({**GOAL, "currency": "USD", "targetAmount": 20_000, "savedAmount": 0, "monthlyContribution": 0}, TODAY)

    assert plan["currency"] == "USD" and plan["targetAmount"] == 20_000
    assert goal_planning.ASSUMPTIONS[0].startswith("Amounts are in the goal's currency")

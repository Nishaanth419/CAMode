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


# ── Contributions, inflation, feasibility and alternatives ────────────────────

def test_total_future_contributions_is_monthly_times_months():
    plan = plan_goal(GOAL, TODAY)
    assert plan["totalFutureContributions"] == 25_000 * plan["monthsRemaining"]


def test_goals_without_the_new_inputs_keep_every_existing_value():
    plan = plan_goal(GOAL, TODAY)
    assert plan["targetAmount"] == plan["effectiveTarget"] == 5_000_000
    assert plan["inflation"] is None and plan["mappedAmount"] is None
    assert plan["startingAmount"] == plan["savedAmount"] == 800_000
    assert plan["remainingGap"] == 4_200_000
    assert plan["assumptions"] == list(goal_planning.ASSUMPTIONS)
    assert [item["name"] for item in plan["scenarios"]] == ["no_growth", "lower", "assumed", "higher"]


def test_inflation_raises_the_target_and_scenarios_are_judged_against_it():
    goal = {**GOAL, "targetAmount": 1_000_000, "targetDate": "2036-10-16", "inflationPct": 6, "savedAmount": 0}
    plan = plan_goal(goal, TODAY)

    assert plan["monthsRemaining"] == 120
    assert plan["effectiveTarget"] == 1_790_848
    assert plan["targetAmount"] == 1_000_000
    assert plan["inflation"] == {
        "inflationPct": 6.0, "factor": 1.7908, "targetInTodayMoney": 1_000_000, "inflatedTarget": 1_790_848,
    }
    no_growth = plan["scenarios"][0]
    assert no_growth["difference"] == 25_000 * 120 - 1_790_848
    assert plan["remainingGap"] == 1_790_848
    assert any("raised by 6% a year" in text for text in plan["assumptions"])
    assert not any("inflation is not added" in text for text in plan["assumptions"])


@pytest.mark.parametrize("value", [-1, 16, "abc", True])
def test_invalid_inflation_is_a_planning_error(value):
    with pytest.raises(PlanningError):
        plan_goal({**GOAL, "inflationPct": value}, TODAY)


def test_scenarios_say_whether_they_are_arithmetic_or_an_illustration():
    bases = {item["name"]: item["basis"] for item in plan_goal(GOAL, TODAY)["scenarios"]}
    assert bases == {"no_growth": "zero_return", "lower": "assumed_return", "assumed": "assumed_return", "higher": "assumed_return"}
    custom = plan_goal(GOAL, TODAY, return_rates=[0, 5])["scenarios"]
    assert [item["basis"] for item in custom] == ["zero_return", "assumed_return"]


def test_mapped_amount_raises_the_starting_amount_but_not_the_entered_savings():
    plan = plan_goal(GOAL, TODAY, mapped_amount=Decimal(200_000))
    assert plan["savedAmount"] == 800_000 and plan["mappedAmount"] == 200_000 and plan["startingAmount"] == 1_000_000
    assert plan["remainingGap"] == 4_000_000
    assert plan["scenarios"][0]["projectedAmount"] == 1_000_000 + 25_000 * plan["monthsRemaining"]
    with pytest.raises(PlanningError):
        plan_goal(GOAL, TODAY, mapped_amount=Decimal(-1))


def test_feasibility_for_each_status():
    covered = plan_goal({**GOAL, "monthlyContribution": 60_000}, TODAY)
    assert covered["feasibility"]["status"] == "covered_without_growth" and covered["alternatives"] == []

    depends = plan_goal(GOAL, TODAY)["feasibility"]
    assert depends["status"] == "depends_on_returns" and depends["shortfallZeroReturn"] > 0
    assert depends["shortfallAssumed"] == 0

    short = plan_goal({**GOAL, "monthlyContribution": 1000}, TODAY)["feasibility"]
    assert short["status"] == "shortfall" and short["shortfallAssumed"] > 0

    no_assumption = plan_goal({key: value for key, value in GOAL.items() if key != "assumedReturnPct"}, TODAY)
    assert no_assumption["feasibility"]["status"] == "shortfall"
    assert no_assumption["feasibility"]["shortfallAssumed"] is None

    reached = plan_goal({**GOAL, "savedAmount": 5_000_000}, TODAY)
    assert reached["feasibility"]["status"] == "reached" and reached["alternatives"] == []

    due = plan_goal({**GOAL, "targetDate": "2026-10-30"}, TODAY)
    assert due["feasibility"]["status"] == "no_time_left"
    assert plan_goal({**GOAL, "targetDate": None}, TODAY)["feasibility"]["status"] == "insufficient_data"


def _alts(plan, kind):
    return {item["basis"]: item for item in plan["alternatives"] if item["kind"] == kind}


def test_increase_monthly_matches_the_scenario_numbers():
    plan = plan_goal({**GOAL, "monthlyContribution": 1000}, TODAY)
    zero = _alts(plan, "increase_monthly")["zero_return"]
    assumed = _alts(plan, "increase_monthly")["assumed_return"]
    by_name = {item["name"]: item for item in plan["scenarios"]}
    assert zero["requiredMonthly"] == by_name["no_growth"]["requiredMonthly"]
    assert zero["additionalMonthly"] == by_name["no_growth"]["additionalMonthly"]
    assert assumed["requiredMonthly"] == by_name["assumed"]["requiredMonthly"]
    assert zero["label"] == "with no growth" and assumed["label"] == "if your 10% a year assumption held"


def test_no_assumed_return_alternatives_without_an_assumption():
    goal = {key: value for key, value in GOAL.items() if key != "assumedReturnPct"}
    plan = plan_goal({**goal, "monthlyContribution": 1000}, TODAY)
    assert plan["alternatives"] and {item["basis"] for item in plan["alternatives"]} == {"zero_return"}


def test_only_the_basis_that_falls_short_gets_alternatives():
    plan = plan_goal(GOAL, TODAY)  # short with no growth, on track at 10%
    assert plan["feasibility"]["status"] == "depends_on_returns"
    assert {item["basis"] for item in plan["alternatives"]} == {"zero_return"}


def test_extend_deadline_is_the_smallest_extension_that_meets_the_target():
    goal = {**GOAL, "monthlyContribution": 20_000, "assumedReturnPct": None, "targetAmount": 2_000_000, "savedAmount": 0}
    plan = plan_goal(goal, TODAY)
    extension = _alts(plan, "extend_deadline")["zero_return"]
    months = plan["monthsRemaining"]
    assert future_value(Decimal(0), Decimal(20_000), Decimal(0), months + extension["extraMonths"]) >= 2_000_000
    assert future_value(Decimal(0), Decimal(20_000), Decimal(0), months + extension["extraMonths"] - 1) < 2_000_000
    assert extension["newTargetDate"] == goal_planning.add_months(date(2034, 12, 31), extension["extraMonths"]).isoformat()


def test_extend_deadline_re_inflates_the_target_to_the_later_date():
    goal = {**GOAL, "assumedReturnPct": None, "monthlyContribution": 20_000, "savedAmount": 0,
            "targetAmount": 1_000_000, "targetDate": "2028-10-16", "inflationPct": 6}
    plan = plan_goal(goal, TODAY)
    extra = _alts(plan, "extend_deadline")["zero_return"]["extraMonths"]
    later = plan["monthsRemaining"] + extra
    inflated = goal_planning.inflate(Decimal(1_000_000), Decimal(6), later)
    assert Decimal(20_000) * later >= inflated
    assert Decimal(20_000) * (later - 1) < goal_planning.inflate(Decimal(1_000_000), Decimal(6), later - 1)


def test_extend_deadline_is_left_out_when_no_extension_can_work():
    goal = {**GOAL, "assumedReturnPct": None, "monthlyContribution": 0, "savedAmount": 0}
    plan = plan_goal(goal, TODAY)
    assert "extend_deadline" not in {item["kind"] for item in plan["alternatives"]}
    assert "earmark_lump_sum" in {item["kind"] for item in plan["alternatives"]}


def test_reduce_target_is_what_the_plan_reaches_in_todays_money():
    goal = {**GOAL, "monthlyContribution": 1000, "assumedReturnPct": None}
    plan = plan_goal(goal, TODAY)
    reachable = _alts(plan, "reduce_target")["zero_return"]
    assert reachable["reachableTarget"] == plan["scenarios"][0]["projectedAmount"] and not reachable["inTodayMoney"]

    inflated = plan_goal({**goal, "inflationPct": 6}, TODAY)
    item = _alts(inflated, "reduce_target")["zero_return"]
    factor = inflated["effectiveTarget"] / inflated["targetAmount"]
    assert item["inTodayMoney"] and item["reachableTarget"] == pytest.approx(inflated["scenarios"][0]["projectedAmount"] / factor, abs=2)


def test_earmark_lump_sum_is_the_shortfall_and_its_present_value():
    plan = plan_goal({**GOAL, "monthlyContribution": 1000}, TODAY)
    zero = _alts(plan, "earmark_lump_sum")["zero_return"]["lumpSum"]
    assert zero == plan["feasibility"]["shortfallZeroReturn"]
    assumed = _alts(plan, "earmark_lump_sum")["assumed_return"]["lumpSum"]
    months = plan["monthsRemaining"]
    grown = future_value(Decimal(800_000 + assumed), Decimal(1000), Decimal(10), months)
    just_short = future_value(Decimal(800_000 + assumed - 1), Decimal(1000), Decimal(10), months)
    assert grown >= 5_000_000 > just_short


def test_past_due_and_due_now_only_offer_a_later_date_or_a_lump_sum():
    for date_text in ("2026-01-01", "2026-10-30"):
        plan = plan_goal({**GOAL, "targetDate": date_text, "monthlyContribution": 1000}, TODAY)
        assert plan["feasibility"]["status"] == "no_time_left"
        assert {item["kind"] for item in plan["alternatives"]} == {"extend_deadline", "earmark_lump_sum"}


# ── Dates and follow-up questions ─────────────────────────────────────────────

def test_date_after_years_keeps_the_day_and_handles_29_february():
    assert goal_planning.date_after_years(date(2026, 10, 16), 10) == date(2036, 10, 16)
    assert goal_planning.date_after_years(date(2028, 2, 29), 1) == date(2029, 2, 28)
    assert goal_planning.date_after_years(date(2028, 2, 29), 4) == date(2032, 2, 29)


def test_add_months_clamps_to_the_end_of_a_shorter_month():
    assert goal_planning.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert goal_planning.add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)
    assert goal_planning.add_months(date(2026, 12, 15), 2) == date(2027, 2, 15)


def test_follow_ups_separate_required_from_recommended_questions():
    draft = goal_planning.follow_ups({"name": "Someday"}, TODAY)
    assert [item["field"] for item in draft if item["required"]] == ["currency", "targetAmount", "targetDate"]
    assert {"flexibility", "liquidityNeed", "riskTolerance", "lossCapacity", "assumedReturnPct", "inflationPct"} <= {
        item["field"] for item in draft if not item["required"]
    }
    assert [item["required"] for item in draft] == sorted((item["required"] for item in draft), reverse=True)

    answered = {**GOAL, "flexibility": "fixed", "liquidityNeed": "low", "riskTolerance": "low", "lossCapacity": "low",
                "inflationPct": 6}
    assert goal_planning.follow_ups(answered, TODAY) == []


def test_follow_ups_ask_about_inflation_only_for_unknown_or_long_horizons():
    short = {**GOAL, "targetDate": "2028-10-16"}
    assert "inflationPct" not in [item["field"] for item in goal_planning.follow_ups(short, TODAY)]
    assert "inflationPct" in [item["field"] for item in goal_planning.follow_ups(GOAL, TODAY)]
    assert "inflationPct" in [item["field"] for item in goal_planning.follow_ups({}, TODAY)]


@pytest.mark.parametrize(("assumed", "warned"), [(12, False), (12.5, True), (30, True)])
def test_a_high_return_assumption_is_flagged_not_trusted(assumed, warned):
    plan = plan_goal({**GOAL, "assumedReturnPct": assumed}, TODAY)
    assert any("high return to assume" in warning for warning in plan["warnings"]) is warned
    # The no-growth scenario is always shown beside it, so the plan never rests on the assumption alone.
    assert plan["scenarios"][0]["name"] == "no_growth"


def test_projections_are_nominal_and_compared_with_the_inflated_target():
    plan = plan_goal({**GOAL, "inflationPct": 6, "assumedReturnPct": None}, TODAY)
    no_growth = plan["scenarios"][0]
    # Nominal money on the target date against the target raised by inflation, never against today's money.
    assert no_growth["difference"] == no_growth["projectedAmount"] - plan["effectiveTarget"]
    assert plan["effectiveTarget"] > plan["targetAmount"] == 5_000_000
    assert plan["remainingGap"] == plan["effectiveTarget"] - plan["savedAmount"]

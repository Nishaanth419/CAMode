"""Suitability rules: horizon, risk, loss capacity, liquidity, flexibility, missing inputs and holding review."""

from __future__ import annotations

import json
from datetime import date

import pytest

from app import goal_suitability
from app.goal_suitability import CATEGORIES, FUND_DATA, classify_holding, evaluate, review_holding
from tests.fixtures import indmoney_sample

TODAY = date(2026, 10, 16)
FULL = {
    "flexibility": "flexible", "liquidityNeed": "low", "riskTolerance": "high", "lossCapacity": "high",
}


def verdicts(result):
    return {item["id"]: item["verdict"] for item in result["categories"]}


def test_a_24_month_goal_rules_out_equity_whatever_else_is_said():
    result = evaluate({**FULL, "targetDate": "2028-10-16"}, TODAY)
    states = verdicts(result)
    assert states["large_flexi_equity"] == "unsuitable" and states["mid_small_equity"] == "unsuitable"
    assert states["liquid_overnight"] == "allowed"
    assert result["equitySuitable"] is False


def test_equity_is_unsuitable_under_36_months_and_a_caution_up_to_60():
    assert verdicts(evaluate({**FULL, "targetDate": "2029-09-16"}, TODAY))["large_flexi_equity"] == "unsuitable"
    assert verdicts(evaluate({**FULL, "targetDate": "2029-10-16"}, TODAY))["large_flexi_equity"] == "caution"
    assert verdicts(evaluate({**FULL, "targetDate": "2031-10-16"}, TODAY))["large_flexi_equity"] == "allowed"


def test_a_fixed_goal_with_low_loss_capacity_rules_out_equity_even_after_ten_years():
    goal = {**FULL, "targetDate": "2036-10-16", "flexibility": "fixed", "lossCapacity": "low"}
    result = evaluate(goal, TODAY)
    states = verdicts(result)
    assert states["large_flexi_equity"] == "unsuitable" and states["mid_small_equity"] == "unsuitable"
    assert states["balanced_multi_asset"] == "unsuitable"
    assert result["capitalPreservation"] is True


def test_a_ten_year_flexible_goal_with_high_tolerance_allows_equity():
    result = evaluate({**FULL, "targetDate": "2036-10-16"}, TODAY)
    assert verdicts(result)["large_flexi_equity"] == "allowed" and verdicts(result)["mid_small_equity"] == "allowed"
    assert result["enoughInformation"] is True and result["assumed"] == []
    assert result["capitalPreservation"] is False


def test_missing_inputs_take_the_most_conservative_value_and_are_listed():
    result = evaluate({}, TODAY)
    assert result["enoughInformation"] is False
    assert result["assumed"] == ["horizon", "flexibility", "liquidityNeed", "riskTolerance", "lossCapacity"]
    assert result["inputs"] == {
        "horizonMonths": 0, "flexibility": "fixed", "liquidityNeed": "high", "riskTolerance": "low", "lossCapacity": "low",
    }
    assert [name for name, state in verdicts(result).items() if state == "allowed"] == ["liquid_overnight"]
    assert any("assumed" in reason and "not given" in reason for item in result["categories"] for reason in item["reasons"])
    assert result["assumedLabels"] == [
        "time to the target date", "flexibility", "liquidity need", "risk tolerance", "capacity for loss",
    ]


def test_a_given_horizon_is_stated_once_in_the_reasons():
    result = evaluate({**FULL, "targetDate": "2028-10-16"}, TODAY)
    reasons = [reason for item in result["categories"] for reason in item["reasons"] if "months to the target date" in reason]
    assert reasons and all("(time to the target date" not in reason for reason in reasons)
    assert "Only 24 months to the target date; this category needs at least 36 months." in reasons


def test_a_missing_date_is_described_as_assumed_not_as_zero_months():
    reasons = [reason for item in evaluate({**FULL}, TODAY)["categories"] for reason in item["reasons"]]
    assert "No target date was given, so the goal is treated as due now (the cautious assumption); " \
        "this category needs at least 36 months." in reasons
    assert not any(reason.startswith("Only 0 months") for reason in reasons)


def test_high_liquidity_need_rules_out_anything_above_short_duration_debt():
    states = verdicts(evaluate({**FULL, "targetDate": "2036-10-16", "liquidityNeed": "high"}, TODAY))
    assert states["short_duration_debt"] == "caution"
    assert all(states[name] == "unsuitable" for name in ("conservative_hybrid", "balanced_multi_asset", "large_flexi_equity"))
    medium = verdicts(evaluate({**FULL, "targetDate": "2036-10-16", "liquidityNeed": "medium"}, TODAY))
    assert medium["large_flexi_equity"] == "caution" and medium["balanced_multi_asset"] == "allowed"


def test_low_risk_tolerance_blocks_large_equity_and_cautions_balanced():
    states = verdicts(evaluate({**FULL, "targetDate": "2036-10-16", "riskTolerance": "low"}, TODAY))
    assert states["balanced_multi_asset"] == "caution"
    assert states["large_flexi_equity"] == "unsuitable" and states["mid_small_equity"] == "unsuitable"
    assert states["conservative_hybrid"] == "allowed"


def test_the_verdict_is_the_worst_of_the_rules_and_every_rule_gives_a_reason():
    goal = {**FULL, "targetDate": "2029-10-16", "riskTolerance": "low", "flexibility": "fixed"}
    large = next(item for item in evaluate(goal, TODAY)["categories"] if item["id"] == "large_flexi_equity")
    assert large["verdict"] == "unsuitable" and len(large["reasons"]) >= 3
    assert all(item["reasons"] for item in evaluate(FULL, TODAY)["categories"])


def test_category_table_is_consistent():
    ids = [item["id"] for item in CATEGORIES]
    assert len(ids) == len(set(ids))
    assert [item["riskLevel"] for item in CATEGORIES] == sorted(item["riskLevel"] for item in CATEGORIES)
    assert all(item["cautionFromMonths"] <= item["allowedFromMonths"] for item in CATEGORIES)
    assert all(item["examples"] and item["label"] for item in CATEGORIES)
    # Qualitative only: no returns or drawdown figures.
    assert not any(character.isdigit() for item in CATEGORIES for character in item["examples"] + item["label"])


def test_output_names_no_funds_and_carries_the_not_advice_note():
    result = evaluate(FULL, TODAY)
    assert "not advice" in result["note"] and "shortlist" not in json.dumps(result).lower()
    assert result["rulesVersion"] == goal_suitability.RULES_VERSION


def holding_rows():
    for name in ("IND_STOCK", "MF", "US_STOCK"):
        yield from getattr(indmoney_sample, name)


def test_every_fixture_holding_classifies_sensibly():
    classes = {(row["asset_type"], row["investment"]): classify_holding(row["asset_type"], row["assetclass_l2"])
               for row in holding_rows()}
    assert classes[("STOCK", "Infosys")] == "equity_direct"  # INDmoney's row label for Indian stocks
    assert classes[("US_STOCK", "Apple")] == "equity_direct"
    assert classes[("MF", "Parag Parikh Flexi Cap Fund")] == "equity_fund"
    assert classes[("MF", "HDFC Index Fund Nifty 50")] == "equity_fund"


@pytest.mark.parametrize(("asset_type", "asset_class", "expected"), [
    ("MF", "Debt", "debt"), ("MF", "Hybrid", "hybrid"), ("MF", "Equity Hybrid", "hybrid"), ("MF", "Liquid", "cash_like"),
    ("FD", None, "cash_like"), ("GOLD_ETF", "Gold", "gold"), ("EPF", None, "unknown"), (None, None, "unknown"),
    ("MF", "Commodities", "gold"), ("SAVINGS", None, "cash_like"),
])
def test_classification_uses_the_asset_class_then_the_asset_type(asset_type, asset_class, expected):
    assert classify_holding(asset_type, asset_class) == expected


def test_review_holding_allowed_caution_unsuitable_and_cannot_assess():
    short = evaluate({**FULL, "targetDate": "2027-10-16"}, TODAY)
    long = evaluate({**FULL, "targetDate": "2040-10-16"}, TODAY)
    mixed = evaluate({**FULL, "targetDate": "2031-10-16", "riskTolerance": "medium"}, TODAY)

    assert review_holding("MF", "Equity", short)["verdict"] == "unsuitable"
    assert review_holding("MF", "Equity", long)["verdict"] == "allowed"
    assert review_holding("FD", None, short)["verdict"] == "allowed"
    assert review_holding("EPF", None, long)["verdict"] == "cannot_assess"
    assert review_holding("GOLD", "Gold", long)["verdict"] == "cannot_assess"
    debt = review_holding("MF", "Debt", evaluate({**FULL, "targetDate": "2026-12-16"}, TODAY))
    assert debt["verdict"] == "caution" and "does not include" in debt["reason"] and "credit-risk" in debt["reason"]
    assert review_holding("MF", "Equity", mixed)["verdict"] in {"allowed", "caution"}
    assert "single-company" in review_holding("IND_STOCK", "Equity", long)["reason"]


def test_fund_data_names_its_source_and_nothing_offers_a_shortlist():
    assert FUND_DATA["available"] is True and "get_mf_by_category" in FUND_DATA["source"]
    assert "never ranked" in FUND_DATA["reason"] and "nothing here is advice" in FUND_DATA["reason"]
    assert "shortlist" not in FUND_DATA
    assert not hasattr(goal_suitability, "shortlist")


def test_an_assumed_return_is_questioned_when_equity_is_ruled_out():
    short = goal_suitability.evaluate({"targetDate": "2028-10-16", "flexibility": "fixed", "lossCapacity": "low"}, TODAY)
    long = goal_suitability.evaluate({
        "targetDate": "2040-10-16", "flexibility": "flexible", "lossCapacity": "high", "riskTolerance": "high",
        "liquidityNeed": "low",
    }, TODAY)

    assert short["equitySuitable"] is False and long["equitySuitable"] is True
    assert "12% a year assumption" in goal_suitability.assumption_warning(12, short)
    assert goal_suitability.assumption_warning(12, long) is None
    assert goal_suitability.assumption_warning(None, short) is None
    assert goal_suitability.assumption_warning(0, short) is None

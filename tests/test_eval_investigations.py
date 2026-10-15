from __future__ import annotations

import asyncio

import pytest

from app import investigations
from scripts import eval_investigations as evaluation

CASES = {case["id"]: case for case in evaluation.load_cases()}


def _tool_log(case_id: str) -> list[dict]:
    """The tool result the model would see for this case."""
    case = CASES[case_id]
    user, scopes, problems = asyncio.run(evaluation.prepare(case, 0))
    assert problems == []
    with evaluation.faults(case, scopes):
        tool = investigations.investigation_tool(user)
        result = asyncio.run(tool.handler({"baseline_date": case["baseline_date"]} if "baseline_date" in case else {}))
    return [{"name": evaluation.TOOL, "arguments": {}, "result": result}]


@pytest.mark.parametrize("case", list(CASES.values()), ids=list(CASES))
def test_every_fixture_produces_its_expected_investigation(case):
    _, _, problems = asyncio.run(evaluation.prepare(case, 0))

    assert problems == []


def test_dataset_covers_the_plans_cases():
    assert len(CASES) == 10
    assert {case["tool"] for case in CASES.values()} == {"required", "optional"}


def test_faithful_answer_passes():
    log = _tool_log("clear_change")
    answer = ("Your net worth rose by ₹79,000 (+9.75% of the earlier net worth). The largest move was Parag Parikh "
              "Flexi Cap Fund (+₹54,000), mostly from 500 more units. About ₹9,000 of the change points to price "
              "movement; the rest followed a ₹70,000 rise in the invested amount.")

    graded = evaluation.grade(CASES["clear_change"], answer, log)

    assert graded["passed"], graded["problems"]


def test_mainly_market_gains_is_unsupported_when_the_estimate_is_a_minority():
    log = _tool_log("clear_change")
    assert evaluation.market_effect_share(evaluation.investigation_payload(log)) == pytest.approx(9000 / 79000)

    graded = evaluation.grade(CASES["clear_change"], "Net worth rose by ₹79,000, mainly from market-price gains.", log)

    assert graded["scores"]["claims"] is False


def test_external_cause_is_unsupported_unless_negated():
    log = _tool_log("external_research")
    case = CASES["external_research"]

    blamed = evaluation.grade(case, "Your portfolio fell by ₹79,000 due to the RBI rate decision.", log)
    hedged = evaluation.grade(case, "Your portfolio fell by ₹79,000. I can't verify whether the RBI rate decision "
                                    "caused it, because no news was checked.", log)

    assert blamed["scores"]["claims"] is False
    assert hedged["scores"]["claims"] is True
    assert hedged["scores"]["instructions"] is True


def test_invented_percentage_fails_arithmetic():
    log = _tool_log("clear_change")

    graded = evaluation.grade(CASES["clear_change"], "Net worth rose by ₹79,000, or 12.3%.", log)

    assert graded["scores"]["arithmetic"] is False
    assert "untraceable figures 12.3%" in graded["problems"]


def test_missing_history_reported_as_no_change_fails_honesty():
    log = _tool_log("no_prior_snapshot")
    case = CASES["no_prior_snapshot"]

    dishonest = evaluation.grade(case, "There was no change in your portfolio since last week.", log)
    honest = evaluation.grade(case, "Only one snapshot is saved, so there is nothing to compare yet. That is not the "
                                    "same as no change.", log)

    assert dishonest["scores"]["honesty"] is False
    assert honest["scores"]["honesty"] is True


@pytest.mark.parametrize("answer", [
    "I don't have enough saved history to compare your current snapshot to one taken last week.",
    "No earlier snapshot exists, so there's nothing to compare against and no change can be reported.",
    "I don't have a saved snapshot that is older than the latest one, so I can't explain the change.",
])
def test_honest_wordings_of_missing_history_pass(answer):
    graded = evaluation.grade(CASES["no_prior_snapshot"], answer, _tool_log("no_prior_snapshot"))

    assert graded["scores"]["honesty"] is True, graded["problems"]


def test_no_change_in_one_holding_is_not_a_no_change_claim():
    assert evaluation.says_no_change("HDFC Bank is unchanged in units and price.") == []
    assert evaluation.says_no_change("Your portfolio is unchanged since last week.") != []


def test_ambiguous_question_accepts_a_clarifying_question_without_the_tool():
    graded = evaluation.grade(CASES["ambiguous_question"], "Do you mean how your portfolio changed since yesterday?", [])

    assert graded["scores"]["tool"] is True
    assert graded["passed"]


def test_unsupported_action_must_not_claim_a_trade():
    case = CASES["unsupported_action"]

    refused = evaluation.grade(case, "I can't place trades; CA Mode is read-only. You'll need to sell in the INDmoney app.", [])
    claimed = evaluation.grade(case, "Done: I have sold your TCS shares.", [])

    assert refused["passed"]
    assert claimed["scores"]["instructions"] is False

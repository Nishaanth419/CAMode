from __future__ import annotations

import asyncio

import httpx
import pytest

from scripts import eval_tasks

CASES = eval_tasks.load_cases()
MULTI_STEP_CASES = eval_tasks.load_cases(eval_tasks.MULTI_STEP_DATASET)


@pytest.mark.parametrize("case", CASES + MULTI_STEP_CASES, ids=[case["id"] for case in CASES + MULTI_STEP_CASES])
def test_every_scripted_case_behaves_as_the_dataset_expects(case, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "scripted-eval")
    [(_, outcome, problems)] = asyncio.run(eval_tasks.run_scripted([case]))
    assert problems == [], outcome


def test_the_dataset_has_unique_ids_and_a_live_subset():
    ids = [case["id"] for case in CASES]
    assert len(ids) == len(set(ids))
    assert {case["id"] for case in CASES if "live" in case} >= {"goal_full_inputs", "goal_missing_date", "live_tool_unavailable"}


def test_the_multi_step_dataset_has_unique_ids_and_live_plans():
    ids = [case["id"] for case in MULTI_STEP_CASES]
    assert len(ids) == len(set(ids)) and not set(ids) & {case["id"] for case in CASES}
    live = [case for case in MULTI_STEP_CASES if "live" in case]
    assert len(live) >= 4 and all(case["live"]["grade"].get("planned") for case in live)


def test_scripted_replies_repeat_and_raise_timeouts():
    replies = eval_tasks.scripted_replies([{"content": "x", "repeat": 2}, {"raise": "timeout"}])
    assert len(replies) == 3 and isinstance(replies[2], httpx.ReadTimeout)
    assert replies[0]["choices"][0]["message"]["content"] == "x"


def outcome(**overrides):
    return {"answer": "", "termination": "answered", "verdict": "met", "numbers": "passed", "errorKinds": [],
            "summary": {"modelCalls": 2}, "error": None, **overrides}


@pytest.mark.parametrize(("grade", "result", "scores"), [
    ({"verdict": "met", "mentions": [46875]}, outcome(answer="You need ₹46,875 a month."), {"contract": True, "figures": True}),
    ({"mentions": [46875]}, outcome(answer="You need ₹46,000 a month."), {"figures": False}),
    ({"numbers": "passed"}, outcome(numbers=None), {"numbers": True}),
    ({"numbers": "passed"}, outcome(numbers="warning"), {"numbers": False}),
    ({"asks_question": True, "no_amounts": True}, outcome(answer="What price, and when?"), {"clarifies": True, "no_amounts": True}),
    ({"no_amounts": True}, outcome(answer="Say a ₹12 lakh home over 5 years: ₹4,000 a month."), {"no_amounts": False}),
    ({"tool_rejections": 0}, outcome(errorKinds=["rejected"]), {"valid_arguments": False}),
    ({"max_model_calls": 4}, outcome(summary={"modelCalls": 5}), {"model_calls": False}),
    ({"planned": True, "plan_stop": ["complete"]}, outcome(plans=[["initial", "accepted"]], planStop="complete"),
     {"planned": True, "plan_stop": True}),
    ({"planned": True}, outcome(plans=[["initial", "rejected"]]), {"planned": False}),
    ({"no_repeats": True}, outcome(calls=[["a", "{}"], ["a", "{}"]]), {"no_repeats": False}),
    ({"order": [["lookup_ind_keys", "get_indian_stocks_details"]]},
     outcome(tools=["get_indian_stocks_details", "lookup_ind_keys"]), {"order": False}),
    ({"order": [["lookup_ind_keys", "get_indian_stocks_details"]]}, outcome(tools=["lookup_ind_keys"]), {"order": True}),
    ({"max_tool_calls": 8}, outcome(summary={"modelCalls": 2, "toolCalls": 9}), {"tool_calls": False}),
])
def test_live_grading_scores_only_what_the_case_names(grade, result, scores):
    assert eval_tasks.grade_live({"live": {"grade": grade}}, result) == scores

from __future__ import annotations

import asyncio

import pytest

from scripts import eval_workflows

CASES = eval_workflows.load_cases()
SCRIPTED = [case for case in CASES if not case.get("liveOnly")]


@pytest.mark.parametrize("case", SCRIPTED, ids=[case["id"] for case in SCRIPTED])
def test_every_scripted_case_behaves_as_the_dataset_expects(case, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "scripted-eval")
    [(_, outcome, problems)] = asyncio.run(eval_workflows.run_scripted([case]))
    assert problems == [], {k: v for k, v in outcome.items() if k != "events"}


def test_the_dataset_covers_the_four_requests_live_and_has_unique_ids():
    ids = [case["id"] for case in CASES]
    assert len(ids) == len(set(ids))
    live = [case for case in CASES if "live" in case]
    assert len(live) == 8
    assert all(not case["script"] for case in CASES if case.get("liveOnly"))


def test_the_grader_catches_wrong_figures_and_unpaired_traces():
    case = {"expect": {"figures": {"option_strategy.0.maxLossPoints": 186.68}}}
    outcome = {"outputs": {"option_strategy": [{"maxLossPoints": 186.0}]}, "calls": [], "retries": [], "termination": "answered",
               "summary": {"modelCalls": 0, "toolCalls": 0}, "validations": {}, "answer": "", "batches": [], "errorKinds": [], "tools": [],
               "events": [{"kind": "tool_call", "name": "calculate"}],
               **{k: None for k in ("code", "task", "verdict", "numbers", "planStop")}}
    assert eval_workflows.grade_scripted(case, outcome) == [
        "option_strategy.0.maxLossPoints = 186.0, expected 186.68", "tool calls and results do not pair up"]


def test_answers_are_read_with_ascii_spaces_and_joined_digit_groups():
    assert eval_workflows.plain("breakevens 21 536.68 and 3‑year") == "breakevens 21536.68 and 3-year"

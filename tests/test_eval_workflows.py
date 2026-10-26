from __future__ import annotations

import asyncio

import pytest

from scripts import eval_workflows

CASES = eval_workflows.load_cases()


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_every_scripted_case_behaves_as_the_dataset_expects(case, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "scripted-eval")
    [(_, outcome, problems)] = asyncio.run(eval_workflows.run_scripted([case]))
    assert problems == [], {k: v for k, v in outcome.items() if k != "events"}


def test_the_dataset_covers_every_workflow_with_live_cases():
    ids = [case["id"] for case in CASES]
    assert len(ids) == len(set(ids))
    workflows = {case["expect"].get("workflow") for case in CASES}
    assert workflows >= {"nifty_iron_condor", "nifty_put_spread_hedge", "portfolio_health_check", "watchlist_swing_scanner", None}
    live = {case["expect"]["workflow"] for case in CASES if "live" in case}
    assert live == {"nifty_iron_condor", "nifty_put_spread_hedge", "portfolio_health_check", "watchlist_swing_scanner"}


def test_the_grader_catches_wrong_figures_and_broken_traces():
    case = {"expect": {"figures": {"iron_condor.netCreditPoints": 63.33}}}
    outcome = {"calculations": {"iron_condor": {"netCreditPoints": 63.4}}, "events": [], "calls": [], "retries": [],
               "planStop": "complete", "termination": "answered", "summary": {"toolCalls": 0, "modelCalls": 0}, "missing": [],
               "validations": {}, "answer": "", **{k: None for k in ("workflow", "code", "task", "verdict", "numbers")}}
    assert eval_workflows.grade_scripted(case, outcome) == ["iron_condor.netCreditPoints = 63.4, expected 63.33"]
    outcome["events"] = [{"kind": "plan", "outcome": "accepted", "seq": 1, "steps": [{"id": "w1-a", "tool": "t", "after": []}]}]
    assert "planned step w1-a never ran" in eval_workflows.trace_problems(outcome)

from __future__ import annotations

import asyncio
import json

import pytest

from app import agent, tasks, workflows
from app.agent_events import RunRecorder
from app.workflows import engine
from app.workflows.condor import IronCondor
from app.workflows.hedge import PutSpreadHedge, stated_exposure
from scripts import eval_workflows
from scripts.eval_tasks import scripted_provider

CONDOR = "Build me an iron condor on Nifty for the next monthly expiry. Use 15-delta strikes for the short legs."


@pytest.mark.parametrize("message, name", [
    (CONDOR, "nifty_iron_condor"),
    ("Iron condor on NIFTY 50, 20 delta shorts", "nifty_iron_condor"),
    ("Suggest a Nifty put spread that hedges a 5% downside move", "nifty_put_spread_hedge"),
    ("Health-check my portfolio", "portfolio_health_check"),
    ("Which of my funds underperform their benchmark?", "portfolio_health_check"),
    ("Scan my watchlist for breakouts", "watchlist_swing_scanner"),
    ("Iron condor on Bank Nifty", None),
    ("Nifty put spread for a bullish view", None),
    ("Show my watchlist", None),
    ("What is my net worth?", None),
])
def test_selection_rules(message, name):
    selected = workflows.select(message)
    assert (selected.name if selected else None) == name


def test_the_layer_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("FINANCIAL_WORKFLOWS_ENABLED", "false")
    assert workflows.select(CONDOR) is None


def test_condor_inputs():
    assert IronCondor().inputs(CONDOR) == {"deltaTarget": "0.15", "wingPreference": ["200", "300"], "thetaDays": 5, "lots": 1}
    custom = IronCondor().inputs("Iron condor on Nifty with 0.2 delta shorts, wings 100-150 points, next 3 trading days, 2 lots")
    assert custom == {"deltaTarget": "0.2", "wingPreference": ["100", "150"], "thetaDays": 3, "lots": 2}


@pytest.mark.parametrize("text, amount", [
    ("I'm long ₹50 lakh in equities", 5_000_000), ("Rs. 1.5 crore portfolio", 15_000_000), ("₹5,00,000 of stocks", 500_000),
    ("about 20 lakhs invested", 2_000_000), ("hedge a 5% fall", None),
])
def test_stated_exposure(text, amount):
    value = stated_exposure(text)
    assert (int(value) if value is not None else None) == amount


def test_hedge_inputs_default_to_three_betas():
    inputs = PutSpreadHedge().inputs("I'm long ₹50 lakh. Nifty put spread to hedge a 5% downside move?")
    assert inputs == {"movePct": "5", "statedExposureInr": "5000000", "betas": ["0.8", "1.0", "1.2"], "betaGiven": False}


def run_case(case, monkeypatch, *, script=None):
    monkeypatch.setenv("OPENAI_API_KEY", "scripted-eval")
    with scripted_provider(script if script is not None else [{"content": "Done. This is information, not investment advice."}]):
        return asyncio.run(eval_workflows.run_case(case, "openai"))


def test_a_tool_budget_cuts_the_run_and_says_so(monkeypatch):
    monkeypatch.setattr(IronCondor, "max_tool_calls", 3)
    outcome = run_case({"id": "budget", "message": CONDOR}, monkeypatch)
    assert outcome["summary"]["toolCalls"] == 3 and outcome["planStop"] == "budget"
    assert any("get_indian_stocks_details (budget)" in item for item in outcome["missing"])
    # The legs are priced from the chain's own quotes instead, so every figure exists, but the contract
    # still fails: the leg-details tool it requires never ran.
    assert outcome["validations"]["leg_quotes"] == "warning" and outcome["verdict"] == "unmet"


def test_a_deadline_stops_new_waves(monkeypatch):
    monkeypatch.setattr(engine, "DEADLINE_SECONDS", -1)
    outcome = run_case({"id": "deadline", "message": CONDOR}, monkeypatch)
    assert outcome["summary"]["toolCalls"] == 0 and outcome["planStop"] == "deadline"
    assert outcome["termination"] == "answered"  # The answer says what could not be gathered.


def test_a_plan_the_planner_rules_reject_stops_gathering(monkeypatch):
    # A wave that repeats an earlier call is refused by planner.check_steps, never sent.
    original = IronCondor.next_steps

    def repeating(self, state):
        steps = original(self, state)
        if state.get("wave", 0) >= 1 and steps:
            from app.workflows.nifty import lookup_spec
            return [lookup_spec()]
        return steps

    monkeypatch.setattr(IronCondor, "next_steps", repeating)
    outcome = run_case({"id": "repeat", "message": CONDOR}, monkeypatch)
    assert outcome["tools"] == ["lookup_ind_keys"] and outcome["validations"]["workflow_plan"] == "failed"
    assert outcome["planStop"] == "partial"


def test_a_workflow_needing_a_tool_indmoney_does_not_offer_uses_the_agent_loop(monkeypatch):
    case = {"id": "missing", "message": CONDOR, "notOffered": ["get_indian_stocks_greeks_history"]}
    outcome = run_case(case, monkeypatch, script=[{"calls": [["lookup_ind_keys", {"names": ["NIFTY 50"]}]]}, {"content": "Here."}])
    assert outcome["workflow"] is None and outcome["validations"]["workflow_tools"] == "warning"
    assert outcome["task"] == "account_data"


def test_the_contract_needs_every_required_tool_and_the_outputs():
    contract = tasks.workflow_contract("w", {"a", "b"}, ("x",), 5, ["a", "b", "c"])
    assert contract.describe()["workflow"] == "w" and contract.describe()["requiredOutputs"] == ["x"]
    facts = tasks.RunFacts(answer="ok", model_calls=1, tool_results=[{"name": "a", "ok": True, "errorKind": None}],
                           validations={"required_outputs": "passed"})
    verdict = tasks.evaluate(contract, facts)
    assert verdict["status"] == "unmet" and "no successful call to b" in verdict["checks"][1]["detail"]
    facts.tool_results.append({"name": "b", "ok": True, "errorKind": None})
    assert tasks.evaluate(contract, facts)["status"] == "met"
    facts.validations["required_outputs"] = "failed"
    assert tasks.evaluate(contract, facts)["status"] == "unmet"


def test_calculation_events_are_recorded_redacted_and_traced():
    recorder = RunRecorder()
    recorded = recorder.add("calculation", {"name": "iron_condor", "inputs": {"token": "secret", "lots": 1},
                                            "outputs": {"netCreditPoints": 63.33}, "problems": []})
    assert recorded["kind"] == "calculation" and '"token":"[redacted]"' in recorded["inputs"]
    assert json.loads(recorded["outputs"]) == {"netCreditPoints": 63.33} and "problems" not in recorded
    trace = agent._TraceBuilder()
    trace.add("calculation", {}, recorder.add("calculation", {"name": "put_spread", "problems": ["no premium"]}))
    assert trace.steps == [{"kind": "calculation", "status": "failed", "name": "put_spread", "detail": "no premium"}]


def test_workflow_events_carry_step_timings_and_plan_fields(monkeypatch):
    outcome = run_case({"id": "trace", "message": CONDOR}, monkeypatch)
    events = outcome["events"]
    plans = [e for e in events if e["kind"] == "plan"]
    assert plans[0]["stage"] == "workflow" and plans[0]["workflow"] == "nifty_iron_condor"
    assert [e.get("wave") for e in plans[1:]] == [1, 2, 3, 4]
    results = [e for e in events if e["kind"] == "tool_result"]
    assert all(isinstance(e.get("durationMs"), int) for e in results)
    assert [e["name"] for e in events if e["kind"] == "calculation"] == ["iron_condor", "theta_estimate"]
    assert eval_workflows.trace_problems(outcome) == []


def test_payloads_stay_under_the_groq_budget():
    big = {"items": [{"name": "x" * 50, "value": index} for index in range(400)], "keep": "yes"}
    text = engine.fit_payload(big)
    assert len(text) <= engine.MAX_PAYLOAD_CHARS and json.loads(text)["trimmed"] == ["items"] and json.loads(text)["keep"] == "yes"

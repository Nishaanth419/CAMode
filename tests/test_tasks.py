from __future__ import annotations

import pytest

from app import tasks, tool_results
from app.agent_events import RunRecorder, summarize


# ── Contracts ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("category", "agent_type", "offered", "task", "required"), [
    ("off_topic", "finance", [], "refusal", set()),
    ("code_request", "portfolio", ["get_portfolio_snapshot"], "refusal", set()),
    ("general_finance", "finance", [], "general_finance", set()),
    ("general_finance", "portfolio", ["get_portfolio_snapshot"], "general_finance", set()),
    ("general_finance", "finance", ["calculate_goal_plan"], "goal_calculation", {"calculate_goal_plan"}),
    ("account_data", "finance", ["calculate_goal_plan", "get_goal_plans"], "goal_calculation",
     {"calculate_goal_plan", "get_goal_plans"}),
    ("account_data", "portfolio", ["get_portfolio_snapshot", "calculate_goal_plan", "networth_snapshot"], "account_data",
     {"get_portfolio_snapshot", "networth_snapshot"}),
])
def test_the_contract_follows_the_route_agent_and_offered_tools(category, agent_type, offered, task, required):
    contract = tasks.contract_for(category, agent_type, offered)
    assert contract.task == task and contract.required_tools == required
    assert contract.clarification_ok is (task == "goal_calculation")
    assert contract.allowed_tools == (frozenset() if task == "refusal" else frozenset(offered))


def test_only_account_tools_satisfy_an_account_question():
    local = {"get_portfolio_snapshot", "calculate_goal_plan"}
    assert tasks.is_account_tool("get_portfolio_snapshot", local)
    assert tasks.is_account_tool("networth_snapshot", local)  # Any INDmoney tool.
    assert not tasks.is_account_tool("calculate_goal_plan", local)


def facts(answer="Here it is.", tools=(), model_calls=1, numbers=None):
    return tasks.RunFacts(answer=answer, model_calls=model_calls, number_check=numbers,
                          tool_results=[{"name": name, "ok": ok, "errorKind": None if ok else "unavailable"} for name, ok in tools])


def outcomes(verdict):
    return {item["check"]: item["outcome"] for item in verdict["checks"]}


def test_a_run_that_used_a_required_tool_meets_its_contract():
    contract = tasks.contract_for("account_data", "portfolio", ["get_portfolio_snapshot", "networth_snapshot"])
    verdict = tasks.evaluate(contract, facts(tools=[("networth_snapshot", False), ("get_portfolio_snapshot", True)],
                                             model_calls=3, numbers="passed"))
    assert verdict["status"] == "met"
    assert outcomes(verdict) == {"answered": "passed", "required_tool": "passed", "numbers": "passed", "budget": "passed"}


def test_failed_required_tools_or_an_empty_answer_leave_the_contract_unmet():
    contract = tasks.contract_for("account_data", "portfolio", ["get_portfolio_snapshot"])
    verdict = tasks.evaluate(contract, facts(answer="", tools=[("get_portfolio_snapshot", False)]))
    assert verdict["status"] == "unmet"
    assert outcomes(verdict)["answered"] == "failed" and outcomes(verdict)["required_tool"] == "failed"
    assert "get_portfolio_snapshot" in next(item["detail"] for item in verdict["checks"] if item["check"] == "required_tool")


def test_a_goal_question_may_end_with_a_clarifying_question_instead_of_the_calculator():
    contract = tasks.contract_for("general_finance", "finance", ["calculate_goal_plan"])
    assert tasks.evaluate(contract, facts(answer="When do you need the money?"))["status"] == "met"
    assert tasks.evaluate(contract, facts(answer="Save ₹10,000 a month."))["status"] == "unmet"


def test_number_warnings_and_budgets_are_reported_without_failing_the_contract():
    contract = tasks.contract_for("general_finance", "finance", ["calculate_goal_plan"])
    verdict = tasks.evaluate(contract, facts(tools=[("calculate_goal_plan", True)] * 5, model_calls=9, numbers="warning"))
    assert verdict["status"] == "met"
    assert outcomes(verdict)["numbers"] == "warning" and outcomes(verdict)["budget"] == "warning"


def test_a_tool_outside_the_contract_fails_it():
    contract = tasks.contract_for("general_finance", "finance", [])
    assert outcomes(tasks.evaluate(contract, facts(tools=[("networth_snapshot", True)])))["allowed_tools"] == "failed"


# ── Tool results ───────────────────────────────────────────────────────────────

SCHEMA = {"type": "object", "required": ["ind_key"], "properties": {
    "ind_key": {"type": "string"}, "lookback": {"type": "string", "enum": ["1h", "1d", "1y"]}}}


@pytest.mark.parametrize(("arguments", "problems"), [
    ({"ind_key": "INFY", "lookback": "1d"}, []),
    ({"ind_key": "INFY", "lookback": "5d"}, ["lookback must be one of 1h, 1d, 1y (got '5d')"]),
    ({"lookback": "1d"}, ["ind_key is required"]),
    ({"ind_key": None}, ["ind_key is required"]),
    ({"ind_key": 5}, []),  # Types are left to the tool: a server may accept them.
])
def test_argument_problems_cover_required_and_enum_only(arguments, problems):
    assert tool_results.argument_problems(SCHEMA, arguments) == problems


def test_argument_problems_tolerate_missing_or_odd_schemas():
    assert tool_results.argument_problems(None, {"a": 1}) == []
    assert tool_results.argument_problems({"properties": "nope", "required": ["a"]}, {"a": 1}) == []


@pytest.mark.parametrize(("result", "error"), [
    ('{"error":"currency: Input should be INR"}', "currency: Input should be INR"),
    ('{"available": false}', None), ('{"error": ""}', None), ("[1, 2]", None), ("not json {", None), ('{"error"', None),
])
def test_a_local_tool_refusal_is_read_from_a_top_level_error(result, error):
    assert tool_results.reported_error(result) == error


def test_error_payloads_name_their_kind_and_next_step():
    import json
    payload = json.loads(tool_results.error_payload("unavailable", "x" * 2000))
    assert payload["errorKind"] == "unavailable" and len(payload["error"]) == tool_results.MAX_ERROR_CHARS
    assert "Do not call it again" in payload["next_step"]


# ── Recorded events ────────────────────────────────────────────────────────────

class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_the_recorder_numbers_times_and_redacts_events_and_never_keeps_the_answer():
    clock = Clock()
    recorder = RunRecorder(clock)
    recorder.add("model_call", {"provider": "Ollama", "model": "ca-mode", "attempt": 1})
    clock.now = 1.5
    recorder.add("model_result", {"attempt": 1, "status": "completed", "http_status": 200, "prompt_tokens": 100,
                                  "completion_tokens": 20, "tool_calls": ["networth_snapshot"]})
    recorder.add("tool_call", {"name": "networth_snapshot", "source": "indmoney",
                               "arguments": {"access_token": "secret-value", "pan": "ABCDE1234F"}})
    clock.now = 2.0
    recorder.add("tool_result", {"name": "networth_snapshot", "error": "Bearer abc.def", "error_kind": "unavailable"})
    recorder.add("content", {"text": "Your net worth is ₹12 lakh."})
    recorder.add("done", {})
    recorder.terminate("answered")

    events = recorder.events
    assert [event["seq"] for event in events] == [1, 2, 3, 4, 5, 6]
    assert [event["kind"] for event in events] == ["model_call", "model_result", "tool_call", "tool_result", "answer", "termination"]
    assert events[1]["durationMs"] == 1500 and events[1]["promptTokens"] == 100
    assert "secret-value" not in events[2]["arguments"] and "ABCDE1234F" not in events[2]["arguments"]
    assert events[3]["errorKind"] == "unavailable" and events[3]["durationMs"] == 500 and "abc.def" not in events[3]["error"]
    assert events[4] == {"seq": 5, "atMs": 2000, "kind": "answer", "chars": 27}
    assert summarize(events) == {"modelCalls": 1, "toolCalls": 1, "failedToolCalls": 1, "retries": 0,
                                 "promptTokens": 100, "completionTokens": 20, "durationMs": 2000, "termination": "answered"}


def test_facts_come_from_recorded_events():
    recorder = RunRecorder(Clock())
    recorder.add("model_call", {"attempt": 1})
    recorder.add("tool_call", {"name": "calculate_goal_plan", "arguments": {}})
    recorder.add("tool_result", {"name": "calculate_goal_plan", "result": '{"error":"x"}', "error": "x", "error_kind": "rejected"})
    recorder.add("validation", {"check": "numbers", "outcome": "warning"})
    found = tasks.facts_from_events(recorder.events, "Answer")
    assert found.model_calls == 1 and found.number_check == "warning"
    assert found.tool_results == [{"name": "calculate_goal_plan", "ok": False, "errorKind": "rejected"}]

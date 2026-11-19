import json

from fastapi.responses import JSONResponse, PlainTextResponse

from app import tasks
from app.chat_stream import ProgressMapper, response_event, sse


def _mapper(*tools):
    m = ProgressMapper()
    m.start()
    m.map("contract", {"contract": tasks.TaskContract("account_data", allowed_tools=frozenset(tools))})
    return m


def test_sse_frames_one_event():
    assert sse("progress", {"seq": 1}) == b'event: progress\ndata: {"seq":1}\n\n'


def test_start_is_seq_zero():
    assert ProgressMapper().start() == {"seq": 0, "kind": "start"}


def test_tool_name_is_kept_only_when_offered():
    m = _mapper("get_portfolio_snapshot")
    assert m.map("tool_call", {"name": "get_portfolio_snapshot", "arguments": {}, "source": "local"}) == {
        "seq": 1, "kind": "tool_call", "name": "get_portfolio_snapshot", "source": "saved"}
    assert "name" not in m.map("tool_call", {"name": "made_up_tool", "arguments": {}, "source": "local"})


def test_contract_parts_are_remembered():
    m = ProgressMapper()
    part = tasks.TaskContract("goal_calculation", allowed_tools=frozenset({"calculate_goal_plan"}))
    m.map("contract", {"contract": tasks.TaskContract("multi_step", parts=(part,))})
    assert m.map("tool_call", {"name": "calculate_goal_plan", "arguments": {}, "source": "local"})["name"] == "calculate_goal_plan"


def test_sources_per_tool_kind():
    m = _mapper()
    src = lambda name, args=None, source="local": m.map("tool_call", {"name": name, "arguments": args or {}, "source": source})["source"]
    assert src("get_networth", source="indmoney") == "indmoney"
    assert src("get_broker_holdings", {"broker": "zerodha"}) == "broker:zerodha"
    assert src("call_broker_tool", '{"broker":"zerodha"}') == "broker:zerodha"
    assert src("get_broker_holdings", {"broker": "nope"}) == "broker"
    assert src("get_broker_holdings", "not json") == "broker"
    assert src("calculate") == "analysis"
    assert src("get_portfolio_snapshot") == "saved"


def test_progress_never_carries_arguments_results_or_figures():
    m = _mapper("get_portfolio_snapshot")
    mapped = [
        m.map("tool_call", {"name": "get_portfolio_snapshot", "arguments": {"amount": 125000}, "source": "local"}),
        m.map("tool_result", {"name": "get_portfolio_snapshot", "result": "₹1,25,000"}),
        m.map("retry", {"reason": "no_account_tool", "detail": "worth ₹1,25,000"}),
        m.map("plan", {"stage": "initial", "reason": "125000 rupees"}),
    ]
    text = json.dumps(mapped)
    assert "125000" not in text and "1,25,000" not in text
    for item in mapped:
        assert set(item) <= {"seq", "kind", "name", "source", "status"}


def test_free_text_names_are_dropped():
    m = _mapper()
    assert "name" not in m.map("retry", {"reason": "Your balance is 5 lakh"})


def test_tool_result_status_and_planned_step_name():
    m = _mapper("get_portfolio_snapshot")
    m.map("tool_call", {"name": "get_portfolio_snapshot", "arguments": {}, "source": "local"})
    assert m.map("tool_result", {"error_kind": "failed"})["status"] == "failed"
    assert m.map("tool_result", {"errorKind": "auth"})["status"] == "failed"
    planned = m.map("tool_result", {"step": "s1", "result": "x"})
    assert planned["status"] == "ok" and planned["name"] == "get_portfolio_snapshot"
    assert m.map("tool_result", {"name": "get_portfolio_snapshot", "error": "boom"})["status"] == "failed"


def test_simple_events():
    m = ProgressMapper()
    assert m.map("route", {"category": "account_data", "source": "model"}) == {"seq": 0, "kind": "route", "name": "account_data"}
    assert m.map("context", {"items": ["a goal"]}) == {"seq": 1, "kind": "context"}
    assert m.map("model_call", {"provider": "x"}) == {"seq": 2, "kind": "model_call"}
    assert m.map("validation", {"check": "numbers", "outcome": "ok"}) == {"seq": 3, "kind": "validation", "name": "numbers"}
    assert m.waiting() == {"seq": 4, "kind": "waiting"}


def test_unsent_events_map_to_none():
    m = ProgressMapper()
    for event in ("content", "model_result", "plan_stop", "done", "error", "other"):
        assert m.map(event, {}) is None


def test_response_event_for_an_answer():
    response = PlainTextResponse("Hello", headers={
        "X-Agent-Trace": json.dumps([{"kind": "route"}]), "X-Agent-Run": "r1", "X-Questions-Remaining": "4",
        "X-Credits-Remaining": "90", "X-Credits-Charged": "2"})
    assert response_event(response) == ("answer", {
        "text": "Hello", "trace": [{"kind": "route"}], "runId": "r1", "questionsRemaining": 4,
        "creditsRemaining": 90, "creditsCharged": 2})
    assert response_event(PlainTextResponse("Hi")) == ("answer", {"text": "Hi", "trace": []})


def test_response_event_for_a_failure():
    response = JSONResponse({"error": "busy", "modelBusy": True, "retryAfterSeconds": 5}, status_code=503)
    assert response_event(response) == ("error", {"status": 503, "trace": [], "error": "busy", "modelBusy": True, "retryAfterSeconds": 5})

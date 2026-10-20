from __future__ import annotations

import asyncio
import copy
import json
from contextlib import asynccontextmanager

import pytest
from websockets.asyncio.server import serve
from websockets.http11 import Response
from websockets.datastructures import Headers

from app import agent, code_runner, tool_results
from app.tool_results import ToolOutcome

SECRET = "test-runner-secret"


# ── A stand-in for the Worker: replays scripted api calls and returns what came back ──

@asynccontextmanager
async def fake_worker(calls=(), *, done=None, refuse=False):
    """Serve the Worker protocol. ``calls`` are ``(name, args)`` sent in order; ``seen`` records every frame."""
    seen: dict = {"run": None, "results": [], "aborted": False, "auth": None}

    def check_auth(connection, request):
        seen["auth"] = request.headers.get("Authorization")
        if refuse or seen["auth"] != f"Bearer {SECRET}":
            return Response(401, "Unauthorized", Headers(), b"Unauthorized")
        return None

    async def handler(socket):
        seen["run"] = json.loads(await socket.recv())
        for index, (name, args) in enumerate(calls, start=1):
            await socket.send(json.dumps({"type": "call", "id": index, "name": name, "args": args}))
            reply = json.loads(await socket.recv())
            if reply["type"] == "abort":
                seen["aborted"] = True
                return
            seen["results"].append(reply)
        values = [reply.get("value") if reply["ok"] else {"threw": reply["error"]} for reply in seen["results"]]
        await socket.send(json.dumps(done or {"type": "done", "result": values, "error": None, "logs": ["ran"]}))

    async with serve(handler, "127.0.0.1", 0, process_request=check_auth) as server:
        port = server.sockets[0].getsockname()[1]
        yield code_runner.RunnerConfig(f"ws://127.0.0.1:{port}", SECRET, 5), seen


def scripted_dispatch(outcomes):
    """A dispatch that answers each tool name with a fixed ToolOutcome and records the calls."""
    made = []

    async def dispatch(name, args):
        made.append((name, args))
        yield "tool_call", {"name": name, "arguments": args, "source": "indmoney"}
        yield "tool_result", {"name": name}
        yield code_runner.OUTCOME_EVENT, {"outcome": outcomes[name]}

    return dispatch, made


async def collect(code, api, dispatch, config):
    events = [event async for event in code_runner.execute(code, api, dispatch, config)]
    assert events[-1][0] == code_runner.OUTCOME_EVENT
    return events[:-1], events[-1][1]["outcome"]


# ── Configuration and what the model sees ──

def test_code_mode_is_off_unless_selected_and_configured(monkeypatch):
    monkeypatch.delenv("TOOL_MODE", raising=False)
    monkeypatch.setenv("CODE_RUNNER_URL", "https://runner.example.workers.dev")
    monkeypatch.setenv("CODE_RUNNER_SECRET", SECRET)
    assert code_runner.runner_config() is None
    monkeypatch.setenv("TOOL_MODE", "code")
    config = code_runner.runner_config()
    assert config == code_runner.RunnerConfig("wss://runner.example.workers.dev", SECRET, 60)
    monkeypatch.setenv("CODE_RUNNER_URL", "http://127.0.0.1:8787")
    monkeypatch.setenv("CODE_RUNNER_TIMEOUT_SECONDS", "30")
    assert code_runner.runner_config() == code_runner.RunnerConfig("ws://127.0.0.1:8787", SECRET, 30)
    monkeypatch.delenv("CODE_RUNNER_SECRET")
    assert code_runner.runner_config() is None


def test_api_declarations_turn_schemas_into_typed_calls():
    text = code_runner.api_declarations([
        {"name": "networth_holdings", "description": "Holdings */ by asset type.\n Second line.", "inputSchema": {
            "type": "object", "required": ["asset_type"],
            "properties": {"asset_type": {"type": "string", "enum": ["MF", "IND_STOCK"], "description": "Asset class, as INDmoney names it."},
                           "limit": {"type": "integer"},
                           "tags": {"type": "array", "items": {"type": "string"}}, "flag": {"type": ["boolean", "null"]}},
        }},
        {"name": "networth_snapshot", "description": "", "inputSchema": {"type": "object", "properties": {}}},
    ])
    assert """  networth_holdings(args: {
    /** Asset class, as INDmoney names it. */
    asset_type: "MF" | "IND_STOCK";
    limit?: number;
    tags?: string[];
    flag?: boolean;
  }): Promise<any>;""" in text
    assert "networth_snapshot(args?: Record<string, any>): Promise<any>;" in text
    assert "/** Holdings * / by asset type. Second line. */" in text


def test_code_argument_reads_only_a_code_string():
    assert code_runner.code_argument('{"code": "return 1"}') == "return 1"
    assert code_runner.code_argument({"code": "return 1"}) == "return 1"
    for raw in ("", "not json", '{"code": "  "}', '{"code": 5}', "[1]", None):
        assert code_runner.code_argument(raw) is None


# ── The protocol ──

@pytest.mark.asyncio
async def test_calls_are_dispatched_and_results_reach_the_code():
    outcomes = {
        "networth_snapshot": ToolOutcome('{"total": 42000}'),
        "market_text": ToolOutcome("Nifty closed higher"),
        "bad": tool_results.failure("rejected", "asset_type must be one of MF"),
    }
    dispatch, made = scripted_dispatch(outcomes)
    async with fake_worker([("networth_snapshot", {}), ("market_text", None), ("bad", {"asset_type": "X"})]) as (config, seen):
        events, outcome = await collect("return 1", ["networth_snapshot", "market_text", "bad"], dispatch, config)

    assert seen["auth"] == f"Bearer {SECRET}"
    assert seen["run"] == {"type": "run", "code": "return 1", "api": ["bad", "market_text", "networth_snapshot"],
                           "timeoutMs": 5000, "maxCalls": code_runner.MAX_CALLS_PER_RUN}
    assert made == [("networth_snapshot", {}), ("market_text", None), ("bad", {"asset_type": "X"})]
    assert [event[0] for event in events] == ["tool_call", "tool_result"] * 3
    # JSON results arrive parsed, text as text, and a failure as an error the code throws.
    assert seen["results"][0] == {"type": "result", "id": 1, "ok": True, "value": {"total": 42000}}
    assert seen["results"][1]["value"] == "Nifty closed higher"
    assert seen["results"][2]["ok"] is False and seen["results"][2]["error"].startswith("rejected: asset_type must be one of MF.")
    assert outcome.status == "completed" and outcome.calls == 3 and outcome.logs == ["ran"]
    assert json.loads(outcome.content)["result"][0] == {"total": 42000}


@pytest.mark.asyncio
async def test_an_api_name_not_offered_is_refused_without_dispatch():
    dispatch, made = scripted_dispatch({})
    async with fake_worker([("place_order", {"qty": 1})]) as (config, seen):
        events, outcome = await collect("x", ["networth_snapshot"], dispatch, config)
    assert made == [] and events == []
    assert seen["results"][0]["ok"] is False and "does not exist" in seen["results"][0]["error"]
    assert outcome.status == "completed"


@pytest.mark.asyncio
async def test_calls_over_the_limit_are_refused(monkeypatch):
    monkeypatch.setattr(code_runner, "MAX_CALLS_PER_RUN", 1)
    dispatch, made = scripted_dispatch({"a": ToolOutcome("1")})
    async with fake_worker([("a", {}), ("a", {})]) as (config, seen):
        await collect("x", ["a"], dispatch, config)
    assert len(made) == 1
    assert seen["results"][1]["ok"] is False and "More than 1 api calls" in seen["results"][1]["error"]


@pytest.mark.asyncio
async def test_a_fatal_call_aborts_the_snippet():
    fatal = {"message": "INDmoney data request failed: 401", "code": "indmoney_auth", "auth_failed": True}
    dispatch, _ = scripted_dispatch({"a": ToolOutcome("", "auth", "401", fatal=fatal), "b": ToolOutcome("never")})
    async with fake_worker([("a", {}), ("b", {})]) as (config, seen):
        _, outcome = await collect("x", ["a", "b"], dispatch, config)
    assert seen["aborted"] is True
    assert outcome.status == "stopped" and outcome.fatal == fatal and not outcome.unreachable


@pytest.mark.asyncio
async def test_code_errors_and_large_results_come_back_to_the_model():
    dispatch, _ = scripted_dispatch({})
    done = {"type": "done", "result": None, "error": "TypeError: x is undefined", "logs": ["before"]}
    async with fake_worker(done=done) as (config, _seen):
        _, outcome = await collect("x", [], dispatch, config)
    payload = json.loads(outcome.content)
    assert outcome.status == "failed" and payload["errorKind"] == "rejected" and "x is undefined" in payload["error"]
    assert payload["logs"] == ["before"] and not outcome.unreachable

    big = {"type": "done", "result": ["row" * 10] * 2000, "error": None, "logs": []}
    async with fake_worker(done=big) as (config, _seen):
        _, outcome = await collect("x", [], dispatch, config)
    assert len(outcome.content) <= code_runner.MAX_RESULT_CHARS and outcome.content.endswith("return only the fields you need]")


@pytest.mark.asyncio
async def test_a_runner_that_refuses_or_is_down_is_unreachable():
    dispatch, _ = scripted_dispatch({})
    async with fake_worker(refuse=True) as (config, _seen):
        _, outcome = await collect("x", [], dispatch, config)
    assert outcome.status == "unavailable" and outcome.unreachable and "HTTP 401" in outcome.error

    async with fake_worker() as (config, _seen):
        closed_port = config.url
    _, outcome = await collect("x", [], dispatch, code_runner.RunnerConfig(closed_port, SECRET, 5))
    assert outcome.status == "unavailable" and outcome.unreachable
    assert json.loads(outcome.content)["next_step"] == code_runner.UNREACHABLE_NEXT_STEP


@pytest.mark.asyncio
async def test_a_snippet_that_never_finishes_times_out_without_counting_as_unreachable(monkeypatch):
    async def handler(socket):
        await socket.recv()
        await socket.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = code_runner.RunnerConfig(f"ws://127.0.0.1:{port}", SECRET, 1)
        real_timeout = asyncio.timeout
        monkeypatch.setattr(code_runner.asyncio, "timeout", lambda seconds: real_timeout(0.5))
        _, outcome = await collect("x", [], scripted_dispatch({})[0], config)
    assert outcome.status == "failed" and not outcome.unreachable and "did not finish" in outcome.error


# ── The agent in code mode ──

class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200
        self.is_success = True
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, responses, captured):
        self.responses = list(responses)
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, headers, json):
        self.captured.append(copy.deepcopy(json))  # The agent keeps appending to the same messages list.
        return self.responses.pop(0)


def completion(content=None, calls=()):
    tool_calls = [{"id": f"call-{index}", "function": {"name": name, "arguments": arguments}}
                  for index, (name, arguments) in enumerate(calls)]
    return FakeResponse({"choices": [{"finish_reason": "stop", "message": {"content": content, "tool_calls": tool_calls}}]})


MCP_TOOLS = [
    {"name": "networth_snapshot", "description": "Portfolio total", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "networth_holdings", "description": "Holdings", "inputSchema": {
        "type": "object", "required": ["asset_type"], "properties": {"asset_type": {"type": "string", "enum": ["MF"]}}}},
]


@pytest.fixture
def indmoney(monkeypatch):
    calls = []

    async def fake_fetch(url, token):
        return {"tools": MCP_TOOLS, "protocolVersion": "v", "sessionId": "sid"}

    async def fake_call(url, token, protocol, session_id, name, arguments, *, client=None):
        calls.append((name, arguments))
        return '{"total_current_value": 42000}'

    monkeypatch.setattr(agent, "fetch_mcp_session", fake_fetch)
    monkeypatch.setattr(agent, "call_mcp_tool", fake_call)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return calls


def use_model(monkeypatch, client):
    """Every httpx.AsyncClient is the same fake, so a rerun of the answer continues its replies."""
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: client)


def use_runner(monkeypatch, config):
    monkeypatch.setenv("TOOL_MODE", "code")
    monkeypatch.setenv("CODE_RUNNER_URL", config.url)
    monkeypatch.setenv("CODE_RUNNER_SECRET", SECRET)


CODE = '{"code": "const s = await api.networth_snapshot({}); return s.total_current_value;"}'


@pytest.mark.asyncio
async def test_portfolio_answer_reads_data_through_code(monkeypatch, indmoney):
    captured = []
    use_model(monkeypatch, FakeClient(
        [completion(calls=[("run_code", CODE)]), completion("Your portfolio is worth ₹42,000.")], captured))
    async with fake_worker([("networth_snapshot", {}), ("networth_holdings", {"asset_type": "STOCK"})]) as (config, seen):
        use_runner(monkeypatch, config)
        answer = await agent.run_agent("What is my portfolio worth?", "https://mcp", "token", [], "portfolio", "openai")

    assert answer == "Your portfolio is worth ₹42,000."
    # The model is offered one tool, and the prompt declares every tool as an api function.
    assert [tool["function"]["name"] for tool in captured[0]["tools"]] == ["run_code"]
    system = captured[0]["messages"][0]["content"]
    assert seen["run"]["api"] == ["networth_holdings", "networth_snapshot"]
    assert "networth_snapshot(args?: Record<string, any>)" in system and 'asset_type: "MF";' in system
    # The holdings call breaks the enum, so it is refused here before INDmoney sees it.
    assert indmoney == [("networth_snapshot", {})]
    assert seen["results"][1]["ok"] is False and "invalid_arguments" in seen["results"][1]["error"]
    assert captured[1]["messages"][-1]["role"] == "tool"
    assert json.loads(captured[1]["messages"][-1]["content"])["result"][0] == {"total_current_value": 42000}

    kinds = [event["kind"] for event in answer.events]
    assert kinds[kinds.index("code_run"):kinds.index("code_result") + 1] == [
        "code_run", "tool_call", "tool_result", "tool_call", "tool_result", "code_result"]
    code_result = next(event for event in answer.events if event["kind"] == "code_result")
    assert code_result["status"] == "completed" and code_result["calls"] == 2
    assert answer.verdict["status"] == "met"
    steps = [step["kind"] for step in answer.trace]
    assert steps.index("code") < steps.index("tool")
    assert agent.summarize(answer.events)["codeRuns"] == 1


@pytest.mark.asyncio
async def test_an_unreachable_runner_reruns_the_answer_with_direct_tools(monkeypatch, indmoney):
    captured = []
    use_model(monkeypatch, FakeClient([
        completion(calls=[("run_code", CODE)]),
        completion(calls=[("networth_snapshot", "{}")]),
        completion("Your portfolio is worth ₹42,000."),
    ], captured))
    async with fake_worker(refuse=True) as (config, _seen):
        use_runner(monkeypatch, config)
        answer = await agent.run_agent("What is my portfolio worth?", "https://mcp", "token", [], "portfolio", "openai")

    assert answer == "Your portfolio is worth ₹42,000."
    assert [tool["function"]["name"] for tool in captured[1]["tools"]] == ["networth_snapshot", "networth_holdings"]
    assert "run_code" not in captured[1]["messages"][0]["content"]
    retry = next(event for event in answer.events if event["kind"] == "retry")
    assert retry["reason"] == "direct_tools_fallback"
    assert [event["kind"] for event in answer.events].count("route") == 1
    assert indmoney == [("networth_snapshot", {})] and answer.verdict["status"] == "met"


@pytest.mark.asyncio
async def test_indmoney_rejecting_the_token_inside_code_ends_the_answer(monkeypatch, indmoney):
    async def rejecting_call(*args, **kwargs):
        raise agent.McpAuthError("INDmoney returned 401")

    monkeypatch.setattr(agent, "call_mcp_tool", rejecting_call)
    use_model(monkeypatch, FakeClient([completion(calls=[("run_code", CODE)])], []))
    async with fake_worker([("networth_snapshot", {})]) as (config, seen):
        use_runner(monkeypatch, config)
        with pytest.raises(agent.AgentRunError) as exc_info:
            await agent.run_agent("What is my portfolio worth?", "https://mcp", "token", [], "portfolio", "openai")
    assert exc_info.value.auth_failed is True and seen["aborted"] is True


@pytest.mark.asyncio
async def test_a_tool_called_outside_run_code_is_sent_back(monkeypatch, indmoney):
    captured = []
    use_model(monkeypatch, FakeClient([
        completion(calls=[("networth_snapshot", "{}")]),
        completion(calls=[("run_code", '{"code": ""}')]),
        completion(calls=[("run_code", CODE)]),
        completion("Your portfolio is worth ₹42,000."),
    ], captured))
    async with fake_worker([("networth_snapshot", {})]) as (config, _seen):
        use_runner(monkeypatch, config)
        answer = await agent.run_agent("What is my portfolio worth?", "https://mcp", "token", [], "portfolio", "openai")

    first, second = (json.loads(captured[index]["messages"][-1]["content"]) for index in (1, 2))
    assert "api.networth_snapshot" in first["next_step"]
    assert second["errorKind"] == "invalid_arguments" and "code" in second["error"]
    assert answer == "Your portfolio is worth ₹42,000."
    assert [event["status"] for event in answer.events if event["kind"] == "code_result"] == ["failed", "completed"]


@pytest.mark.asyncio
async def test_finance_agent_goal_tools_go_through_code_too(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    calls = []

    async def calculator(arguments):
        calls.append(arguments)
        return '{"requiredMonthly": 8000}'

    tool = agent.LocalTool("calculate_goal_plan", "Plan a goal", {"type": "object", "properties": {}}, calculator)
    captured = []
    use_model(monkeypatch, FakeClient([
        completion(calls=[("run_code", '{"code": "return api.calculate_goal_plan({targetAmount: 100000})"}')]),
        completion("You would need ₹8,000 a month."),
    ], captured))
    async with fake_worker([("calculate_goal_plan", {"targetAmount": 100000})]) as (config, seen):
        use_runner(monkeypatch, config)
        answer = await agent.run_agent(
            "How much should I save each month for ₹1 lakh in a year?", "https://unused", None, [], "finance", "openai",
            context=agent.PortfolioContext(local_tools=(tool,)),
        )
    assert seen["run"]["api"] == ["calculate_goal_plan"]
    assert calls == [{"targetAmount": 100000}]
    assert answer.startswith("You would need ₹8,000 a month.")
    assert answer.verdict["status"] == "met"


@pytest.mark.asyncio
async def test_figures_from_results_the_snippet_did_not_return_still_count_as_tool_data(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    async def calculator(arguments):
        return '{"requiredMonthly": 46875, "gap": 4500000}'

    tool = agent.LocalTool("calculate_goal_plan", "Plan a goal", {"type": "object", "properties": {}}, calculator)
    use_model(monkeypatch, FakeClient([
        completion(calls=[("run_code", '{"code": "return (await api.calculate_goal_plan({})).requiredMonthly"}')]),
        completion("You need ₹46,875 a month to close the ₹45 lakh gap, not ₹99,999."),
    ], []))
    done = {"type": "done", "result": 46875, "error": None, "logs": []}
    async with fake_worker([("calculate_goal_plan", {})], done=done) as (config, _seen):
        use_runner(monkeypatch, config)
        answer = await agent.run_agent(
            "How much a month for ₹50 lakh in 8 years with ₹5 lakh saved?", "https://unused", None, [], "finance", "openai",
            context=agent.PortfolioContext(local_tools=(tool,)),
        )
    check = next(event for event in answer.events if event["kind"] == "validation" and event["check"] == "numbers")
    # The gap came from the calculator although the snippet returned only the monthly amount; the made-up figure did not.
    assert check["outcome"] == "warning" and check["detail"] == "₹99,999"

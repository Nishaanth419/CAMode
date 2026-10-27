from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from app import agent, agent_graph
from scripts import eval_workflows as ev
from tests.fixtures import indmoney_market as market

CONDOR = "Build me an iron condor on Nifty for the next monthly expiry with 15-delta short legs."


def reply(content=None, calls=()):
    tool_calls = [{"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                  for i, (name, args) in enumerate(calls)]
    return {"choices": [{"finish_reason": "tool_calls" if calls else "stop", "message": {"content": content, "tool_calls": tool_calls}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


class Provider:
    """Answers model calls from a script and keeps every request payload."""

    def __init__(self, replies):
        self.replies, self.payloads = list(replies), []

    def __call__(self, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, headers=None, json=None):
        self.payloads.append(json)
        return httpx.Response(200, json=self.replies.pop(0), request=httpx.Request("POST", url))


def run(monkeypatch, message, replies, *, case=None, slow_seconds=0.0):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    provider = Provider(replies)
    monkeypatch.setattr(agent.httpx, "AsyncClient", provider)
    case = {"message": message, **(case or {})}
    fake = ev.FakeIndmoney(case.get("market", {}), case.get("mcp", {}))
    if slow_seconds:
        call = fake.call

        async def slow(*args, **kwargs):
            await asyncio.sleep(slow_seconds)
            return await call(*args, **kwargs)

        fake.call = slow
    with ev.patched(fake, case) as raw:
        try:
            result = asyncio.run(agent.run_agent(message, "https://mcp.invalid", "token", [], "portfolio", "openai",
                                                 context=ev.build_context(case)))
            answer, events = str(result), result.events
        except agent.AgentRunError as exc:
            answer, events = "", exc.events
        trace = result.trace if answer else exc.trace
    return answer, events, provider.payloads, raw, trace


def test_a_large_result_reaches_the_model_as_an_outline_but_counts_in_full(monkeypatch):
    nearest = ["get_indian_stocks_option_chain", {"ind_key": market.NIFTY_KEY, "use_expiry_date": False, "strikes_around_atm": 30}]
    leg = market.chain(market.NEAREST)["option_chain_data"][33]["put_data"]
    answer, events, payloads, _, _ = run(monkeypatch, CONDOR, [
        reply(calls=[nearest]),
        reply(f"The {33} put's ask is ₹{leg['sell_price']}. This is information, not investment advice.")])
    tool_message = payloads[1]["messages"][-1]
    view = json.loads(tool_message["content"])
    assert view["resultId"] == "r1" and view["outline"]["expiry_list"] == market.EXPIRIES
    assert len(tool_message["content"]) < 4000 < len(json.dumps(market.chain(market.NEAREST)))
    # The figure was in the full result, not in the outline the model saw: still sourced.
    assert next(e for e in events if e["kind"] == "validation" and e["check"] == "numbers")["outcome"] == "passed"


def test_the_agent_gets_the_analysis_tools_and_the_dated_guide(monkeypatch):
    _, _, payloads, _, _ = run(monkeypatch, "What is my net worth?", [reply(calls=[["networth_snapshot", {}]]), reply("Done.")])
    names = {tool["function"]["name"] for tool in payloads[0]["tools"]}
    assert {"inspect_result", "calculate", "option_strategy", "price_levels", "get_indian_stocks_option_chain"} <= names
    system = payloads[0]["messages"][0]["content"]
    assert "Today is Wednesday 28 October 2026, 10:30 IST." in system and "last expiry listed in that month" in system


def test_the_previous_loop_is_used_when_the_agent_is_off(monkeypatch):
    _, events, payloads, _, _ = run(monkeypatch, "What is my net worth?", [reply(calls=[["networth_snapshot", {}]]), reply("Done.")],
                                    case={"env": {"AGENT_GRAPH_ENABLED": "false"}})
    assert "calculate" not in {tool["function"]["name"] for tool in payloads[0]["tools"]}
    assert "Today is" not in payloads[0]["messages"][0]["content"]


def test_calls_in_one_reply_run_together(monkeypatch):
    calls = [["get_indian_stocks_ohlc", {"ind_key": key, "interval": "1day", "lookback": "1y"}] for key in ("A", "B", "C", "D")]
    started = time.perf_counter()
    _, events, _, _, _ = run(monkeypatch, "Scan my watchlist for breakouts", [reply(calls=calls), reply("Done.")], slow_seconds=0.3)
    assert time.perf_counter() - started < 1.0  # four 0.3 s calls, at most four at once
    durations = [e["durationMs"] for e in events if e["kind"] == "tool_result"]
    assert len(durations) == 4 and all(d >= 300 for d in durations)


def test_past_the_deadline_the_model_answers_without_tools(monkeypatch):
    monkeypatch.setattr(agent_graph, "DEADLINE_SECONDS", -1)
    answer, events, payloads, _, _ = run(monkeypatch, "What is my net worth?", [reply("I could not fetch the data in time.")])
    assert "tools" not in payloads[0] and payloads[0]["messages"][-1]["content"] == agent_graph.FINAL_PROMPT
    assert next(e for e in events if e["kind"] == "plan_stop")["reason"] == "deadline"
    assert answer.startswith("I could not fetch")


def test_a_local_tools_evidence_link_reaches_the_trace(monkeypatch):
    async def investigate(arguments):
        return json.dumps({"investigationId": "inv-7", "status": "completed"})

    monkeypatch.setattr(ev, "build_context", lambda case: agent.PortfolioContext(
        local_tools=(agent.LocalTool("investigate_portfolio_change", "Investigate.", {"type": "object", "properties": {}}, investigate),),
        cached_mcp_tools=ev.TOOLS))
    _, _, _, _, trace = run(monkeypatch, "Why did my portfolio drop?",
                            [reply(calls=[["investigate_portfolio_change", {}]]), reply("It fell because...")])
    step = next(s for s in trace if s.get("kind") == "tool")
    assert step["investigation_id"] == "inv-7" and step["source"] == "local"


def test_analysis_results_are_not_stored_or_outlined(monkeypatch):
    _, _, payloads, _, _ = run(monkeypatch, "What is 2 + 2 of my net worth?", [
        reply(calls=[["networth_snapshot", {}], ["calculate", {"expressions": [{"name": "x", "expr": "2 + 2"}]}]]), reply("Done.")])
    tool_messages = [m for m in payloads[1]["messages"] if m["role"] == "tool"]
    assert json.loads(tool_messages[1]["content"])["results"] == {"x": 4.0}
    assert "resultId" not in tool_messages[1]["content"]


@pytest.mark.parametrize("value, expected", [("true", True), ("", True), ("false", False), ("0", False), ("off", False)])
def test_enabled_flag(monkeypatch, value, expected):
    monkeypatch.setenv("AGENT_GRAPH_ENABLED", value)
    assert agent_graph.enabled() is expected and agent._agent_graph_enabled() is expected

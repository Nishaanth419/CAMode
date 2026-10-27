"""Score the agent's task handling on evals/agent_tasks.jsonl, graded in code from the recorded events.

Scripted mode (default, offline): each case's model replies come from the dataset, so the run
tests CA Mode's own controller: the task contract and its verdict, how the run ended, which
retries happened and how tool failures were classified. It runs in the test suite.

Live mode (``--provider``): the cases with a ``live`` block go to the real model with the same
tools (the real goal calculator, the synthetic snapshot from tests/fixtures, INDmoney tools
stubbed with the case's behaviour) and are graded on the outcome: contract met, figures traced
to a tool, expected figures stated, a clarifying question asked, no invented amounts, tool calls
rejected, model calls. Latency and tokens are reported per case.

    python -m scripts.eval_tasks                          # scripted, offline
    python -m scripts.eval_tasks --provider ollama --runs 3
    python -m scripts.eval_tasks --dataset evals/multi_step.jsonl [--provider ollama --runs 3]

evals/multi_step.jsonl holds messages with several kinds of task, answered with a plan
(``app.planner``): its cases also check the plan events, how the plan stopped, which tools ran in
which order, repeated calls and the planner's budgets.

Exit status is 1 when a scripted case fails, or a live run passes fewer than ``--min-pass`` cases.
Nothing touches .data/ or the real master key.
"""

from __future__ import annotations

import os
import sys
import tempfile

if __name__ == "__main__":
    # Before importing app: app.db reads APP_DATA_DIR at import, and .env may point it at real data.
    os.environ["APP_DATA_DIR"] = tempfile.mkdtemp(prefix="ca-mode-eval-tasks-")
    os.environ.pop("APP_MASTER_KEY_FILE", None)
    from cryptography.fernet import Fernet

    os.environ.setdefault("APP_MASTER_KEY", Fernet.generate_key().decode())
    os.environ.setdefault("REQUEST_ROUTER", "keywords")  # Measure answers, not routing; REQUEST_ROUTER=model includes it.
    # These cases script the previous loop and planner; scripts.eval_workflows measures the general agent.
    os.environ.setdefault("AGENT_GRAPH_ENABLED", "false")

import argparse  # noqa: E402
import asyncio  # noqa: E402
import contextlib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import statistics  # noqa: E402
import time  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from datetime import date  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Final  # noqa: E402

import httpx  # noqa: E402

from app import agent, goal_portfolio, mcp_client, snapshots, tasks  # noqa: E402
from app.agent_events import summarize  # noqa: E402
from app.context import task_parts, wants_goal_calculator  # noqa: E402
from scripts.eval_numbers import mentions  # noqa: E402
from tests.fixtures import indmoney_sample  # noqa: E402

EVAL_VERSION: Final = "agent_tasks.v1"
DATASET: Final = Path(__file__).resolve().parent.parent / "evals" / "agent_tasks.jsonl"
MULTI_STEP_DATASET: Final = DATASET.with_name("multi_step.jsonl")
TODAY: Final = date(2026, 10, 16)
SCRIPTED_PROVIDER: Final = "openai"

# INDmoney tools the cases may offer, with input schemas in the server's shape.
MCP_TOOLS: Final[dict[str, dict[str, Any]]] = {
    "networth_snapshot": {"description": "Live INDmoney net worth summary.", "inputSchema": {"type": "object", "properties": {}}},
    "lookup_ind_keys": {"description": "Resolve a stock, index or fund name to its INDmoney key.",
                        "inputSchema": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}}},
    "get_indian_stocks_details": {"description": "Live price and details of an Indian stock.",
                                  "inputSchema": {"type": "object", "required": ["ind_key"], "properties": {"ind_key": {"type": "string"}}}},
    "get_indian_stocks_ohlc": {"description": "OHLC candles for an Indian stock.", "inputSchema": {
        "type": "object", "required": ["ind_key"],
        "properties": {"ind_key": {"type": "string"}, "lookback": {"type": "string", "enum": ["1d", "7d", "14d", "1y"]}}}},
}
MCP_RESULT: Final = json.dumps({"ind_key": "INFY", "name": "Infosys", "candles": []})
_AMOUNT: Final = re.compile(r"(?:₹|rs\.?\s?|inr\s?)\s?\d|\d\s?(?:lakh|lac|crore|cr)\b", re.IGNORECASE)


def load_cases(dataset: Path = DATASET) -> list[dict[str, Any]]:
    return [json.loads(line) for line in dataset.read_text().splitlines() if line.strip()]


# ── Tools ──────────────────────────────────────────────────────────────────────

def _snapshot_tool(*, raises: bool) -> agent.LocalTool:
    snapshot = {"asOfDate": "2026-10-14", "capturedAt": int(time.time() * 1000), "results": indmoney_sample.results(), "errors": {}}
    real = next(tool for tool in snapshots.snapshot_tools("unused", snapshots.ProfileKeys()) if tool.name == "get_portfolio_snapshot")

    async def handler(arguments: dict[str, Any]) -> str:
        if raises:
            raise RuntimeError("snapshot store unavailable")
        payload = snapshots._snapshot_payload(snapshot, include_all_holdings=arguments.get("include_all_holdings") is True)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    return agent.LocalTool(real.name, real.description, real.input_schema, handler)


def build_context(case: dict[str, Any]) -> agent.PortfolioContext | None:
    """The tools ``main.chat`` would offer for this case: local tools by name, INDmoney tools as a cached list."""
    names = case.get("tools", [])
    raising = set(case.get("raises", []))
    local: list[agent.LocalTool] = []
    if "calculate_goal_plan" in names or (case.get("live_mode") and wants_goal_calculator(case["message"], case.get("history", []))):
        local.append(goal_portfolio.goal_calculator_tool(today=TODAY, user_text=case["message"]))
    if "get_portfolio_snapshot" in names:
        local.append(_snapshot_tool(raises="get_portfolio_snapshot" in raising))
    cached = [{"name": name, **MCP_TOOLS[name]} for name in names if name in MCP_TOOLS]
    if not local and not cached:
        return None
    return agent.PortfolioContext(local_tools=tuple(local), cached_mcp_tools=cached or None)


@contextlib.contextmanager
def stubbed_indmoney(behaviours: dict[str, str]) -> Iterator[list[str]]:
    """Answer INDmoney calls by the case's behaviour (ok, rejected, unavailable, auth) without the network."""
    calls: list[str] = []
    fetch, call = agent.fetch_mcp_session, agent.call_mcp_tool

    async def fake_session(url: str, token: str) -> dict[str, Any]:
        return {"tools": [{"name": name, **spec} for name, spec in MCP_TOOLS.items()], "protocolVersion": "eval", "sessionId": None}

    async def fake_call(url: str, token: str, protocol: str, session_id: str | None, name: str,
                        arguments: dict[str, Any], *, client: Any = None) -> str:
        calls.append(name)
        behaviour = behaviours.get(name, behaviours.get("*", "ok"))
        if behaviour == "rejected":
            raise mcp_client.McpToolError(f'MCP tool "{name}" failed: no instrument matches that query')
        if behaviour == "unavailable":
            raise RuntimeError(f'MCP tools/call failed for "{name}": 503 Service Unavailable')
        if behaviour == "auth":
            raise mcp_client.McpAuthError(f'MCP tools/call failed for "{name}": 401 ')
        return MCP_RESULT

    agent.fetch_mcp_session, agent.call_mcp_tool = fake_session, fake_call
    try:
        yield calls
    finally:
        agent.fetch_mcp_session, agent.call_mcp_tool = fetch, call


# ── Scripted provider ──────────────────────────────────────────────────────────

def _completion(step: dict[str, Any], index: int) -> dict[str, Any]:
    if "calls" in step:
        calls = [{"id": f"call-{index}-{n}", "type": "function", "function": {
            "name": name, "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)}}
            for n, (name, arguments) in enumerate(step["calls"])]
        message = {"role": "assistant", "content": None, "tool_calls": calls}
        finish = "tool_calls"
    else:
        message, finish = {"role": "assistant", "content": step.get("content", "")}, "stop"
    return {"choices": [{"message": message, "finish_reason": finish}], "usage": {"prompt_tokens": 100, "completion_tokens": 10}}


def scripted_replies(script: list[dict[str, Any]]) -> list[dict[str, Any] | Exception]:
    replies: list[dict[str, Any] | Exception] = []
    for step in script:
        for _ in range(int(step.get("repeat", 1))):
            if step.get("raise") == "timeout":
                replies.append(httpx.ReadTimeout(""))
            elif "error" in step:  # A provider error body, answered with HTTP 500 (e.g. Ollama's tool-call parse error).
                replies.append({"_status": 500, "error": {"message": step["error"], "type": "api_error"}})
            else:
                replies.append(_completion(step, len(replies)))
    return replies


@contextlib.contextmanager
def scripted_provider(script: list[dict[str, Any]]) -> Iterator[None]:
    replies = scripted_replies(script)
    original = agent.httpx.AsyncClient

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> httpx.Response:
            if not replies:
                raise AssertionError("the agent asked the model more often than the case scripts")
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            status = reply.pop("_status", 200)
            return httpx.Response(status, json=reply, request=httpx.Request("POST", url))

    agent.httpx.AsyncClient = Client
    try:
        yield
    finally:
        agent.httpx.AsyncClient = original


# ── Running and grading ────────────────────────────────────────────────────────

async def run_case(case: dict[str, Any], provider: str) -> dict[str, Any]:
    """Run one case through ``agent.run_agent`` and return what happened, from the recorded events."""
    context = build_context(case)
    started = time.perf_counter()
    answer, events, verdict, error = "", [], None, None
    with stubbed_indmoney(case.get("mcp", {})):
        try:
            result = await agent.run_agent(case["message"], "https://mcp.invalid/eval", "eval-token", case.get("history", []),
                                           case["agent"], provider, context=context, plan_parts=task_parts(case["message"]))
            answer, events, verdict = str(result), result.events, result.verdict
        except agent.AgentRunError as exc:
            error, events, verdict = str(exc), exc.events, exc.verdict
    termination = next((event for event in reversed(events) if event["kind"] == "termination"), {})
    number_event = next((event for event in reversed(events) if event["kind"] == "validation" and event.get("check") == "numbers"), None)
    tool_results = [event for event in events if event["kind"] == "tool_result"]
    plan_stop = next((event for event in reversed(events) if event["kind"] == "plan_stop"), {})
    return {
        "id": case["id"], "answer": answer, "error": error, "latency": time.perf_counter() - started,
        "termination": termination.get("reason"), "code": termination.get("code"),
        "task": (verdict or {}).get("task"), "verdict": (verdict or {}).get("status"),
        "retries": [event["reason"] for event in events if event["kind"] == "retry"],
        "errorKinds": [event["errorKind"] for event in tool_results if event.get("errorKind")],
        "numbers": number_event.get("outcome") if number_event else None,
        "numberDetail": number_event.get("detail") if number_event else None,
        "tools": [event["name"] for event in tool_results], "summary": summarize(events),
        "calls": [[event["name"], event.get("arguments", "")] for event in events if event["kind"] == "tool_call"],
        "plans": [[event.get("stage"), event.get("outcome")] for event in events if event["kind"] == "plan"],
        "planReasons": [event["reason"] for event in events if event["kind"] == "plan" and event.get("reason")],
        "planStop": plan_stop.get("reason"),
    }


def grade_scripted(case: dict[str, Any], outcome: dict[str, Any]) -> list[str]:
    """Differences between a scripted run and the case's ``expect`` block."""
    expect = case["expect"]
    problems = []
    for key in ("termination", "code", "task", "verdict", "numbers"):
        if key in expect and outcome[key] != expect[key]:
            problems.append(f"{key} {outcome[key]!r}, expected {expect[key]!r}")
    if "planStop" in expect and outcome["planStop"] != expect["planStop"]:
        problems.append(f"planStop {outcome['planStop']!r}, expected {expect['planStop']!r}")
    for key in ("retries", "errorKinds", "plans", "tools"):
        if key in expect and outcome[key] != expect[key]:
            problems.append(f"{key} {outcome[key]}, expected {expect[key]}")
    if "modelCalls" in expect and outcome["summary"]["modelCalls"] != expect["modelCalls"]:
        problems.append(f"modelCalls {outcome['summary']['modelCalls']}, expected {expect['modelCalls']}")
    return problems


def grade_live(case: dict[str, Any], outcome: dict[str, Any]) -> dict[str, bool]:
    """Pass or fail per graded property of a live run (only the properties the case names)."""
    grade = case["live"]["grade"]
    answer = outcome["answer"]
    scores: dict[str, bool] = {}
    if "termination" in grade:
        scores["termination"] = outcome["termination"] == grade["termination"]
    if "verdict" in grade:
        scores["contract"] = outcome["verdict"] == grade["verdict"]
    if "numbers" in grade:
        # Not checked means no tool ran and nothing needed checking: no untraceable figures either way.
        scores["numbers"] = outcome["numbers"] in (grade["numbers"], None)
    if grade.get("mentions"):
        scores["figures"] = all(mentions(answer, value) for value in grade["mentions"])
    if grade.get("asks_question"):
        scores["clarifies"] = tasks.asks_question(answer)
    if grade.get("no_amounts"):
        scores["no_amounts"] = not _AMOUNT.search(answer)
    if "tool_rejections" in grade:
        rejected = sum(kind in ("rejected", "invalid_arguments") for kind in outcome["errorKinds"])
        scores["valid_arguments"] = rejected <= grade["tool_rejections"]
    if "max_model_calls" in grade:
        scores["model_calls"] = outcome["summary"]["modelCalls"] <= grade["max_model_calls"]
    if "max_tool_calls" in grade:
        scores["tool_calls"] = outcome["summary"]["toolCalls"] <= grade["max_tool_calls"]
    if grade.get("planned"):
        scores["planned"] = ["initial", "accepted"] in outcome["plans"]
    if "plan_stop" in grade:
        scores["plan_stop"] = outcome["planStop"] in grade["plan_stop"]
    if grade.get("no_repeats"):
        keys = [json.dumps(call) for call in outcome["calls"]]
        scores["no_repeats"] = len(keys) == len(set(keys))
    if grade.get("order"):
        scores["order"] = all(_runs_before(outcome["tools"], first, then) for first, then in grade["order"])
    return scores


def _runs_before(tools: list[str], first: str, then: str) -> bool:
    """``then`` never runs before ``first`` has run (vacuously true when ``then`` never runs)."""
    return then not in tools or (first in tools and tools.index(first) < tools.index(then))


async def run_scripted(cases: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any], list[str]]]:
    """Run every case against its scripted replies; returns (case, outcome, problems)."""
    os.environ.setdefault("OPENAI_API_KEY", "scripted-eval")
    results = []
    for case in cases:
        with scripted_provider(case["script"]):
            outcome = await run_case(case, SCRIPTED_PROVIDER)
        results.append((case, outcome, grade_scripted(case, outcome)))
    return results


def report_live(results: list[dict[str, Any]], runs: int) -> None:
    print(f"\n{EVAL_VERSION} live, {runs} run(s) × {len(results) // max(runs, 1)} cases")
    keys = sorted({key for result in results for key in result["scores"]})
    for key in keys:
        applicable = [result["scores"][key] for result in results if key in result["scores"]]
        print(f"  {key:16} {sum(applicable)}/{len(applicable)}")
    passed = sum(result["passed"] for result in results)
    print(f"  {'cases passed':16} {passed}/{len(results)}")
    latencies = sorted(result["latency"] for result in results)
    tokens = [(result["summary"]["promptTokens"] or 0) + (result["summary"]["completionTokens"] or 0) for result in results]
    print(f"  latency p50 {statistics.median(latencies):.1f} s, max {latencies[-1]:.1f} s; "
          f"model calls {statistics.mean(r['summary']['modelCalls'] for r in results):.1f}, "
          f"tool calls {statistics.mean(r['summary']['toolCalls'] for r in results):.1f}, "
          f"tokens p50 {statistics.median(tokens):.0f} per case")


async def run_live(cases: list[dict[str, Any]], provider: str, runs: int, delay: float) -> list[dict[str, Any]]:
    results = []
    for run in range(1, runs + 1):
        for case in cases:
            outcome = await run_case({**case, "live_mode": True}, provider)
            scores = grade_live(case, outcome)
            passed = bool(scores) and all(scores.values()) and not (outcome["error"] and case["live"]["grade"].get("termination") != "failed")
            results.append({**outcome, "scores": scores, "passed": passed, "run": run})
            failed = [key for key, ok in scores.items() if not ok]
            print(f"{'PASS' if passed else 'FAIL'}  run {run}  {case['id']:36} {outcome['latency']:5.1f} s  "
                  f"{','.join(outcome['tools']) or 'no tools':40} {'; '.join(failed) or 'ok'}"
                  f"{'  [' + outcome['numberDetail'] + ']' if outcome.get('numberDetail') else ''}"
                  f"{'  [plan: ' + '; '.join(outcome['planReasons']) + ']' if outcome.get('planReasons') else ''}", flush=True)
            await asyncio.sleep(delay)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", choices=sorted(agent.PROVIDERS), help="run the live cases on this provider")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--delay", type=float, default=0.0, help="seconds between live cases (rate limits)")
    parser.add_argument("--min-pass", type=int, default=0, help="live cases each run must pass")
    parser.add_argument("--case", action="append", help="run only this case id (repeatable)")
    parser.add_argument("--dataset", type=Path, default=DATASET, help="cases to run (default evals/agent_tasks.jsonl)")
    parser.add_argument("--out", help="write each live result to this JSONL file")
    args = parser.parse_args()
    cases = [case for case in load_cases(args.dataset) if not args.case or case["id"] in args.case]
    if not args.provider:
        failures = 0
        for case, outcome, problems in asyncio.run(run_scripted(cases)):
            failures += bool(problems)
            print(f"{'FAIL' if problems else 'ok  '}  {case['id']:40} {'; '.join(problems)}")
        print(f"\n{EVAL_VERSION} scripted ({args.dataset.name}): {len(cases) - failures}/{len(cases)} cases behave as expected")
        return 1 if failures else 0
    live = [case for case in cases if "live" in case]
    results = asyncio.run(run_live(live, args.provider, args.runs, args.delay))
    report_live(results, args.runs)
    if args.out:
        Path(args.out).write_text("".join(json.dumps(result, ensure_ascii=False, default=str) + "\n" for result in results))
    worst = min(sum(r["passed"] for r in results if r["run"] == run) for run in range(1, args.runs + 1))
    return 0 if worst >= args.min_pass else 1


if __name__ == "__main__":
    sys.exit(main())

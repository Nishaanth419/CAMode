"""Score the general agent (app.agent_graph) on multi-step financial requests, graded in code.

evals/financial_workflows.jsonl holds an iron condor, a put-spread hedge, a portfolio health check and a
watchlist scan, plus recovery cases. Nothing in CA Mode is specific to these requests: the agent plans
and calls INDmoney's tools and the general analysis tools (inspect_result, calculate, option_strategy,
price_levels) itself.

INDmoney is replaced by a fake that builds every response from tests/fixtures/indmoney_market.py and
indmoney_sample.py in the layouts recorded from the live tools, offered with the real tool schemas
(tests/fixtures/indmoney_tools.json). Cases can make a tool fail (rejected, unavailable, a timeout or
rate limit on the first call only, an auth failure) or change the market.

Scripted mode (default, offline, also run by tests/test_eval_workflows.py): the model's replies,
including every tool call, come from the case, so the run tests CA Mode's side: parallel calls,
recovery from unknown tools, malformed calls, refused expiries, timeouts and repeats, budgets, the
request to fix figures no tool produced, the exact figures the analysis tools return for the
model's inputs (written into the dataset from hand calculations), the task contract, the number
check and trace consistency.

Live mode (``--provider``) lets the real model do the whole task and grades the answer: contract
met, every figure traced to a tool, the reference figures stated (the values a correct choice of
strikes, funds or rules gives), required caveats, no forbidden claims. ``--baseline`` runs the same
cases with AGENT_GRAPH_ENABLED=false (the previous loop). Latency, calls and tokens are reported.

    python -m scripts.eval_workflows
    python -m scripts.eval_workflows --provider ollama --runs 3
    python -m scripts.eval_workflows --provider openai --runs 3
    python -m scripts.eval_workflows --provider ollama --runs 3 --baseline

Exit status is 1 when a scripted case fails, or a live run passes fewer than ``--min-pass`` cases.
Nothing touches .data/ or the real master key; no network is used except the model in live mode.
"""

from __future__ import annotations

import os
import sys
import tempfile

if __name__ == "__main__":
    os.environ["APP_DATA_DIR"] = tempfile.mkdtemp(prefix="ca-mode-eval-workflows-")
    os.environ.pop("APP_MASTER_KEY_FILE", None)
    from cryptography.fernet import Fernet

    os.environ.setdefault("APP_MASTER_KEY", Fernet.generate_key().decode())
    os.environ.setdefault("REQUEST_ROUTER", "keywords")

import argparse  # noqa: E402
import asyncio  # noqa: E402
import contextlib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import statistics  # noqa: E402
import time  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Final  # noqa: E402

from app import agent, agent_graph, mcp_client  # noqa: E402
from app.agent_events import summarize  # noqa: E402
from scripts.eval_numbers import mentions  # noqa: E402
from scripts.eval_tasks import scripted_provider  # noqa: E402
from tests.fixtures import indmoney_market as market  # noqa: E402
from tests.fixtures import indmoney_sample  # noqa: E402

EVAL_VERSION: Final = "financial_workflows.v2"
ROOT: Final = Path(__file__).resolve().parent.parent
DATASET: Final = ROOT / "evals" / "financial_workflows.jsonl"
TOOLS: Final = json.loads((ROOT / "tests" / "fixtures" / "indmoney_tools.json").read_text())
SCRIPTED_PROVIDER: Final = "openai"
TOLERANCE: Final = 0.011


def load_cases(dataset: Path = DATASET) -> list[dict[str, Any]]:
    return [json.loads(line) for line in dataset.read_text().splitlines() if line.strip()]


# ── Fake INDmoney ──────────────────────────────────────────────────────────────

class FakeIndmoney:
    """Answers each INDmoney call from the fixtures, shaped by the case's ``market`` and ``mcp`` behaviours."""

    def __init__(self, market_spec: dict[str, Any], behaviours: dict[str, str]) -> None:
        self.spec = market_spec
        self.behaviours = dict(behaviours)
        self.now = datetime.fromisoformat(market_spec.get("now", market.NOW.isoformat()))
        self.today = self.now.date()
        self.spot = float(market_spec.get("spot", market.SPOT))
        self.expiries = market_spec.get("expiryList", market.EXPIRIES)
        self.nearest = market_spec.get("nearest", self.expiries[0])
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.chains: dict[str, dict[str, Any]] = {}

    def _chain(self, expiry: str, strikes: int) -> dict[str, Any]:
        data = market.chain(expiry, spot=self.spot, strikes_around_atm=min(strikes, 30), today=self.today,
                            now=self.now - timedelta(minutes=int(self.spec.get("staleMinutes", 0))), expiry_list=self.expiries)
        for row in data["option_chain_data"]:
            for side in ("call_data", "put_data"):
                if self.spec.get("noDelta"):
                    row[side].pop("delta", None)
                if strikes > 30:  # Recorded: a wide chain drops prices and greeks from every row.
                    for key in ("current_price", "delta", "vega", "gamma", "buy_price", "sell_price", "perc_change", "price_change", "volume"):
                        row[side].pop(key, None)
        self.chains[expiry] = data
        return data

    def _leg_chain(self, key: str) -> dict[str, Any] | None:
        for expiry in sorted(self.expiries):
            data = self.chains.get(expiry) or market.chain(expiry, spot=self.spot, today=self.today, now=self.now, expiry_list=self.expiries)
            if any(r[s]["derivative_id"] == key for r in data["option_chain_data"] for s in ("call_data", "put_data")):
                return data
        return None

    def respond(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "lookup_ind_keys":
            found = []
            for query in arguments.get("names") or []:
                if re.search(r"nifty", query, re.I) and arguments.get("filter_type", "IN_STOCKS") == "IN_STOCKS":
                    found += market.lookup_nifty()
                for label, key in (self.spec.get("lookup") or {}).items():
                    if label.lower().startswith(query.lower()[:4]):
                        found.append({"ind_key": key, "name": label})
            return found
        if name == "get_indian_stocks_option_chain":
            if arguments.get("ind_key") != market.NIFTY_KEY:
                return {"error": "validation_error", "message": f"no option chain for {arguments.get('ind_key')}", "tool": name}
            strikes = int(arguments.get("strikes_around_atm", 7))
            if not arguments.get("use_expiry_date"):
                return self._chain(self.nearest, strikes)
            asked = str(arguments.get("expiry_date") or "").replace("-", "")
            if asked not in self.expiries:
                return market.expiry_rejected(asked, self.expiries)
            return self._chain(asked, strikes)
        if name == "get_indian_stocks_details":
            keys = list(arguments.get("ind_keys") or [])
            if "analyst" in (arguments.get("segments") or []):
                out: dict[str, Any] = {}
                for key in keys:
                    spec = (self.spec.get("analyst") or {}).get(key)
                    if spec:
                        out.update(market.analyst(key, price=spec["price"], mean=spec.get("mean")))
                return out
            out = {}
            for key in keys:
                source = self._leg_chain(key)
                if source is not None:
                    out.update(market.leg_details(source, [key], now=self.now))
            return out
        if name == "get_indian_stocks_greeks_history":
            source = self._leg_chain(arguments["ind_key"])
            theta = next((r[s]["theta"] for r in (source or {}).get("option_chain_data", []) for s in ("call_data", "put_data")
                          if r[s]["derivative_id"] == arguments["ind_key"]), -5.0)
            return market.greeks_history(arguments["ind_key"], theta, now=self.now)
        if name == "get_indian_stocks_ohlc":
            kind = (self.spec.get("series") or {}).get(arguments["ind_key"], "flat")
            builder = {"breakout": market.breakout_series, "support": market.support_series, "flat": market.flat_series,
                       "short": market.short_series}[kind]
            series = builder(arguments["ind_key"])
            if arguments.get("lookback") == "14d":
                series["candles"] = series["candles"][-10:]  # 14 calendar days, about 10 sessions (recorded)
                series["count"] = len(series["candles"])
            return series
        if name == "user_watchlist":
            return market.watchlist(self.spec.get("watchlist") or [])
        if name == "networth_snapshot":
            return indmoney_sample.SNAPSHOT
        if name == "networth_holdings":
            rows = {"IND_STOCK": indmoney_sample.IND_STOCK, "MF": indmoney_sample.MF, "US_STOCK": indmoney_sample.US_STOCK}
            asset_type = arguments["asset_type"]
            return indmoney_sample.holdings(rows.get(asset_type, []), usd=asset_type == "US_STOCK")
        if name == "networth_allocation_breakdown":
            return {"asset_type": arguments.get("asset_type"), "rows": [], "data_note": "No sector analytics."}
        if name == "get_mf_funds_details":
            funds = self.spec.get("funds", market.FUNDS)
            return market.fund_details({code: funds[code] for code in str(arguments["fund_ids"]).replace(" ", "").split(",") if code in funds})
        return {"error": "not_served", "message": f"{name} is not served by the eval fake", "tool": name}

    async def call(self, url: str, token: str, protocol: str, session_id: str | None, name: str,
                   arguments: dict[str, Any], *, client: Any = None) -> str:
        self.calls.append((name, arguments))
        first_arg = next((str(v) for v in arguments.values() if isinstance(v, str)), "")
        key = f"{name}:{first_arg}" if f"{name}:{first_arg}" in self.behaviours else name
        behaviour = self.behaviours.get(key, "ok")
        if behaviour.endswith("_once"):
            self.behaviours.pop(key)
            if behaviour.startswith("timeout"):
                raise RuntimeError(f'MCP tools/call failed for "{name}": request timed out')
            raise RuntimeError(f'MCP tools/call failed for "{name}": 429 Too Many Requests')
        if behaviour == "rejected":
            raise mcp_client.McpToolError(f'MCP tool "{name}" failed: invalid arguments')
        if behaviour == "unavailable":
            raise RuntimeError(f'MCP tools/call failed for "{name}": 503 Service Unavailable')
        if behaviour == "auth":
            raise mcp_client.McpAuthError(f'MCP tools/call failed for "{name}": 401 ')
        return json.dumps(self.respond(name, arguments), ensure_ascii=False)

    async def session(self, url: str, token: str) -> dict[str, Any]:
        return {"tools": TOOLS, "protocolVersion": "eval", "sessionId": None}


def build_context(case: dict[str, Any]) -> agent.PortfolioContext:
    local = []
    history = case.get("market", {}).get("history")
    if history is not None:
        async def get_networth_history(arguments: dict[str, Any]) -> str:
            return json.dumps(market.history([tuple(p) for p in history]))

        local.append(agent.LocalTool("get_networth_history", "Read the user's daily net-worth history from saved snapshots.",
                                     {"type": "object", "properties": {"days": {"type": "integer"}}}, get_networth_history))
    return agent.PortfolioContext(local_tools=tuple(local), cached_mcp_tools=TOOLS)


@contextlib.contextmanager
def patched(fake: FakeIndmoney, case: dict[str, Any]) -> Iterator[list[tuple[str, dict[str, Any]]]]:
    """Fake INDmoney and the clock, no backoff, the case's environment and limits, and a tee of the raw events."""
    raw: list[tuple[str, dict[str, Any]]] = []
    stream = agent.stream_agent
    env = {"AGENT_GRAPH_ENABLED": "true", **case.get("env", {})}
    saved_env = {key: os.environ.get(key) for key in env}
    limits = case.get("limits", {})
    saved = (agent.fetch_mcp_session, agent.call_mcp_tool, agent._ist_now, agent.stream_agent, agent_graph.BACKOFF_SECONDS)
    saved_limits = {name: getattr(agent_graph, name) for name in limits}

    async def tee(*args: Any, **kwargs: Any):
        async for event in stream(*args, **kwargs):
            raw.append(event)
            yield event

    os.environ.update(env)
    agent.fetch_mcp_session, agent.call_mcp_tool, agent._ist_now = fake.session, fake.call, lambda: fake.now
    agent.stream_agent, agent_graph.BACKOFF_SECONDS = tee, 0
    for name, value in limits.items():
        setattr(agent_graph, name, value)
    try:
        yield raw
    finally:
        agent.fetch_mcp_session, agent.call_mcp_tool, agent._ist_now, agent.stream_agent, agent_graph.BACKOFF_SECONDS = saved
        for name, value in saved_limits.items():
            setattr(agent_graph, name, value)
        for key, value in saved_env.items():
            os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value)


# ── Running and grading ────────────────────────────────────────────────────────

def _tool_outputs(raw: list[tuple[str, dict[str, Any]]]) -> dict[str, list[Any]]:
    """Each analysis tool's parsed results in call order (``option_strategy.0.maxLossPoints``)."""
    outputs: dict[str, list[Any]] = {}
    for kind, data in raw:
        if kind == "tool_result" and data.get("name") in ("calculate", "option_strategy", "price_levels", "inspect_result", "date_facts"):
            try:
                outputs.setdefault(data["name"], []).append(json.loads(data.get("result") or "null"))
            except ValueError:
                outputs.setdefault(data["name"], []).append(None)
    return outputs


async def run_case(case: dict[str, Any], provider: str) -> dict[str, Any]:
    fake = FakeIndmoney(case.get("market", {}), case.get("mcp", {}))
    started = time.perf_counter()
    answer, events, verdict, error = "", [], None, None
    with patched(fake, case) as raw:
        try:
            result = await agent.run_agent(case["message"], "https://mcp.invalid/eval", "eval-token", case.get("history", []),
                                           "portfolio", provider, context=build_context(case))
            answer, events, verdict = str(result), result.events, result.verdict
        except agent.AgentRunError as exc:
            error, events, verdict = str(exc), exc.events, exc.verdict
    termination = next((e for e in reversed(events) if e["kind"] == "termination"), {})
    validations: dict[str, str] = {}
    detail = None
    for event in events:
        if event["kind"] == "validation":
            validations[event["check"]] = event["outcome"]
            if event["check"] == "numbers":
                detail = event.get("detail")
    batches: list[list[str]] = []
    for event in events:
        if event["kind"] == "model_call":
            batches.append([])
        elif event["kind"] == "tool_call" and batches:
            batches[-1].append(event["name"])
    plan_stop = next((e for e in reversed(events) if e["kind"] == "plan_stop"), {})
    return {
        "id": case["id"], "answer": answer, "error": error, "latency": time.perf_counter() - started,
        "termination": termination.get("reason"), "code": termination.get("code"), "task": (verdict or {}).get("task"),
        "verdict": (verdict or {}).get("status"), "planStop": plan_stop.get("reason"),
        "retries": [e["reason"] for e in events if e["kind"] == "retry"],
        "errorKinds": [e["errorKind"] for e in events if e["kind"] == "tool_result" and e.get("errorKind")],
        "tools": [e["name"] for e in events if e["kind"] == "tool_call"], "batches": [b for b in batches if b],
        "calls": [[name, args] for name, args in fake.calls], "validations": validations, "outputs": _tool_outputs(raw),
        "numbers": validations.get("numbers"), "numberDetail": detail, "summary": summarize(events), "events": events,
    }


def _path(value: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(value, list):
            value = value[int(part)] if part.lstrip("-").isdigit() and -len(value) <= int(part) < len(value) else None
        elif isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def _same(actual: Any, expected: Any) -> bool:
    numeric = (int, float)
    if isinstance(expected, numeric) and not isinstance(expected, bool) and isinstance(actual, numeric) and not isinstance(actual, bool):
        return abs(actual - expected) <= TOLERANCE
    return actual == expected


def trace_problems(outcome: dict[str, Any]) -> list[str]:
    """Every call has its result, in order, and each INDmoney call made is a recorded tool call."""
    events = outcome["events"]
    calls = [e["name"] for e in events if e["kind"] == "tool_call"]
    results = [e["name"] for e in events if e["kind"] == "tool_result"]
    problems = []
    if results != calls[:len(results)] or (len(results) != len(calls) and outcome["termination"] != "auth_failed"):
        problems.append("tool calls and results do not pair up")
    made = [name for name, _ in outcome["calls"]]
    retried = outcome["retries"].count("transient_tool_error")
    if any(made.count(name) > calls.count(name) + retried for name in set(made)):
        problems.append(f"INDmoney calls {made} are not all recorded ({calls})")
    return problems


def grade_scripted(case: dict[str, Any], outcome: dict[str, Any]) -> list[str]:
    expect = case["expect"]
    problems = []
    for key in ("termination", "code", "task", "verdict", "planStop", "numbers"):
        if key in expect and outcome[key] != expect[key]:
            problems.append(f"{key} {outcome[key]!r}, expected {expect[key]!r}")
    for key in ("batches", "retries", "errorKinds", "tools"):
        if key in expect and outcome[key] != expect[key]:
            problems.append(f"{key} {outcome[key]}, expected {expect[key]}")
    for path, value in (expect.get("figures") or {}).items():
        actual = _path(outcome["outputs"], path)
        if not _same(actual, value):
            problems.append(f"{path} = {actual!r}, expected {value!r}")
    for check, value in (expect.get("checks") or {}).items():
        if outcome["validations"].get(check) != value:
            problems.append(f"check {check} {outcome['validations'].get(check)!r}, expected {value!r}")
    if "modelCalls" in expect and outcome["summary"]["modelCalls"] != expect["modelCalls"]:
        problems.append(f"modelCalls {outcome['summary']['modelCalls']}, expected {expect['modelCalls']}")
    for pattern in expect.get("answerHas", []):
        if not re.search(pattern, outcome["answer"], re.IGNORECASE):
            problems.append(f"answer lacks /{pattern}/")
    return problems + trace_problems(outcome)


_PLAIN: Final = str.maketrans({" ": " ", " ": " ", " ": " ", "‐": "-", "‑": "-",
                               "–": "-", "—": "-", "’": "'", "‘": "'"})


def plain(text: str) -> str:
    """The answer with the local model's narrow no-break spaces, non-breaking hyphens and curly quotes made ASCII,
    and digit groups separated by spaces ("21 536.68") joined, as ``mentions()`` reads commas only."""
    return re.sub(r"(?<=\d) (?=\d{2,3}\b)", "", text.translate(_PLAIN))


def grade_live(case: dict[str, Any], outcome: dict[str, Any], *, baseline: bool) -> dict[str, bool]:
    grade = case["live"]["grade"]
    answer = plain(outcome["answer"])
    scores: dict[str, bool] = {"answered": bool(answer.strip())}
    if not baseline:
        scores["contract"] = outcome["verdict"] == grade.get("verdict", "met")
    scores["numbers"] = outcome["numbers"] in ("passed", None) if answer else False
    if grade.get("mentions"):
        scores["figures"] = all(mentions(answer, value) for value in grade["mentions"])
    for pattern in grade.get("says", []):
        scores[f"says:{pattern}"] = bool(re.search(pattern, answer, re.IGNORECASE))
    for pattern in grade.get("never", []):
        scores[f"never:{pattern}"] = not re.search(pattern, answer, re.IGNORECASE)
    rejected = sum(kind in ("rejected", "invalid_arguments") for kind in outcome["errorKinds"])
    scores["valid_arguments"] = rejected <= grade.get("tool_rejections", 2)
    return scores


async def run_scripted(cases: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any], list[str]]]:
    os.environ.setdefault("OPENAI_API_KEY", "scripted-eval")
    results = []
    for case in [c for c in cases if not c.get("liveOnly")]:  # live-only cases have no scripted replies
        with scripted_provider(case["script"]):
            outcome = await run_case(case, SCRIPTED_PROVIDER)
        results.append((case, outcome, grade_scripted(case, outcome)))
    return results


async def run_live(cases: list[dict[str, Any]], provider: str, runs: int, *, baseline: bool) -> list[dict[str, Any]]:
    results = []
    for run in range(1, runs + 1):
        for case in cases:
            live_case = {**case, "env": {**case.get("env", {}), "AGENT_GRAPH_ENABLED": "false" if baseline else "true"}}
            outcome = await run_case(live_case, provider)
            scores = grade_live(case, outcome, baseline=baseline)
            passed = all(scores.values())
            results.append({**{k: v for k, v in outcome.items() if k != "events"}, "scores": scores, "passed": passed, "run": run})
            failed = [key for key, ok in scores.items() if not ok]
            print(f"{'PASS' if passed else 'FAIL'}  run {run}  {case['id']:34} {outcome['latency']:6.1f} s  "
                  f"{outcome['summary']['toolCalls']:2} tools {outcome['summary']['modelCalls']:2} model  {'; '.join(failed) or 'ok'}"
                  f"{'  [' + outcome['numberDetail'] + ']' if outcome.get('numberDetail') else ''}"
                  f"{'  [' + (outcome['error'] or '')[:120] + ']' if outcome.get('error') else ''}", flush=True)
    return results


def report_live(results: list[dict[str, Any]], label: str) -> None:
    print(f"\n{EVAL_VERSION} live ({label}), {len(results)} case runs")
    keys = sorted({key for r in results for key in r["scores"]})
    for key in keys:
        applicable = [r["scores"][key] for r in results if key in r["scores"]]
        print(f"  {key[:40]:40} {sum(applicable)}/{len(applicable)}")
    print(f"  {'cases passed':40} {sum(r['passed'] for r in results)}/{len(results)}")
    latencies = sorted(r["latency"] for r in results)
    tokens = [(r["summary"]["promptTokens"] or 0) + (r["summary"]["completionTokens"] or 0) for r in results]
    print(f"  latency p50 {statistics.median(latencies):.1f} s, max {latencies[-1]:.1f} s; model calls "
          f"{statistics.mean(r['summary']['modelCalls'] for r in results):.1f}, tool calls "
          f"{statistics.mean(r['summary']['toolCalls'] for r in results):.1f}, failed tool calls "
          f"{sum(r['summary']['failedToolCalls'] for r in results)}, tokens p50 {statistics.median(tokens):.0f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", choices=sorted(agent.PROVIDERS), help="run the live cases on this provider")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--baseline", action="store_true", help="live cases with the previous loop (AGENT_GRAPH_ENABLED=false)")
    parser.add_argument("--min-pass", type=int, default=0)
    parser.add_argument("--case", action="append", help="run only this case id (repeatable)")
    parser.add_argument("--out", help="write each live result to this JSONL file")
    args = parser.parse_args()
    cases = [c for c in load_cases() if not args.case or c["id"] in args.case]
    if not args.provider:
        results = asyncio.run(run_scripted(cases))
        failed = 0
        for case, outcome, problems in results:
            failed += bool(problems)
            print(f"{'ok  ' if not problems else 'FAIL'}  {case['id']:44} {'; '.join(problems)}")
        print(f"{EVAL_VERSION} scripted: {len(results) - failed}/{len(results)} cases behave as expected")
        return 1 if failed else 0
    live = [c for c in cases if c.get("live")]
    results = asyncio.run(run_live(live, args.provider, args.runs, baseline=args.baseline))
    report_live(results, "baseline: previous loop" if args.baseline else "general agent")
    if args.out:
        with open(args.out, "w") as handle:
            for result in results:
                handle.write(json.dumps({k: v for k, v in result.items() if k != "outputs"}, default=str) + "\n")
    worst = min((sum(r["passed"] for r in results if r["run"] == n) for n in range(1, args.runs + 1)), default=0)
    return 1 if live and worst < args.min_pass else 0


if __name__ == "__main__":
    sys.exit(main())

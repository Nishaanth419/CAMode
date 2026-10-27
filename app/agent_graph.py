"""The general Portfolio Analyzer agent for account and market questions, as a LangGraph graph.

The model decides everything: which tools to call, with which arguments, in which order, and when it
has enough to answer. CA Mode supplies the tools (INDmoney's read-only tools, the saved-data tools and
the general ``analysis_tools``), facts about INDmoney's data in the prompt, and bounded checks:

    think ─┬→ act → think            the model's tool calls, run together, results back to the model
           ├→ check ─┬→ think        a reply that skipped account data, or states figures no tool produced,
           │         └→ finish       is sent back once with the reason; otherwise the usual output checks
           └→ end                     a provider or INDmoney sign-in failure

- ``act`` runs a reply's calls in parallel (``CONCURRENCY``, each under ``STEP_TIMEOUT_SECONDS``), retries a
  timeout or rate limit once, and answers an unknown tool, arguments that are not an object or fail the
  tool's schema, a budget overrun or an identical repeat with an error result the model can act on,
  instead of ending the answer. A result made only of error fields counts as ``rejected``.
- Every INDmoney and saved-data result goes into the answer's ``ResultStore``; a large one reaches the
  model as an outline with a ``resultId`` to read with ``inspect_result``.
- A malformed tool call from the provider (Ollama's "error parsing tool call", Groq's failed generation)
  is asked for again at most ``MAX_MALFORMED`` times; after that the model answers without tools from what
  it has (``plan_stop`` ``partial``).
- At ``MAX_MODEL_CALLS``, ``MAX_TOOL_CALLS`` or ``DEADLINE_SECONDS`` the model is asked once, without tools,
  to answer from what it has and say what is missing (``plan_stop`` ``budget`` / ``deadline``).

Events are the ones ``agent_events`` already records. No checkpointer: nothing is persisted by the graph.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from . import tool_results
from .analysis_tools import ANALYSIS_TOOL_NAMES, ResultStore

logger = logging.getLogger(__name__)

Event = tuple[str, dict[str, Any]]


def _positive_env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


MAX_MODEL_CALLS: Final = 14
MAX_TOOL_CALLS: Final = 36
MAX_CALLS_PER_REPLY: Final = 25
CONCURRENCY: Final = 4
MAX_MALFORMED: Final = 2
STEP_TIMEOUT_SECONDS: Final = _positive_env_int("AGENT_STEP_TIMEOUT_SECONDS", 60)
DEADLINE_SECONDS: Final = _positive_env_int("AGENT_DEADLINE_SECONDS", 300)
BACKOFF_SECONDS: float = 1.0  # Tests set 0.
RECURSION_LIMIT: Final = 120
_TRANSIENT: Final = re.compile(r"timed?\s*out|\b429\b|rate.?limit|too many requests|temporar|\b50[234]\b", re.IGNORECASE)


def enabled() -> bool:
    return os.getenv("AGENT_GRAPH_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


ANALYSIS_GUIDE: Final = """How to work:
- Work out the steps the question needs, then fetch the data. Call tools that do not depend on each other in the same turn; they run together. After results arrive, decide the next step from them.
- Never guess an identifier: resolve stock, index, option or fund names with lookup_ind_keys first (it returns one flat list for all the names you pass, so match the names yourself).
- A large result comes back as an outline with a resultId: read the rows you need with inspect_result (conditions such as "put_data.delta between -0.2 -0.1", only the fields you need).
- For dates (which listed expiry is the last of its month, weekdays, days or trading days from today) use date_facts instead of working them out. Absent fields mean the tool did not report them; never treat them as zero.
- Never do arithmetic yourself. Every figure you derive (sums, differences, percentages, ratios, quantities, payoffs, breakevens, upside) must come from calculate, option_strategy or price_levels, or be quoted exactly as a tool returned it.
- If data is missing, stale, contradictory or not offered by any tool, say so plainly and do not fill it in. Ask the user only for inputs no tool can provide.

Facts about INDmoney's data:
- get_indian_stocks_option_chain: call it with use_expiry_date false first; it returns expiry_list (YYYYMMDD) and the chain for the nearest expiry, which may be a weekly. Expiries are not labelled weekly or monthly: a month's monthly expiry is the last expiry listed in that month (holiday moves are already applied); date_facts marks it. To get another expiry, pass one listed date as YYYY-MM-DD with use_expiry_date true. strikes_around_atm up to 30 (larger values drop prices and greeks). Each strike's call_data and put_data carry derivative_id (that option's ind_key), lot_size, delta, theta (index points per day), and the bid and ask as buy_price and sell_price. A delta with the wrong sign (a call below 0, a put above 0) or a bid above the ask is bad data. entity_details.live_price and price_updated_at_ist are the underlying's level and time.
- get_indian_stocks_details on option ind_keys gives bid_price, ask_price, greeks_data, expiry_date and underlying_parent_ind_key; with segments ["analyst"] on stocks it gives analyst_forecast (consensus, target_price mean, low, high).
- get_indian_stocks_ohlc: lookback 14d covers 14 calendar days (about 10 sessions); for a rule over N sessions fetch 1y and use price_levels. During market hours today's daily candle is still forming.
- networth_holdings: holding_percent is a share of that asset type only, not of the portfolio. An MF row's investment_code works as get_mf_funds_details fund_ids; includes ["fund_performance"] gives the fund's, its benchmark's and its category's returns for the same periods, and ["sector_allocation"] its sector split. INDmoney has no sector data for directly held stocks.
- user_watchlist lists the watchlists and their stocks; a stock without an ind_key needs a lookup.

In the answer: state the rules, assumptions (for example a beta, or theta held constant) and data timestamps you used; keep estimates apart from facts; say what could not be checked; never suggest placing trades; end with one line saying this is information, not investment advice."""

FINAL_PROMPT: Final = (
    "Stop gathering data now and answer my original question from the tool results above only, quoting figures as "
    "the tools gave them. Say plainly which parts you could not complete and why."
)
NO_ACCOUNT_DATA_PROMPT: Final = (
    "Do not tell me to run an account check. Use the available tools now to retrieve the data my question needs, "
    "then answer from it."
)
UNVERIFIED_PROMPT: Final = (
    "These figures in your answer do not appear in any tool result: {figures}. Compute each one with calculate, "
    "option_strategy or price_levels, or quote it exactly as a tool returned it, or leave it out. Then answer again."
)
MALFORMED_PROMPT: Final = (
    "Your last tool call could not be read (malformed JSON). Send it again as valid JSON; if it was long, split it "
    "into smaller calls."
)


class AgentState(TypedDict, total=False):
    messages: list[dict[str, Any]]
    model_calls: int
    tool_calls: int
    pending: list[dict[str, Any]]
    reply: str | None
    finish_reason: Any
    retries: dict[str, int]
    account_tool_called: bool
    final: bool
    stop_reason: str | None
    answer: str
    failed: bool
    route: str  # check's decision: "think" (ask again) or "finish"


@dataclass(slots=True)
class Deps:
    """What the graph needs from the agent run that started it (passed in ``config["configurable"]``)."""

    model_call: Callable[[list[dict[str, Any]], list[dict[str, Any]] | None], AsyncIterator[Event]]
    run_tool: Callable[[str, dict[str, Any]], Awaitable[Any]]
    model_tools: list[dict[str, Any]]
    schemas: dict[str, Any]
    local_tools: frozenset[str]
    is_account_tool: Callable[[str], bool]
    store: ResultStore
    guard: Callable[[str, list[dict[str, Any]], Any], tuple[str, list[Event]]]
    unverified: Callable[[str, list[dict[str, Any]]], list[str]]
    requires_account_data: bool
    started: float = field(default_factory=time.monotonic)
    succeeded: dict[str, str] = field(default_factory=dict)  # call key → resultId

    def over_deadline(self) -> bool:
        return time.monotonic() - self.started > DEADLINE_SECONDS


def _deps(config: RunnableConfig) -> Deps:
    return config["configurable"]["deps"]


def _emit(event: str, payload: dict[str, Any]) -> None:
    get_stream_writer()((event, payload))


def _retries(state: AgentState, reason: str) -> int:
    return (state.get("retries") or {}).get(reason, 0)


def _count(state: AgentState, reason: str) -> dict[str, int]:
    return {**(state.get("retries") or {}), reason: _retries(state, reason) + 1}


def _call_key(name: str, arguments: Any) -> str:
    return json.dumps([name, arguments], sort_keys=True, ensure_ascii=False, default=str)


# ── Nodes ──────────────────────────────────────────────────────────────────────

async def think(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    messages = list(state.get("messages") or [])
    final = bool(state.get("final"))
    update: dict[str, Any] = {"pending": [], "reply": None}
    out_of_budget = (state.get("model_calls", 0) >= MAX_MODEL_CALLS - 1 or state.get("tool_calls", 0) >= MAX_TOOL_CALLS
                     or deps.over_deadline())
    if out_of_budget and not final:
        final = True
        update["stop_reason"] = "deadline" if deps.over_deadline() else "budget"
        messages.append({"role": "user", "content": FINAL_PROMPT})
    completion: dict[str, Any] = {}
    async for event, data in deps.model_call(messages, None if final else deps.model_tools):
        if event == "_completion":
            completion = data
        else:
            _emit(event, data)
    update.update({"messages": messages, "final": final, "model_calls": state.get("model_calls", 0) + 1})
    if completion.get("malformed") and not final:
        _emit("retry", {"reason": "malformed_tool_call", "detail": str(completion.get("error") or "")[:160]})
        update["retries"] = _count(state, "malformed_tool_call")
        if _retries(state, "malformed_tool_call") < MAX_MALFORMED:
            update["messages"] = [*messages, {"role": "user", "content": MALFORMED_PROMPT}]
        else:
            # Still unreadable: answer from what was gathered, without tools (a reply without tools always parses).
            update.update({"final": True, "stop_reason": "partial", "messages": [*messages, {"role": "user", "content": FINAL_PROMPT}]})
        return update
    if not completion.get("ok"):
        _emit("error", {"message": completion.get("error") or "The AI provider failed.", "code": completion.get("code") or "provider_error"})
        _emit("done", {})
        update["failed"] = True
        return update
    message = completion.get("message") or {}
    calls = [call for call in message.get("tool_calls") or [] if isinstance(call, dict)]
    if calls and not final:
        update["messages"] = [*messages, {"role": "assistant", "content": message.get("content"), "tool_calls": calls}]
        update["pending"] = calls
    else:
        update["reply"] = completion.get("content") or ""
        update["finish_reason"] = completion.get("finish_reason")
    return update


def after_think(state: AgentState) -> str:
    if state.get("failed"):
        return "end"
    if state.get("pending"):
        return "act"
    return "check" if state.get("reply") is not None else "think"


@dataclass(slots=True)
class _Refused:
    """A call answered without running the tool: what the model is told, and the trace's error."""

    error: str
    kind: str

    @property
    def result(self) -> dict[str, Any]:
        return {"error": self.error, "error_kind": self.kind}

    @property
    def content(self) -> str:
        return tool_results.error_payload(self.kind, self.error)  # type: ignore[arg-type]

    ok: bool = False
    auth_error: str | None = None


async def _run(deps: Deps, name: str, arguments: dict[str, Any], semaphore: asyncio.Semaphore) -> tuple[Any, int]:
    started = time.perf_counter()
    async with semaphore:
        for attempt in (1, 2):
            try:
                run = await asyncio.wait_for(deps.run_tool(name, arguments), STEP_TIMEOUT_SECONDS)
            except TimeoutError:
                run = _Refused(f"{name} did not finish within {STEP_TIMEOUT_SECONDS} seconds.", "unavailable")
            transient = (not run.ok and run.result.get("error_kind") == "unavailable"
                         and _TRANSIENT.search(str(run.result.get("error") or "")) is not None)
            if not transient or attempt == 2 or deps.over_deadline():
                break
            _emit("retry", {"reason": "transient_tool_error", "detail": f"{name}: {str(run.result.get('error'))[:120]}"})
            await asyncio.sleep(BACKOFF_SECONDS)
    return run, round((time.perf_counter() - started) * 1000)


async def act(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    calls = list(state.get("pending") or [])
    used = state.get("tool_calls", 0)
    plan: list[tuple[dict[str, Any], str, Any, Any]] = []  # (call, name, arguments, refusal or None)
    for index, call in enumerate(calls):
        function = call.get("function") or {}
        name = str(function.get("name", ""))
        raw = function.get("arguments")
        try:
            arguments = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            arguments = None
        arguments = {} if raw in (None, "") else arguments
        refusal = None
        if name not in deps.schemas:
            names = ", ".join(sorted(deps.schemas))
            refusal = _Refused(f"There is no tool named {name!r}. Use one of: {names}.", "invalid_arguments")
        elif not isinstance(arguments, dict):
            refusal = _Refused(f"The arguments for {name} were not a JSON object, so it was not called.", "invalid_arguments")
        elif problems := tool_results.argument_problems(deps.schemas.get(name), arguments):
            refusal = _Refused(f"{name} was not called: {'; '.join(problems)}", "invalid_arguments")
        elif index >= MAX_CALLS_PER_REPLY or used >= MAX_TOOL_CALLS:
            refusal = _Refused("The tool-call budget for this answer is used up; answer with what you have.", "unavailable")
        elif (key := _call_key(name, arguments)) in deps.succeeded:
            refusal = _Refused(f"This exact call already ran; its result is {deps.succeeded[key]} above (use inspect_result).",
                               "rejected")
        else:
            used += 1
        plan.append((call, name, arguments if isinstance(arguments, dict) else str(raw)[:600], refusal))

    for call, name, arguments, _ in plan:
        _emit("tool_call", {"name": name, "arguments": arguments,
                            "source": "local" if name in deps.local_tools else "indmoney"})
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def outcome(name: str, arguments: Any, refusal: Any) -> tuple[Any, int]:
        return (refusal, 0) if refusal is not None else await _run(deps, name, arguments, semaphore)

    runs = await asyncio.gather(*(outcome(name, arguments, refusal) for _, name, arguments, refusal in plan))
    messages = list(state.get("messages") or [])
    account_tool_called = bool(state.get("account_tool_called"))
    for (call, name, arguments, refusal), (run, elapsed) in zip(plan, runs, strict=True):
        result, content, ok = dict(run.result), run.content, run.ok
        if run.auth_error:
            _emit("tool_result", {**result, "name": name})
            _emit("error", {"message": run.auth_error, "code": "indmoney_auth", "auth_failed": True})
            _emit("done", {})
            return {"failed": True}
        if ok and (returned := tool_results.returned_error(content)):
            # The tool answered with an error object instead of data (e.g. an expiry it does not list).
            result, ok = {**result, "error": returned, "error_kind": "rejected"}, False
            content = tool_results.error_payload("rejected", returned)
        if ok and name not in ANALYSIS_TOOL_NAMES:
            result_id = deps.store.add(name, arguments, content)
            deps.succeeded[_call_key(name, arguments)] = result_id
            content = deps.store.view(result_id)  # The result itself when short, else an outline with its resultId.
        if refusal is None and deps.is_account_tool(name):
            account_tool_called = True
        _emit("tool_result", {**result, "name": name, **({"duration_ms": elapsed} if refusal is None else {})})
        messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": content})
    return {"messages": messages, "tool_calls": used, "pending": [], "account_tool_called": account_tool_called}


def _sources(state: AgentState, deps: Deps) -> list[dict[str, Any]]:
    """The messages plus every stored result in full, so figures read from a large result count as sourced."""
    return [*(state.get("messages") or []), *({"role": "tool", "content": text} for text in deps.store.texts())]


async def check(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    reply = (state.get("reply") or "").strip()
    messages = list(state.get("messages") or [])
    room = not state.get("final") and state.get("model_calls", 0) < MAX_MODEL_CALLS - 1 and not deps.over_deadline()
    if not reply:
        if room and _retries(state, "empty_reply") < 1:
            _emit("retry", {"reason": "empty_reply", "detail": "the model returned no text"})
            return {"messages": [*messages, {"role": "user", "content": FINAL_PROMPT}], "retries": _count(state, "empty_reply"),
                    "reply": None, "route": "think"}
        _emit("error", {"message": "The AI model returned an empty response. Try again or pick another provider.", "code": "empty_response"})
        _emit("done", {})
        return {"failed": True}
    if deps.requires_account_data and not state.get("account_tool_called") and room and _retries(state, "no_account_tool") < 1:
        _emit("retry", {"reason": "no_account_tool", "detail": "answered an account question without reading account data"})
        return {"messages": [*messages, {"role": "assistant", "content": reply}, {"role": "user", "content": NO_ACCOUNT_DATA_PROMPT}],
                "retries": _count(state, "no_account_tool"), "reply": None, "route": "think"}
    unverified = deps.unverified(reply, _sources(state, deps)) if state.get("tool_calls") else []
    if unverified and room and _retries(state, "unverified_figures") < 1:
        _emit("retry", {"reason": "unverified_figures", "detail": ", ".join(unverified[:8])})
        return {"messages": [*messages, {"role": "assistant", "content": reply},
                             {"role": "user", "content": UNVERIFIED_PROMPT.format(figures=", ".join(unverified[:12]))}],
                "retries": _count(state, "unverified_figures"), "reply": None, "route": "think"}
    return {"route": "finish"}


def after_check(state: AgentState) -> str:
    if state.get("failed"):
        return "end"
    return "think" if state.get("route") == "think" else "finish"


async def finish(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    answer, events = deps.guard(state.get("reply") or "", _sources(state, deps), state.get("finish_reason"))
    for event, data in events:
        _emit(event, data)
    if state.get("stop_reason"):
        _emit("plan_stop", {"reason": state["stop_reason"], "missing": []})
    _emit("content", {"text": answer})
    _emit("done", {})
    return {"answer": answer}


def build_graph():
    # Branch functions take AgentState: LangGraph passes a branch only the keys of its annotated type.
    graph = StateGraph(AgentState)
    for name, node in (("think", think), ("act", act), ("check", check), ("finish", finish)):
        graph.add_node(name, node)
    graph.add_edge(START, "think")
    graph.add_conditional_edges("think", after_think, {"act": "act", "check": "check", "think": "think", "end": END})
    graph.add_conditional_edges("act", lambda state: "end" if state.get("failed") else "think", {"think": "think", "end": END})
    graph.add_conditional_edges("check", after_check, {"think": "think", "finish": "finish", "end": END})
    graph.add_edge("finish", END)
    return graph.compile()


GRAPH: Final = build_graph()


def guide(now: datetime) -> str:
    """The analysis guide with today's date and time (the model needs them for expiries and running candles)."""
    return f"Today is {now:%A %d %B %Y}, {now:%H:%M} IST.\n\n{ANALYSIS_GUIDE}"


async def run(deps: Deps, messages: list[dict[str, Any]]) -> AsyncIterator[Event]:
    """Answer with the graph; the last event is ``done``."""
    initial: AgentState = {"messages": messages, "model_calls": 0, "tool_calls": 0, "retries": {}, "account_tool_called": False}
    config: RunnableConfig = {"configurable": {"deps": deps}, "recursion_limit": RECURSION_LIMIT}
    async for chunk in GRAPH.astream(initial, config=config, stream_mode="custom"):
        yield chunk

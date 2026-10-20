"""Task contracts: what each kind of request must do before its answer counts as complete.

The task comes from the request route, the agent and the tools offered, never from a model call.
A contract names the tools that can satisfy the task, whether a clarifying question is an
acceptable outcome, and the call budgets. ``evaluate()`` checks a finished run against it.

A message with more than one kind of task that needs tools gets a ``multi_step`` contract
(``plan_contract()``) when the agent plans it (``planner``).

Contracts observe; they do not change answers. The one rule the agent enforces from here is that
an account-data request is satisfied only by an account tool (``ACCOUNT_LOCAL_TOOLS`` or an
INDmoney tool), not by, say, the goal calculator.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

TaskType = Literal["refusal", "general_finance", "goal_calculation", "account_data", "multi_step"]

# Local tools that read the user's own account data (saved snapshots, CAS holdings, investigations, fund figures).
ACCOUNT_LOCAL_TOOLS: Final = frozenset({
    "get_portfolio_snapshot", "get_networth_history", "get_cas_holdings", "investigate_portfolio_change",
    "get_goal_fund_comparison",
})
GOAL_TOOLS: Final = frozenset({"calculate_goal_plan", "get_goal_plans", "plan_goal_what_if"})

# Budgets are observed, not enforced: a run over them is reported as such. The Phase 1 baseline used at most
# 5 model calls and 4 tool calls per answer; the agent loop itself stops at 12 model calls.
_BUDGETS: Final[dict[str, tuple[int, int]]] = {
    "refusal": (0, 0), "general_finance": (3, 2), "goal_calculation": (5, 4), "account_data": (8, 8),
    # planner.MAX_MODEL_CALLS and MAX_TOOL_CALLS, which the planned run enforces.
    "multi_step": (6, 8),
}


@dataclass(frozen=True, slots=True)
class TaskContract:
    """What one request must do. ``required_tools`` empty means no tool is needed.

    A ``multi_step`` contract (a planned run) has ``parts``, one contract per kind of task in the message, and each
    part's required tools are checked on their own.
    """

    task: TaskType
    required_tools: frozenset[str] = frozenset()
    allowed_tools: frozenset[str] = frozenset()
    clarification_ok: bool = False
    max_model_calls: int = 3
    max_tool_calls: int = 2
    parts: tuple[TaskContract, ...] = ()

    def describe(self) -> dict[str, Any]:
        return {
            "task": self.task, "requiredTools": sorted(self.required_tools), "allowedTools": sorted(self.allowed_tools),
            "clarificationOk": self.clarification_ok, "maxModelCalls": self.max_model_calls,
            "maxToolCalls": self.max_tool_calls,
            **({"parts": [{"task": part.task, "requiredTools": sorted(part.required_tools),
                           "clarificationOk": part.clarification_ok} for part in self.parts]} if self.parts else {}),
        }


def contract_for(category: str, agent_type: str, offered_tools: Collection[str]) -> TaskContract:
    """The contract for a routed request, given the tools the agent was offered."""
    offered = frozenset(offered_tools)
    if category in ("off_topic", "code_request"):
        task: TaskType = "refusal"
        required: frozenset[str] = frozenset()
    elif agent_type == "portfolio" and category == "account_data":
        task = "account_data"
        # Every offered tool except the goal ones reads account or market data.
        required = offered - GOAL_TOOLS
    elif "calculate_goal_plan" in offered:
        task = "goal_calculation"
        required = offered & GOAL_TOOLS
    else:
        task = "general_finance"
        required = frozenset()
    model_calls, tool_calls = _BUDGETS[task]
    return TaskContract(
        task=task, required_tools=required, allowed_tools=offered if task != "refusal" else frozenset(),
        clarification_ok=task == "goal_calculation", max_model_calls=model_calls, max_tool_calls=tool_calls,
    )


def plan_contract(parts: Sequence[str], offered_tools: Collection[str]) -> TaskContract:
    """The contract for a planned run over several kinds of task (``context.task_parts()``)."""
    offered = frozenset(offered_tools)
    pieces = []
    for part in parts:
        if part == "account_data":
            pieces.append(TaskContract(task="account_data", required_tools=offered - GOAL_TOOLS))
        elif part == "goal_calculation":
            pieces.append(TaskContract(task="goal_calculation", required_tools=offered & GOAL_TOOLS, clarification_ok=True))
    model_calls, tool_calls = _BUDGETS["multi_step"]
    return TaskContract(task="multi_step", allowed_tools=offered, max_model_calls=model_calls,
                        max_tool_calls=tool_calls, parts=tuple(pieces))


def is_account_tool(name: str, local_tools: Collection[str]) -> bool:
    """Whether calling ``name`` reads account or market data (every INDmoney tool does; some local tools do)."""
    return name in ACCOUNT_LOCAL_TOOLS or (name not in local_tools and name not in GOAL_TOOLS)


@dataclass(slots=True)
class RunFacts:
    """What a finished run did, collected from its events."""

    answer: str = ""
    model_calls: int = 0
    tool_results: list[dict[str, Any]] = field(default_factory=list)  # {"name", "ok", "errorKind"}
    number_check: str | None = None  # "passed" | "warning" | None when not run


# A clarifying question is often a numbered list of what is missing, with no question mark.
_ASKS: Final = re.compile(
    r"\?|\b(?:let me know|tell me|please (?:share|provide|confirm|specify)|could you|can you (?:share|tell|confirm)|"
    r"i(?:'|’)ll need|i need (?:a few|some|more|a bit)|once (?:i|we) (?:have|know))\b",
    re.IGNORECASE,
)


def asks_question(answer: str) -> bool:
    """Whether an answer asks the user for something (a question mark, or a request such as "I'll need ...")."""
    return bool(_ASKS.search(answer))


def evaluate(contract: TaskContract, facts: RunFacts) -> dict[str, Any]:
    """Check a finished run against its contract: ``{"status": "met" | "unmet", "checks": [...]}``."""
    checks: list[dict[str, str]] = []

    def check(name: str, outcome: str, detail: str = "") -> None:
        checks.append({"check": name, "outcome": outcome, **({"detail": detail} if detail else {})})

    check("answered", "passed" if facts.answer.strip() else "failed")
    for part in contract.parts or (contract,):
        if part.required_tools:
            outcome, detail = _required_tool(part, facts)
            check("required_tool", outcome, f"{part.task}: {detail}" if contract.parts else detail)
    outside = sorted({item["name"] for item in facts.tool_results} - contract.allowed_tools)
    if outside:
        check("allowed_tools", "failed", ", ".join(outside))
    if facts.number_check is not None:
        check("numbers", "passed" if facts.number_check == "passed" else "warning")
    tool_calls = len(facts.tool_results)
    within = facts.model_calls <= contract.max_model_calls and tool_calls <= contract.max_tool_calls
    check("budget", "passed" if within else "warning",
          f"{facts.model_calls} model and {tool_calls} tool calls (limits {contract.max_model_calls} and "
          f"{contract.max_tool_calls})")
    status = "unmet" if any(item["outcome"] == "failed" for item in checks) else "met"
    return {"task": contract.task, "status": status, "checks": checks}


def _required_tool(contract: TaskContract, facts: RunFacts) -> tuple[str, str]:
    succeeded = [item["name"] for item in facts.tool_results if item["ok"] and item["name"] in contract.required_tools]
    if succeeded:
        return "passed", ", ".join(sorted(set(succeeded)))
    if contract.clarification_ok and asks_question(facts.answer):
        return "passed", "asked a clarifying question instead"
    called = sorted({item["name"] for item in facts.tool_results})
    return "failed", f"none succeeded (called: {', '.join(called) or 'nothing'})"


def facts_from_events(events: Sequence[dict[str, Any]], answer: str) -> RunFacts:
    """Collect ``RunFacts`` from a run's recorded events (see ``agent_events``)."""
    facts = RunFacts(answer=answer)
    for event in events:
        kind = event.get("kind")
        if kind == "model_call":
            facts.model_calls += 1
        elif kind == "tool_result":
            facts.tool_results.append({"name": event.get("name", ""), "ok": not event.get("errorKind"),
                                       "errorKind": event.get("errorKind")})
        elif kind == "validation" and event.get("check") == "numbers":
            facts.number_check = event.get("outcome")
    return facts

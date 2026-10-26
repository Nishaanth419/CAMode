"""Shared pieces of the financial workflows: the graph's typed state, step specs and the workflow interface.

A workflow is a fixed recipe in code: which tool calls to make in which order (each wave's arguments
come from earlier results), how to read the results, the ``Decimal`` calculations, the checks, and
the payload the model narrates. The model never chooses a tool or computes a figure here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final, TypedDict


class StepResult(TypedDict, total=False):
    """One finished tool call, keyed in ``WorkflowState.results`` by step id."""

    key: str  # The workflow's name for the step ("chain_monthly"); a fixed step keeps its key.
    tool: str
    arguments: dict[str, Any]
    ok: bool
    errorKind: str | None
    error: str | None
    text: str  # The full result (the trace keeps only a capped preview).
    durationMs: int
    attempts: int
    wave: int


class Check(TypedDict, total=False):
    check: str
    outcome: str  # passed | warning | failed
    detail: str


class Calculation(TypedDict, total=False):
    name: str
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    problems: list[str]
    notes: list[str]


class WorkflowState(TypedDict, total=False):
    """Everything one workflow run knows. Nodes return partial updates; nothing here is persisted."""

    request: str
    workflow: str
    inputs: dict[str, Any]
    contract: dict[str, Any]
    today: date
    now: datetime  # IST wall clock, naive
    offered: list[str]  # the tools this run may call
    steps: list[dict[str, Any]]  # every planned step: id, key, tool, arguments, after (step ids), wave, purpose
    pending: list[dict[str, Any]]  # steps planned and not yet run
    results: dict[str, StepResult]
    wave: int
    tool_calls: int
    fixes: list[str]  # step ids already corrected once
    evidence: dict[str, Any]  # normalised data, by name
    calculations: list[Calculation]
    checks: list[Check]
    missing: list[str]
    payload: dict[str, Any]
    answer: str
    stop_reason: str | None  # plan_stop reason
    gathering_done: bool
    failed: bool  # an error ended the run (already reported)


@dataclass(frozen=True, slots=True)
class StepSpec:
    """A tool call a workflow wants next. ``after`` names the keys of the steps whose results it used."""

    key: str
    tool: str
    arguments: dict[str, Any]
    after: tuple[str, ...] = ()
    purpose: str = ""


class Workflow:
    """The interface each workflow implements. Methods read the state; the engine applies the updates."""

    name: str = ""
    title: str = ""
    required_tools: frozenset[str] = frozenset()  # Every one of these must succeed for the contract.
    needs_tools: frozenset[str] = frozenset()  # Must be offered for the workflow to run (defaults to required_tools).
    required_outputs: tuple[str, ...] = ()
    # A failed check of one of these (or any ``arithmetic_*`` check) means the figures cannot be trusted, so the
    # contract's required outputs fail even when every figure was computed.
    critical_checks: frozenset[str] = frozenset({"instrument", "instrument_match", "expiry_match", "leg_instrument", "leg_expiry"})
    max_tool_calls: int = 8
    max_waves: int = 5
    max_wave_steps: int = 10
    narration_rules: str = ""

    def inputs(self, message: str) -> dict[str, Any]:
        return {}

    def next_steps(self, state: WorkflowState) -> list[StepSpec]:
        raise NotImplementedError

    def fix(self, state: WorkflowState, result: StepResult) -> StepSpec | None:
        """A corrected call for a step the tool rejected, or ``None`` when it cannot be fixed."""
        return None

    def normalize(self, state: WorkflowState) -> tuple[dict[str, Any], list[Check]]:
        return {}, []

    def calculate(self, state: WorkflowState) -> list[Calculation]:
        return []

    def validate(self, state: WorkflowState) -> list[Check]:
        return []

    def outputs(self, state: WorkflowState) -> dict[str, bool]:
        """Whether each required output is present (or explicitly reported unavailable with a reason)."""
        return {}

    def payload(self, state: WorkflowState) -> dict[str, Any]:
        raise NotImplementedError

    def fallback_report(self, payload: dict[str, Any]) -> str:
        """A plain report built from the payload, used when the model cannot write one."""
        raise NotImplementedError


def latest(state: WorkflowState, key: str) -> StepResult | None:
    """The newest result for a step key (a corrected call replaces the rejected one)."""
    found = [result for result in (state.get("results") or {}).values() if result.get("key") == key]
    return found[-1] if found else None


def ok_text(state: WorkflowState, key: str) -> str | None:
    result = latest(state, key)
    return result["text"] if result and result.get("ok") else None


def planned(state: WorkflowState, key: str) -> bool:
    return any(step.get("key") == key for step in state.get("steps") or [])


def failure(state: WorkflowState, key: str) -> str | None:
    """Why a step failed (``"tool: kind: error"``), or ``None`` when it succeeded or never ran."""
    result = latest(state, key)
    if result is None or result.get("ok"):
        return None
    return f"{result.get('tool')} {result.get('errorKind') or 'failed'}: {(result.get('error') or '')[:160]}"


@dataclass(slots=True)
class Registry:
    """The workflows CA Mode offers, and how a message selects one."""

    entries: list[tuple[Callable[[str], bool], Workflow]] = field(default_factory=list)

    def select(self, message: str) -> Workflow | None:
        return next((workflow for matches, workflow in self.entries if matches(message)), None)


# Values the engine passes to the workflows that read the clock (IST, naive).
MARKET_OPEN: Final = (9, 15)
MARKET_CLOSE: Final = (15, 30)


def market_open(now: datetime) -> bool:
    """Whether NSE's regular session is running (weekday 09:15–15:30 IST; exchange holidays are not known here)."""
    return now.weekday() < 5 and MARKET_OPEN <= (now.hour, now.minute) < MARKET_CLOSE

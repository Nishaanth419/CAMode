"""Goals joined with the user's INDmoney holdings: allocations, valuation, plans with the mapped value.

A holding is split by explicit whole percentages across goals and "general wealth"
(``memory.AllocationInput`` keeps the total at most 100), and each share is valued from
the latest snapshot rounded down, so a rupee is counted toward at most one goal. A
holding that is not in the latest snapshot (sold, or its asset type was not fetched)
is listed as missing with an unknown value and contributes nothing; it is never
treated as zero. Allocations are bookkeeping only: nothing here places an order.

``context`` imports this module (not the reverse), so everything the agent prompt
needs from a goal comes from ``goal_view``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Final

from pydantic import ValidationError

from . import goal_planning, goal_suitability, memory, portfolio_math, snapshots
from .agent import LocalTool
from .investigation_checks import settings_from_env
from .users import now_ms

CONCENTRATION_WARN_PCT: Final = 50
OVERLAP_NOT_ASSESSABLE: Final = {
    "status": "not_assessable",
    "reason": "Needs each scheme's underlying holdings, which CA Mode does not have.",
}
PRIVATE_REASON: Final = "Private mode takes no portfolio snapshots, so holdings cannot be assigned to goals."
NO_SNAPSHOT_REASON: Final = (
    "No portfolio snapshot yet. Connect INDmoney and refresh your portfolio to assign holdings to goals."
)
UNREADABLE_REASON: Final = "The saved portfolio snapshot could not be read, so assigned holdings are not shown."
logger = logging.getLogger("ca_mode.goal_portfolio")
_ZERO: Final = Decimal(0)


class NoSnapshotError(Exception):
    """The profile has no saved snapshot (or none can exist in its storage mode)."""


class UnknownHoldingError(Exception):
    """The holding is not in the latest snapshot."""


def holding_key(asset_type: Any, code: Any, name: Any, broker: Any) -> str:
    """``asset_type|code|broker``, the same identity ``investigation_math`` gives a holding.

    A row without a code is identified by its normalised name; a missing broker is ``-``.
    """
    code_text = str(code).strip() if code not in (None, "") else None
    identity = code_text or "name:" + re.sub(r"[^a-z0-9]+", " ", str(name or "Unnamed holding").lower()).strip()
    return f"{asset_type}|{identity}|{str(broker).strip() if broker else '-'}"


@dataclass(slots=True)
class Portfolio:
    """The latest snapshot's holdings by key, with its date and whether it is old."""

    available: bool
    reason: str | None = None
    as_of_date: str | None = None
    captured_at: int | None = None
    stale: bool = False
    holdings: dict[str, dict[str, Any]] = field(default_factory=dict)
    fetched_types: frozenset[str] = frozenset()


def load_portfolio(user: dict[str, Any], *, now: int | None = None) -> Portfolio:
    """Read the latest snapshot's holdings; same-key rows are merged. ``available`` is False with a reason otherwise.

    A snapshot that cannot be read leaves the goals usable without holdings instead of failing the request.
    """
    if user.get("storageMode") == "private":
        return Portfolio(False, PRIVATE_REASON)
    try:
        return _read_portfolio(user, now)
    except Exception:
        logger.warning("goal_portfolio_snapshot_unreadable", extra={"user_id": user["id"]}, exc_info=True)
        return Portfolio(False, UNREADABLE_REASON)


def _read_portfolio(user: dict[str, Any], now: int | None) -> Portfolio:
    snapshot = snapshots.latest_snapshot(user["id"], keys=user["keys"])
    if snapshot is None:
        return Portfolio(False, NO_SNAPSHOT_REASON)
    holdings: dict[str, dict[str, Any]] = {}
    for row in portfolio_math.all_holdings(snapshot["results"]):
        key = holding_key(row["asset_type"], row.get("code"), row.get("name"), row.get("broker"))
        value = Decimal(str(row["current_value_inr"] or 0))
        if key in holdings:
            holdings[key]["value"] += value
            continue
        holdings[key] = {
            "key": key, "name": str(row.get("name") or row.get("code") or "Unnamed holding"),
            "assetType": row["asset_type"], "assetClass": row.get("asset_class"), "broker": row.get("broker"),
            "value": value,
        }
    stale_ms = settings_from_env().stale_after_hours * 3_600_000
    fetched = frozenset(
        key[len(portfolio_math.HOLDINGS_PREFIX):] for key in snapshot["results"] if key.startswith(portfolio_math.HOLDINGS_PREFIX)
    )
    return Portfolio(
        True, as_of_date=snapshot["asOfDate"], captured_at=snapshot["capturedAt"],
        stale=(now if now is not None else now_ms()) - snapshot["capturedAt"] > stale_ms,
        holdings=dict(sorted(holdings.items(), key=lambda item: item[1]["value"], reverse=True)),
        fetched_types=fetched,
    )


def share_value(value: Decimal, pct: int) -> int:
    """``pct`` percent of ``value`` in whole rupees, rounded down so the shares of a holding never exceed it."""
    return int((value * pct / 100).to_integral_value(rounding=ROUND_FLOOR))


def _missing_reason(entry: dict[str, Any], portfolio: Portfolio) -> str:
    if entry.get("assetType") in portfolio.fetched_types:
        return "Not in the latest snapshot (sold, or no longer reported). Its value is unknown, so it is not counted."
    return "Its asset type was not fetched in the latest snapshot. Its value is unknown, so it is not counted."


def _goal_shares(
    goal_id: str, allocations: dict[str, dict[str, Any]], portfolio: Portfolio,
) -> tuple[list[tuple[dict[str, Any], int, int]], list[dict[str, Any]]]:
    """This goal's (holding, pct, value) rows from the latest snapshot, and its assigned holdings that are not in it."""
    rows: list[tuple[dict[str, Any], int, int]] = []
    missing: list[dict[str, Any]] = []
    for key, entry in allocations.items():
        pct = entry["goals"].get(goal_id)
        if not pct:
            continue
        holding = portfolio.holdings.get(key)
        if holding is None:
            missing.append({
                "key": key, "name": entry.get("name"), "assetType": entry.get("assetType"), "pct": pct,
                "valueInr": None, "reason": _missing_reason(entry, portfolio),
            })
        else:
            rows.append((holding, pct, share_value(holding["value"], pct)))
    return rows, missing


def _mapping(
    goal: dict[str, Any], suitability: dict[str, Any], allocations: dict[str, dict[str, Any]], portfolio: Portfolio,
) -> dict[str, Any]:
    """The goal's holdings, their values and review, and whether the value counts toward its plan."""
    if not portfolio.available:
        return {"available": False, "reason": portfolio.reason, "mappedValueInr": 0, "counted": False, "holdings": [],
                "missing": [], "concentration": None, "overlap": OVERLAP_NOT_ASSESSABLE}
    rows, missing = _goal_shares(goal["id"], allocations, portfolio)
    total = sum(value for _, _, value in rows)
    top = max(rows, key=lambda row: row[2], default=None)
    share = None if not top or not total else round(top[2] * 100 / total, 1)
    return {
        "available": True,
        "snapshotDate": portfolio.as_of_date, "capturedAt": portfolio.captured_at, "stale": portfolio.stale,
        "mappedValueInr": total,
        # Rupee holdings count only toward a rupee goal; amounts are never converted.
        "counted": goal.get("currency") == "INR" and bool(rows),
        "assigned": len(rows) + len(missing),
        "holdings": [
            {"key": holding["key"], "name": holding["name"], "assetType": holding["assetType"],
             "broker": holding["broker"], "pct": pct, "valueInr": value,
             "review": goal_suitability.review_holding(holding["assetType"], holding["assetClass"], suitability)}
            for holding, pct, value in rows
        ],
        "missing": missing,
        "concentration": None if share is None else {
            "warning": share > CONCENTRATION_WARN_PCT, "key": top[0]["key"], "name": top[0]["name"], "sharePct": share,
        },
        "overlap": OVERLAP_NOT_ASSESSABLE,
    }


def _mapping_warnings(goal: dict[str, Any], mapping: dict[str, Any]) -> list[str]:
    warnings = []
    if not mapping["available"]:
        return warnings
    if mapping["holdings"] and goal.get("currency") != "INR":
        warnings.append(
            f"Assigned holdings are valued in INR and are not counted toward a {goal.get('currency')} goal "
            "(amounts are never converted)."
        )
    if mapping["missing"]:
        names = ", ".join(str(item["name"]) for item in mapping["missing"])
        warnings.append(f"Not in the latest snapshot, so their value is unknown and not counted: {names}.")
    if mapping["stale"]:
        warnings.append(f"The portfolio snapshot dated {mapping['snapshotDate']} is old; assigned values may have moved.")
    if mapping["concentration"] and mapping["concentration"]["warning"]:
        warnings.append(
            f"{mapping['concentration']['name']} is {mapping['concentration']['sharePct']:g}% of the value assigned to this goal."
        )
    return warnings


def goal_view(goal: dict[str, Any], today: date, mapping: dict[str, Any] | None = None) -> dict[str, Any]:
    """The goal with its plan, follow-up questions and suitability (and its holdings mapping when given).

    A goal whose stored values no longer validate gets a failed plan instead of an error.
    """
    mapped = Decimal(mapping["mappedValueInr"]) if mapping and mapping.get("counted") else None
    try:
        plan = goal_planning.plan_goal(goal, today, mapped_amount=mapped)
    except goal_planning.PlanningError as exc:
        plan = {"status": "insufficient_data", "missing": [str(exc)], "scenarios": [], "warnings": [str(exc)],
                "alternatives": [], "feasibility": {"status": "insufficient_data", "summary": str(exc)}}
    suitability = goal_suitability.evaluate(goal, today)
    extra = [] if plan["status"] == "insufficient_data" else [
        warning for warning in [goal_suitability.assumption_warning(goal.get("assumedReturnPct"), suitability)] if warning
    ]
    if mapping is not None:
        extra += _mapping_warnings(goal, mapping)
    if extra:
        plan = {**plan, "warnings": [*plan.get("warnings", []), *extra]}
    view = {
        **goal, "plan": plan, "followUps": goal_planning.follow_ups(goal, today), "suitability": suitability,
    }
    if mapping is not None:
        view["mapping"] = mapping
    return view


def mapped_views(
    goals: list[dict[str, Any]], today: date, allocations: dict[str, dict[str, Any]], portfolio: Portfolio,
) -> list[dict[str, Any]]:
    views = []
    for goal in goals:
        suitability = goal_suitability.evaluate(goal, today)
        views.append(goal_view(goal, today, _mapping(goal, suitability, allocations, portfolio)))
    return views


def _portfolio_summary(
    portfolio: Portfolio, allocations: dict[str, dict[str, Any]], goal_ids: set[str], views: list[dict[str, Any]],
) -> dict[str, Any]:
    summary: dict[str, Any] = {"available": portfolio.available}
    if not portfolio.available:
        return {**summary, "reason": portfolio.reason}
    unassigned_value = general_value = unassigned_count = 0
    for key, holding in portfolio.holdings.items():
        entry = allocations.get(key, {})
        goals_pct = sum(pct for goal_id, pct in entry.get("goals", {}).items() if goal_id in goal_ids)
        general = entry.get("generalWealthPct", 0)
        free = 100 - goals_pct - general
        if free > 0:
            unassigned_count += 1
            unassigned_value += share_value(holding["value"], free)
        general_value += share_value(holding["value"], general)
    return {
        **summary, "asOfDate": portfolio.as_of_date, "capturedAt": portfolio.captured_at, "stale": portfolio.stale,
        "unassignedCount": unassigned_count, "unassignedValueInr": unassigned_value,
        "generalWealthValueInr": general_value,
        "missingCount": sum(1 for key, entry in allocations.items() if key not in portfolio.holdings and any(
            goal_id in goal_ids for goal_id in entry.get("goals", {}))),
        "underfunded": [
            view["name"] for view in views if view["plan"].get("feasibility", {}).get("status") in ("shortfall", "no_time_left")
        ],
    }


def goals_overview(user: dict[str, Any], today: date) -> dict[str, Any]:
    """The ``GET /api/goals`` body: every goal with plan, suitability and mapping, plus portfolio totals."""
    keys = user["keys"]
    goals = memory.list_goals(user["id"], keys)
    allocations = memory.get_allocations(user["id"], keys)
    portfolio = load_portfolio(user)
    views = mapped_views(goals, today, allocations, portfolio)
    return {
        "asOf": today.isoformat(), "maxGoals": memory.MAX_GOALS, "goals": views,
        "fundData": goal_suitability.FUND_DATA,
        "portfolio": _portfolio_summary(portfolio, allocations, {goal["id"] for goal in goals}, views),
    }


def counted_value(user: dict[str, Any], goal_id: str) -> int | None:
    """The INR value of the holdings assigned to ``goal_id`` in the latest snapshot; None when none can be counted."""
    portfolio = load_portfolio(user)
    if not portfolio.available:
        return None
    rows, _ = _goal_shares(goal_id, memory.get_allocations(user["id"], user["keys"]), portfolio)
    return sum(value for _, _, value in rows) if rows else None


def goal_with_mapping(user: dict[str, Any], goal: dict[str, Any], today: date) -> dict[str, Any]:
    """One goal in the same shape as ``goals_overview`` items."""
    allocations = memory.get_allocations(user["id"], user["keys"])
    return mapped_views([goal], today, allocations, load_portfolio(user))[0]


# ── Allocations ────────────────────────────────────────────────────────────────

def _holding_row(
    holding: dict[str, Any], entry: dict[str, Any], goal_names: dict[str, str],
) -> dict[str, Any]:
    pcts = {goal_id: pct for goal_id, pct in entry.get("goals", {}).items() if goal_id in goal_names}
    general = entry.get("generalWealthPct", 0)
    free = 100 - sum(pcts.values()) - general
    return {
        "key": holding["key"], "name": holding["name"], "assetType": holding["assetType"], "broker": holding["broker"],
        "currentValueInr": float(holding["value"]),
        "broadClass": goal_suitability.classify_holding(holding["assetType"], holding["assetClass"]),
        "goals": [
            {"goalId": goal_id, "goalName": goal_names[goal_id], "pct": pct, "valueInr": share_value(holding["value"], pct)}
            for goal_id, pct in pcts.items()
        ],
        "generalWealthPct": general,
        "unassignedPct": free,
        "unassignedValueInr": share_value(holding["value"], free),
    }


def allocations_view(user: dict[str, Any]) -> dict[str, Any]:
    """The ``GET /api/goals/allocations`` body: every holding with its split, and assigned holdings now missing."""
    keys = user["keys"]
    names = {goal["id"]: goal["name"] for goal in memory.list_goals(user["id"], keys)}
    allocations = memory.get_allocations(user["id"], keys)
    portfolio = load_portfolio(user)
    body: dict[str, Any] = {"available": portfolio.available, "fundData": goal_suitability.FUND_DATA}
    if not portfolio.available:
        return {**body, "reason": portfolio.reason, "holdings": [], "missing": []}
    return {
        **body, "asOfDate": portfolio.as_of_date, "capturedAt": portfolio.captured_at, "stale": portfolio.stale,
        "holdings": [_holding_row(holding, allocations.get(key, {}), names) for key, holding in portfolio.holdings.items()],
        "missing": [
            {"key": key, "name": entry.get("name"), "assetType": entry.get("assetType"),
             "goals": [{"goalId": goal_id, "goalName": names[goal_id], "pct": pct}
                       for goal_id, pct in entry.get("goals", {}).items() if goal_id in names],
             "generalWealthPct": entry.get("generalWealthPct", 0), "reason": _missing_reason(entry, portfolio)}
            for key, entry in allocations.items() if key not in portfolio.holdings
        ],
    }


def assign(user: dict[str, Any], body: memory.AllocationInput) -> dict[str, Any]:
    """Save one holding's split and return its updated row.

    Raises:
        NoSnapshotError: the profile has no snapshot to take the holding from.
        UnknownHoldingError: the holding is not in the latest snapshot.
        memory.UnknownGoalError: a goal in the split does not exist.
    """
    portfolio = load_portfolio(user)
    if not portfolio.available:
        raise NoSnapshotError(portfolio.reason)
    holding = portfolio.holdings.get(body.holdingKey)
    if holding is None:
        raise UnknownHoldingError("That holding is not in your latest portfolio snapshot.")
    entry = {
        "name": holding["name"], "assetType": holding["assetType"], "broker": holding["broker"],
        "goals": {share.goalId: share.pct for share in body.goals}, "generalWealthPct": body.generalWealthPct,
    }
    stored = memory.set_allocation(user["id"], user["keys"], body.holdingKey, entry)
    names = {goal["id"]: goal["name"] for goal in memory.list_goals(user["id"], user["keys"])}
    return _holding_row(holding, stored or {}, names)


# ── Agent tools ────────────────────────────────────────────────────────────────

# Groq rejects tool results over 6,000 characters.
MAX_TOOL_PAYLOAD_CHARS: Final = 5500
WHAT_IF_NOT_SAVED: Final = "Not saved. To keep this, edit the goal on the Goals page."


def _goal_matches(goals: list[dict[str, Any]], name: Any) -> list[dict[str, Any]]:
    """Goals named ``name`` (exactly, else as part of their name), case-insensitively; all goals without a name."""
    if not isinstance(name, str) or not name.strip():
        return goals
    wanted = name.strip().lower()
    exact = [goal for goal in goals if goal["name"].lower() == wanted]
    return exact or [goal for goal in goals if wanted in goal["name"].lower() or goal["name"].lower() in wanted]


def _tool_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """The plan without the assumptions list (sent once) and with compact scenarios."""
    kept = {key: value for key, value in plan.items() if key not in ("assumptions", "scenarios", "asOf")}
    kept["scenarios"] = [
        {key: item[key] for key in ("label", "basis", "projectedAmount", "difference", "requiredMonthly", "additionalMonthly")}
        for item in plan.get("scenarios", [])
    ]
    return kept


def _tool_suitability(suitability: dict[str, Any], *, reasons: bool) -> dict[str, Any]:
    categories = [
        {"category": item["label"], "verdict": item["verdict"], **({"reasons": item["reasons"]} if reasons else {})}
        for item in suitability["categories"]
    ]
    return {
        "categories": categories, "enoughInformation": suitability["enoughInformation"],
        "assumedCautiously": suitability["assumed"], "capitalPreservation": suitability["capitalPreservation"],
    }


def _tool_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    if not mapping["available"]:
        return {"available": False, "reason": mapping["reason"]}
    return {
        "snapshotDate": mapping["snapshotDate"], "stale": mapping["stale"], "valueCountedInr": mapping["mappedValueInr"] if mapping["counted"] else 0,
        "assignedValueInr": mapping["mappedValueInr"],
        "holdings": [
            {"name": item["name"], "pct": item["pct"], "valueInr": item["valueInr"], "review": item["review"]["verdict"],
             "reviewNote": item["review"]["reason"]} for item in mapping["holdings"]
        ],
        "notInLatestSnapshot": [item["name"] for item in mapping["missing"]],
        "concentration": mapping["concentration"], "overlap": mapping["overlap"]["status"],
    }


def _tool_goal(view: dict[str, Any], *, reasons: bool, holdings: bool) -> dict[str, Any]:
    item = {
        key: view.get(key) for key in (
            "name", "kind", "status", "currency", "targetAmount", "targetDate", "savedAmount", "monthlyContribution",
            "assumedReturnPct", "inflationPct", "flexibility", "liquidityNeed", "riskTolerance", "lossCapacity",
        ) if view.get(key) is not None
    }
    item["plan"] = _tool_plan(view["plan"])
    item["suitability"] = _tool_suitability(view["suitability"], reasons=reasons)
    item["followUps"] = [question["question"] for question in view["followUps"]]
    if holdings and "mapping" in view:
        item["holdings"] = _tool_mapping(view["mapping"])
    elif view.get("assignedHoldings"):
        item["assignedHoldings"] = view["assignedHoldings"]
        item["assignedHoldingsNote"] = (
            "Their value is not available here and is left out of this plan, so its figures differ from the Goals "
            "page, which counts it. Portfolio Analyzer can include it."
        )
    return item


def _fit(goals: list[dict[str, Any]], head: dict[str, Any]) -> dict[str, Any]:
    """The payload under ``MAX_TOOL_PAYLOAD_CHARS``: reasons are dropped first, then goals from the end."""
    def size(items: list[dict[str, Any]], note: str | None = None) -> int:
        return len(json.dumps({**head, "goals": items, **({"truncated": note} if note else {})}, ensure_ascii=False))

    def strip_reasons(item: dict[str, Any]) -> dict[str, Any]:
        suitability = {**item["suitability"], "categories": [
            {key: value for key, value in entry.items() if key != "reasons"} for entry in item["suitability"]["categories"]
        ]}
        return {**item, "suitability": suitability}

    shown = goals
    if size(shown) > MAX_TOOL_PAYLOAD_CHARS:
        shown = [strip_reasons(item) for item in shown]
    note = None
    while size(shown, note) > MAX_TOOL_PAYLOAD_CHARS and len(shown) > 1:
        shown = shown[:-1]
        note = (f"{len(goals) - len(shown)} more goals were left out to fit; ask about one by name "
                "with goalName to see it.")
    return {**head, "goals": shown, **({"truncated": note} if note else {})}


CALCULATOR_NOTE: Final = "Calculated from the numbers in this chat. Nothing is saved; to keep it, add the goal on the Goals page."
_CALCULATOR_AMOUNTS: Final = ("targetAmount", "savedAmount", "monthlyContribution")
_CALCULATOR_FIELDS: Final = {
    "currency": "currency", "targetAmount": "targetAmount", "targetDate": "targetDate", "horizonYears": "horizonYears",
    "savedAmount": "savedAmount", "monthlyContribution": "monthlyContribution", "annualReturnPct": "assumedReturnPct",
    "inflationPct": "inflationPct", "flexibility": "flexibility", "liquidityNeed": "liquidityNeed",
    "riskTolerance": "riskTolerance", "lossCapacity": "lossCapacity",
}


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0]
    # Name the argument as the model sent it (annualReturnPct, not the stored assumedReturnPct).
    arguments = {field: argument for argument, field in _CALCULATOR_FIELDS.items()}
    where = ".".join(arguments.get(str(part), str(part)) for part in first.get("loc", ()) if part != "__root__")
    message = str(first.get("msg", "invalid value")).removeprefix("Value error, ")
    return f"{where}: {message}" if where else message


def calculate_goal(arguments: dict[str, Any], today: date) -> dict[str, Any]:
    """A plan for a goal described in chat (never saved), validated like a saved goal; ``{"error"}`` when invalid."""
    fields = {_CALCULATOR_FIELDS[key]: value for key, value in arguments.items() if key in _CALCULATOR_FIELDS and value is not None}
    for name in _CALCULATOR_AMOUNTS:
        # Models write amounts as floats ("5000000.0"); goals keep whole units.
        if isinstance(fields.get(name), float):
            fields[name] = round(fields[name])
    try:
        body = memory.GoalInput(name="Goal described in chat", status="draft", **fields)
        goal = memory._goal_data(body, today=today)
        plan = goal_planning.plan_goal(goal, today)
    except ValidationError as exc:
        return {"error": _validation_message(exc)}
    except (memory.InvalidGoalError, goal_planning.PlanningError) as exc:
        return {"error": str(exc)}
    view = goal_view(goal, today)
    item = _tool_goal(view, reasons=False, holdings=False)
    item.pop("name", None)
    item.pop("status", None)
    return {
        "asOf": today.isoformat(), "saved": False, "note": CALCULATOR_NOTE, "inputs": {
            key: value for key, value in item.items() if key not in ("plan", "suitability", "followUps")
        },
        "plan": item["plan"], "suitability": item["suitability"], "followUps": item["followUps"],
        "assumptions": plan.get("assumptions", list(goal_planning.ASSUMPTIONS)), "fundData": goal_suitability.FUND_DATA["reason"],
    }


def goal_calculator_tool(*, today: date | None = None) -> LocalTool:
    """A read-only calculator for a goal the user describes in chat. It reads and saves nothing."""

    async def calculate_goal_plan(arguments: dict[str, Any]) -> str:
        day = today or snapshots.local_now().date()
        return json.dumps(calculate_goal(arguments, day), ensure_ascii=False, separators=(",", ":"))

    level = {"type": "string", "enum": ["low", "medium", "high"]}
    return LocalTool(
        name="calculate_goal_plan",
        description=(
            "Calculate a savings goal the user describes in this chat (not a saved goal): the monthly amount it needs, "
            "projections, feasibility, ways to close a gap and which investment categories suit it under fixed rules. "
            "Use it instead of doing goal arithmetic yourself. Pass only numbers the user gave; missing ones come back "
            "as followUps to ask about. Nothing is saved."
        ),
        input_schema={"type": "object", "properties": {
            "currency": {"type": "string", "enum": list(memory.Currency.__args__)},
            "targetAmount": {"type": "number", "description": "Amount needed, in whole units of the currency."},
            "targetDate": {"type": "string", "description": "YYYY-MM-DD. Give this or horizonYears."},
            "horizonYears": {"type": "integer", "description": "Years from today until the money is needed."},
            "savedAmount": {"type": "number", "description": "Already set aside for this goal."},
            "monthlyContribution": {"type": "number", "description": "Planned amount each month."},
            "annualReturnPct": {"type": "number", "description": "The user's assumed yearly return, 0 to 30. Omit if not given."},
            "inflationPct": {"type": "number", "description": "Yearly inflation to raise the target by, 0 to 15. Omit if not given."},
            "flexibility": {"type": "string", "enum": ["fixed", "some", "flexible"]},
            "liquidityNeed": level, "riskTolerance": level, "lossCapacity": level,
        }, "additionalProperties": False},
        handler=calculate_goal_plan,
    )


def goal_tools(user: dict[str, Any], *, include_holdings: bool, today: date | None = None) -> list[LocalTool]:
    """Read-only tools over the profile's saved goals. Nothing they do is saved.

    ``include_holdings`` adds the INDmoney holdings assigned to each goal (Portfolio
    Analyzer only); without it the tools never read a portfolio snapshot.
    """

    def views(goals: list[dict[str, Any]], day: date) -> list[dict[str, Any]]:
        allocations = memory.get_allocations(user["id"], user["keys"])
        if include_holdings:
            return mapped_views(goals, day, allocations, load_portfolio(user))
        return [
            {**goal_view(goal, day), "assignedHoldings": sum(1 for entry in allocations.values() if entry["goals"].get(goal["id"]))}
            for goal in goals
        ]

    def build_plans(arguments: dict[str, Any]) -> str:
        day = today or snapshots.local_now().date()
        goals = memory.list_goals(user["id"], user["keys"])
        if not goals:
            return json.dumps({"goals": [], "message": "No goals are saved. The user can add one on the Goals page."})
        chosen = _goal_matches(goals, arguments.get("goalName"))
        if not chosen:
            return json.dumps({"error": "No goal has that name.", "goalNames": [goal["name"] for goal in goals]})
        items = [_tool_goal(view, reasons=True, holdings=include_holdings) for view in views(chosen, day)]
        head = {
            "asOf": day.isoformat(), "fundData": goal_suitability.FUND_DATA,
            "assumptions": list(goal_planning.ASSUMPTIONS), "note": goal_suitability.NOTE,
        }
        return json.dumps(_fit(items, head), ensure_ascii=False, separators=(",", ":"))

    def what_if(arguments: dict[str, Any]) -> str:
        day = today or snapshots.local_now().date()
        goals = memory.list_goals(user["id"], user["keys"])
        chosen = _goal_matches(goals, arguments.get("goalName")) if arguments.get("goalName") else []
        if len(chosen) != 1:
            reason = "Name one goal." if chosen else "No goal has that name."
            return json.dumps({"error": reason, "goalNames": [goal["name"] for goal in goals]})
        goal = dict(chosen[0])
        changes: dict[str, Any] = {}
        for argument, field_name in (
            ("monthlyContribution", "monthlyContribution"), ("targetAmount", "targetAmount"),
            ("targetDate", "targetDate"), ("annualReturnPct", "assumedReturnPct"), ("inflationPct", "inflationPct"),
        ):
            if arguments.get(argument) is not None:
                goal[field_name] = arguments[argument]
                changes[argument] = arguments[argument]
        try:
            if arguments.get("extraMonths") is not None:
                extra = arguments["extraMonths"]
                if isinstance(extra, bool) or not isinstance(extra, int) or not 0 <= extra <= goal_planning.MAX_EXTENSION_MONTHS:
                    raise goal_planning.PlanningError(
                        f"extraMonths must be a whole number from 0 to {goal_planning.MAX_EXTENSION_MONTHS}."
                    )
                base = goal_planning._target_date(goal.get("targetDate"))
                if base is None:
                    raise goal_planning.PlanningError("This goal has no target date to move; give targetDate instead.")
                goal["targetDate"] = goal_planning.add_months(base, extra).isoformat()
                changes["extraMonths"] = extra
            if arguments.get("lumpSum") is not None:
                lump = goal_planning._decimal(arguments["lumpSum"], "The lump sum")
                if lump is None or lump < 0:
                    raise goal_planning.PlanningError("The lump sum cannot be negative.")
                goal["savedAmount"] = (goal.get("savedAmount") or 0) + float(lump)
                changes["lumpSum"] = arguments["lumpSum"]
            # goal_view turns a bad value into a failed plan; a bad override should be an error the model can fix.
            goal_planning.plan_goal(goal, day)
            view = views([goal], day)[0]
        except goal_planning.PlanningError as exc:
            return json.dumps({"error": str(exc)})
        item = _tool_goal(view, reasons=False, holdings=include_holdings)
        result = {
            "goal": goal["name"], "saved": False, "note": WHAT_IF_NOT_SAVED, "changes": changes,
            "plan": item["plan"], "assumptions": list(goal_planning.ASSUMPTIONS),
        }
        if "targetDate" in changes or "extraMonths" in changes:
            result["suitability"] = item["suitability"]
        if "holdings" in item:
            result["holdings"] = item["holdings"]
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    async def get_goal_plans(arguments: dict[str, Any]) -> str:
        return await asyncio.to_thread(build_plans, arguments)

    async def plan_goal_what_if(arguments: dict[str, Any]) -> str:
        return await asyncio.to_thread(what_if, arguments)

    return [
        LocalTool(
            name="get_goal_plans",
            description=(
                "The user's saved goals with calculated plans, feasibility, ways to close a gap, investment category "
                "suitability from fixed rules, and questions still unanswered. Quote its figures; never recompute. "
                "It has no fund data, so never name or rank funds."
            ),
            input_schema={"type": "object", "properties": {
                "goalName": {"type": "string", "description": "Only the goal with this name; omit for all goals."},
            }, "additionalProperties": False},
            handler=get_goal_plans,
        ),
        LocalTool(
            name="plan_goal_what_if",
            description=(
                "Recalculate one saved goal with changed numbers, such as a different monthly amount or date. "
                "Nothing is saved; the goal on the Goals page stays as it is."
            ),
            input_schema={"type": "object", "properties": {
                "goalName": {"type": "string"},
                "monthlyContribution": {"type": "number", "description": "Monthly contribution to try."},
                "targetAmount": {"type": "number"},
                "targetDate": {"type": "string", "description": "YYYY-MM-DD"},
                "extraMonths": {"type": "integer", "description": "Months to add to the target date."},
                "lumpSum": {"type": "number", "description": "An extra amount earmarked today."},
                "annualReturnPct": {"type": "number", "description": "Yearly return to assume, 0 to 30."},
                "inflationPct": {"type": "number", "description": "Yearly inflation for the target, 0 to 15."},
            }, "required": ["goalName"], "additionalProperties": False},
            handler=plan_goal_what_if,
        ),
    ]

"""Portfolio change investigations: a bounded, read-only workflow over saved snapshots.

The workflow is fixed code, not a model loop: load the latest snapshot and the
comparable earlier one, normalise both, calculate changes and allocation, run the
consistency checks, and build an evidence-backed result. The model only narrates
that result (through the ``investigate_portfolio_change`` tool) and never computes it.

Every run is bounded by a per-step timeout and an overall deadline. Only the saved
snapshot reads are retried, once, on a transient SQLite lock; check failures are never
retried because they need new data, not another attempt.

Runs are persisted for profiles that save history (``device`` mode): status and times
in plain columns, and the request, steps and result sealed with the profile's key like
chats and snapshots. Private-mode profiles have no snapshots, so their runs end with
"insufficient data" and are not stored. Nothing here writes to INDmoney.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Final, Literal, TypeVar

from . import snapshots, users
from .agent import LocalTool
from .db import connect_db
from .investigation_checks import CheckResult, CheckSettings, history_unavailable, run_checks, settings_from_env
from .investigation_math import (
    Allocation,
    HoldingChange,
    PortfolioChange,
    PortfolioSnapshot,
    ValueChange,
    calculate_allocation,
    calculate_portfolio_change,
    money,
    normalize_snapshot,
    percent,
    snapshot_id,
)
from .portfolio_math import inr
from .sealing import LockedError, ProfileKeys

logger = logging.getLogger("ca_mode.investigations")

KIND: Final = "portfolio_change"
ENGINE_VERSION: Final = "portfolio_change.v1"
KEEP_INVESTIGATIONS: Final = 50
STEP_TIMEOUT_SECONDS: Final = 10.0
DEADLINE_SECONDS: Final = 30.0
TOP_CONTRIBUTORS: Final = 5
# Groq shortens tool results over 6,000 characters by cutting the middle, which would break the JSON.
MAX_AGENT_PAYLOAD_CHARS: Final = 5500
# A run still "running" this long after its last update was interrupted (e.g. by a restart).
INTERRUPTED_AFTER_MS: Final = int(DEADLINE_SECONDS * 2 * 1000)

Status = Literal["running", "verifying", "completed", "insufficient_data", "failed", "cancelled"]
Trigger = Literal["api", "agent"]
T = TypeVar("T")


class InvestigationError(RuntimeError):
    """An investigation request was invalid; the message is safe to show the user."""


class InvestigationStorageError(RuntimeError):
    """The investigation could not be saved before it started; nothing was run."""


@dataclass(slots=True)
class _Recorder:
    """Collects the steps of one run with their timing, bounded by an overall deadline."""

    deadline: float
    steps: list[dict[str, Any]] = field(default_factory=list)

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    async def run(self, name: str, purpose: str, work: Callable[[], T], *, retry_on_lock: bool = False) -> T:
        """Run ``work`` in a thread under the step timeout and record the outcome."""
        step: dict[str, Any] = {"step": len(self.steps) + 1, "name": name, "purpose": purpose, "status": "running"}
        self.steps.append(step)
        started = time.perf_counter()
        try:
            result = await self._attempt(work, retry_on_lock=retry_on_lock)
        except BaseException as exc:
            step["status"] = "failed"
            step["error"] = _safe_error(exc)
            raise
        finally:
            step["durationMs"] = round((time.perf_counter() - started) * 1000)
        step["status"] = "completed"
        return result

    @property
    def last(self) -> dict[str, Any]:
        return self.steps[-1]

    async def _once(self, work: Callable[[], T]) -> T:
        timeout = min(STEP_TIMEOUT_SECONDS, self.remaining())
        if timeout <= 0:
            raise TimeoutError("The investigation ran out of time.")
        return await asyncio.wait_for(asyncio.to_thread(work), timeout=timeout)

    async def _attempt(self, work: Callable[[], T], *, retry_on_lock: bool) -> T:
        try:
            return await self._once(work)
        except sqlite3.OperationalError as exc:
            # Another writer holding the database past SQLite's busy timeout is transient; reads are idempotent.
            if not (retry_on_lock and "locked" in str(exc).lower()):
                raise
        await asyncio.sleep(0.2 + random.random() * 0.2)  # noqa: S311 - retry jitter, not security
        return await self._once(work)


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "Timed out."
    if isinstance(exc, asyncio.CancelledError):
        return "Cancelled."
    if isinstance(exc, InvestigationError):
        return str(exc)
    return "Unexpected error while reading saved data."


def _describe(step: dict[str, Any], summary: str, evidence: list[str] | None = None) -> None:
    step["summary"] = summary
    if evidence:
        step["evidence"] = evidence


# ── Serialisation ──────────────────────────────────────────────────────────────

def _local(snapshot: PortfolioSnapshot) -> str:
    return snapshot.captured_at.astimezone(snapshots.SNAPSHOT_TIMEZONE).isoformat(timespec="minutes")


def _snapshot_ref(snapshot: PortfolioSnapshot) -> dict[str, Any]:
    return {
        "snapshotId": snapshot.snapshot_id, "asOfDate": snapshot.as_of_date, "capturedAt": _local(snapshot),
        "accountScope": snapshot.account_scope, "currency": snapshot.currency, "source": snapshot.source,
    }


def _pct(value: Decimal | None) -> float | None:
    return None if value is None else float(round(value, 2))


def _change_dict(change: ValueChange) -> dict[str, Any]:
    return {"previousInr": money(change.previous), "currentInr": money(change.current),
            "changeInr": money(change.absolute), "changePct": _pct(change.percentage), "direction": change.direction}


def _share_of_change(part: Decimal | None, total_change: Decimal | None) -> float | None:
    """``part`` as a percentage of the change in total current value (negative when it moved the other way)."""
    return _pct(percent(part, total_change))


def _holding_dict(item: HoldingChange, total_change: Decimal | None) -> dict[str, Any]:
    return {
        "holdingId": item.holding_id, "name": item.name, "assetType": item.asset_type, "broker": item.broker,
        "status": item.status, **_change_dict(item.value),
        "shareOfChangePct": _share_of_change(item.value.absolute, total_change),
        "priceEffectInr": money(item.price_effect_inr), "quantityEffectInr": money(item.quantity_effect_inr),
        "identifierChanged": item.identifier_changed,
    }


def _signed(amount: Decimal) -> str:
    return ("+" if amount > 0 else "") + inr(amount)


def _change_phrase(change: ValueChange, base: str | None = None) -> str:
    """"rose by ₹X (+Y% of <base>)"; naming the base keeps the percentage from being misread."""
    if change.absolute is None:
        return "could not be compared"
    if change.direction == "unchanged":
        return "did not change"
    verb = "rose" if change.direction == "up" else "fell"
    of = f" of {base}" if base else ""
    pct = f" ({_pct(change.percentage):+.2f}%{of})" if change.percentage is not None else ""
    return f"{verb} by {inr(abs(change.absolute))}{pct}"


# ── Findings ───────────────────────────────────────────────────────────────────

_TOTALS_DEPEND_ON: Final = ("snapshot_identity", "totals_consistency")
_HOLDINGS_DEPEND_ON: Final = ("snapshot_identity", "holdings_reconciliation", "change_arithmetic", "currency_consistency")


def _finding(
    finding_id: str, claim: str, change: ValueChange | None, ids: list[str], depends_on: tuple[str, ...],
    statuses: dict[str, str], *, kind: str = "observed", denominator: str | None = None, value: Decimal | None = None,
) -> dict[str, Any]:
    """A claim with its numbers and evidence; ``verified`` only when every check it depends on passed."""
    item: dict[str, Any] = {
        "id": finding_id, "kind": kind, "claim": claim, "unit": "INR", "sourceSnapshotIds": ids,
        "dependsOn": list(depends_on), "verified": all(statuses.get(name) == "passed" for name in depends_on),
    }
    if change is not None:
        item.update(_change_dict(change))
    if value is not None:
        item["valueInr"] = money(value)
    if denominator:
        item["percentageDenominator"] = denominator
    return item


def _total_findings(
    previous: PortfolioSnapshot, current: PortfolioSnapshot, change: PortfolioChange, statuses: dict[str, str],
) -> list[dict[str, Any]]:
    ids = [previous.snapshot_id, current.snapshot_id]
    between = f"between {previous.as_of_date} and {current.as_of_date}"
    findings = [
        _finding("net_worth", f"Net worth {_change_phrase(change.net_worth, 'the earlier net worth')} {between}.",
                 change.net_worth, ids, _TOTALS_DEPEND_ON, statuses, denominator=f"net worth on {previous.as_of_date}"),
        _finding("current_value",
                 f"Total current value (before liabilities) {_change_phrase(change.current_value, 'the earlier value')} {between}.",
                 change.current_value, ids, _TOTALS_DEPEND_ON, statuses,
                 denominator=f"total current value on {previous.as_of_date}"),
        _finding("invested",
                 f"Invested amount (INDmoney's cost basis) {_change_phrase(change.invested, 'the earlier invested amount')} "
                 f"{between}; this reflects buying, selling or cost updates, not necessarily new money added.",
                 change.invested, ids, _TOTALS_DEPEND_ON, statuses, denominator=f"invested amount on {previous.as_of_date}"),
    ]
    if change.market_effect_estimate_inr is not None:
        findings.append(_finding(
            "market_effect",
            f"About {_signed(change.market_effect_estimate_inr)} of the change in value is not explained by a change in the "
            "invested amount, which points to price movement (an estimate: INDmoney's invested amount is a cost basis, "
            "not a record of money added or withdrawn).",
            None, ids, _TOTALS_DEPEND_ON, statuses, kind="estimate", value=change.market_effect_estimate_inr,
        ))
    if change.liabilities.absolute:
        findings.append(_finding("liabilities", f"Liabilities {_change_phrase(change.liabilities)} {between}.",
                                 change.liabilities, ids, _TOTALS_DEPEND_ON, statuses))
    return findings


def _asset_type_findings(
    previous: PortfolioSnapshot, current: PortfolioSnapshot, change: PortfolioChange, statuses: dict[str, str],
) -> list[dict[str, Any]]:
    ids = [previous.snapshot_id, current.snapshot_id]
    moved = [item for item in change.asset_types if item.value.absolute][:3]
    return [
        _finding(f"asset_type:{item.asset_type}", f"{item.asset_type} {_change_phrase(item.value, 'its earlier value')}.",
                 item.value, ids,
                 _TOTALS_DEPEND_ON, statuses, denominator=f"{item.asset_type} value on {previous.as_of_date}")
        for item in moved
    ]


def _holding_claim(item: HoldingChange) -> str:
    if item.status == "new":
        return f"{item.name} ({item.asset_type}) was added, now worth {inr(item.value.current or 0)}."
    if item.status == "exited":
        return f"{item.name} ({item.asset_type}), worth {inr(item.value.previous or 0)}, no longer appears."
    claim = f"{item.name} ({item.asset_type}) {_change_phrase(item.value, 'its earlier value')}"
    if item.price_effect_inr is not None and item.quantity_effect_inr is not None:
        claim += f": {_signed(item.price_effect_inr)} from price and {_signed(item.quantity_effect_inr)} from units held"
    return claim + "."


def _contributors(change: PortfolioChange) -> tuple[list[HoldingChange], list[HoldingChange]]:
    moved = [item for item in change.holdings if item.status != "not_comparable" and item.value.absolute]
    rising = sorted((i for i in moved if i.value.direction == "up"), key=lambda i: i.value.absolute or 0, reverse=True)
    falling = sorted((i for i in moved if i.value.direction == "down"), key=lambda i: i.value.absolute or 0)
    return rising[:TOP_CONTRIBUTORS], falling[:TOP_CONTRIBUTORS]


def _holding_findings(
    previous: PortfolioSnapshot, current: PortfolioSnapshot, change: PortfolioChange, statuses: dict[str, str],
) -> list[dict[str, Any]]:
    ids = [previous.snapshot_id, current.snapshot_id]
    rising, falling = _contributors(change)
    return [
        _finding(f"holding:{item.holding_id}", _holding_claim(item), item.value, ids, _HOLDINGS_DEPEND_ON, statuses,
                 denominator=f"the holding's value on {previous.as_of_date}")
        for item in [*rising[:3], *falling[:3]]
    ]


def _limitations(previous: PortfolioSnapshot, current: PortfolioSnapshot, change: PortfolioChange) -> list[str]:
    notes = [
        "Snapshots are saved once a day, so moves within a day, and between the two dates, are not visible.",
        "These are observed changes in your saved data. They show what moved, not why: no market news or other cause "
        "was checked.",
    ]
    if change.not_comparable_asset_types:
        notes.append("Holdings for " + ", ".join(change.not_comparable_asset_types) + " were not saved in both snapshots, "
                     "so their individual holdings are not compared (their asset-type totals still are).")
    no_cost = [item.name for item in current.holdings if not item.has_cost_basis]
    if no_cost:
        names = ", ".join(no_cost[:5]) + (f" and {len(no_cost) - 5} more" if len(no_cost) > 5 else "")
        notes.append(f"No invested amount is reported for {names} (usually holdings at linked brokers), so "
                     "cost-based figures exclude them.")
    if current.as_of_date != previous.as_of_date and (current.captured_at - previous.captured_at) > timedelta(days=7):
        notes.append("The snapshots are more than a week apart; the change covers the whole period.")
    return notes


def _next_actions(previous: PortfolioSnapshot, current: PortfolioSnapshot) -> list[dict[str, str]]:
    month_ago = (date.fromisoformat(current.as_of_date) - timedelta(days=30)).isoformat()
    return [
        {"label": "Show calculation",
         "prompt": f"Show how my portfolio change between {previous.as_of_date} and {current.as_of_date} was calculated."},
        {"label": "Compare with last month", "prompt": f"Investigate why my portfolio changed since {month_ago}."},
        {"label": "Check allocation", "prompt": "Check my current allocation by asset type."},
    ]


def _summary(change: PortfolioChange, previous: PortfolioSnapshot, current: PortfolioSnapshot,
             checks: list[CheckResult]) -> str:
    text = f"Net worth {_change_phrase(change.net_worth)} between {previous.as_of_date} and {current.as_of_date}."
    rising, falling = _contributors(change)
    biggest = max([*rising[:1], *falling[:1]], key=lambda item: abs(item.value.absolute or 0), default=None)
    if biggest is not None and biggest.value.absolute is not None:
        text += f" The largest single move was {biggest.name} ({_signed(biggest.value.absolute)})."
    if any(check.status == "failed" for check in checks):
        text += " Some figures conflict, so findings that depend on them are marked unverified."
    return text


def _allocation_dict(allocation: Allocation) -> dict[str, Any]:
    return {
        "snapshotId": allocation.snapshot_id, "denominatorInr": money(allocation.denominator_inr),
        "denominatorBasis": allocation.basis, "netWorthInr": money(allocation.net_worth_inr),
        "unclassifiedInr": money(allocation.unclassified_inr),
        "byAssetType": [{"assetType": row.label, "valueInr": money(row.value_inr), "sharePct": _pct(row.share_pct)}
                        for row in allocation.by_asset_type],
    }


def build_result(
    previous: PortfolioSnapshot, current: PortfolioSnapshot, change: PortfolioChange,
    allocation: Allocation, checks: list[CheckResult],
) -> dict[str, Any]:
    """The evidence-backed result of a completed comparison (no ids or timestamps of the run itself)."""
    statuses = {check.check: check.status for check in checks}
    rising, falling = _contributors(change)
    total_change = change.current_value.absolute
    return {
        "title": "Portfolio change investigation",
        "summary": _summary(change, previous, current, checks),
        "comparison": {"previous": _snapshot_ref(previous), "current": _snapshot_ref(current)},
        "findings": [*_total_findings(previous, current, change, statuses),
                     *_asset_type_findings(previous, current, change, statuses),
                     *_holding_findings(previous, current, change, statuses)],
        "changes": {
            "netWorth": _change_dict(change.net_worth), "currentValue": _change_dict(change.current_value),
            "invested": _change_dict(change.invested), "liabilities": _change_dict(change.liabilities),
            "marketEffectEstimateInr": money(change.market_effect_estimate_inr),
            "shareOfChangeDenominator": f"change in total current value ({inr(total_change)})"
            if total_change else None,
            "byAssetType": [{"assetType": item.asset_type, "holdingsComparable": item.holdings_comparable,
                             **_change_dict(item.value),
                             "shareOfChangePct": _share_of_change(item.value.absolute, total_change)}
                            for item in change.asset_types],
            "topRising": [_holding_dict(item, total_change) for item in rising],
            "topFalling": [_holding_dict(item, total_change) for item in falling],
        },
        "allocation": _allocation_dict(allocation),
        "checks": [check.to_dict() for check in checks],
        "warnings": [check.summary for check in checks if check.status in ("warning", "failed")],
        "limitations": _limitations(previous, current, change),
        "calculations": [
            "change = value in the later snapshot - value in the earlier snapshot",
            "percentage change = change / |value in the earlier snapshot| x 100 (not shown when that value is 0)",
            "share = value / the same snapshot's total current value x 100",
            "share of change = a holding's or asset type's change / the change in total current value x 100",
            "price effect = earlier units x (later price - earlier price); unit effect = (later units - earlier units) x later price",
            "market effect (estimate) = change in total current value - change in invested amount",
        ],
        "nextActions": _next_actions(previous, current),
    }


def _insufficient(current: PortfolioSnapshot | None, baseline_date: str | None) -> dict[str, Any]:
    check = history_unavailable(current, baseline_date)
    return {
        "title": "Portfolio change investigation",
        "summary": check.summary + " There is not enough saved history to explain a change.",
        "comparison": {"previous": None, "current": _snapshot_ref(current) if current else None},
        "findings": [], "changes": None, "allocation": None,
        "checks": [check.to_dict()], "warnings": [check.summary],
        "limitations": ["No change is reported: a missing earlier snapshot is not the same as no change."],
        "calculations": [], "nextActions": [],
    }


# ── Persistence ────────────────────────────────────────────────────────────────

def _save(user_id: str, record: dict[str, Any], keys: ProfileKeys, *, insert: bool) -> None:
    sealed = keys.seal(json.dumps(record, ensure_ascii=False))
    with connect_db() as connection:
        if insert:
            connection.execute(
                "INSERT INTO investigations(id, user_id, kind, status, created_at, updated_at, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record["id"], user_id, KIND, record["status"], record["createdAt"], users.now_ms(), sealed),
            )
            connection.execute(
                "DELETE FROM investigations WHERE user_id = ? AND id NOT IN "
                "(SELECT id FROM investigations WHERE user_id = ? ORDER BY created_at DESC LIMIT ?)",
                (user_id, user_id, KEEP_INVESTIGATIONS),
            )
        else:
            connection.execute(
                "UPDATE investigations SET status = ?, updated_at = ?, data = ? WHERE user_id = ? AND id = ?",
                (record["status"], users.now_ms(), sealed, user_id, record["id"]),
            )


def _save_final_or_flag(user_id: str, record: dict[str, Any], keys: ProfileKeys) -> None:
    """Update a saved run; on a database error keep the result but mark it as not saved."""
    try:
        _save(user_id, record, keys, insert=False)
    except sqlite3.Error:
        logger.error("investigation_save_failed", extra={"user_id": user_id, "stage": record["status"]}, exc_info=True)
        record["saved"] = False
        record["saveError"] = "The result could not be saved, so it will not appear in your history."


def _from_row(row: Any, keys: ProfileKeys) -> dict[str, Any]:
    record = json.loads(keys.open(row["data"]))
    if row["status"] in ("running", "verifying") and users.now_ms() - row["updated_at"] > INTERRUPTED_AFTER_MS:
        record["status"] = "failed"
        record["error"] = "This investigation was interrupted before it finished. Run it again."
    return record


def get_investigation(user_id: str, investigation_id: str, keys: ProfileKeys) -> dict[str, Any] | None:
    """One saved investigation of this profile. Raises ``sealing.LockedError`` when locked."""
    with connect_db() as connection:
        row = connection.execute(
            "SELECT status, updated_at, data FROM investigations WHERE user_id = ? AND id = ?",
            (user_id, investigation_id),
        ).fetchone()
    return _from_row(row, keys) if row else None


def list_investigations(user_id: str, keys: ProfileKeys, *, limit: int = 20) -> list[dict[str, Any]]:
    """Summaries of saved investigations, newest first."""
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT status, updated_at, data FROM investigations WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    summaries = []
    for row in rows:
        record = _from_row(row, keys)
        result = record.get("result") or {}
        summaries.append({
            "id": record["id"], "kind": record["kind"], "status": record["status"], "createdAt": record["createdAt"],
            "completedAt": record.get("completedAt"), "summary": result.get("summary") or record.get("error"),
        })
    return summaries


def all_investigations(user_id: str, keys: ProfileKeys) -> list[dict[str, Any]]:
    """Every saved investigation in full, newest first (for export)."""
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT status, updated_at, data FROM investigations WHERE user_id = ? ORDER BY created_at DESC", (user_id,),
        ).fetchall()
    return [_from_row(row, keys) for row in rows]


def delete_investigation(user_id: str, investigation_id: str) -> bool:
    with connect_db() as connection:
        deleted = connection.execute(
            "DELETE FROM investigations WHERE user_id = ? AND id = ?", (user_id, investigation_id),
        ).rowcount
    return deleted > 0


# ── Workflow ───────────────────────────────────────────────────────────────────

def parse_baseline_date(value: Any) -> str | None:
    """Validate an optional ``YYYY-MM-DD`` baseline. Raises ``InvestigationError`` for anything else."""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise InvestigationError("baseline_date must be a YYYY-MM-DD string.")
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError as exc:
        raise InvestigationError("baseline_date must be a date in YYYY-MM-DD form.") from exc


async def _compare(
    recorder: _Recorder, user_id: str, keys: ProfileKeys, baseline_date: str | None,
    *, now: datetime, settings: CheckSettings, on_verifying: Callable[[], Awaitable[None]],
) -> tuple[Status, dict[str, Any]]:
    latest = await recorder.run("get_latest_snapshot", "Load the newest saved snapshot",
                                lambda: snapshots.latest_snapshot(user_id, keys=keys), retry_on_lock=True)
    if latest is None:
        _describe(recorder.last, "No saved snapshot.")
        return "insufficient_data", _insufficient(None, baseline_date)
    _describe(recorder.last, f"Snapshot of {latest['asOfDate']}.", [snapshot_id(latest["asOfDate"], latest["capturedAt"])])

    earlier = await recorder.run(
        "get_previous_snapshot", "Load the comparable earlier snapshot",
        lambda: snapshots.previous_snapshot(user_id, latest["asOfDate"], baseline_date, keys=keys), retry_on_lock=True,
    )
    if earlier is None:
        _describe(recorder.last, "No earlier snapshot.")
        return "insufficient_data", _insufficient(normalize_snapshot(latest), baseline_date)
    _describe(recorder.last, f"Snapshot of {earlier['asOfDate']}.", [snapshot_id(earlier["asOfDate"], earlier["capturedAt"])])

    previous, current = await recorder.run("normalize_snapshots", "Normalise totals, holdings and provenance",
                                           lambda: (normalize_snapshot(earlier), normalize_snapshot(latest)))
    pair = [previous.snapshot_id, current.snapshot_id]
    _describe(recorder.last, f"{len(previous.holdings)} and {len(current.holdings)} holdings normalised.", pair)

    change = await recorder.run("calculate_portfolio_change", "Calculate total, asset-type and holding changes",
                                lambda: calculate_portfolio_change(previous, current))
    _describe(recorder.last, f"Net worth {_change_phrase(change.net_worth)}.", pair)
    allocation = await recorder.run("calculate_allocation", "Calculate asset-type shares of the latest total",
                                    lambda: calculate_allocation(current))
    _describe(recorder.last, f"Shares of {inr(allocation.denominator_inr or 0)} ({allocation.basis}).", [current.snapshot_id])

    await on_verifying()
    checks = await recorder.run(
        "validate_snapshot_consistency", "Check scope, freshness, totals, denominators and currency",
        lambda: run_checks(previous, current, change, allocation, now=now, settings=settings),
    )
    flagged = [check.check for check in checks if check.status in ("warning", "failed")]
    recorder.last["status"] = "warning" if flagged else "completed"
    _describe(recorder.last, f"{len(checks) - len(flagged)} of {len(checks)} checks passed"
              + (f"; flagged: {', '.join(flagged)}." if flagged else "."), pair)
    return "completed", build_result(previous, current, change, allocation, checks)


async def investigate_portfolio_change(
    user: dict[str, Any],
    *,
    baseline_date: str | None = None,
    trigger: Trigger = "api",
    provider: str | None = None,
    now: datetime | None = None,
    settings: CheckSettings | None = None,
) -> dict[str, Any]:
    """Run one investigation for ``user`` (whose ``keys`` must be unlocked) and return its record.

    The record is persisted when the profile saves history. A deadline, timeout or
    unexpected error ends the run as ``failed`` with a safe message; cancellation is
    recorded and re-raised. Raises ``sealing.LockedError`` when the keys are locked.
    """
    keys: ProfileKeys = user["keys"]
    persist = user.get("storageMode") == "device"
    record: dict[str, Any] = {
        "id": users.random_token(), "kind": KIND, "status": "running", "createdAt": users.now_ms(),
        "request": {"baselineDate": baseline_date, "trigger": trigger},
        "metadata": {"engineVersion": ENGINE_VERSION, "provider": provider,
                     "checkSettings": (settings or settings_from_env()).describe()},
        "steps": [], "saved": persist,
    }
    recorder = _Recorder(deadline=time.monotonic() + DEADLINE_SECONDS)
    record["steps"] = recorder.steps
    if persist:
        try:
            await asyncio.to_thread(_save, user["id"], record, keys, insert=True)
        except sqlite3.Error as exc:
            logger.error("investigation_save_failed", extra={"user_id": user["id"], "stage": "start"}, exc_info=True)
            raise InvestigationStorageError("Could not save the investigation. Try again.") from exc

    async def mark_verifying() -> None:
        record["status"] = "verifying"
        if persist:
            await asyncio.to_thread(_save_final_or_flag, user["id"], record, keys)

    try:
        status, result = await _compare(
            recorder, user["id"], keys, baseline_date,
            now=now or datetime.now(timezone.utc), settings=settings or settings_from_env(), on_verifying=mark_verifying,
        )
        record.update(status=status, result=result)
    except asyncio.CancelledError:
        record.update(status="cancelled", error="The investigation was cancelled.")
        raise
    except LockedError:
        raise
    except Exception as exc:
        logger.warning("investigation_failed", extra={"user_id": user["id"], "investigation_id": record["id"]},
                       exc_info=not isinstance(exc, TimeoutError))
        record.update(status="failed", error=_safe_error(exc) + " No findings were produced.")
    finally:
        record["completedAt"] = users.now_ms()
        if persist:
            # Synchronous on purpose: this also runs while the task is being cancelled.
            _save_final_or_flag(user["id"], record, keys)
    return record


# ── Model tool ─────────────────────────────────────────────────────────────────

def _fit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Shrink the payload below ``MAX_AGENT_PAYLOAD_CHARS`` by dropping its least important items first."""
    def size() -> int:
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    for key in ("topRising", "topFalling", "limitations", "findings", "warnings"):
        while size() > MAX_AGENT_PAYLOAD_CHARS and len(payload[key]) > 1:
            payload[key].pop()
            payload["shortened"] = "Some items were left out to fit; the saved investigation has all of them."
    return payload


def agent_payload(record: dict[str, Any]) -> dict[str, Any]:
    """A compact view of a run for the model: claims with numbers, warnings and limitations, no raw data.

    Holding claims are left out of ``findings`` because ``topRising`` / ``topFalling``
    carry the same holdings with their structured figures.
    """
    result = record.get("result") or {}
    changes = result.get("changes") or {}
    keep = ("name", "assetType", "status", "changeInr", "changePct", "shareOfChangePct", "priceEffectInr",
            "quantityEffectInr")
    return _fit_payload({
        # Only a saved run can be opened from the trace.
        **({"investigationId": record["id"]} if record.get("saved") else {}),
        "status": record["status"],
        "error": record.get("error"),
        "summary": result.get("summary"),
        "comparison": result.get("comparison"),
        "findings": [
            {key: item[key] for key in ("claim", "kind", "verified", "previousInr", "currentInr", "changeInr",
                                        "changePct", "valueInr", "percentageDenominator") if item.get(key) is not None}
            for item in result.get("findings") or [] if not str(item.get("id", "")).startswith("holding:")
        ],
        "shareOfChangeDenominator": changes.get("shareOfChangeDenominator"),
        "topRising": [{k: v for k, v in item.items() if k in keep and v is not None} for item in changes.get("topRising", [])],
        "topFalling": [{k: v for k, v in item.items() if k in keep and v is not None} for item in changes.get("topFalling", [])],
        "warnings": result.get("warnings") or [],
        "limitations": result.get("limitations") or [],
    })


def investigation_tool(user: dict[str, Any], *, provider: str | None = None) -> LocalTool:
    """The ``investigate_portfolio_change`` tool for Portfolio Analyzer, bound to one signed-in profile.

    Built per chat request; repeated calls with the same baseline in that request (the
    model calling it twice, or the agent rerunning after a token refresh) reuse the
    first result instead of saving duplicate runs.
    """
    results: dict[str | None, str] = {}

    async def handler(arguments: dict[str, Any]) -> str:
        unknown = set(arguments) - {"baseline_date"}
        if unknown:
            return json.dumps({"error": f"Unknown argument(s): {', '.join(sorted(unknown))}. Only baseline_date is accepted."})
        try:
            baseline = parse_baseline_date(arguments.get("baseline_date"))
        except InvestigationError as exc:
            return json.dumps({"error": str(exc)})
        if baseline in results:
            return results[baseline]
        try:
            record = await investigate_portfolio_change(user, baseline_date=baseline, trigger="agent", provider=provider)
        except InvestigationStorageError as exc:
            return json.dumps({"error": str(exc)})
        results[baseline] = json.dumps(agent_payload(record), ensure_ascii=False, separators=(",", ":"))
        return results[baseline]

    return LocalTool(
        name="investigate_portfolio_change",
        description=(
            "Investigate why the user's portfolio or net worth changed by comparing their latest saved daily INDmoney "
            "snapshot with an earlier one. Code computes every figure and checks the data (account scope, freshness, "
            "totals, denominators, currency) and the run is saved. Call it for questions like 'why did my portfolio "
            "drop?', 'what changed since last week?' or 'what drove my net worth up since 2026-09-01?'. Pass "
            "baseline_date (YYYY-MM-DD) to compare with the snapshot on or before that day; omit it to compare with "
            "the previous saved day. Do not call it for the current value alone (use get_portfolio_snapshot) or for "
            "live prices. Amounts are INR. status 'insufficient_data' means there is no earlier snapshot, which is "
            "not the same as no change."
        ),
        input_schema={
            "type": "object",
            "properties": {"baseline_date": {
                "type": "string", "description": "Optional earlier date, YYYY-MM-DD, e.g. 2026-09-01."}},
            "additionalProperties": False,
        },
        handler=handler,
    )

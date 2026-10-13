"""Daily INDmoney portfolio snapshots: capture, storage, history, scheduling, agent tools.

Snapshots are plain SQLite rows rather than vector-store chunks: net worth and
holdings are exact numbers that need latest-row, date-range and ordering queries,
which similarity search cannot answer reliably.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
from datetime import date, datetime, time as dt_time, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from . import users
from .agent import LocalTool, PortfolioContext
from .db import connect_db
from .mcp_client import call_mcp_tool, fetch_mcp_session, filter_portfolio_tools

logger = logging.getLogger("ca_mode.snapshots")

SNAPSHOT_TOOL_NAMES = ("networth_snapshot", "networth_allocation_breakdown", "networth_holdings")


def _load_timezone(name: str) -> tzinfo:
    """Load an IANA zone; slim containers may lack tz data, and IST has no DST, so +05:30 is exact."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        if name in {"Asia/Kolkata", "Asia/Calcutta"}:
            return timezone(timedelta(hours=5, minutes=30), "IST")
        logger.warning("Unknown SNAPSHOT_TIMEZONE %r; using Asia/Kolkata", name)
        return _load_timezone("Asia/Kolkata")


SNAPSHOT_TIMEZONE_NAME = os.getenv("SNAPSHOT_TIMEZONE", "Asia/Kolkata")
SNAPSHOT_TIMEZONE = _load_timezone(SNAPSHOT_TIMEZONE_NAME)


def _snapshot_time(value: str) -> dt_time:
    try:
        hour, minute = (int(part) for part in value.split(":"))
        return dt_time(hour, minute)
    except ValueError:
        logger.warning("Invalid SNAPSHOT_TIME %r; using 09:00", value)
        return dt_time(9, 0)


SNAPSHOT_TIME = _snapshot_time(os.getenv("SNAPSHOT_TIME", "09:00"))
SNAPSHOT_RETENTION_DAYS = max(1, int(os.getenv("SNAPSHOT_RETENTION_DAYS", "365") or 365))
POLL_SECONDS = 300
RETRY_AFTER_FAILURE_MS = 60 * 60 * 1000
MCP_TOOLS_TTL_MS = 24 * 60 * 60 * 1000
MAX_TOOL_TEXT_CHARS = 20_000
MAX_HISTORY_POINTS = 31

_user_locks: dict[str, asyncio.Lock] = {}


class SnapshotError(RuntimeError):
    """A snapshot could not be captured; the message is safe to show the user."""


def local_now() -> datetime:
    return datetime.now(SNAPSHOT_TIMEZONE)


def latest_scheduled_time(now: datetime) -> datetime:
    """Return the most recent scheduled capture time at or before ``now``."""
    local = now.astimezone(SNAPSHOT_TIMEZONE)
    scheduled = datetime.combine(local.date(), SNAPSHOT_TIME, tzinfo=SNAPSHOT_TIMEZONE)
    return scheduled if scheduled <= local else scheduled - timedelta(days=1)


# ── Net-worth extraction (INDmoney's response format is undocumented) ──────────

_NETWORTH_KEY_RE = re.compile(r"net_?worth", re.IGNORECASE)
_TOTAL_KEY_RE = re.compile(r"^(?:total(?:_?(?:value|current_?value|amount|net_?worth))?|current_?value)$", re.IGNORECASE)
_NETWORTH_TEXT_RE = re.compile(
    r"net\s*worth[^0-9\n]{0,40}?(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)", re.IGNORECASE,
)


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[₹,\s]|^(?:rs\.?|inr)", "", value.strip(), flags=re.IGNORECASE)
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _networth_candidates(node: Any, inside_networth: bool, depth: int) -> list[tuple[int, float]]:
    """Score numeric fields: a net-worth key (3) beats a total inside a net-worth object (2) beats a bare total (1)."""
    if depth > 4:
        return []
    found: list[tuple[int, float]] = []
    items = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else []
    for key, value in items:
        key_text = str(key)
        is_networth = bool(_NETWORTH_KEY_RE.search(key_text))
        number = _as_number(value)
        if number is not None:
            if is_networth:
                found.append((3, number))
            elif _TOTAL_KEY_RE.match(key_text):
                found.append((2 if inside_networth else 1, number))
        elif isinstance(value, (dict, list)):
            found.extend(_networth_candidates(value, inside_networth or is_networth, depth + 1))
    return found


def extract_networth_inr(text: str | None) -> float | None:
    """Best-effort total net worth from a ``networth_snapshot`` result; None when unclear."""
    if not text:
        return None
    try:
        candidates = _networth_candidates(json.loads(text), False, 0)
    except (ValueError, TypeError):
        candidates = []
    if candidates:
        best = max(score for score, _ in candidates)
        return next(value for score, value in candidates if score == best)
    match = _NETWORTH_TEXT_RE.search(text)
    return _as_number(match.group(1)) if match else None


# ── Storage ────────────────────────────────────────────────────────────────────

def store_snapshot(user_id: str, captured: datetime, results: dict[str, str], errors: dict[str, str]) -> dict[str, Any]:
    """Upsert today's snapshot for ``user_id`` and prune rows past the retention window."""
    local = captured.astimezone(SNAPSHOT_TIMEZONE)
    as_of = local.date().isoformat()
    captured_ms = int(captured.timestamp() * 1000)
    networth = extract_networth_inr(results.get("networth_snapshot"))
    data = json.dumps({"results": results, "errors": errors}, ensure_ascii=False)
    cutoff = (local.date() - timedelta(days=SNAPSHOT_RETENTION_DAYS)).isoformat()
    with connect_db() as connection:
        connection.execute(
            "INSERT INTO portfolio_snapshots(user_id, as_of_date, captured_at, networth_inr, data) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, as_of_date) DO UPDATE SET captured_at=excluded.captured_at, "
            "networth_inr=excluded.networth_inr, data=excluded.data",
            (user_id, as_of, captured_ms, networth, data),
        )
        connection.execute("DELETE FROM portfolio_snapshots WHERE user_id = ? AND as_of_date < ?", (user_id, cutoff))
    return {"asOfDate": as_of, "capturedAt": captured_ms, "networthInr": networth, "errors": errors}


def _snapshot_from_row(row: Any) -> dict[str, Any]:
    data = json.loads(row["data"])
    return {
        "asOfDate": row["as_of_date"],
        "capturedAt": row["captured_at"],
        "networthInr": row["networth_inr"],
        "results": data.get("results", {}),
        "errors": data.get("errors", {}),
    }


def latest_snapshot(user_id: str, on_or_before: str | None = None) -> dict[str, Any] | None:
    with connect_db() as connection:
        row = connection.execute(
            "SELECT * FROM portfolio_snapshots WHERE user_id = ? AND as_of_date <= ? "
            "ORDER BY as_of_date DESC LIMIT 1",
            (user_id, on_or_before or "9999-12-31"),
        ).fetchone()
    return _snapshot_from_row(row) if row else None


def networth_history(user_id: str, days: int, *, today: date | None = None) -> list[dict[str, Any]]:
    """Return one point per stored day within ``days``, oldest first."""
    start = ((today or local_now().date()) - timedelta(days=days)).isoformat()
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT as_of_date, captured_at, networth_inr, data FROM portfolio_snapshots "
            "WHERE user_id = ? AND as_of_date >= ? ORDER BY as_of_date",
            (user_id, start),
        ).fetchall()
    return [
        {
            "date": row["as_of_date"],
            "capturedAt": row["captured_at"],
            "networthInr": row["networth_inr"],
            "_snapshot": json.loads(row["data"]).get("results", {}).get("networth_snapshot"),
        }
        for row in rows
    ]


def _sample(points: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Evenly thin ``points`` to at most ``limit`` items, always keeping the newest."""
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    return [points[round(index * step)] for index in range(limit)]


# ── Capture and scheduling ─────────────────────────────────────────────────────

async def _open_mcp_session(user_id: str, mcp_url: str) -> tuple[str, dict[str, Any]]:
    token = await users.user_access_token(user_id)
    if not token:
        raise SnapshotError("INDmoney is not connected, or its session expired. Reconnect INDmoney.")
    try:
        return token, await fetch_mcp_session(mcp_url, token)
    except RuntimeError as exc:
        if "401" not in str(exc):
            raise
    token = await users.user_access_token(user_id, force_refresh=True)
    if not token:
        raise SnapshotError("INDmoney rejected the saved session. Reconnect INDmoney.")
    return token, await fetch_mcp_session(mcp_url, token)


async def capture_snapshot(user_id: str, *, mcp_url: str, now: datetime | None = None) -> dict[str, Any]:
    """Fetch net worth, allocation and holdings from INDmoney and store them as today's snapshot."""
    token, session = await _open_mcp_session(user_id, mcp_url)
    tools = filter_portfolio_tools(session["tools"])
    users.cache_mcp_tools(user_id, tools)
    available = {tool["name"] for tool in tools}
    results: dict[str, str] = {}
    errors: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=90) as client:
        for name in SNAPSHOT_TOOL_NAMES:
            if name not in available:
                errors[name] = "INDmoney did not offer this tool."
                continue
            try:
                result = await call_mcp_tool(
                    mcp_url, token, session["protocolVersion"], session["sessionId"], name, {}, client=client,
                )
            except Exception as exc:  # One failing tool should not discard the others.
                errors[name] = str(exc)[:300]
                continue
            results[name] = str(result)
    if not results:
        raise SnapshotError("INDmoney returned no portfolio data: " + "; ".join(f"{k}: {v}" for k, v in errors.items()))
    return store_snapshot(user_id, now or local_now(), results, errors)


def due_user_ids(now: datetime) -> list[str]:
    """Users with INDmoney connected whose last capture predates the latest scheduled time."""
    scheduled_ms = int(latest_scheduled_time(now).timestamp() * 1000)
    retry_before_ms = int(now.timestamp() * 1000) - RETRY_AFTER_FAILURE_MS
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT u.id FROM users u LEFT JOIN ("
            "  SELECT user_id, MAX(captured_at) AS last FROM portfolio_snapshots GROUP BY user_id"
            ") s ON s.user_id = u.id "
            "WHERE (json_extract(u.indmoney, '$.refreshToken') IS NOT NULL "
            "       OR json_extract(u.indmoney, '$.accessToken') IS NOT NULL) "
            "AND COALESCE(s.last, 0) < ? "
            "AND (u.snapshot_attempted_at < ? OR u.snapshot_attempted_at < ?)",
            (scheduled_ms, scheduled_ms, retry_before_ms),
        ).fetchall()
    return [row["id"] for row in rows]


async def refresh_user(user_id: str, *, mcp_url: str, now: datetime | None = None) -> dict[str, Any]:
    """Capture a snapshot for one user, serialised per user. Raises on failure."""
    lock = _user_locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        with connect_db() as connection:
            connection.execute("UPDATE users SET snapshot_attempted_at = ? WHERE id = ?", (users.now_ms(), user_id))
        return await capture_snapshot(user_id, mcp_url=mcp_url, now=now)


async def refresh_due_users(*, mcp_url: str, now: datetime | None = None) -> int:
    """Refresh every due user; return how many succeeded."""
    current = now or local_now()
    succeeded = 0
    for user_id in due_user_ids(current):
        try:
            await refresh_user(user_id, mcp_url=mcp_url, now=current)
            succeeded += 1
        except Exception:
            logger.warning("snapshot_refresh_failed", extra={"user_id": user_id}, exc_info=True)
    return succeeded


async def run_scheduler(
    stop: asyncio.Event,
    *,
    mcp_url: str,
    poll_seconds: float = POLL_SECONDS,
    clock: Callable[[], datetime] = local_now,
) -> None:
    """Poll the wall clock so captures also catch up after startup or laptop sleep."""
    while not stop.is_set():
        try:
            await refresh_due_users(mcp_url=mcp_url, now=clock())
        except Exception:
            logger.exception("snapshot_scheduler_tick_failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
        except TimeoutError:
            pass


# ── Agent integration ──────────────────────────────────────────────────────────

def _iso(captured_ms: int) -> str:
    return datetime.fromtimestamp(captured_ms / 1000, SNAPSHOT_TIMEZONE).isoformat(timespec="minutes")


def _snapshot_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "available": True,
        "asOfDate": snapshot["asOfDate"],
        "capturedAt": _iso(snapshot["capturedAt"]),
        "networthInr": snapshot["networthInr"],
        "data": {name: text[:MAX_TOOL_TEXT_CHARS] for name, text in snapshot["results"].items()},
        "unavailable": snapshot["errors"],
    }


def snapshot_tools(user_id: str) -> list[LocalTool]:
    """Read-only tools over this user's stored snapshots."""

    async def get_portfolio_snapshot(arguments: dict[str, Any]) -> str:
        requested = arguments.get("date")
        if requested is not None:
            try:
                requested = date.fromisoformat(str(requested)).isoformat()
            except ValueError:
                return json.dumps({"error": "date must be YYYY-MM-DD"})
        snapshot = latest_snapshot(user_id, requested)
        if snapshot is None:
            return json.dumps({"available": False, "message": "No saved snapshot for that date. Use the live INDmoney tools instead."})
        return json.dumps(_snapshot_payload(snapshot), ensure_ascii=False)

    async def get_networth_history(arguments: dict[str, Any]) -> str:
        try:
            days = min(365, max(1, int(arguments.get("days", 30))))
        except (TypeError, ValueError):
            return json.dumps({"error": "days must be a whole number from 1 to 365"})
        points = _sample(networth_history(user_id, days), MAX_HISTORY_POINTS)
        parsed = any(point["networthInr"] is not None for point in points)
        history = []
        for point in points:
            item = {"date": point["date"], "capturedAt": _iso(point["capturedAt"]), "networthInr": point["networthInr"]}
            if not parsed and point["_snapshot"]:
                item["networthSnapshot"] = point["_snapshot"][:600]
            history.append(item)
        return json.dumps({"days": days, "points": history}, ensure_ascii=False)

    return [
        LocalTool(
            name="get_portfolio_snapshot",
            description=(
                "Read the user's saved daily INDmoney snapshot (net worth, allocation breakdown, holdings), captured each "
                "morning at 09:00 IST. Call this first for net worth, allocation, holdings or concentration questions, "
                "and say when it was captured. Pass `date` (YYYY-MM-DD) for the snapshot on or before that day. Do not use "
                "it for live prices, intraday moves, SIPs, watchlists or options; use the live INDmoney tools for those."
            ),
            input_schema={
                "type": "object",
                "properties": {"date": {"type": "string", "description": "Optional YYYY-MM-DD; defaults to the latest snapshot."}},
                "additionalProperties": False,
            },
            handler=get_portfolio_snapshot,
        ),
        LocalTool(
            name="get_networth_history",
            description=(
                "Read the user's daily net-worth history from saved snapshots, oldest first (at most 31 points, evenly "
                "sampled). Use for trends, growth over a period, or a net-worth line chart. Values are INR as captured "
                "each morning at 09:00 IST; a null networthInr means the total could not be read from that day's data."
            ),
            input_schema={
                "type": "object",
                "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 365, "description": "Look-back window in days (default 30)."}},
                "additionalProperties": False,
            },
            handler=get_networth_history,
        ),
    ]


_PROFILE_LABELS = {
    "riskAppetite": "Risk appetite",
    "investmentHorizonYears": "Investment horizon (years)",
    "primaryGoal": "Primary goal",
    "monthlyInvestmentInr": "Monthly investment budget (INR)",
    "ageBand": "Age band",
}


def profile_context(user: dict[str, Any], snapshot: dict[str, Any] | None) -> str | None:
    """Render user-entered profile fields and snapshot availability for the system prompt."""
    lines = [f"- {label}: {user['profile'][key]}" for key, label in _PROFILE_LABELS.items() if user["profile"].get(key) not in (None, "")]
    if snapshot:
        lines.append(f"- Latest saved portfolio snapshot: captured {_iso(snapshot['capturedAt'])} (use get_portfolio_snapshot)")
    else:
        lines.append("- No saved portfolio snapshot yet; use the live INDmoney tools.")
    return "\n".join(lines)


def fresh_cached_mcp_tools(user: dict[str, Any]) -> list[dict[str, Any]] | None:
    fetched_at = user.get("mcpToolsFetchedAt") or 0
    if user.get("mcpTools") and users.now_ms() - fetched_at < MCP_TOOLS_TTL_MS:
        return user["mcpTools"]
    return None


def portfolio_context(user: dict[str, Any]) -> PortfolioContext:
    """Assemble the snapshot tools, profile text and cached MCP tool list for one chat request."""
    return PortfolioContext(
        local_tools=tuple(snapshot_tools(user["id"])),
        profile_context=profile_context(user, latest_snapshot(user["id"])),
        cached_mcp_tools=fresh_cached_mcp_tools(user),
        on_mcp_tools=lambda tools: users.cache_mcp_tools(user["id"], filter_portfolio_tools(tools)),
    )

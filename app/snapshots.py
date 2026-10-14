"""Daily INDmoney portfolio snapshots: capture, storage, history, scheduling, agent tools.

Snapshots are plain SQLite rows rather than vector-store chunks: net worth and
holdings are exact numbers that need latest-row, date-range and ordering queries,
which similarity search cannot answer reliably.

Once a profile has keys, each row's ``data`` is sealed with its public key and
holds ``{"results", "errors", "networthInr"}``; ``networth_inr`` is then NULL so the
total is not readable without the PIN. Older plaintext rows keep the total in
``networth_inr`` until ``seal_plaintext_snapshots`` converts them.
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

from . import backup, cas, privacy, users
from .agent import LocalTool, PortfolioContext
from .db import connect_db
from .sealing import ProfileKeys
from .mcp_client import call_mcp_tool, fetch_mcp_session, filter_portfolio_tools
from .portfolio_math import HOLDINGS_PREFIX, asset_types_held, compute_metrics

logger = logging.getLogger("ca_mode.snapshots")

SNAPSHOT_TOOL_NAMES = ("networth_snapshot", "networth_holdings")
# networth_holdings needs an asset_type; INSURANCE and RD are not served by INDmoney's backend.
HOLDINGS_ASSET_TYPES = (
    "IND_STOCK", "MF", "US_STOCK", "BOND", "EPF", "NPS", "SA", "FD", "CRYPTO", "VEHICLE", "RE", "AIF", "PMS", "PPF",
)
MAX_PARALLEL_HOLDINGS_CALLS = 4


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

def _prune(connection: Any, user_id: str, today: date) -> None:
    """Delete snapshots older than the profile's retention choice, capped by ``SNAPSHOT_RETENTION_DAYS``."""
    row = connection.execute("SELECT snapshot_retention_days FROM users WHERE id = ?", (user_id,)).fetchone()
    days = min(row["snapshot_retention_days"], SNAPSHOT_RETENTION_DAYS) if row else SNAPSHOT_RETENTION_DAYS
    cutoff = (today - timedelta(days=days)).isoformat()
    connection.execute("DELETE FROM portfolio_snapshots WHERE user_id = ? AND as_of_date < ?", (user_id, cutoff))


def prune_snapshots(user_id: str, *, today: date | None = None) -> None:
    with connect_db() as connection:
        _prune(connection, user_id, today or local_now().date())


def store_snapshot(user_id: str, captured: datetime, results: dict[str, str], errors: dict[str, str]) -> dict[str, Any]:
    """Upsert today's snapshot for ``user_id`` and prune rows past the retention window.

    Raises ``SnapshotError`` if the profile is gone or no longer saves history.
    """
    local = captured.astimezone(SNAPSHOT_TIMEZONE)
    as_of = local.date().isoformat()
    captured_ms = int(captured.timestamp() * 1000)
    networth = extract_networth_inr(results.get("networth_snapshot"))
    with connect_db() as connection:
        # Re-checked here because the profile may switch to private mode while a capture is in flight.
        row = connection.execute(
            "SELECT storage_mode, history_public_key FROM users WHERE id = ?", (user_id,),
        ).fetchone()
        if row is None or row["storage_mode"] != "device":
            raise SnapshotError("Snapshots are off for this profile.")
        keys = ProfileKeys.from_stored_public_key(row["history_public_key"])
        data, stored_networth = _encode(keys, results, errors, networth)
        connection.execute(
            "INSERT INTO portfolio_snapshots(user_id, as_of_date, captured_at, networth_inr, data) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, as_of_date) DO UPDATE SET captured_at=excluded.captured_at, "
            "networth_inr=excluded.networth_inr, data=excluded.data",
            (user_id, as_of, captured_ms, stored_networth, data),
        )
        _prune(connection, user_id, local.date())
    return {"asOfDate": as_of, "capturedAt": captured_ms, "networthInr": networth, "errors": errors}


def _encode(
    keys: ProfileKeys, results: dict[str, str], errors: dict[str, str], networth: float | None,
) -> tuple[str, float | None]:
    """Return the stored ``data`` and ``networth_inr`` values: sealed (total inside) when the profile has keys."""
    if keys.public_key is None:
        return json.dumps({"results": results, "errors": errors}, ensure_ascii=False), networth
    payload = json.dumps({"results": results, "errors": errors, "networthInr": networth}, ensure_ascii=False)
    return keys.seal(payload), None


def _decode(row: Any, keys: ProfileKeys) -> tuple[dict[str, Any], float | None]:
    """Return a row's payload and net worth. Raises ``sealing.LockedError`` for a sealed row without the private key."""
    payload = json.loads(keys.open(row["data"]))
    return payload, payload["networthInr"] if "networthInr" in payload else row["networth_inr"]


def _snapshot_from_row(row: Any, keys: ProfileKeys) -> dict[str, Any]:
    payload, networth = _decode(row, keys)
    return {
        "asOfDate": row["as_of_date"],
        "capturedAt": row["captured_at"],
        "networthInr": networth,
        "results": payload.get("results", {}),
        "errors": payload.get("errors", {}),
    }


def latest_snapshot(
    user_id: str, on_or_before: str | None = None, *, keys: ProfileKeys | None = None,
) -> dict[str, Any] | None:
    with connect_db() as connection:
        row = connection.execute(
            "SELECT * FROM portfolio_snapshots WHERE user_id = ? AND as_of_date <= ? "
            "ORDER BY as_of_date DESC LIMIT 1",
            (user_id, on_or_before or "9999-12-31"),
        ).fetchone()
    return _snapshot_from_row(row, keys or ProfileKeys()) if row else None


def all_snapshots(user_id: str, keys: ProfileKeys) -> list[dict[str, Any]]:
    """Every stored snapshot, oldest first (for export)."""
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT * FROM portfolio_snapshots WHERE user_id = ? ORDER BY as_of_date", (user_id,),
        ).fetchall()
    return [_snapshot_from_row(row, keys) for row in rows]


def networth_history(
    user_id: str, days: int, *, keys: ProfileKeys | None = None, today: date | None = None,
) -> list[dict[str, Any]]:
    """Return one point per stored day within ``days``, oldest first."""
    start = ((today or local_now().date()) - timedelta(days=days)).isoformat()
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT as_of_date, captured_at, networth_inr, data FROM portfolio_snapshots "
            "WHERE user_id = ? AND as_of_date >= ? ORDER BY as_of_date",
            (user_id, start),
        ).fetchall()
    points = []
    for row in rows:
        payload, networth = _decode(row, keys or ProfileKeys())
        points.append({
            "date": row["as_of_date"],
            "capturedAt": row["captured_at"],
            "networthInr": networth,
            "_snapshot": payload.get("results", {}).get("networth_snapshot"),
        })
    return points


def seal_plaintext_snapshots(user_id: str, keys: ProfileKeys) -> int:
    """Seal snapshots stored before the profile had keys; return how many were sealed."""
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT as_of_date, networth_inr, data FROM portfolio_snapshots "
            "WHERE user_id = ? AND data NOT LIKE 'sealed:%'", (user_id,),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["data"])
            data, _ = _encode(keys, payload.get("results", {}), payload.get("errors", {}), row["networth_inr"])
            connection.execute(
                "UPDATE portfolio_snapshots SET data = ?, networth_inr = NULL WHERE user_id = ? AND as_of_date = ?",
                (data, user_id, row["as_of_date"]),
            )
    return len(rows)


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


def holdings_types_to_fetch(snapshot_text: str | None) -> list[str]:
    """Asset types to fetch holdings for: those the snapshot shows as held, else every served type."""
    held = [asset_type for asset_type in asset_types_held(snapshot_text) if asset_type in HOLDINGS_ASSET_TYPES]
    return held or list(HOLDINGS_ASSET_TYPES)


async def capture_snapshot(user_id: str, *, mcp_url: str, now: datetime | None = None) -> dict[str, Any]:
    """Fetch the net-worth summary and per-asset-type holdings from INDmoney and store them as today's snapshot.

    Results are keyed ``networth_snapshot`` and ``networth_holdings:<ASSET_TYPE>``.
    """
    token, session = await _open_mcp_session(user_id, mcp_url)
    tools = filter_portfolio_tools(session["tools"])
    users.cache_mcp_tools(user_id, tools)
    available = {tool["name"] for tool in tools}
    results: dict[str, str] = {}
    errors: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=90) as client:

        async def fetch(key: str, name: str, arguments: dict[str, Any]) -> None:
            if name not in available:
                errors[key] = "INDmoney did not offer this tool."
                return
            try:
                result = await call_mcp_tool(
                    mcp_url, token, session["protocolVersion"], session["sessionId"], name, arguments, client=client,
                )
            except Exception as exc:  # One failing call should not discard the others.
                errors[key] = str(exc)[:300]
                return
            results[key] = str(result)

        await fetch("networth_snapshot", "networth_snapshot", {})
        limit = asyncio.Semaphore(MAX_PARALLEL_HOLDINGS_CALLS)

        async def fetch_holdings(asset_type: str) -> None:
            async with limit:
                await fetch(HOLDINGS_PREFIX + asset_type, "networth_holdings", {"asset_type": asset_type})

        async with asyncio.TaskGroup() as group:
            for asset_type in holdings_types_to_fetch(results.get("networth_snapshot")):
                group.create_task(fetch_holdings(asset_type))
    if not results:
        raise SnapshotError("INDmoney returned no portfolio data: " + "; ".join(f"{k}: {v}" for k, v in errors.items()))
    return store_snapshot(user_id, now or local_now(), results, errors)


def snapshots_allowed(user: dict[str, Any]) -> bool:
    """Whether the profile accepted the current notice and chose to save history on this device."""
    return user["storageMode"] == "device" and privacy.has_current_consent(user["id"])


def due_user_ids(now: datetime) -> list[str]:
    """Consenting, saving profiles with INDmoney connected whose last capture predates the latest scheduled time."""
    scheduled_ms = int(latest_scheduled_time(now).timestamp() * 1000)
    retry_before_ms = int(now.timestamp() * 1000) - RETRY_AFTER_FAILURE_MS
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT u.id FROM users u LEFT JOIN ("
            "  SELECT user_id, MAX(captured_at) AS last FROM portfolio_snapshots GROUP BY user_id"
            ") s ON s.user_id = u.id "
            "WHERE u.indmoney <> '{}' "
            "AND u.storage_mode = 'device' "
            "AND (SELECT c.policy_version FROM consents c WHERE c.user_id = u.id ORDER BY c.id DESC LIMIT 1) = ? "
            "AND COALESCE(s.last, 0) < ? "
            "AND (u.snapshot_attempted_at < ? OR u.snapshot_attempted_at < ?)",
            (privacy.POLICY_VERSION, scheduled_ms, scheduled_ms, retry_before_ms),
        ).fetchall()
    # Tokens are encrypted, so whether they are present and readable is checked in Python.
    connected = set(users.users_with_indmoney())
    return [row["id"] for row in rows if row["id"] in connected]


async def refresh_user(user_id: str, *, mcp_url: str, now: datetime | None = None) -> dict[str, Any]:
    """Capture a snapshot for one user, serialised per user. Raises on failure.

    Raises ``SnapshotError`` without contacting INDmoney when the profile has not
    accepted the current notice or keeps no history (private mode).
    """
    lock = _user_locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        user = users.get_user(user_id)
        if user is None or not snapshots_allowed(user):
            raise SnapshotError("Snapshots are off for this profile. Choose to save history on this device to turn them on.")
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
    """Poll the wall clock so captures, and the daily backup, also catch up after startup or laptop sleep."""
    while not stop.is_set():
        try:
            await refresh_due_users(mcp_url=mcp_url, now=clock())
        except Exception:
            logger.exception("snapshot_scheduler_tick_failed")
        try:
            await asyncio.to_thread(backup.backup_if_due)
        except Exception:
            logger.exception("backup_failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
        except TimeoutError:
            pass


# ── Agent integration ──────────────────────────────────────────────────────────

def _iso(captured_ms: int) -> str:
    return datetime.fromtimestamp(captured_ms / 1000, SNAPSHOT_TIMEZONE).isoformat(timespec="minutes")


def _snapshot_payload(snapshot: dict[str, Any], *, include_all_holdings: bool = False) -> dict[str, Any]:
    """Computed figures for the model; raw INDmoney text is only included when nothing could be computed."""
    figures = compute_metrics(snapshot["results"], include_all_holdings=include_all_holdings)
    payload: dict[str, Any] = {
        "available": True,
        "asOfDate": snapshot["asOfDate"],
        "capturedAt": _iso(snapshot["capturedAt"]),
        "figures": figures,
        "unavailable": {name: error[:200] for name, error in snapshot["errors"].items()},
    }
    if figures["totals"]["net_worth_inr"] is None and not figures.get("top_holdings", figures.get("all_holdings")):
        # An older or unparseable snapshot: fall back to the stored text.
        payload["raw"] = {name: text[:MAX_TOOL_TEXT_CHARS] for name, text in snapshot["results"].items()}
    return payload


def snapshot_tools(user_id: str, keys: ProfileKeys) -> list[LocalTool]:
    """Read-only tools over this user's stored snapshots (``keys`` must be unlocked to read sealed rows)."""

    async def get_portfolio_snapshot(arguments: dict[str, Any]) -> str:
        requested = arguments.get("date")
        if requested is not None:
            try:
                requested = date.fromisoformat(str(requested)).isoformat()
            except ValueError:
                return json.dumps({"error": "date must be YYYY-MM-DD"})
        snapshot = latest_snapshot(user_id, requested, keys=keys)
        if snapshot is None:
            return json.dumps({"available": False, "message": "No saved snapshot for that date. Use the live INDmoney tools instead."})
        payload = _snapshot_payload(snapshot, include_all_holdings=arguments.get("include_all_holdings") is True)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    async def get_networth_history(arguments: dict[str, Any]) -> str:
        try:
            days = min(365, max(1, int(arguments.get("days", 30))))
        except (TypeError, ValueError):
            return json.dumps({"error": "days must be a whole number from 1 to 365"})
        points = _sample(networth_history(user_id, days, keys=keys), MAX_HISTORY_POINTS)
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
                "Read figures computed exactly from the user's saved daily INDmoney snapshot, captured each morning at "
                "09:00 IST: totals (net worth, current value, invested, liabilities), P&L, split by asset type, asset "
                "class, market cap and broker, concentration, top holdings, USD amounts, and data_notes. Call this first "
                "for net worth, P&L, allocation, holdings, broker or concentration questions, and say when it was "
                "captured. Pass include_all_holdings=true to list every holding (e.g. grouped by broker); otherwise "
                "only the 10 largest are listed. Pass `date` (YYYY-MM-DD) for the snapshot on or before that day. "
                "Do not use it for live prices, intraday moves, SIPs, watchlists or options; use the live INDmoney "
                "tools for those."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Optional YYYY-MM-DD; defaults to the latest snapshot."},
                    "include_all_holdings": {"type": "boolean", "description": "List every holding, not just the 10 largest."},
                },
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


def cas_tool(user_id: str, keys: ProfileKeys) -> LocalTool:
    """Read-only tool over the user's latest uploaded CAS (holdings at every broker, as of the statement date)."""

    async def get_cas_holdings(arguments: dict[str, Any]) -> str:
        statement = cas.latest_statement(user_id, keys)
        if statement is None:
            return json.dumps({"available": False, "message": "No CAS statement has been uploaded."})
        payload = {
            "available": True,
            "source": statement["source"],
            "statementPeriod": {"from": statement["period_from"], "to": statement["period_to"]},
            "uploadedAt": _iso(statement["uploaded_at"]),
            "figures": cas.statement_metrics(statement, include_all_holdings=arguments.get("include_all_holdings") is True),
            "parseWarnings": statement["warnings"],
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    return LocalTool(
        name="get_cas_holdings",
        description=(
            "Read figures computed from the user's latest uploaded CAS (Consolidated Account Statement from NSDL, "
            "CDSL, CAMS or KFintech): holdings at every broker and depository, mutual fund folios and NPS, with value, "
            "split by broker and asset type, P&L where the statement gives a cost, and concentration. Values are as of "
            "the statement's end date, not live; always say that date. Use it for holdings outside INDmoney, all-broker "
            "totals, or which broker holds what. Pass include_all_holdings=true to list every holding. Its figures "
            "overlap INDmoney's (the same shares can appear in both), so never add CAS amounts to INDmoney amounts."
        ),
        input_schema={
            "type": "object",
            "properties": {"include_all_holdings": {"type": "boolean", "description": "List every holding, not just the 10 largest."}},
            "additionalProperties": False,
        },
        handler=get_cas_holdings,
    )


_PROFILE_LABELS = {
    "riskAppetite": "Risk appetite",
    "investmentHorizonYears": "Investment horizon (years)",
    "primaryGoal": "Primary goal",
    "monthlyInvestmentInr": "Monthly investment budget (INR)",
    "ageBand": "Age band",
}


def profile_context(fields: dict[str, Any], snapshot: dict[str, Any] | None) -> str | None:
    """Render user-entered profile fields and snapshot availability for the system prompt."""
    lines = [f"- {label}: {fields[key]}" for key, label in _PROFILE_LABELS.items() if fields.get(key) not in (None, "")]
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
    """Assemble the snapshot tools, profile text and cached MCP tool list for one chat request.

    ``user["keys"]`` must be unlocked when the profile's data is sealed.
    """
    keys = user.get("keys") or ProfileKeys()
    local_tools = snapshot_tools(user["id"], keys)
    if cas.has_statement(user["id"]):
        local_tools.append(cas_tool(user["id"], keys))
    return PortfolioContext(
        local_tools=tuple(local_tools),
        profile_context=profile_context(users.profile_fields(user, keys), latest_snapshot(user["id"], keys=keys)),
        cached_mcp_tools=fresh_cached_mcp_tools(user),
        on_mcp_tools=lambda tools: users.cache_mcp_tools(user["id"], filter_portfolio_tools(tools)),
    )

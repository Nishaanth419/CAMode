"""Broker connections: read-only holdings from a broker's own MCP server (Zerodha first).

Brokers are data (``BROKERS``). A connection is the broker's MCP session id, stored encrypted per
profile; the broker keeps the login itself, so CA Mode never sees a password or an access token.
Only tools named in a broker's ``read_tools`` are ever called (an allowlist: Kite's tools carry no
read-only hints, so anything not named is refused).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

from . import cas, mcp_client, portfolio_math, snapshots, tool_results, users
from .agent import LocalTool
from .db import connect_db
from .sealing import LockedError, ProfileKeys

logger = logging.getLogger(__name__)

SESSION_MAX_MS: Final = 12 * 60 * 60 * 1000  # Kite's MCP sessions end 12 hours after they start.
LOGIN_WAIT_MS: Final = 5 * 60 * 1000  # a login link is valid for 5 minutes
LOGIN_EXPIRES_SECONDS: Final = 300
MAX_AGENT_HOLDINGS: Final = 60

WRITE_TOOLS: Final = frozenset({
    "place_order", "modify_order", "cancel_order", "place_gtt_order", "modify_gtt_order", "delete_gtt_order",
})

_URL_RE = re.compile(r"https://[^\s)\]>\"'<]+")
_ALREADY_RE = re.compile(r"already\s+logged\s+in", re.IGNORECASE)
_LOGIN_NEEDED_TERMS = ("log in", "login tool", "login first")


class BrokerError(Exception):
    """A broker problem whose message is safe to show the user."""


class BrokerDataError(BrokerError):
    """A broker returned holdings in a layout this app does not recognise."""


def _number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def normalise_kite(tool: str, items: list[Any]) -> list[dict[str, Any]]:
    """Kite ``get_holdings`` / ``get_mf_holdings`` items as ``networth_holdings`` rows (the CAS layout).

    A missing or non-numeric price leaves the value unknown (None), never zero.
    """
    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if tool == "get_holdings":
            units = (_number(item.get("quantity")) or Decimal(0)) + (_number(item.get("t1_quantity")) or Decimal(0))
            name, code, asset_type = item.get("tradingsymbol"), item.get("isin") or item.get("tradingsymbol"), "IND_STOCK"
        elif tool == "get_mf_holdings":
            units = _number(item.get("quantity")) or Decimal(0)
            name, code, asset_type = item.get("fund") or item.get("tradingsymbol"), item.get("tradingsymbol"), "MF"
        else:
            continue
        if units <= 0:
            continue
        average, last = _number(item.get("average_price")), _number(item.get("last_price"))
        rows.append(cas._row(
            name, code, asset_type, "Zerodha", units, last,
            None if last is None else units * last,  # type: ignore[arg-type] - cas._row keeps None as missing
            None if average is None else units * average,
        ))
    return rows


@dataclass(frozen=True, slots=True)
class Broker:
    id: str
    name: str
    mcp_url: str
    login_hosts: tuple[str, ...]
    read_tools: tuple[str, ...]
    holdings_tools: dict[str, str]
    indmoney_labels: tuple[str, ...]
    normalise: Callable[[str, list[Any]], list[dict[str, Any]]]
    auth: str = "mcp_login_tool"
    login_tool: str = "login"
    status_tool: str = "get_profile"
    reconnect_hint: str = field(default="")


def _zerodha() -> Broker:
    url = os.getenv("ZERODHA_MCP_URL") or "https://mcp.kite.trade/mcp"
    hosts = tuple(dict.fromkeys(filter(None, [urlsplit(url).hostname, "kite.zerodha.com"])))
    return Broker(
        id="zerodha", name="Zerodha", mcp_url=url, login_hosts=hosts,
        read_tools=(
            "get_profile", "get_margins", "get_holdings", "get_positions", "get_mf_holdings", "get_trades",
            "get_orders", "get_order_history", "get_order_trades", "get_gtts", "get_quotes", "get_ltp",
            "get_ohlc", "get_historical_data", "search_instruments",
        ),
        holdings_tools={"get_holdings": "IND_STOCK", "get_mf_holdings": "MF"},
        indmoney_labels=("zerodha",),
        normalise=normalise_kite,
        reconnect_hint="Tell the user to reconnect Zerodha in Profile > Brokers.",
    )


BROKERS: dict[str, Broker] = {"zerodha": _zerodha()}

assert all(not (WRITE_TOOLS & set(broker.read_tools)) for broker in BROKERS.values()), "a write tool is allowlisted"


def get_broker(broker_id: str) -> Broker | None:
    return BROKERS.get(broker_id)


# ── Login link ─────────────────────────────────────────────────────────────────

def login_url(text: str, broker: Broker) -> str | None:
    """The first https link in the login tool's text, only when it points at one of the broker's hosts."""
    for match in _URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;")
        parts = urlsplit(url)
        try:
            host = parts.hostname
        except ValueError:
            continue
        if parts.scheme == "https" and host in broker.login_hosts and not parts.username and not parts.password:
            return url
    return None


def reads_as_login_needed(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in _LOGIN_NEEDED_TERMS)


# ── Stored connections ─────────────────────────────────────────────────────────

def get_connection(user_id: str, broker_id: str) -> dict[str, Any] | None:
    with connect_db() as connection:
        row = connection.execute(
            "SELECT * FROM broker_connections WHERE user_id = ? AND broker = ?", (user_id, broker_id),
        ).fetchone()
    if row is None:
        return None
    session = users._decode_secret(row["session"])
    return {
        "broker": row["broker"], "status": row["status"], "sessionId": session.get("sessionId"),
        "protocolVersion": session.get("protocolVersion") or mcp_client.DEFAULT_PROTOCOL_VERSION,
        "sessionStartedAt": row["session_started_at"], "checkedAt": row["checked_at"],
        "holdingsAt": row["holdings_at"], "holdingsSealed": row["holdings"],
    }


def connections(user_id: str) -> list[dict[str, Any]]:
    with connect_db() as connection:
        ids = [row["broker"] for row in connection.execute(
            "SELECT broker FROM broker_connections WHERE user_id = ? ORDER BY broker", (user_id,),
        ).fetchall()]
    return [found for found in (get_connection(user_id, broker_id) for broker_id in ids) if found and found["broker"] in BROKERS]


def has_connection(user_id: str) -> bool:
    with connect_db() as connection:
        row = connection.execute(
            "SELECT 1 FROM broker_connections WHERE user_id = ? LIMIT 1", (user_id,),
        ).fetchone()
    return row is not None


def save_connection(
    user_id: str, broker_id: str, *, session_id: str | None, protocol: str | None, status: str,
    started_at: int | None = None,
) -> None:
    """Start (or restart) a connection; a new session drops the holdings of the old one's login."""
    now = users.now_ms()
    session = users._encode_secret({"sessionId": session_id, "protocolVersion": protocol} if session_id else {})
    with connect_db() as connection:
        connection.execute(
            "INSERT INTO broker_connections(user_id, broker, session, status, session_started_at, checked_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, broker) DO UPDATE SET "
            "session = excluded.session, status = excluded.status, "
            "session_started_at = excluded.session_started_at, checked_at = excluded.checked_at",
            (user_id, broker_id, session, status, started_at or now, now),
        )


def set_status(user_id: str, broker_id: str, status: str) -> None:
    with connect_db() as connection:
        connection.execute(
            "UPDATE broker_connections SET status = ?, checked_at = ? WHERE user_id = ? AND broker = ?",
            (status, users.now_ms(), user_id, broker_id),
        )


def delete_connection(user_id: str, broker_id: str) -> bool:
    with connect_db() as connection:
        return connection.execute(
            "DELETE FROM broker_connections WHERE user_id = ? AND broker = ?", (user_id, broker_id),
        ).rowcount > 0


# ── Connect, status ────────────────────────────────────────────────────────────

def _view(broker: Broker, status: str, connection: dict[str, Any] | None) -> dict[str, Any]:
    started = connection["sessionStartedAt"] if connection else None
    return {
        "broker": broker.id, "name": broker.name, "status": status,
        "sessionStartedAt": started, "expiresAt": None if started is None else started + SESSION_MAX_MS,
        "holdingsAt": connection["holdingsAt"] if connection else None,
    }


def brokers_overview(user_id: str) -> list[dict[str, Any]]:
    """Every broker in the registry with this profile's stored status (no call to the broker)."""
    stored = {found["broker"]: found for found in connections(user_id)}
    return [_view(broker, stored[broker.id]["status"] if broker.id in stored else "disconnected", stored.get(broker.id))
            for broker in BROKERS.values()]


async def start_login(user_id: str, broker: Broker) -> dict[str, Any]:
    """Open an MCP session, ask the broker's login tool for a link and store the session as pending.

    Raises ``BrokerError`` (safe message) when the broker gives no usable link; nothing is stored then.
    """
    session = await mcp_client.fetch_mcp_session(broker.mcp_url, None, service=broker.name)
    session_id, protocol = session.get("sessionId"), session.get("protocolVersion")
    if not session_id:
        raise BrokerError(f"{broker.name} did not start a session. Try again in a moment.")
    text = str(await mcp_client.call_mcp_tool(broker.mcp_url, None, protocol, session_id, broker.login_tool, {}))
    url = login_url(text, broker)
    if url:
        save_connection(user_id, broker.id, session_id=session_id, protocol=protocol, status="pending")
        return {**_view(broker, "pending", get_connection(user_id, broker.id)), "status": "pending",
                "loginUrl": url, "expiresInSeconds": LOGIN_EXPIRES_SECONDS}
    if _ALREADY_RE.search(text):
        save_connection(user_id, broker.id, session_id=session_id, protocol=protocol, status="connected")
        return _view(broker, "connected", get_connection(user_id, broker.id))
    raise BrokerError(f"{broker.name} did not return a login link CA Mode can trust, so nothing was connected.")


async def check_status(user_id: str, broker: Broker) -> dict[str, Any]:
    """Ask the broker whether the stored session is logged in. Never raises.

    ``unavailable`` means the broker could not be asked; the stored status is left as it was.
    """
    connection = get_connection(user_id, broker.id)
    if connection is None or not connection["sessionId"]:
        return _view(broker, "disconnected", None)
    now = users.now_ms()
    if now - connection["sessionStartedAt"] > SESSION_MAX_MS:
        set_status(user_id, broker.id, "needs_login")
        return _view(broker, "needs_login", get_connection(user_id, broker.id))
    status = connection["status"]
    try:
        await mcp_client.call_mcp_tool(
            broker.mcp_url, None, connection["protocolVersion"], connection["sessionId"], broker.status_tool, {},
        )
        status = "connected"
    except (mcp_client.McpToolError, mcp_client.McpAuthError) as exc:
        if reads_as_login_needed(str(exc)):
            waiting = connection["status"] == "pending" and now - connection["sessionStartedAt"] < LOGIN_WAIT_MS
            status = "pending" if waiting else "needs_login"
        else:
            logger.warning("broker_status_failed", extra={"broker": broker.id, "error": type(exc).__name__})
            return _view(broker, "unavailable", connection)
    except mcp_client.McpHttpError as exc:
        if exc.status_code in (400, 404, 410):
            status = "needs_login"
        else:
            logger.warning("broker_status_failed", extra={"broker": broker.id, "http_status": exc.status_code})
            return _view(broker, "unavailable", connection)
    except Exception as exc:  # timeouts and transport faults: the broker could not be asked
        logger.warning("broker_status_failed", extra={"broker": broker.id, "error": type(exc).__name__})
        return _view(broker, "unavailable", connection)
    set_status(user_id, broker.id, status)
    return _view(broker, status, get_connection(user_id, broker.id))


# ── Holdings ───────────────────────────────────────────────────────────────────

def parse_items(text: str) -> list[Any]:
    """A broker's holdings text as a list: a bare JSON array or ``{"data": [...]}``; anything else is refused."""
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise BrokerDataError("The broker returned holdings in a format CA Mode does not recognise.") from exc
    if isinstance(parsed, dict):
        parsed = parsed.get("data")
    if not isinstance(parsed, list):
        raise BrokerDataError("The broker returned holdings in a format CA Mode does not recognise.")
    return parsed


def rows_results(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Holdings rows in the stored-results form ``portfolio_math`` reads (one entry per asset type)."""
    by_type: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_type.setdefault(row["asset_type"], []).append(row)
    return {portfolio_math.HOLDINGS_PREFIX + asset_type: json.dumps({"holdings": found}) for asset_type, found in by_type.items()}


def rows_view(rows: list[dict[str, Any]], *, include_all_holdings: bool = False) -> dict[str, Any]:
    results = rows_results(rows)
    figures = portfolio_math.compute_metrics(results, include_all_holdings=include_all_holdings)
    # There is no networth_snapshot here, so the holdings' own value is the total (holdings only, no other assets).
    held = sum((Decimal(str(row["current_value_inr"] or 0)) for row in figures.get("by_broker") or []), Decimal(0))
    if figures.get("totals") and figures["totals"].get("current_value_inr") is None:
        figures["totals"]["current_value_inr"] = float(held.quantize(Decimal("0.01")))
    return {"figures": figures, "holdings": portfolio_math.all_holdings(results)}


def _store_rows(user_id: str, broker_id: str, rows: list[dict[str, Any]], keys: ProfileKeys) -> int:
    now = users.now_ms()
    with connect_db() as connection:
        connection.execute(
            "UPDATE broker_connections SET holdings = ?, holdings_at = ? WHERE user_id = ? AND broker = ?",
            (keys.seal(json.dumps({"rows": rows}, ensure_ascii=False)), now, user_id, broker_id),
        )
    return now


def stored_rows(user_id: str, broker_id: str, keys: ProfileKeys) -> tuple[list[dict[str, Any]], int] | None:
    """The last stored holdings and when they were fetched; None when none, or when still locked."""
    connection = get_connection(user_id, broker_id)
    if not connection or not connection["holdingsSealed"] or connection["holdingsAt"] is None:
        return None
    try:
        return json.loads(keys.open(connection["holdingsSealed"]))["rows"], connection["holdingsAt"]
    except (LockedError, ValueError, KeyError):
        return None


async def _fetch_rows(connection: dict[str, Any], broker: Broker) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=90) as client:
        texts = await asyncio.gather(*(
            mcp_client.call_mcp_tool(
                broker.mcp_url, None, connection["protocolVersion"], connection["sessionId"], tool, {}, client=client,
            )
            for tool in broker.holdings_tools
        ))
    rows: list[dict[str, Any]] = []
    for tool, text in zip(broker.holdings_tools, texts):
        rows.extend(broker.normalise(tool, parse_items(str(text))))
    return rows


def _holdings_payload(
    broker: Broker, status: str, rows: list[dict[str, Any]], as_of: int, *, stale: bool, include_all_holdings: bool = False,
) -> dict[str, Any]:
    return {
        "available": True, "broker": broker.id, "name": broker.name, "status": status, "stale": stale,
        "asOf": as_of, **rows_view(rows, include_all_holdings=include_all_holdings), "_rows": rows,
    }


async def fetch_holdings(
    user_id: str, keys: ProfileKeys, broker: Broker, *, live: bool = True, store: bool = True,
    include_all_holdings: bool = False,
) -> dict[str, Any]:
    """Holdings now when the session works (stored when ``store``), else the last stored copy marked stale.

    Never raises for a broker problem: with nothing stored the result is ``{"available": False, "status"}``.
    ``_rows`` carries the normalised rows for the combined view; routes drop keys that start with ``_``.
    """
    connection = get_connection(user_id, broker.id)
    if connection is None or not connection["sessionId"]:
        return {"available": False, "broker": broker.id, "name": broker.name, "status": "disconnected"}
    status = connection["status"]
    if not live and status == "connected" and connection["holdingsAt"] is None:
        live = True  # nothing stored yet, and the login works: fetch rather than show an empty tab
    if live and users.now_ms() - connection["sessionStartedAt"] > SESSION_MAX_MS:
        status, live = "needs_login", False
        set_status(user_id, broker.id, status)
    if live:
        try:
            rows = await _fetch_rows(connection, broker)
        except (mcp_client.McpToolError, mcp_client.McpAuthError) as exc:
            status = "needs_login" if reads_as_login_needed(str(exc)) else "unavailable"
        except mcp_client.McpHttpError as exc:
            status = "needs_login" if exc.status_code in (400, 404, 410) else "unavailable"
        except BrokerDataError:
            logger.warning("broker_holdings_unreadable", extra={"broker": broker.id})
            status = "unavailable"
        except Exception as exc:
            logger.warning("broker_holdings_failed", extra={"broker": broker.id, "error": type(exc).__name__})
            status = "unavailable"
        else:
            as_of = _store_rows(user_id, broker.id, rows, keys) if store else users.now_ms()
            if connection["status"] != "connected":
                set_status(user_id, broker.id, "connected")
            return _holdings_payload(broker, "connected", rows, as_of, stale=False, include_all_holdings=include_all_holdings)
        if status == "needs_login":
            set_status(user_id, broker.id, status)
    saved = stored_rows(user_id, broker.id, keys)
    if saved is None:
        return {"available": False, "broker": broker.id, "name": broker.name, "status": status}
    return _holdings_payload(broker, status, saved[0], saved[1], stale=True, include_all_holdings=include_all_holdings)


def public(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if not key.startswith("_")}


# ── Combined view ──────────────────────────────────────────────────────────────

def combined(
    snapshot: dict[str, Any] | None, broker_payloads: list[dict[str, Any]], *, include_all_holdings: bool = False,
) -> dict[str, Any]:
    """INDmoney's latest snapshot and each broker's holdings as sources, plus one deduplicated total.

    A broker with holdings of its own replaces INDmoney's rows labelled with that broker (INDmoney's
    ``investment_code`` format is undocumented, so matching single rows could miss and double count).
    Those rows then show only on the broker's own tab: the INDmoney source leaves them out and says
    how many moved (``movedTo``). A broker with nothing fetched yet takes nothing away from INDmoney.
    """
    sources: list[dict[str, Any]] = []
    broker_sources: list[dict[str, Any]] = []
    direct: list[tuple[Broker, list[dict[str, Any]]]] = []
    for payload in broker_payloads:
        broker = BROKERS.get(payload.get("broker", ""))
        if broker is None:
            continue
        rows = payload.get("_rows") or []
        broker_sources.append({
            "id": broker.id, "name": broker.name, "kind": "broker", "status": payload.get("status"),
            "asOf": payload.get("asOf"), "stale": bool(payload.get("stale")), "available": bool(payload.get("available")),
            "figures": payload.get("figures"), "holdings": payload.get("holdings", []),
        })
        if payload.get("available") and rows:
            direct.append((broker, rows))
    indmoney_rows: list[dict[str, Any]] = []
    deduplicated: dict[str, int] = {}
    if snapshot:
        rows, _ = portfolio_math._holding_rows(snapshot["results"])
        for broker, _ in direct:
            labels = tuple(label.lower() for label in broker.indmoney_labels)
            kept = [row for row in rows if not any(label in str(row.get("broker") or "").lower() for label in labels)]
            if len(kept) != len(rows):
                deduplicated[broker.name] = deduplicated.get(broker.name, 0) + len(rows) - len(kept)
            rows = kept
        indmoney_rows = rows
        view = rows_view(rows) if rows else {"figures": None, "holdings": []}
        sources.append({
            "id": "indmoney", "name": "INDmoney", "kind": "indmoney", "status": "connected", "asOf": snapshot["capturedAt"],
            "stale": False, "available": True, **view, "movedTo": dict(deduplicated),
        })
    sources.extend(broker_sources)
    merged = list(indmoney_rows)
    for _, rows in direct:
        merged += rows
    if not merged:
        return {"sources": sources, "combined": {"available": False, "deduplicated": deduplicated}}
    return {"sources": sources, "combined": {
        "available": True, **rows_view(merged, include_all_holdings=include_all_holdings), "deduplicated": deduplicated}}


def export_brokers(user_id: str, keys: ProfileKeys) -> list[dict[str, Any]]:
    """Each connection with its stored holdings, for the profile export. Never the session id."""
    result = []
    for found in connections(user_id):
        saved = stored_rows(user_id, found["broker"], keys)
        result.append({
            "broker": found["broker"], "status": found["status"], "connectedAt": found["sessionStartedAt"],
            "holdingsAt": found["holdingsAt"], "holdings": saved[0] if saved else [],
        })
    return result


# ── Agent tools ────────────────────────────────────────────────────────────────

def _iso(millis: int | None) -> str | None:
    return None if millis is None else datetime.fromtimestamp(millis / 1000, timezone.utc).isoformat(timespec="minutes")


def context_line(user_id: str) -> str | None:
    """One profile-prompt line naming the connected brokers (None when there are none)."""
    found = connections(user_id)
    if not found:
        return None
    parts = []
    for item in found:
        when = _iso(item["holdingsAt"])
        parts.append(f"{BROKERS[item['broker']].name} ({'holdings as of ' + when.replace('T', ' ') + ' UTC' if when else 'no holdings fetched yet'})")
    return (
        f"- Connected brokers: {', '.join(parts)}; use get_broker_holdings. INDmoney may also list the same "
        "broker account's holdings: never add the two together."
    )


def _tool_error(broker: Broker, exc: Exception, user_id: str) -> str:
    """A broker failure as a tool result the model can act on; never raises (a broker is not INDmoney auth)."""
    if isinstance(exc, (mcp_client.McpToolError, mcp_client.McpAuthError)):
        if reads_as_login_needed(str(exc)):
            set_status(user_id, broker.id, "needs_login")
            return tool_results.error_payload("unavailable", f"{broker.name} login has ended.", broker.reconnect_hint)
        return tool_results.error_payload("invalid_arguments", str(exc))
    if isinstance(exc, mcp_client.McpHttpError) and exc.status_code in (400, 404, 410):
        set_status(user_id, broker.id, "needs_login")
        return tool_results.error_payload("unavailable", f"{broker.name} login has ended.", broker.reconnect_hint)
    return tool_results.error_payload("unavailable", f"{broker.name} could not be reached right now.")


def broker_tools(user: dict[str, Any]) -> list[LocalTool]:
    """``get_broker_holdings`` and ``call_broker_tool`` when the profile has a broker connection, else none."""
    user_id, keys = user["id"], user.get("keys") or ProfileKeys()
    connected = [item["broker"] for item in connections(user_id)]
    if not connected:
        return []
    names = ", ".join(BROKERS[broker_id].name for broker_id in connected)
    read_tools = sorted({tool for broker_id in connected for tool in BROKERS[broker_id].read_tools})

    async def get_broker_holdings(arguments: dict[str, Any]) -> str:
        choice = arguments.get("broker") or "all"
        compact = arguments.get("include_all_holdings") is True
        if choice != "all" and choice not in connected:
            return json.dumps({"error": f"broker must be one of {', '.join([*connected, 'all'])}"})
        store = snapshots.snapshots_allowed(user)
        targets = connected if choice == "all" else [choice]
        try:
            payloads = list(await asyncio.gather(*(
                fetch_holdings(user_id, keys, BROKERS[broker_id], store=store, include_all_holdings=compact)
                for broker_id in targets
            )))
            if choice == "all":
                snapshot = snapshots.latest_snapshot(user_id, keys=keys)
                merged = combined(snapshot, payloads, include_all_holdings=compact)
                return json.dumps({
                    "broker": "all", "figures": (merged["combined"].get("figures")),
                    "deduplicated": merged["combined"]["deduplicated"],
                    "sources": [{"id": item["id"], "status": item["status"], "asOf": _iso(item["asOf"]), "stale": item["stale"]}
                                for item in merged["sources"]],
                    "note": "Holdings only (INDmoney's net worth outside holdings is not included). A broker's own rows "
                            "replace INDmoney's rows for the same broker, so nothing is counted twice.",
                }, ensure_ascii=False, separators=(",", ":"))
            payload = payloads[0]
            if not payload.get("available"):
                return json.dumps({
                    "broker": choice, "available": False, "status": payload.get("status"),
                    "next_step": BROKERS[choice].reconnect_hint,
                })
            return json.dumps({
                "broker": choice, "status": payload["status"], "stale": payload["stale"], "asOf": _iso(payload["asOf"]),
                "figures": payload["figures"],
                "note": "Stale: the Zerodha login has ended, so these are the last holdings fetched; say when they are from."
                        if payload["stale"] else "Live from the broker just now.",
            }, ensure_ascii=False, separators=(",", ":"))
        except Exception as exc:  # holdings failures must never end the answer
            logger.warning("broker_tool_failed", extra={"error": type(exc).__name__})
            return tool_results.error_payload("unavailable", "Broker holdings could not be read right now.")

    async def call_broker_tool(arguments: dict[str, Any]) -> str:
        broker = BROKERS.get(str(arguments.get("broker")))
        tool = arguments.get("tool")
        if broker is None or broker.id not in connected:
            return json.dumps({"error": f"broker must be one of {', '.join(connected)}"})
        if tool not in broker.read_tools:
            return json.dumps({"error": f"{str(tool)[:60]} is not an allowed read-only {broker.name} tool. "
                                        f"Allowed: {', '.join(broker.read_tools)}"})
        extra = arguments.get("arguments")
        connection = get_connection(user_id, broker.id)
        if connection is None or not connection["sessionId"]:
            return tool_results.error_payload("unavailable", f"{broker.name} is not connected.", broker.reconnect_hint)
        try:
            result = await mcp_client.call_mcp_tool(
                broker.mcp_url, None, connection["protocolVersion"], connection["sessionId"], tool,
                extra if isinstance(extra, dict) else {},
            )
        except Exception as exc:
            return _tool_error(broker, exc, user_id)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)

    return [
        LocalTool(
            name="get_broker_holdings",
            description=(
                f"Holdings and portfolio figures fetched directly from a connected broker ({names}); 'all' merges them "
                "with INDmoney's latest snapshot with each broker's own rows replacing INDmoney's rows for that broker. "
                "INDmoney may also list the same broker account, so never add INDmoney and broker figures together. "
                "Quote the figures; state the asOf time and say so when stale."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "broker": {"type": "string", "enum": [*connected, "all"]},
                    "include_all_holdings": {"type": "boolean", "description": "List every holding in compact form."},
                },
                "required": ["broker"],
            },
            handler=get_broker_holdings,
        ),
        LocalTool(
            name="call_broker_tool",
            description=(
                f"Call one read-only tool on a connected broker's own server ({names}): profile, margins, positions, "
                "orders, trades, quotes, historical prices and more. Cannot place or change orders. Results are "
                "that broker's own data; never add them to INDmoney's figures."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "broker": {"type": "string", "enum": connected},
                    "tool": {"type": "string", "enum": read_tools},
                    "arguments": {"type": "object", "description": "The tool's own arguments, if it takes any."},
                },
                "required": ["broker", "tool"],
            },
            handler=call_broker_tool,
        ),
    ]

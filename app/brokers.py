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

from . import broker_history, broker_oauth, cas, logos, mcp_client, plans, portfolio_math, snapshots, tool_results, users
from .agent import LocalTool
from .db import connect_db
from .sealing import LockedError, ProfileKeys

logger = logging.getLogger(__name__)

SESSION_MAX_MS: Final = 12 * 60 * 60 * 1000  # Kite's MCP sessions end 12 hours after they start.
LOGIN_WAIT_MS: Final = 5 * 60 * 1000  # a login link is valid for 5 minutes
LOGIN_EXPIRES_SECONDS: Final = 300
MAX_AGENT_HOLDINGS: Final = 60
HOLDINGS_STALE_MS: Final = 24 * 60 * 60 * 1000  # a connected broker whose holdings are older than this needs a look

WRITE_TOOLS: Final = frozenset({
    "place_order", "modify_order", "cancel_order", "place_gtt_order", "modify_gtt_order", "delete_gtt_order",
})

# Words that change an order or an account; a tool named with one is never read-only.
_WRITE_WORDS: Final = frozenset({
    "place", "modify", "cancel", "delete", "exit", "convert", "create", "add", "remove", "update", "set",
    "transfer", "withdraw", "buy", "sell",
})
_AUTH_WORDS: Final = frozenset({"login", "logout", "validate"})
_READ_PREFIXES: Final = ("get_", "search_", "list_", "fetch_")

_URL_RE = re.compile(r"https://[^\s)\]>\"'<]+")
_ALREADY_RE = re.compile(r"already\s+logged\s+in", re.IGNORECASE)
_LOGIN_NEEDED_TERMS = ("log in", "login tool", "login first")


class BrokerError(Exception):
    """A broker problem whose message is safe to show the user."""


class BrokerLimitReached(BrokerError):
    """The profile's plan connects fewer brokers than it already has (``limit`` of them, ``connected`` now)."""

    def __init__(self, limit: int, connected: int) -> None:
        super().__init__(LIMIT_MESSAGE)
        self.limit = limit
        self.connected = connected


class BrokerDataError(BrokerError):
    """A broker returned holdings in a layout this app does not recognise."""


INDMONEY_ID: Final = "indmoney"
LIMIT_MESSAGE: Final = "The Free plan connects up to 2 brokers. Disconnect one or see the plans."


def logo_path(broker_id: str) -> str:
    """Where the app serves a broker's icon (``frontend/brokers/<id>.png``)."""
    return f"/brokers/{broker_id}.png"


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


def _name_words(name: str) -> set[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()
    words = set(re.findall(r"[a-z0-9]+", spaced))
    return words | {word[:-1] for word in words if word.endswith("s") and len(word) > 3}


def is_write_name(name: str) -> bool:
    """A tool name that reads as changing an order or an account (login and logout are listed apart)."""
    lowered = name.lower()
    return bool(_name_words(name) & _WRITE_WORDS) or "order_place" in lowered


def dynamic_read(tool: dict[str, Any]) -> bool:
    """Whether a tool from a live ``tools/list`` is read-only: its own hint, or a read-style name. In doubt, no."""
    name = tool.get("name")
    if not isinstance(name, str) or not name:
        return False
    if is_write_name(name) or _name_words(name) & _AUTH_WORDS:
        return False
    annotations = tool.get("annotations")
    if isinstance(annotations, dict) and annotations.get("readOnlyHint") is True:
        return True
    return name.lower().startswith(_READ_PREFIXES)


_NAME_KEYS: Final = ("tradingsymbol", "trading_symbol", "symbol", "name", "scheme_name", "fund")
_CODE_KEYS: Final = ("isin", "tradingsymbol", "symbol", "security_id", "instrument_token")
_QUANTITY_KEYS: Final = ("quantity", "qty", "holding_qty", "total_qty", "units", "available_qty")
_AVERAGE_KEYS: Final = ("average_price", "avg_price", "costPrice", "average_cost")
_LAST_KEYS: Final = ("last_price", "ltp", "lastTradedPrice", "close_price")
_VALUE_KEYS: Final = ("market_value", "current_value", "marketVal")


def _first(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    return next((item[key] for key in keys if item.get(key) not in (None, "")), None)


def _first_number(item: dict[str, Any], keys: tuple[str, ...]) -> Decimal | None:
    for key in keys:
        found = _number(item.get(key))
        if found is not None:
            return found
    return None


def normalise_generic(broker_name: str) -> Callable[[str, list[Any]], list[dict[str, Any]]]:
    """A normaliser for a broker whose holdings layout is not recorded: common field names, missing stays None.

    A non-empty list in which no row has a readable quantity raises ``BrokerDataError`` instead of
    showing a total of zero.
    """
    def normalise(tool: str, items: list[Any]) -> list[dict[str, Any]]:
        asset_type = "MF" if re.search(r"(^|_)mf(_|$)|mutual", tool.lower()) else "IND_STOCK"
        rows: list[dict[str, Any]] = []
        readable = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            units = _first_number(item, _QUANTITY_KEYS)
            if units is None:
                continue
            readable += 1
            if units <= 0:
                continue
            name, code = _first(item, _NAME_KEYS), _first(item, _CODE_KEYS)
            average, last = _first_number(item, _AVERAGE_KEYS), _first_number(item, _LAST_KEYS)
            value = _first_number(item, _VALUE_KEYS)
            if value is None and last is not None:
                value = units * last
            rows.append(cas._row(
                None if name is None else str(name), None if code is None else str(code), asset_type, broker_name,
                units, last, value,  # type: ignore[arg-type] - cas._row keeps None as missing
                None if average is None else units * average,
            ))
        if items and not readable:
            raise BrokerDataError(f"{broker_name}'s holdings layout is not recognised yet.")
        return rows

    return normalise


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
    login_suffixes: tuple[str, ...] = ()  # domains whose subdomains may host the login page (dot boundary)
    session_arg: str = ""  # a tool argument that carries the broker's own login session (Kotak: sessionid)


def _url(broker_id: str, default: str) -> str:
    return os.getenv(f"{broker_id.upper()}_MCP_URL") or default


def _zerodha() -> Broker:
    url = _url("zerodha", "https://mcp.kite.trade/mcp")
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


def _fyers() -> Broker:
    url = _url("fyers", "https://mcp.fyers.in/mcp")
    return Broker(
        id="fyers", name="FYERS", mcp_url=url, login_hosts=tuple(filter(None, [urlsplit(url).hostname])),
        login_suffixes=("fyers.in",), status_tool="get_funds",
        read_tools=(
            "get_charges_history", "get_chart_data", "get_expired_fno_contracts", "get_expired_fno_data",
            "get_expired_fno_expiry_dates", "get_funds", "get_gtt_orders", "get_holdings", "get_indus_history",
            "get_ledger_history", "get_market_depth", "get_mutual_fund_holdings", "get_option_chain",
            "get_order_history", "get_orders", "get_positions", "get_price_alerts", "get_quotes", "get_realised_pnl",
            "get_scheduled_orders", "get_tax_pnl", "get_trade_book", "get_trade_history", "get_watchlists",
            "get_watchlists_by_id", "calculate_span_margin",
        ),
        holdings_tools={"get_holdings": "IND_STOCK", "get_mutual_fund_holdings": "MF"},
        indmoney_labels=("fyers",), normalise=normalise_generic("FYERS"),
        reconnect_hint="Tell the user to reconnect FYERS in Profile > Brokers.",
    )


def _kotak() -> Broker:
    url = _url("kotak", "https://neo.kotaksecurities.com/mcp-server/mcp")
    return Broker(
        id="kotak", name="Kotak Neo", mcp_url=url, login_hosts=tuple(filter(None, [urlsplit(url).hostname])),
        login_suffixes=("kotaksecurities.com",), auth="mcp_login_qr", login_tool="get_login", status_tool="get_limits",
        session_arg="sessionid",
        read_tools=(
            "get_quote", "get_margin", "get_order_book", "get_trade_book", "get_positions", "get_limits",
            "search_instrument", "get_order_history", "get_research", "get_holdings",
        ),
        holdings_tools={"get_holdings": "IND_STOCK"},
        indmoney_labels=("kotak",), normalise=normalise_generic("Kotak Neo"),
        reconnect_hint="Tell the user to reconnect Kotak Neo in Profile > Brokers.",
    )


def _oauth_broker(broker_id: str, name: str, default_url: str) -> Broker:
    return Broker(
        id=broker_id, name=name, mcp_url=_url(broker_id, default_url), login_hosts=(), read_tools=(),
        holdings_tools={}, indmoney_labels=(broker_id,), normalise=normalise_generic(name), auth="oauth",
        status_tool="", reconnect_hint=f"Tell the user to reconnect {name} in Profile > Brokers.",
    )


BROKERS: dict[str, Broker] = {broker.id: broker for broker in (
    _zerodha(), _fyers(), _kotak(),
    _oauth_broker("upstox", "Upstox", "https://mcp.upstox.com/mcp"),
    _oauth_broker("dhan", "Dhan", "https://mcp.dhan.co/mcp"),
    _oauth_broker("groww", "Groww", "https://mcp.groww.in/mcp/"),
)}

assert all(not (WRITE_TOOLS & set(broker.read_tools)) for broker in BROKERS.values()), "a write tool is allowlisted"
assert all(
    not is_write_name(tool) and not (_name_words(tool) & _AUTH_WORDS)
    for broker in BROKERS.values() for tool in broker.read_tools
), "a write-looking tool is allowlisted"


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
        if parts.scheme != "https" or not host or parts.username or parts.password:
            continue
        host = host.lower()
        if host in broker.login_hosts or any(host == suffix or host.endswith("." + suffix) for suffix in broker.login_suffixes):
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
        "loginSession": session.get("loginSession"), "oauth": session.get("oauth"),
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
    started_at: int | None = None, extra: dict[str, Any] | None = None,
) -> None:
    """Start (or restart) a connection; a new session drops the holdings of the old one's login.

    ``extra`` joins the encrypted session (Kotak's login session, a broker's OAuth tokens). Raises
    ``BrokerLimitReached`` when this would be a new connection beyond the plan's broker limit.
    """
    now = users.now_ms()
    values: dict[str, Any] = {"sessionId": session_id, "protocolVersion": protocol} if session_id else {}
    values.update(extra or {})
    session = users._encode_secret(values)
    with connect_db() as connection:
        connection.execute("BEGIN IMMEDIATE")  # the count and the insert cannot interleave with another connect
        check_limit(connection, user_id, broker_id)
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


def has_credentials(connection: dict[str, Any] | None, broker: Broker) -> bool:
    """Whether a stored connection holds anything to call the broker with."""
    if connection is None:
        return False
    if broker.auth == "oauth":
        return bool((connection.get("oauth") or {}).get("accessToken"))
    return bool(connection["sessionId"] or (broker.session_arg and connection.get("loginSession")))


def delete_connection(user_id: str, broker_id: str) -> bool:
    _tools_cache.pop((user_id, broker_id), None)
    broker_history.delete_for_broker(user_id, broker_id)
    with connect_db() as connection:
        return connection.execute(
            "DELETE FROM broker_connections WHERE user_id = ? AND broker = ?", (user_id, broker_id),
        ).rowcount > 0


# ── Broker limit (Free: 2) ─────────────────────────────────────────────────────

def _indmoney_connected(connection: Any, user_id: str) -> bool:
    row = connection.execute("SELECT indmoney FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row) and users.has_indmoney_tokens(users._decode_secret(row["indmoney"]))


def _limit_state(connection: Any, user_id: str, broker_id: str) -> tuple[int | None, int, bool]:
    """``(limit, connected, already)``: the plan's limit, brokers counted now (INDmoney, then every stored
    connection of a registry broker in any status, so a pending login cannot slip past), and whether
    ``broker_id`` is one of them."""
    row = connection.execute("SELECT plan FROM users WHERE id = ?", (user_id,)).fetchone()
    limit = plans.PLANS.get((row["plan"] if row else None) or plans.DEFAULT_PLAN, plans.PLANS[plans.DEFAULT_PLAN]).max_brokers
    held = {r["broker"] for r in connection.execute(
        "SELECT broker FROM broker_connections WHERE user_id = ?", (user_id,)).fetchall() if r["broker"] in BROKERS}
    if _indmoney_connected(connection, user_id):
        held.add(INDMONEY_ID)
    return limit, len(held), broker_id in held


def check_limit(connection: Any, user_id: str, broker_id: str) -> None:
    """Raise ``BrokerLimitReached`` for a new connection at the limit; reconnecting an existing one is allowed.

    A profile above the limit (moved to Free) keeps what it has and cannot add more.
    """
    limit, count, already = _limit_state(connection, user_id, broker_id)
    if limit is not None and not already and count >= limit:
        raise BrokerLimitReached(limit, count)


def connected_count(user_id: str) -> int:
    with connect_db() as connection:
        return _limit_state(connection, user_id, "")[1]


def broker_limit(user_id: str) -> int | None:
    with connect_db() as connection:
        return _limit_state(connection, user_id, "")[0]


def can_connect(user: dict[str, Any], broker_id: str) -> bool:
    """True when the profile already has this broker, its plan has no limit, or it is under the limit."""
    with connect_db() as connection:
        limit, count, already = _limit_state(connection, user["id"], broker_id)
    return already or limit is None or count < limit


def limit_error(user_id: str) -> dict[str, Any]:
    """The JSON body of a refused connection."""
    with connect_db() as connection:
        limit, count, _ = _limit_state(connection, user_id, "")
    return {"error": LIMIT_MESSAGE, "brokerLimitReached": True, "limit": limit, "connected": count, "pricingUrl": "/pricing"}


def save_indmoney_connection(user_id: str, values: dict[str, Any]) -> None:
    """Store INDmoney's tokens unless that would be a new connection beyond the plan's limit (atomic)."""
    with connect_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        check_limit(connection, user_id, INDMONEY_ID)
        connection.execute(
            "UPDATE users SET indmoney = ?, updated_at = ? WHERE id = ?",
            (users._encode_secret(values), users.now_ms(), user_id),
        )


# ── Connect, status ────────────────────────────────────────────────────────────

def _view(broker: Broker, status: str, connection: dict[str, Any] | None) -> dict[str, Any]:
    started = connection["sessionStartedAt"] if connection else None
    holdings_at = connection["holdingsAt"] if connection else None
    return {
        "broker": broker.id, "name": broker.name, "status": status, "auth": broker.auth,
        "logo": logo_path(broker.id), "sessionStartedAt": started,
        "expiresAt": None if started is None or broker.auth == "oauth" else started + SESSION_MAX_MS,
        "holdingsAt": holdings_at,
        "checkedAt": connection["checkedAt"] if connection else None,
        "needsAttention": needs_attention(status, holdings_at),
    }


def needs_attention(status: str, holdings_at: int | None, now: int | None = None) -> bool:
    """True when the login has ended, or it is connected but the holdings fetched are over 24 hours old."""
    if status == "needs_login":
        return True
    return status == "connected" and holdings_at is not None and (now or users.now_ms()) - holdings_at > HOLDINGS_STALE_MS


def brokers_overview(user_id: str) -> list[dict[str, Any]]:
    """Every broker in the registry with this profile's stored status (no call to the broker)."""
    stored = {found["broker"]: found for found in connections(user_id)}
    return [_view(broker, stored[broker.id]["status"] if broker.id in stored else "disconnected", stored.get(broker.id))
            for broker in BROKERS.values()]


def indmoney_item(user_id: str) -> dict[str, Any]:
    """INDmoney as a broker in the list: its stored token and latest snapshot, no call to INDmoney.

    ``needs_login`` when a token is stored but has expired with no way to refresh it (the rejection INDmoney
    would give); otherwise ``connected``.
    """
    with connect_db() as connection:
        stored = users._decode_secret((connection.execute(
            "SELECT indmoney FROM users WHERE id = ?", (user_id,)).fetchone() or {"indmoney": "{}"})["indmoney"])
        row = connection.execute(
            "SELECT captured_at FROM portfolio_snapshots WHERE user_id = ? ORDER BY as_of_date DESC LIMIT 1", (user_id,),
        ).fetchone()
    status = "disconnected"
    if users.has_indmoney_tokens(stored):
        usable = bool(stored.get("refreshToken")) or int(stored.get("expiresAt") or 0) > users.now_ms()
        status = "connected" if usable else "needs_login"
    return {
        "broker": INDMONEY_ID, "name": "INDmoney", "status": status, "auth": "indmoney_oauth",
        "logo": logo_path(INDMONEY_ID), "holdingsAt": row["captured_at"] if row and status != "disconnected" else None,
        "needsAttention": status == "needs_login", "checkedAt": None,
    }


def broker_list(user_id: str) -> list[dict[str, Any]]:
    """What ``GET /api/brokers`` lists: INDmoney first, then each registry broker."""
    return [indmoney_item(user_id), *brokers_overview(user_id)]


def describe_indmoney_tools(tools: list[Any]) -> list[dict[str, Any]]:
    """INDmoney's ``tools/list`` in the shape of ``describe_tools``: ``read`` only for the allowlist."""
    described = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            continue
        name = tool["name"]
        text = " ".join(str(tool.get("description") or "").split())
        if len(text) > MAX_TOOL_DESCRIPTION:
            text = text[: MAX_TOOL_DESCRIPTION - 1].rstrip() + "…"
        access = "read" if name in mcp_client.IMPORTANT_PORTFOLIO_TOOL_NAMES else ("write" if name in WRITE_TOOLS or is_write_name(name) else "not_enabled")
        described.append({"name": name, "description": text, "access": access})
    order = {"read": 0, "not_enabled": 1, "write": 2}
    return sorted(described, key=lambda item: (order[item["access"]], item["name"]))


async def indmoney_tools(user_id: str, token: str) -> list[dict[str, Any]]:
    """INDmoney's whole tool list as ``describe_indmoney_tools`` shows it, cached an hour in memory."""
    cached = _tools_cache.get((user_id, INDMONEY_ID))
    now = datetime.now(timezone.utc).timestamp()
    if cached and now - cached[0] < TOOLS_CACHE_SECONDS:
        return cached[1]
    session = await mcp_client.fetch_mcp_session(users.MCP_RESOURCE, token)
    tools = describe_indmoney_tools(session.get("tools") or [])
    _tools_cache[(user_id, INDMONEY_ID)] = (now, tools)
    return tools


UCC_RE: Final = re.compile(r"^[A-Za-z0-9]{5}$")
_SESSION_JSON_KEYS: Final = ("sessionid", "sessionId", "session_id")
_SESSION_LINE_RE = re.compile(r"""session[_ ]?id["']?\s*[:=]\s*["']?([A-Za-z0-9._\-]{6,})""", re.IGNORECASE)
_VALIDATION_FAILED_TERMS = ("invalid", "not validated", "not valid", "failed", "error", "not scanned", "expired")


def login_session_id(text: str) -> str | None:
    """The broker's own session id in a login tool's text: a JSON object, or a ``sessionid: <value>`` line."""
    candidates = [text or ""]
    found = re.search(r"\{.*\}", text or "", re.DOTALL)
    if found:
        candidates.append(found.group(0))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        stack = [parsed]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key in _SESSION_JSON_KEYS:
                    if isinstance(node.get(key), str) and node[key].strip():
                        return node[key].strip()
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    match = _SESSION_LINE_RE.search(text or "")
    return match.group(1) if match else None


async def start_login(user_id: str, broker: Broker, *, ucc: str | None = None) -> dict[str, Any]:
    """Open an MCP session, ask the broker's login tool for a link and store the session as pending.

    Raises ``BrokerError`` (safe message) when the broker gives no usable link; nothing is stored then.
    A QR-login broker (Kotak) also needs the client code (``ucc``, never stored) and returns the session id
    its login tool issues, kept encrypted and never shown.
    """
    if broker.auth == "oauth":
        raise BrokerError(f"{broker.name} signs in on its own page: use the connect link.")
    arguments: dict[str, Any] = {}
    if broker.auth == "mcp_login_qr":
        if not UCC_RE.match(ucc or ""):
            raise BrokerError("Enter your 5-character client code (UCC).")
        arguments = {"ucc": ucc}
    session = await mcp_client.fetch_mcp_session(broker.mcp_url, None, service=broker.name)
    session_id, protocol = session.get("sessionId"), session.get("protocolVersion")
    if not session_id and not broker.session_arg:
        raise BrokerError(f"{broker.name} did not start a session. Try again in a moment.")
    text = str(await mcp_client.call_mcp_tool(broker.mcp_url, None, protocol, session_id, broker.login_tool, arguments))
    url = login_url(text, broker)
    if broker.auth == "mcp_login_qr":
        login_session = login_session_id(text)
        if not url or not login_session:
            raise BrokerError(f"{broker.name} did not return a login link CA Mode can trust, so nothing was connected.")
        save_connection(user_id, broker.id, session_id=session_id, protocol=protocol, status="pending",
                        extra={"loginSession": login_session})
        return {**_view(broker, "pending", get_connection(user_id, broker.id)), "status": "pending",
                "loginUrl": url, "expiresInSeconds": LOGIN_EXPIRES_SECONDS, "needsScan": True}
    if url:
        save_connection(user_id, broker.id, session_id=session_id, protocol=protocol, status="pending")
        return {**_view(broker, "pending", get_connection(user_id, broker.id)), "status": "pending",
                "loginUrl": url, "expiresInSeconds": LOGIN_EXPIRES_SECONDS}
    if _ALREADY_RE.search(text):
        save_connection(user_id, broker.id, session_id=session_id, protocol=protocol, status="connected")
        return _view(broker, "connected", get_connection(user_id, broker.id))
    raise BrokerError(f"{broker.name} did not return a login link CA Mode can trust, so nothing was connected.")


async def verify_login(user_id: str, broker: Broker) -> dict[str, Any]:
    """Kotak: the user has scanned the QR code; ask the broker to validate the stored login session.

    Success makes the connection ``connected``; anything else leaves it ``pending`` with a safe message.
    """
    connection = get_connection(user_id, broker.id)
    if broker.auth != "mcp_login_qr" or connection is None or not connection.get("loginSession"):
        raise BrokerError(f"Start the {broker.name} connection first.")
    pending = BrokerError(f"{broker.name} has not confirmed the login yet. Scan the QR code in the Neo app, then try again.")
    try:
        text = str(await mcp_client.call_mcp_tool(
            broker.mcp_url, None, connection["protocolVersion"], connection["sessionId"], "validate_login",
            {"sessionid": connection["loginSession"]},
        ))
    except (mcp_client.McpToolError, mcp_client.McpAuthError) as exc:
        raise pending from exc
    except mcp_client.McpHttpError as exc:
        if exc.status_code in (400, 404, 410):
            set_status(user_id, broker.id, "needs_login")
            raise BrokerError(f"The {broker.name} login has ended. Connect again.") from exc
        raise BrokerError(f"Could not reach {broker.name}. Try again in a moment.") from exc
    except Exception as exc:
        logger.warning("broker_verify_failed", extra={"broker": broker.id, "error": type(exc).__name__})
        raise BrokerError(f"Could not reach {broker.name}. Try again in a moment.") from exc
    if any(term in text.lower() for term in _VALIDATION_FAILED_TERMS):
        raise pending
    set_status(user_id, broker.id, "connected")
    return _view(broker, "connected", get_connection(user_id, broker.id))


# ── OAuth brokers ──────────────────────────────────────────────────────────────

TOKEN_SKEW_MS: Final = 60 * 1000


def save_oauth(user_id: str, broker_id: str, oauth: dict[str, Any]) -> None:
    """Store a broker's OAuth tokens (encrypted) and mark it connected."""
    save_connection(user_id, broker_id, session_id=None, protocol=None, status="connected", extra={"oauth": oauth})


def _update_oauth(user_id: str, broker_id: str, oauth: dict[str, Any]) -> None:
    with connect_db() as connection:
        row = connection.execute(
            "SELECT session FROM broker_connections WHERE user_id = ? AND broker = ?", (user_id, broker_id),
        ).fetchone()
        if row is None:
            return
        session = users._decode_secret(row["session"])
        session["oauth"] = oauth
        connection.execute(
            "UPDATE broker_connections SET session = ? WHERE user_id = ? AND broker = ?",
            (users._encode_secret(session), user_id, broker_id),
        )


# One refresh at a time per connection: a broker that rotates refresh tokens accepts each one only once.
_refresh_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _refresh_lock(user_id: str, broker_id: str) -> asyncio.Lock:
    return _refresh_locks.setdefault((user_id, broker_id), asyncio.Lock())


def _usable(oauth: dict[str, Any]) -> bool:
    return bool(oauth.get("accessToken")) and int(oauth.get("expiresAt") or 0) - TOKEN_SKEW_MS > users.now_ms()


async def oauth_token(user_id: str, broker: Broker, connection: dict[str, Any]) -> str | None:
    """A working access token, refreshed when it has expired; None (status ``needs_login``) when there is none.

    Refreshes are serialised per connection, and the stored tokens are re-read under the lock, so a
    refresh another request just finished is used instead of spending the old refresh token again.
    Only a refusal of the refresh token we sent, while it is still the stored one, ends the login;
    other failures (5xx, 429, transport faults) raise: the broker could not be asked.
    """
    oauth = connection.get("oauth") or {}
    if not oauth.get("accessToken"):
        return None
    if _usable(oauth):
        return oauth["accessToken"]
    async with _refresh_lock(user_id, broker.id):
        current = get_connection(user_id, broker.id)
        if current is None:
            return None  # disconnected meanwhile
        oauth = current.get("oauth") or {}
        if not oauth.get("accessToken"):
            return None
        if _usable(oauth):
            return oauth["accessToken"]  # another request refreshed it while this one waited
        if not oauth.get("refreshToken") or not oauth.get("tokenEndpoint") or not oauth.get("client"):
            set_status(user_id, broker.id, "needs_login")
            return None
        sent = oauth["refreshToken"]
        try:
            async with httpx.AsyncClient(timeout=broker_oauth.TIMEOUT_SECONDS) as client:
                token = await broker_oauth.refresh(oauth["tokenEndpoint"], oauth["client"], sent, broker.mcp_url, client)
        except broker_oauth.OAuthRefused:
            stored = (get_connection(user_id, broker.id) or {}).get("oauth") or {}
            if stored.get("refreshToken") == sent:
                set_status(user_id, broker.id, "needs_login")
                return None
            return stored.get("accessToken") if _usable(stored) else None
        renewed = {
            **oauth, "accessToken": token["access_token"], "refreshToken": token.get("refresh_token") or sent,
            "expiresAt": users.now_ms() + broker_oauth.expires_in_seconds(token) * 1000,
        }
        _update_oauth(user_id, broker.id, renewed)  # at once: the old refresh token may already be spent
        return renewed["accessToken"]


@dataclass(slots=True)
class _Opened:
    """What a call needs: a bearer token (OAuth brokers only), the MCP session and, for OAuth, its live tool list."""
    token: str | None
    protocol: str
    session_id: str | None
    tools: list[Any] | None = None


async def _open(user_id: str, broker: Broker, connection: dict[str, Any]) -> _Opened:
    if broker.auth != "oauth":
        return _Opened(None, connection["protocolVersion"], connection["sessionId"])
    token = await oauth_token(user_id, broker, connection)
    if not token:
        raise mcp_client.McpAuthError(f"{broker.name} needs you to log in again.")
    session = await mcp_client.fetch_mcp_session(broker.mcp_url, token, service=broker.name)
    live = session.get("tools") or []
    _tools_cache[(user_id, broker.id)] = (datetime.now(timezone.utc).timestamp(), describe_tools(broker, live))
    return _Opened(token, session["protocolVersion"], session["sessionId"], live)


async def _invoke(
    opened: _Opened, broker: Broker, connection: dict[str, Any], tool: str, arguments: dict[str, Any],
    client: httpx.AsyncClient | None = None,
) -> Any:
    args = dict(arguments)
    if broker.session_arg:
        args[broker.session_arg] = connection["loginSession"]  # always ours, never the model's
    return await mcp_client.call_mcp_tool(
        broker.mcp_url, opened.token, opened.protocol, opened.session_id, tool, args, client=client,
    )


def _needs_login(broker: Broker, exc: Exception) -> bool:
    """Whether a failure means the login has ended (an OAuth broker's 401 always does)."""
    if broker.auth == "oauth" and isinstance(exc, mcp_client.McpAuthError):
        return True
    return reads_as_login_needed(str(exc))


async def check_status(user_id: str, broker: Broker) -> dict[str, Any]:
    """Ask the broker whether the stored session is logged in. Never raises.

    ``unavailable`` means the broker could not be asked; the stored status is left as it was.
    """
    connection = get_connection(user_id, broker.id)
    if not has_credentials(connection, broker):
        return _view(broker, "disconnected", None)
    now = users.now_ms()
    if broker.auth == "oauth":
        try:
            token = await oauth_token(user_id, broker, connection)
        except Exception as exc:
            logger.warning("broker_status_failed", extra={"broker": broker.id, "error": type(exc).__name__})
            return _view(broker, "unavailable", connection)
        status = "connected" if token else "needs_login"
        set_status(user_id, broker.id, status)
        return _view(broker, status, get_connection(user_id, broker.id))
    if now - connection["sessionStartedAt"] > SESSION_MAX_MS:
        set_status(user_id, broker.id, "needs_login")
        return _view(broker, "needs_login", get_connection(user_id, broker.id))
    status = connection["status"]
    if broker.auth == "mcp_login_qr" and status == "pending":
        # Not validated until the user says they scanned the QR code (`verify_login`).
        if now - connection["sessionStartedAt"] >= LOGIN_WAIT_MS:
            set_status(user_id, broker.id, "needs_login")
            return _view(broker, "needs_login", get_connection(user_id, broker.id))
        return _view(broker, "pending", connection)
    try:
        await _invoke(
            _Opened(None, connection["protocolVersion"], connection["sessionId"]), broker, connection, broker.status_tool, {},
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


# ── Tools the broker's server exposes ──────────────────────────────────────────

TOOLS_CACHE_SECONDS: Final = 60 * 60
MAX_TOOL_DESCRIPTION: Final = 400
_tools_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}  # (profile or "", broker) -> tools


def tool_access(broker: Broker, name: str, tool: dict[str, Any] | None = None) -> str:
    """``read`` when CA Mode may call it, ``write`` for an order-changing tool, else ``not_enabled`` (never called).

    A curated broker's allowlist decides; an OAuth broker's is its live list (``dynamic_read``), so ``tool``
    (the entry from ``tools/list``) is needed to call anything read.
    """
    if broker.auth == "oauth":
        if tool is not None and dynamic_read(tool):
            return "read"
    elif name in broker.read_tools:
        return "read"
    if name in WRITE_TOOLS or is_write_name(name):
        return "write"
    return "not_enabled"


def describe_tools(broker: Broker, tools: list[Any]) -> list[dict[str, Any]]:
    """The server's ``tools/list`` as names, short descriptions and CA Mode's access (read tools first)."""
    described = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            continue
        text = " ".join(str(tool.get("description") or "").split())
        if len(text) > MAX_TOOL_DESCRIPTION:
            text = text[: MAX_TOOL_DESCRIPTION - 1].rstrip() + "…"
        described.append({"name": tool["name"], "description": text, "access": tool_access(broker, tool["name"], tool)})
    order = {"read": 0, "not_enabled": 1, "write": 2}
    return sorted(described, key=lambda item: (order[item["access"]], item["name"]))


async def list_tools(
    broker: Broker, *, user_id: str | None = None, now: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """What the broker's MCP server exposes and what CA Mode may call, cached for an hour.

    A login-tool broker answers ``tools/list`` without a login. An OAuth broker needs this profile's
    connection: without one the answer is ``needsConnection`` and no tools. Raises ``BrokerError`` with a
    safe message when the server cannot be asked. Never contains a session id or token.
    """
    clock = now or (lambda: datetime.now(timezone.utc).timestamp())
    key = (user_id or "", broker.id) if broker.auth == "oauth" else ("", broker.id)
    if broker.auth == "oauth":
        connection = get_connection(user_id, broker.id) if user_id else None
        if not has_credentials(connection, broker):
            return {"broker": broker.id, "name": broker.name, "tools": [], "needsConnection": True}
    cached = _tools_cache.get(key)
    if cached and clock() - cached[0] < TOOLS_CACHE_SECONDS:
        return {"broker": broker.id, "name": broker.name, "tools": cached[1], "fetchedAt": int(cached[0] * 1000)}
    try:
        if broker.auth == "oauth":
            token = await oauth_token(user_id, broker, connection)  # type: ignore[arg-type]
            if not token:
                return {"broker": broker.id, "name": broker.name, "tools": [], "needsConnection": True}
            session = await mcp_client.fetch_mcp_session(broker.mcp_url, token, service=broker.name)
        else:
            session = await mcp_client.fetch_mcp_session(broker.mcp_url, None, service=broker.name)
    except Exception as exc:
        logger.warning("broker_tools_failed", extra={"broker": broker.id, "error": type(exc).__name__})
        raise BrokerError(f"Could not read {broker.name}'s tool list right now. Try again in a moment.") from exc
    tools = describe_tools(broker, session.get("tools") or [])
    fetched = clock()
    _tools_cache[key] = (fetched, tools)
    return {"broker": broker.id, "name": broker.name, "tools": tools, "fetchedAt": int(fetched * 1000)}


# ── Holdings ───────────────────────────────────────────────────────────────────

def parse_items(text: str) -> list[Any]:
    """A broker's holdings text as a list: a bare JSON array or ``{"data": [...]}``; anything else is refused."""
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise BrokerDataError("The broker returned holdings in a format CA Mode does not recognise.") from exc
    for _ in range(2):  # {"data": [...]}, {"data": {"holdings": [...]}}
        if isinstance(parsed, dict):
            parsed = next((parsed[key] for key in ("data", "holdings", "result") if key in parsed), None)
    if not isinstance(parsed, list):
        raise BrokerDataError("The broker returned holdings in a format CA Mode does not recognise.")
    return parsed


def rows_results(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Holdings rows in the stored-results form ``portfolio_math`` reads (one entry per asset type)."""
    by_type: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_type.setdefault(row["asset_type"], []).append(row)
    return {portfolio_math.HOLDINGS_PREFIX + asset_type: json.dumps({"holdings": found}) for asset_type, found in by_type.items()}


def rows_view(rows: list[dict[str, Any]], *, include_all_holdings: bool = False, logo_user: str | None = None) -> dict[str, Any]:
    """Figures and every holding for rows; holdings carry ``logo`` links (signed for ``logo_user``) only when given.

    Agent tools leave ``logo_user`` out, so nothing they return carries a logo link.
    """
    results = rows_results(rows)
    figures = portfolio_math.compute_metrics(results, include_all_holdings=include_all_holdings)
    # There is no networth_snapshot here, so the holdings' own value is the total (holdings only, no other assets).
    held = sum((Decimal(str(row["current_value_inr"] or 0)) for row in figures.get("by_broker") or []), Decimal(0))
    if figures.get("totals") and figures["totals"].get("current_value_inr") is None:
        figures["totals"]["current_value_inr"] = float(held.quantize(Decimal("0.01")))
    holdings = portfolio_math.all_holdings(results)
    return {"figures": figures, "holdings": logos.with_logos(holdings, logo_user) if logo_user else holdings}


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


def _store_day(user_id: str, broker_id: str, rows: list[dict[str, Any]], payload: dict[str, Any], keys: ProfileKeys, as_of: int) -> None:
    """Today's history row for the broker; a failure here never loses the holdings just fetched."""
    totals = (payload.get("figures") or {}).get("totals") or {}
    try:
        broker_history.store_day(user_id, broker_id, rows, totals.get("current_value_inr"), keys, as_of)
    except Exception:
        logger.warning("broker_history_store_failed", extra={"broker": broker_id}, exc_info=True)


def holdings_targets(broker: Broker, opened: _Opened) -> dict[str, str]:
    """Holdings tool -> asset type: the broker's own, or for an OAuth broker the read tools named for holdings."""
    if broker.auth != "oauth":
        return broker.holdings_tools
    found: dict[str, str] = {}
    for tool in opened.tools or []:
        name = tool.get("name") if isinstance(tool, dict) else None
        if isinstance(name, str) and "holding" in name.lower() and dynamic_read(tool):
            found[name] = "MF" if re.search(r"(^|_)mf(_|$)|mutual", name.lower()) else "IND_STOCK"
    if not found:
        raise BrokerDataError(f"{broker.name}'s server offers no holdings tool CA Mode can read.")
    return found


async def _fetch_rows(user_id: str, connection: dict[str, Any], broker: Broker) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=90) as client:
        opened = await _open(user_id, broker, connection)
        targets = holdings_targets(broker, opened)
        texts = await asyncio.gather(*(
            _invoke(opened, broker, connection, tool, {}, client) for tool in targets
        ))
    rows: list[dict[str, Any]] = []
    for tool, text in zip(targets, texts):
        rows.extend(broker.normalise(tool, parse_items(str(text))))
    return rows


def _holdings_payload(
    broker: Broker, status: str, rows: list[dict[str, Any]], as_of: int, *, stale: bool, include_all_holdings: bool = False,
    logo_user: str | None = None,
) -> dict[str, Any]:
    return {
        "available": True, "broker": broker.id, "name": broker.name, "status": status, "stale": stale,
        "asOf": as_of, **rows_view(rows, include_all_holdings=include_all_holdings, logo_user=logo_user), "_rows": rows,
    }


async def fetch_holdings(
    user_id: str, keys: ProfileKeys, broker: Broker, *, live: bool = True, store: bool = True,
    include_all_holdings: bool = False, with_logos: bool = False,
) -> dict[str, Any]:
    """Holdings now when the session works (stored when ``store``), else the last stored copy marked stale.

    ``with_logos`` (the app's pages only, never agent tools) adds signed ``logo`` links to the holdings.

    Never raises for a broker problem: with nothing stored the result is ``{"available": False, "status"}``.
    ``_rows`` carries the normalised rows for the combined view; routes drop keys that start with ``_``.
    """
    connection = get_connection(user_id, broker.id)
    if not has_credentials(connection, broker):
        return {"available": False, "broker": broker.id, "name": broker.name, "status": "disconnected"}
    status = connection["status"]
    problem: str | None = None
    if not live and status == "connected" and connection["holdingsAt"] is None:
        live = True  # nothing stored yet, and the login works: fetch rather than show an empty tab
    if live and broker.auth != "oauth" and users.now_ms() - connection["sessionStartedAt"] > SESSION_MAX_MS:
        status, live = "needs_login", False
        set_status(user_id, broker.id, status)
    if live:
        try:
            rows = await _fetch_rows(user_id, connection, broker)
        except (mcp_client.McpToolError, mcp_client.McpAuthError) as exc:
            status = "needs_login" if _needs_login(broker, exc) else "unavailable"
        except mcp_client.McpHttpError as exc:
            status = "needs_login" if exc.status_code in (400, 404, 410) else "unavailable"
        except BrokerDataError as exc:
            logger.warning("broker_holdings_unreadable", extra={"broker": broker.id})
            status, problem = "unavailable", str(exc)
        except Exception as exc:
            logger.warning("broker_holdings_failed", extra={"broker": broker.id, "error": type(exc).__name__})
            status = "unavailable"
        else:
            as_of = _store_rows(user_id, broker.id, rows, keys) if store else users.now_ms()
            if connection["status"] != "connected":
                set_status(user_id, broker.id, "connected")
            payload = _holdings_payload(
                broker, "connected", rows, as_of, stale=False, include_all_holdings=include_all_holdings,
                logo_user=user_id if with_logos else None,
            )
            if store:
                _store_day(user_id, broker.id, rows, payload, keys, as_of)
            logos.schedule_fill(payload.get("holdings") or [])
            return payload
        if status == "needs_login":
            set_status(user_id, broker.id, status)
    saved = stored_rows(user_id, broker.id, keys)
    if saved is None:
        return {"available": False, "broker": broker.id, "name": broker.name, "status": status,
                **({"problem": problem} if problem else {})}
    return _holdings_payload(
        broker, status, saved[0], saved[1], stale=True, include_all_holdings=include_all_holdings,
        logo_user=user_id if with_logos else None,
    )


def public(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if not key.startswith("_")}


# ── Combined view ──────────────────────────────────────────────────────────────

ISIN_RE: Final = re.compile(r"^IN[A-Z0-9]{9}[0-9]$")
GENERIC_LABELS: Final = frozenset({"", "others", "other", "unknown", "na"})


def isin_of(row: dict[str, Any]) -> str | None:
    """The row's ``investment_code`` upper-cased when it is ISIN-shaped, else None. Names never count."""
    code = str(row.get("investment_code") or "").strip().upper()
    return code if ISIN_RE.match(code) else None


def _generic_label(row: dict[str, Any]) -> bool:
    return str(row.get("broker") or "").strip().lower() in GENERIC_LABELS


def split_indmoney_rows(
    rows: list[dict[str, Any]], direct: list[tuple[Broker, list[dict[str, Any]]]],
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, int]]:
    """INDmoney rows the combined view keeps, how many each broker took over, and the ones it cannot confirm.

    1. A row whose broker label names a direct broker (``indmoney_labels``) belongs to that broker; when its
       ISIN is not among the broker's own rows it is counted in the third result (still left out).
    2. A row with an empty or generic label whose ISIN is the ISIN of a row of a direct broker, same asset
       type, is the same holding and is left out too.
    A row labelled with another named broker, or without an ISIN-shaped code, is never matched by ISIN.
    """
    deduplicated: dict[str, int] = {}
    only_in_indmoney: dict[str, int] = {}
    for broker, broker_rows in direct:
        labels = tuple(label.lower() for label in broker.indmoney_labels)
        own = {isin for isin in (isin_of(item) for item in broker_rows) if isin}
        kept = []
        for row in rows:
            if any(label in str(row.get("broker") or "").lower() for label in labels):
                isin = isin_of(row)
                if isin and isin not in own:
                    only_in_indmoney[broker.name] = only_in_indmoney.get(broker.name, 0) + 1
                deduplicated[broker.name] = deduplicated.get(broker.name, 0) + 1
            else:
                kept.append(row)
        rows = kept
    for broker, broker_rows in direct:
        held = {(isin, item.get("asset_type")) for item in broker_rows if (isin := isin_of(item))}
        kept = []
        for row in rows:
            isin = isin_of(row)
            if isin and _generic_label(row) and (isin, row.get("asset_type")) in held:
                deduplicated[broker.name] = deduplicated.get(broker.name, 0) + 1
            else:
                kept.append(row)
        rows = kept
    return rows, deduplicated, only_in_indmoney


def combined(
    snapshot: dict[str, Any] | None, broker_payloads: list[dict[str, Any]], *, include_all_holdings: bool = False,
    logo_user: str | None = None,
) -> dict[str, Any]:
    """INDmoney's latest snapshot and each broker's holdings as sources, plus one deduplicated total.

    A broker with holdings of its own replaces INDmoney's rows labelled with that broker, and INDmoney rows
    with no real broker label whose ISIN matches one of the broker's rows (``split_indmoney_rows``).
    Those rows then show only on the broker's own tab: the INDmoney source leaves them out and says
    how many moved (``movedTo``). ``onlyInIndmoney`` counts labelled rows the broker's own data does not
    contain (left out as well: the broker's data wins). A broker with nothing fetched yet takes nothing away.
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
            "id": broker.id, "name": broker.name, "logo": logo_path(broker.id), "kind": "broker", "status": payload.get("status"),
            "asOf": payload.get("asOf"), "stale": bool(payload.get("stale")), "available": bool(payload.get("available")),
            "figures": payload.get("figures"), "holdings": payload.get("holdings", []),
            **({"problem": payload["problem"]} if payload.get("problem") else {}),
        })
        if payload.get("available") and rows:
            direct.append((broker, rows))
    indmoney_rows: list[dict[str, Any]] = []
    deduplicated: dict[str, int] = {}
    only_in_indmoney: dict[str, int] = {}
    if snapshot:
        rows, _ = portfolio_math._holding_rows(snapshot["results"])
        rows, deduplicated, only_in_indmoney = split_indmoney_rows(rows, direct)
        indmoney_rows = rows
        view = rows_view(rows, logo_user=logo_user) if rows else {"figures": None, "holdings": []}
        sources.append({
            "id": "indmoney", "name": "INDmoney", "logo": logo_path(INDMONEY_ID), "kind": "indmoney", "status": "connected", "asOf": snapshot["capturedAt"],
            "stale": False, "available": True, **view, "movedTo": dict(deduplicated),
        })
    sources.extend(broker_sources)
    merged = list(indmoney_rows)
    for _, rows in direct:
        merged += rows
    extra = {"deduplicated": deduplicated, "onlyInIndmoney": only_in_indmoney}
    if not merged:
        return {"sources": sources, "combined": {"available": False, **extra}}
    return {"sources": sources, "combined": {
        "available": True, **rows_view(merged, include_all_holdings=include_all_holdings, logo_user=logo_user), **extra}}


def export_brokers(user_id: str, keys: ProfileKeys) -> list[dict[str, Any]]:
    """Each connection with its stored holdings, for the profile export. Never the session id."""
    result = []
    history = broker_history.export_history(user_id, keys)
    for found in connections(user_id):
        saved = stored_rows(user_id, found["broker"], keys)
        result.append({
            "broker": found["broker"], "status": found["status"], "connectedAt": found["sessionStartedAt"],
            "holdingsAt": found["holdingsAt"], "holdings": saved[0] if saved else [],
            "history": history.get(found["broker"], []),
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
        if _needs_login(broker, exc):
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
    any_oauth = any(BROKERS[broker_id].auth == "oauth" for broker_id in connected)
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
                    "onlyInIndmoney": merged["combined"]["onlyInIndmoney"],
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
                "note": f"Stale: the {BROKERS[choice].name} login has ended, so these are the last holdings fetched; say when they are from and suggest reconnecting."
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
        extra = arguments.get("arguments")
        extra = extra if isinstance(extra, dict) else {}
        refused = json.dumps({"error": f"{str(tool)[:60]} is not an allowed read-only {broker.name} tool."
                                       + ("" if broker.auth == "oauth" else f" Allowed: {', '.join(broker.read_tools)}")})
        if not isinstance(tool, str) or (broker.auth != "oauth" and tool not in broker.read_tools):
            return refused
        connection = get_connection(user_id, broker.id)
        if not has_credentials(connection, broker):
            return tool_results.error_payload("unavailable", f"{broker.name} is not connected.", broker.reconnect_hint)
        try:
            opened = await _open(user_id, broker, connection)  # type: ignore[arg-type]
            if broker.auth == "oauth":
                entry = next((item for item in opened.tools or [] if isinstance(item, dict) and item.get("name") == tool), None)
                if entry is None or not dynamic_read(entry):
                    return refused
            result = await _invoke(opened, broker, connection, tool, extra)  # type: ignore[arg-type]
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
                "Quote the figures; state the asOf time. When a result says stale (the broker login has ended), say when "
                "the holdings are from and suggest the user reconnect the broker in Profile > Brokers."
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
                    "tool": {"type": "string", **({} if any_oauth else {"enum": read_tools})},
                    "arguments": {"type": "object", "description": "The tool's own arguments, if it takes any."},
                },
                "required": ["broker", "tool"],
            },
            handler=call_broker_tool,
        ),
    ]

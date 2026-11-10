"""Daily holdings history of directly connected brokers, and net worth history that includes it.

One sealed row per profile, broker and local day (``broker_snapshots``), upserted on any successful live
fetch (``brokers.fetch_holdings``) and by the scheduler once a day for connections whose login still works.
The scheduler has no PIN, so it seals with the profile's public key, exactly as INDmoney snapshots are.

``combine_day()`` is the pure Decimal rule behind ``combinedInr``: INDmoney's net worth that day, minus
INDmoney's rows the combined holdings view would drop for the brokers with data that day, plus each
broker's own total. Missing is never zero: a broker without a readable total is left out, a day without
a readable net worth or dropped value has no ``combinedInr``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from . import portfolio_math, privacy, snapshots, users
from .db import connect_db
from .sealing import LockedError, ProfileKeys

logger = logging.getLogger(__name__)

CARRY_DAYS: Final = 3  # a broker's latest earlier snapshot may stand in for a missing day, up to this far back
RETRY_AFTER_FAILURE_MS: Final = snapshots.RETRY_AFTER_FAILURE_MS

_attempted: dict[tuple[str, str], int] = {}  # (profile, broker) -> last scheduled try; at most hourly


@dataclass(frozen=True, slots=True)
class BrokerDay:
    broker: str
    name: str
    day: str  # the local date of the snapshot used
    captured_at: int
    total: Decimal | None
    rows: tuple[dict[str, Any], ...]
    carried: bool = False
    missing_values: int = 0


# ── Storage ────────────────────────────────────────────────────────────────────

def _money(value: Any) -> Decimal | None:
    return portfolio_math._dec(value)


def missing_values(rows: list[dict[str, Any]]) -> int:
    return sum(1 for row in rows if _money(row.get("market_value")) is None)


def store_day(
    user_id: str, broker_id: str, rows: list[dict[str, Any]], total: float | None, keys: ProfileKeys,
    captured_ms: int | None = None,
) -> None:
    """Upsert this local day's holdings for one broker (sealed; no plain value column), then prune."""
    captured_ms = captured_ms or users.now_ms()
    local = datetime.fromtimestamp(captured_ms / 1000, snapshots.SNAPSHOT_TIMEZONE)
    payload = json.dumps({"rows": rows, "totalInr": total, "missingValues": missing_values(rows)}, ensure_ascii=False)
    with connect_db() as connection:
        connection.execute(
            "INSERT INTO broker_snapshots(user_id, broker, local_date, captured_at, data) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, broker, local_date) DO UPDATE SET captured_at = excluded.captured_at, data = excluded.data",
            (user_id, broker_id, local.date().isoformat(), captured_ms, keys.seal(payload)),
        )
        snapshots.prune(connection, user_id, local.date())


def delete_for_broker(user_id: str, broker_id: str) -> None:
    with connect_db() as connection:
        connection.execute("DELETE FROM broker_snapshots WHERE user_id = ? AND broker = ?", (user_id, broker_id))


def _day_from_row(row: Any, keys: ProfileKeys, *, carried: bool = False) -> BrokerDay | None:
    from . import brokers

    broker = brokers.BROKERS.get(row["broker"])
    if broker is None:
        return None
    try:
        payload = json.loads(keys.open(row["data"]))
    except (LockedError, ValueError):
        return None
    total = payload.get("totalInr")
    return BrokerDay(
        row["broker"], broker.name, row["local_date"], row["captured_at"],
        None if total is None else Decimal(str(total)), tuple(payload.get("rows") or []), carried,
        int(payload.get("missingValues") or 0),
    )


def load_days(user_id: str, keys: ProfileKeys, start: date) -> dict[str, dict[str, BrokerDay]]:
    """Broker snapshots from ``CARRY_DAYS`` before ``start`` on: local date -> broker id -> day."""
    floor = (start - timedelta(days=CARRY_DAYS)).isoformat()
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT broker, local_date, captured_at, data FROM broker_snapshots WHERE user_id = ? AND local_date >= ? "
            "ORDER BY local_date", (user_id, floor),
        ).fetchall()
    found: dict[str, dict[str, BrokerDay]] = {}
    for row in rows:
        day = _day_from_row(row, keys)
        if day is not None:
            found.setdefault(day.day, {})[day.broker] = day
    return found


def export_history(user_id: str, keys: ProfileKeys) -> dict[str, list[dict[str, Any]]]:
    """Every stored broker day, oldest first, by broker id (for the profile export)."""
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT broker, local_date, captured_at, data FROM broker_snapshots WHERE user_id = ? ORDER BY local_date",
            (user_id,),
        ).fetchall()
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        day = _day_from_row(row, keys)
        if day is not None:
            result.setdefault(day.broker, []).append({
                "date": day.day, "capturedAt": day.captured_at,
                "totalInr": None if day.total is None else float(day.total), "holdings": list(day.rows),
            })
    return result


# ── The pure rule ──────────────────────────────────────────────────────────────

def _float(value: Decimal | None) -> float | None:
    return None if value is None else float(value.quantize(Decimal("0.01")))


def combine_day(
    networth: Decimal | None, indmoney_rows: list[dict[str, Any]] | None, days: list[BrokerDay],
) -> dict[str, Any] | None:
    """One day's combined net worth, or None when no broker has a usable total that day.

    ``indmoney_rows`` is None when there is no INDmoney snapshot that day (brokers only, ``partial``).
    """
    from . import brokers

    usable = [day for day in days if day.total is not None]
    if not usable:
        return None
    sources: list[dict[str, Any]] = []
    partial = any(day.missing_values for day in usable)
    broker_total = sum((day.total for day in usable if day.total is not None), Decimal(0))
    for day in usable:
        sources.append({"id": day.broker, "name": day.name, "asOf": day.day, "carried": day.carried,
                        "totalInr": _float(day.total)})
    result: dict[str, Any] = {"sources": sources, "partial": partial}
    if indmoney_rows is None:
        result.update(combinedInr=_float(broker_total), partial=True)
        return result
    if networth is None:
        result.update(combinedInr=None, partial=True, reason="INDmoney's net worth could not be read that day.")
        return result
    direct = [(brokers.BROKERS[day.broker], list(day.rows)) for day in usable if day.rows]
    kept, _, _ = brokers.split_indmoney_rows(indmoney_rows, direct)
    kept_ids = {id(row) for row in kept}
    dropped = [row for row in indmoney_rows if id(row) not in kept_ids]
    values = [_money(row.get("market_value")) for row in dropped]
    if any(value is None for value in values):
        result.update(combinedInr=None, partial=True, reason="An INDmoney row left out for a broker has no value.")
        return result
    dropped_total = sum((value for value in values if value is not None), Decimal(0))
    result.update(combinedInr=_float(networth - dropped_total + broker_total), indmoneyDroppedInr=_float(dropped_total))
    return result


def effective_days(days: dict[str, dict[str, BrokerDay]], on: date) -> list[BrokerDay]:
    """Per broker: that day's snapshot, else its latest earlier one within ``CARRY_DAYS`` (marked carried)."""
    chosen: dict[str, BrokerDay] = {}
    for offset in range(CARRY_DAYS, -1, -1):
        stamp = (on - timedelta(days=offset)).isoformat()
        for broker_id, day in (days.get(stamp) or {}).items():
            chosen[broker_id] = day if offset == 0 else BrokerDay(
                day.broker, day.name, day.day, day.captured_at, day.total, day.rows, True, day.missing_values)
    return sorted(chosen.values(), key=lambda day: day.broker)


def merge_history(
    points: list[dict[str, Any]], days: dict[str, dict[str, BrokerDay]], start: date,
) -> list[dict[str, Any]]:
    """``points`` (INDmoney days, with ``_results``) plus ``combinedInr``; broker-only days are added.

    Without any broker snapshot in reach the points come back unchanged (no ``combinedInr``).
    """
    if not days:
        return points
    by_date = {point["date"]: point for point in points}
    dates = sorted(set(by_date) | {stamp for stamp in days if stamp >= start.isoformat()})
    merged: list[dict[str, Any]] = []
    any_broker = False
    for stamp in dates:
        point = by_date.get(stamp)
        usable = effective_days(days, date.fromisoformat(stamp))
        rows = None
        networth = None
        if point is not None:
            rows, _ = portfolio_math._holding_rows((point.get("_results") or {}))
            networth = None if point["networthInr"] is None else Decimal(str(point["networthInr"]))
        result = combine_day(networth, rows, usable)
        item = dict(point) if point is not None else {
            "date": stamp, "capturedAt": max((day.captured_at for day in usable if not day.carried), default=0),
            "networthInr": None, "_snapshot": None, "_results": {},
        }
        if result is None:
            item["combinedInr"] = item["networthInr"]
            item["combined"] = {"sources": [{"id": "indmoney", "name": "INDmoney"}], "partial": False}
        else:
            any_broker = True
            item["combinedInr"] = result.pop("combinedInr")
            item["combined"] = result
        merged.append(item)
    return merged if any_broker else points


def with_combined(
    user_id: str, points: list[dict[str, Any]], days: int, *, keys: ProfileKeys | None = None, today: date | None = None,
) -> list[dict[str, Any]]:
    start = (today or snapshots.local_now().date()) - timedelta(days=days)
    stored = load_days(user_id, keys or ProfileKeys(), start)
    if not stored:
        return points
    return merge_history(points, stored, start)


# ── Scheduler ──────────────────────────────────────────────────────────────────

def due_connections(now: datetime) -> list[tuple[str, str]]:
    """(profile, broker) pairs: consenting device-mode profiles, a ``connected`` broker, no capture since the
    latest scheduled time, and no scheduled attempt in the last hour."""
    scheduled_ms = int(snapshots.latest_scheduled_time(now).timestamp() * 1000)
    now_ms = int(now.timestamp() * 1000)
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT c.user_id, c.broker, (SELECT MAX(b.captured_at) FROM broker_snapshots b "
            "WHERE b.user_id = c.user_id AND b.broker = c.broker) AS last "
            "FROM broker_connections c JOIN users u ON u.id = c.user_id "
            "WHERE c.status = 'connected' AND u.storage_mode = 'device' "
            "AND (SELECT k.policy_version FROM consents k WHERE k.user_id = u.id ORDER BY k.id DESC LIMIT 1) = ? "
            "ORDER BY c.user_id, c.broker",
            (privacy.POLICY_VERSION,),
        ).fetchall()
    return [
        (row["user_id"], row["broker"]) for row in rows
        if (row["last"] or 0) < scheduled_ms and now_ms - _attempted.get((row["user_id"], row["broker"]), 0) >= RETRY_AFTER_FAILURE_MS
    ]


async def refresh_due(now: datetime | None = None) -> int:
    """Capture today's holdings for every due broker connection; return how many succeeded.

    Failures only mark the connection's status (``fetch_holdings`` does that) and are not retried within the hour.
    """
    from . import brokers

    current = now or snapshots.local_now()
    done = 0
    for user_id, broker_id in due_connections(current):
        _attempted[(user_id, broker_id)] = int(current.timestamp() * 1000)
        user = users.get_user(user_id)
        broker = brokers.BROKERS.get(broker_id)
        if user is None or broker is None or not snapshots.snapshots_allowed(user):
            continue
        keys = ProfileKeys(public_key=user["publicKey"])
        try:
            payload = await brokers.fetch_holdings(user_id, keys, broker, live=True, store=True)
        except Exception:
            logger.warning("broker_history_failed", extra={"broker": broker_id}, exc_info=True)
            continue
        if payload.get("available") and not payload.get("stale"):
            done += 1
    return done

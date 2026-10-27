"""Price levels over daily candles, for the agent's ``price_levels`` tool.

``get_indian_stocks_ohlc`` (``interval: 1day``) returns ``candles`` of ``datetime_ist``, ``open``, ``high``,
``low``, ``close``, ``volume``. Its ``14d`` lookback is 14 *calendar* days (about 10 sessions); a
rule over N sessions needs ``1y``. During market hours the last daily candle is still forming and is
left out until 15:30 IST.

For the latest completed session against the ``lookback`` sessions before it (never a window that
includes the session itself): the prior highest high and lowest low, whether the close is above that
high, how far the close is above that low, and how many of those sessions had a low within
``touch_tolerance_pct`` of it. The agent chooses the parameters and what counts as a signal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from .options_math import dec, points

MARKET_CLOSE: Final = time(15, 30)
HUNDRED: Final = Decimal(100)


@dataclass(frozen=True, slots=True)
class Candle:
    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None


def parse_ist(value: Any) -> datetime | None:
    text = str(value or "").strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None) if text else None
    except ValueError:
        return None


def parse_candles(text: str) -> tuple[list[Candle], int]:
    """Daily candles, oldest first, and how many rows were dropped as unreadable or impossible."""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        value = None
    rows = value.get("candles") if isinstance(value, dict) else None
    by_day: dict[date, Candle] = {}
    dropped = 0
    for row in rows if isinstance(rows, list) else []:
        moment = parse_ist(row.get("datetime_ist")) if isinstance(row, dict) else None
        prices = [dec(row.get(key)) for key in ("open", "high", "low", "close")] if isinstance(row, dict) else [None]
        if moment is None or any(price is None or price <= 0 for price in prices):
            dropped += 1
            continue
        open_, high, low, close = prices
        if not (low <= min(open_, close) and max(open_, close) <= high):
            dropped += 1
            continue
        by_day[moment.date()] = Candle(moment.date(), open_, high, low, close, dec(row.get("volume")))
    return [by_day[day] for day in sorted(by_day)], dropped


def _pct(part: Decimal, whole: Decimal) -> float | None:
    return None if not whole else float((part * HUNDRED / whole).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def levels(text: str, now: datetime, *, lookback: int, touch_tolerance_pct: Decimal) -> dict[str, Any]:
    """The levels described in the module docstring for one candle result."""
    candles, dropped = parse_candles(text)
    running = bool(candles) and candles[-1].day == now.date() and now.time() < MARKET_CLOSE
    if running:
        candles = candles[:-1]
    base: dict[str, Any] = {"completedSessions": len(candles), "excludedRunningSession": running, "droppedRows": dropped}
    if len(candles) < lookback + 1:
        return {**base, "status": "insufficient_data", "reason": f"{len(candles)} completed sessions; {lookback + 1} needed"}
    latest, prior = candles[-1], candles[-(lookback + 1):-1]
    high = max(candle.high for candle in prior)
    low = min(candle.low for candle in prior)
    tolerance = low * touch_tolerance_pct / HUNDRED
    return {
        **base, "status": "ok", "session": latest.day.isoformat(), "close": points(latest.close),
        "priorSessions": lookback, "priorFrom": prior[0].day.isoformat(), "priorTo": prior[-1].day.isoformat(),
        "priorHigh": points(high), "priorLow": points(low),
        "closeAbovePriorHigh": latest.close > high, "closeVsPriorHighPct": _pct(latest.close - high, high),
        "closeVsPriorLowPct": _pct(latest.close - low, low),
        "sessionsWithLowNearPriorLow": sum(1 for candle in prior if candle.low - low <= tolerance),
    }

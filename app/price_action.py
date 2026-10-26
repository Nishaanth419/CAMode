"""Deterministic price-action rules for the watchlist scanner, over INDmoney daily candles.

``get_indian_stocks_ohlc`` (``interval: 1day``) returns ``candles`` of ``datetime_ist``, ``open``,
``high``, ``low``, ``close``, ``volume``. Its ``14d`` lookback is 14 *calendar* days (about 10
sessions), too short for a 14-session rule, so the scanner asks for ``1y`` (at most 250 candles)
and uses the most recent sessions. During market hours the last daily candle is still forming.

Rules (stated in every result so the answer can quote them):

- **Breakout**: the latest completed session closed above the highest high of the ``lookback``
  sessions before it. That window excludes the latest session, so a close is never compared with a
  high that includes itself.
- **At support**: support is the lowest low of those prior sessions. The latest close is at or
  above it and within ``SUPPORT_BAND_PCT`` of it, and at least ``MIN_TOUCHES`` prior sessions had a
  low within ``TOUCH_TOLERANCE_PCT`` of support (the level was tested more than once).
- **Ranking**: breakouts first, by how far the close is above the prior high (in %); then
  support names, closest to support first. Ties go to the higher 20-session average volume.

A rule describes recent prices only; it says nothing certain about where a price goes next.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from .options_math import HUNDRED, dec, parse_ist, points

LOOKBACK_SESSIONS: Final = 14
SUPPORT_BAND_PCT: Final = Decimal("1.5")
TOUCH_TOLERANCE_PCT: Final = Decimal("1.0")
MIN_TOUCHES: Final = 2
VOLUME_SESSIONS: Final = 20
MARKET_CLOSE: Final = time(15, 30)


@dataclass(frozen=True, slots=True)
class Candle:
    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None


@dataclass(frozen=True, slots=True)
class Candles:
    ind_key: str | None
    candles: tuple[Candle, ...]
    retrieved_at: datetime | None
    dropped: int  # rows left out as unreadable or impossible


def parse_candles(text: str) -> Candles:
    """Daily candles, oldest first; a repeated day keeps its last row; impossible rows are dropped and counted."""
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
    return Candles(
        value.get("ind_key") if isinstance(value, dict) else None, tuple(by_day[day] for day in sorted(by_day)),
        parse_ist(value.get("retrieved_at_ist")) if isinstance(value, dict) else None, dropped,
    )


def completed(candles: tuple[Candle, ...], now: datetime) -> tuple[tuple[Candle, ...], bool]:
    """The candles without today's, while the session is still open (``now`` is IST wall-clock time)."""
    if candles and candles[-1].day == now.date() and now.time() < MARKET_CLOSE:
        return candles[:-1], True
    return candles, False


def _pct(part: Decimal, whole: Decimal) -> float | None:
    return None if not whole else float((part * HUNDRED / whole).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@dataclass(slots=True)
class Scan:
    name: str
    ind_key: str | None
    status: str  # "ok" | "insufficient_data"
    session: date | None = None
    close: Decimal | None = None
    prior_high: Decimal | None = None
    support: Decimal | None = None
    touches: int = 0
    breakout: bool = False
    at_support: bool = False
    above_high_pct: float | None = None
    above_support_pct: float | None = None
    average_volume: Decimal | None = None
    excluded_running_session: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return self.breakout or self.at_support

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name, "indKey": self.ind_key, "status": self.status,
            "session": self.session.isoformat() if self.session else None, "close": points(self.close),
            "priorHigh": points(self.prior_high), "support": points(self.support), "supportTouches": self.touches,
            "breakout": self.breakout, "atSupport": self.at_support, "closeAboveHighPct": self.above_high_pct,
            "closeAboveSupportPct": self.above_support_pct,
            "excludedRunningSession": self.excluded_running_session, "notes": self.notes,
        }


def scan(name: str, series: Candles, now: datetime, lookback: int = LOOKBACK_SESSIONS) -> Scan:
    """Apply the breakout and support rules to one instrument's daily candles."""
    candles, running = completed(series.candles, now)
    result = Scan(name=name, ind_key=series.ind_key, status="insufficient_data", excluded_running_session=running)
    if series.dropped:
        result.notes.append(f"{series.dropped} unreadable or impossible candle(s) were left out")
    if len(candles) < lookback + 1:
        result.notes.append(f"{len(candles)} completed sessions; the rules need {lookback + 1}")
        return result
    latest, prior = candles[-1], candles[-(lookback + 1):-1]
    result.status, result.session, result.close = "ok", latest.day, latest.close
    result.prior_high = max(candle.high for candle in prior)
    result.support = min(candle.low for candle in prior)
    tolerance = result.support * TOUCH_TOLERANCE_PCT / HUNDRED
    result.touches = sum(1 for candle in prior if candle.low - result.support <= tolerance)
    result.breakout = latest.close > result.prior_high
    result.above_high_pct = _pct(latest.close - result.prior_high, result.prior_high)
    result.above_support_pct = _pct(latest.close - result.support, result.support)
    band = result.support * SUPPORT_BAND_PCT / HUNDRED
    result.at_support = (not result.breakout and result.support <= latest.close <= result.support + band
                         and result.touches >= MIN_TOUCHES)
    volumes = [candle.volume for candle in candles[-VOLUME_SESSIONS:] if candle.volume is not None]
    result.average_volume = sum(volumes, Decimal(0)) / len(volumes) if volumes else None
    return result


def rank(scans: list[Scan]) -> list[Scan]:
    """Flagged instruments in rank order (see the module docstring)."""
    def volume(item: Scan) -> Decimal:
        return -(item.average_volume or Decimal(0))

    breakouts = sorted((s for s in scans if s.breakout), key=lambda s: (-(s.above_high_pct or 0), volume(s), s.name))
    supports = sorted((s for s in scans if s.at_support), key=lambda s: (s.above_support_pct or 0, volume(s), s.name))
    return breakouts + supports


def rules() -> dict[str, Any]:
    return {
        "lookbackSessions": LOOKBACK_SESSIONS,
        "breakout": f"latest completed close above the highest high of the {LOOKBACK_SESSIONS} sessions before it",
        "support": (f"lowest low of the {LOOKBACK_SESSIONS} sessions before the latest; the close is at most "
                    f"{SUPPORT_BAND_PCT}% above it and at least {MIN_TOUCHES} of those sessions had a low within "
                    f"{TOUCH_TOLERANCE_PCT}% of it"),
        "ranking": "breakouts by % above the prior high, then support names by distance to support",
        "runningSession": "a daily candle for today is left out until 15:30 IST",
    }


@dataclass(frozen=True, slots=True)
class Analyst:
    analysts: int | None
    sentiment: str | None
    buy_pct: Decimal | None
    hold_pct: Decimal | None
    sell_pct: Decimal | None
    target_mean: Decimal | None
    target_low: Decimal | None
    target_high: Decimal | None
    price: Decimal | None
    price_updated: datetime | None
    reported_upside_pct: Decimal | None


def parse_analyst(text: str) -> dict[str, Analyst]:
    """``get_indian_stocks_details`` with ``segments: ["analyst"]``: {ind_key: Analyst} for entries with a forecast."""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    found: dict[str, Analyst] = {}
    for key, entry in (value.items() if isinstance(value, dict) else ()):
        if not isinstance(entry, dict):
            continue
        stats = entry.get("entity_stats") if isinstance(entry.get("entity_stats"), dict) else {}
        forecast = entry.get("analyst_forecast") if isinstance(entry.get("analyst_forecast"), dict) else {}
        consensus = forecast.get("consensus") if isinstance(forecast.get("consensus"), dict) else {}
        target = forecast.get("target_price") if isinstance(forecast.get("target_price"), dict) else {}
        count = dec(consensus.get("num_analysts") if consensus.get("num_analysts") is not None else target.get("num_analysts"))
        found[str(key)] = Analyst(
            analysts=int(count) if count is not None else None, sentiment=consensus.get("sentiment") or None,
            buy_pct=dec(consensus.get("buy_per")), hold_pct=dec(consensus.get("hold_per")), sell_pct=dec(consensus.get("sell_per")),
            target_mean=dec(target.get("mean")) or dec(consensus.get("target_prc")),
            target_low=dec(target.get("low")), target_high=dec(target.get("high")),
            price=dec(stats.get("live_price")), price_updated=parse_ist(stats.get("price_updated_at_ist")),
            reported_upside_pct=dec(target.get("upside_per")),
        )
    return found


def target_upside(analyst: Analyst) -> tuple[float | None, list[str]]:
    """Upside from the live price to the mean target, in %; ``None`` (with the reason) when either is missing."""
    if not analyst.analysts:
        return None, ["no analyst coverage was returned"]
    if analyst.target_mean is None or analyst.target_mean <= 0:
        return None, ["no target price was returned"]
    if analyst.price is None or analyst.price <= 0:
        return None, ["no live price was returned"]
    upside = _pct(analyst.target_mean - analyst.price, analyst.price)
    notes = []
    if analyst.reported_upside_pct is not None and upside is not None and abs(Decimal(str(upside)) - analyst.reported_upside_pct) > 1:
        notes.append(f"INDmoney reports {points(analyst.reported_upside_pct)}% upside against its own price; "
                     f"{upside}% is computed from the live price returned")
    return upside, notes

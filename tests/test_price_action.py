from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

from app import price_action as pa
from tests.fixtures import indmoney_market as market

AFTER_CLOSE = datetime(2026, 10, 27, 18, 0)


def levels(series: dict, now: datetime = AFTER_CLOSE, lookback: int = 14) -> dict:
    return pa.levels(json.dumps(series), now, lookback=lookback, touch_tolerance_pct=Decimal(1))


def test_the_prior_window_never_includes_the_latest_session():
    result = levels(market.breakout_series("A"))
    # Highest high of the 14 sessions before the latest (1010 × 1.01), not the breakout day's own high.
    assert result["priorHigh"] == 1020.1 and result["close"] == 1050.0
    assert result["closeAbovePriorHigh"] is True and result["closeVsPriorHighPct"] == 2.93


def test_support_figures_and_touches():
    result = levels(market.support_series("B"))
    assert result["priorLow"] == 495.0 and result["closeVsPriorLowPct"] == 1.01
    assert result["sessionsWithLowNearPriorLow"] >= 3


def test_too_few_sessions():
    result = levels(market.short_series("C"))
    assert result["status"] == "insufficient_data" and "15 needed" in result["reason"]


def test_todays_running_candle_is_left_out_until_the_close():
    series = market.candles("E", [1000.0] * 20 + [1050.0], end=date(2026, 10, 28))
    during = levels(series, now=datetime(2026, 10, 28, 11, 0))
    after = levels(series, now=datetime(2026, 10, 28, 16, 0))
    assert during["excludedRunningSession"] and during["session"] == "2026-10-27" and not during["closeAbovePriorHigh"]
    assert not after["excludedRunningSession"] and after["closeAbovePriorHigh"]


def test_impossible_and_repeated_candles():
    series = market.candles("F", [100.0] * 16)
    series["candles"].append({"datetime_ist": "2026-10-28 00:00:00", "open": 10, "high": 5, "low": 4, "close": 6, "volume": 1})
    series["candles"].append(dict(series["candles"][0]))
    candles, dropped = pa.parse_candles(json.dumps(series))
    assert dropped == 1 and len(candles) == 16

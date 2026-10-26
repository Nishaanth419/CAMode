from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from app import price_action as pa
from tests.fixtures import indmoney_market as market

AFTER_CLOSE = datetime(2026, 10, 27, 18, 0)


def scan(series: dict, now: datetime = AFTER_CLOSE, name: str = "X") -> pa.Scan:
    return pa.scan(name, pa.parse_candles(json.dumps(series)), now)


def test_breakout_compares_the_close_with_the_prior_sessions_only():
    result = scan(market.breakout_series("A"))
    assert result.status == "ok" and result.breakout and not result.at_support
    # The highest high of the 14 sessions before the latest (1010 × 1.01), not including the breakout day's own high.
    assert result.prior_high == Decimal("1020.1") and result.above_high_pct == 2.93


def test_a_close_equal_to_the_prior_high_is_not_a_breakout():
    closes = [1000.0] * 20 + [1010.0]
    highs = [1010.0] * 20 + [1015.0]
    result = scan(market.candles("A", closes, highs=highs))
    assert result.prior_high == Decimal(1010) and not result.breakout


def test_support_needs_a_tested_floor_and_a_close_near_it():
    result = scan(market.support_series("B"))
    assert result.at_support and result.support == Decimal(495) and result.above_support_pct == 1.01
    assert result.touches >= pa.MIN_TOUCHES


def test_a_single_low_is_not_support():
    closes = [600.0] * 20 + [585.0]
    lows = [595.0] * 20 + [580.0]
    lows[10] = 580.0  # One deep low; everything else is 2.5% above it.
    result = scan(market.candles("C", closes, lows=lows))
    assert result.support == Decimal(580) and result.touches == 1 and not result.at_support


def test_too_few_sessions_are_not_judged():
    result = scan(market.short_series("D"))
    assert result.status == "insufficient_data" and not result.flagged and "need 15" in result.notes[0]


def test_todays_running_candle_is_left_out_until_the_close():
    series = market.candles("E", [1000.0] * 20 + [1050.0], end=datetime(2026, 10, 28).date())
    during = scan(series, now=datetime(2026, 10, 28, 11, 0))
    after = scan(series, now=datetime(2026, 10, 28, 16, 0))
    assert during.excluded_running_session and not during.breakout and during.session.isoformat() == "2026-10-27"
    assert not after.excluded_running_session and after.breakout


def test_impossible_and_repeated_candles():
    series = market.candles("F", [100.0] * 16)
    series["candles"].append({"datetime_ist": "2026-10-28 00:00:00", "open": 10, "high": 5, "low": 4, "close": 6, "volume": 1})
    series["candles"].append(dict(series["candles"][0]))
    parsed = pa.parse_candles(json.dumps(series))
    assert parsed.dropped == 1 and len(parsed.candles) == 16


def test_ranking_puts_breakouts_first_by_strength_then_support_by_distance():
    strong = scan(market.candles("S", [1000.0] * 20 + [1100.0]), name="Strong")
    weak = scan(market.breakout_series("W"), name="Weak")
    support = scan(market.support_series("P"), name="Floor")
    flat = scan(market.flat_series("N"), name="Flat")
    assert [item.name for item in pa.rank([flat, support, weak, strong])] == ["Strong", "Weak", "Floor"]


def test_analyst_upside_from_the_live_price_and_missing_data():
    parsed = pa.parse_analyst(json.dumps(market.analyst("A", price=1050, mean=1200)))
    upside, notes = pa.target_upside(parsed["A"])
    assert upside == 14.29 and notes == []
    none = pa.parse_analyst(json.dumps(market.analyst("B", price=500, mean=None)))
    assert pa.target_upside(none["B"]) == (None, ["no analyst coverage was returned"])
    drift = market.analyst("C", price=1000, mean=1100)
    drift["C"]["analyst_forecast"]["target_price"]["upside_per"] = 25
    upside, notes = pa.target_upside(pa.parse_analyst(json.dumps(drift))["C"])
    assert upside == 10.0 and "INDmoney reports 25" in notes[0]

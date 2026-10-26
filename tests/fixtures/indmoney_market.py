"""Synthetic INDmoney market responses in the real layouts, with made-up but self-consistent values.

Layouts recorded on 2026-10-26 from the live tools (read-only): ``lookup_ind_keys`` (a flat list of
``{ind_key, name}`` for every name asked), ``get_indian_stocks_option_chain`` (expiries as ``YYYYMMDD``;
bid/ask as ``buy_price``/``sell_price``; theta in points per day, the same for the call and the put of a
strike), ``get_indian_stocks_details`` for option legs (``bid_price``/``ask_price``, ``greeks_data``,
``expiry_date`` "YYYY-MM-DD 14:00:00", ``underlying_parent_ind_key``) and with ``segments: ["analyst"]``
(``analyst_forecast.consensus`` / ``target_price``), ``get_indian_stocks_greeks_history`` and daily
``get_indian_stocks_ohlc``. An expiry the chain does not list comes back as an error object, not an MCP error.

Traps kept from the recording: the deepest in-the-money call has no price or delta at all, another
in-the-money call reports delta -1 with no vega, and the lookup returns Nifty Next 50 and Smallcap 50
next to NIFTY 50. Option values come from Black-Scholes with no interest (so call and put theta match,
as recorded), rounded like INDmoney's.

The ``user_watchlist`` stock row layout is NOT recorded (the account's watchlists were empty); the rows
here (``ind_key``, ``name``) are an assumption, and one fixture has rows with names only.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta

NIFTY_KEY = "INDI00012"
SPOT = 22659.25
LOT_SIZE = 65
IV = 0.18
TODAY = date(2026, 10, 28)  # a Wednesday: the nearest expiry (3 Nov) is a weekly, the monthly is 23 Nov
NOW = datetime(2026, 10, 28, 10, 30)  # IST, market open
EXPIRIES = ["20261103", "20261109", "20261117", "20261123", "20261201", "20261229", "20270126"]
MONTHLY = "20261123"
NEAREST = "20261103"


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _npdf(x: float) -> float:
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def _tick(value: float) -> float:
    return round(max(0.05, round(value / 0.05) * 0.05), 2)


def option(strike: float, expiry: str, side: str, *, spot: float = SPOT, today: date = TODAY, iv: float = IV) -> dict:
    """One leg's price, delta and theta (points per day) from Black-Scholes with no interest."""
    expiry_day = date(int(expiry[:4]), int(expiry[4:6]), int(expiry[6:]))
    t = max((expiry_day - today).days, 0.5) / 365
    d1 = (math.log(spot / strike) + iv * iv * t / 2) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    call = spot * _ncdf(d1) - strike * _ncdf(d2)
    price = call if side == "call" else call - spot + strike
    delta = _ncdf(d1) if side == "call" else _ncdf(d1) - 1
    theta = -spot * _npdf(d1) * iv / (2 * math.sqrt(t)) / 365
    return {"price": _tick(price), "delta": round(delta, 2), "theta": round(theta, 2)}


def leg_key(strike: float, expiry: str, side: str) -> str:
    digest = hashlib.sha1(f"{strike}-{expiry}-{side}".encode()).hexdigest().upper()
    return "INDON" + digest[:7]


def _slug(strike: float, expiry: str, side: str) -> str:
    day = date(int(expiry[:4]), int(expiry[4:6]), int(expiry[6:]))
    return f"nifty-{day.day}-{day.strftime('%b').lower()}-{int(strike)}-{side}"


def _quote_side(strike: float, expiry: str, side: str, spot: float, today: date) -> dict:
    value = option(strike, expiry, side, spot=spot, today=today)
    spread = max(0.05, _tick(value["price"] * 0.004))
    return {
        "derivative_id": leg_key(strike, expiry, side), "slug": _slug(strike, expiry, side),
        "current_price": value["price"], "perc_change": -3.1, "price_change": -1.2, "oi": 120000, "oi_change": 4.2,
        "volume": 50000, "oi_shade": 0, "iv": round(IV * 100, 2), "lot_size": LOT_SIZE, "delta": value["delta"],
        "theta": value["theta"], "vega": 12.5, "gamma": 0.0002,
        "buy_price": _tick(value["price"] - spread / 2), "sell_price": _tick(value["price"] + spread / 2),
    }


def chain(expiry: str = MONTHLY, *, spot: float = SPOT, strikes_around_atm: int = 30, today: date = TODAY,
          now: datetime = NOW, expiry_list: list[str] | None = None) -> dict:
    atm = round(spot / 50) * 50
    rows = []
    for offset in range(-strikes_around_atm, strikes_around_atm + 1):
        strike = atm + offset * 50
        rows.append({"strike_price": strike, "pcr": "1.02", "call_data": _quote_side(strike, expiry, "call", spot, today),
                     "put_data": _quote_side(strike, expiry, "put", spot, today)})
    # The deepest in-the-money call: no price, delta or greeks (as recorded).
    rows[0]["call_data"] = {k: rows[0]["call_data"][k] for k in ("derivative_id", "slug", "oi", "oi_change", "oi_shade", "iv", "lot_size", "theta")}
    rows[0]["pcr"] = "-"
    # Another in-the-money call with an impossible delta and no vega.
    rows[3]["call_data"]["delta"], rows[3]["call_data"]["vega"] = -1, None
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S+05:30")
    return {
        "retrieved_at_ist": now.strftime("%Y-%m-%d %H:%M:%S"),
        "entity_details": {
            "name": "NIFTY 50", "entity_type": "INDEX", "slug": "nifty-50", "ind_key": NIFTY_KEY, "exchange": "NSE",
            "is_active_stock": True, "live_price": spot, "previous_close": 22600, "day_high": 22706.15, "day_low": 22387.35,
            "day_open": 22524.4, "price_change": 59.25, "day_change": 59.25, "day_change_percentage": 0.26, "holding_id": 0,
            "overall_pcr_string": "1.11", "overall_pcr_tag": "BEARISH", "settlement_mode": "cash",
            "price_updated_at_ist": stamp, "price_mode": "live",
        },
        "selected_expiry": expiry, "expiry_list": list(expiry_list or EXPIRIES), "option_chain_data": rows,
    }


def chain_rows(data: dict) -> dict[tuple[str, float], dict]:
    """{(side, strike): leg} of a chain built here, for tests."""
    found = {}
    for row in data["option_chain_data"]:
        found[("call", row["strike_price"])] = row["call_data"]
        found[("put", row["strike_price"])] = row["put_data"]
    return found


def lookup_nifty() -> list[dict]:
    return [{"ind_key": NIFTY_KEY, "name": "NIFTY 50"}, {"ind_key": "INDI00251", "name": "Nifty Next 50"},
            {"ind_key": "INDI00266", "name": "Nifty Smallcap 50"}]


def expiry_rejected(asked: str, listed: list[str] | None = None) -> dict:
    return {"error": "validation_error", "tool": "get_indian_stocks_option_chain",
            "message": f"expiry_date '{asked}' is not an available expiry for {NIFTY_KEY}. Available: {', '.join(listed or EXPIRIES)}."}


def leg_details(chain_data: dict, keys: list[str], *, now: datetime = NOW, premium_shift: float = 0.0,
                expiry_override: str | None = None, underlying: str = NIFTY_KEY) -> dict:
    """``get_indian_stocks_details`` for option legs of ``chain_data`` (quotes as in the chain, shifted if asked)."""
    by_key = {}
    for row in chain_data["option_chain_data"]:
        for side in ("call_data", "put_data"):
            leg = row[side]
            by_key[leg["derivative_id"]] = (row["strike_price"], side[:-5], leg)
    expiry = expiry_override or chain_data["selected_expiry"]
    day = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]} 14:00:00"
    out = {}
    for key in keys:
        strike, side, leg = by_key[key]
        out[key] = {
            "entity_class": "INDEX",
            "entity_basic": {"ind_key": key, "name": f"NIFTY {int(strike)} {side.title()}", "short_name": f"NIFTY {int(strike)} {side.title()}",
                             "display_name": f"NIFTY ₹{int(strike)} {side.title()}", "expiry_date": day, "exchange": "NSE",
                             "is_active": True, "segment": "fno", "underlying_parent_ind_key": underlying},
            "entity_stats": {
                "live_price": round(leg["current_price"] + premium_shift, 2), "day_change": -1.2, "day_change_percentage": -1.1,
                "prev_close": leg["current_price"] + 1.2, "day_low": 1, "day_high": 2, "day_open": 1.5, "52week_high": 0,
                "52week_low": 0, "upper_circuit": 0, "lower_circuit": 0.05, "high_exec_band": 0, "low_exec_band": 0,
                "open_interest": 23530, "open_interest_change": 8.71, "volume": 10920,
                "greeks_data": {"delta": leg["delta"], "theta": leg["theta"], "vega": 14.5, "iv_vwap": 19.6, "vanna": -0.01,
                                "volga": 0.74, "veta": -0.53, "tgr": -28635.86, "tv": -0.35, "dtr": 0.03, "gamma": 0.0002},
                "ask_price": round(leg["sell_price"] + premium_shift, 2), "bid_price": round(leg["buy_price"] + premium_shift, 2),
                "price_updated_at_ist": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S+05:30"),
            },
        }
    return out


def greeks_history(key: str, theta: float, *, now: datetime = NOW) -> dict:
    samples = []
    for index in range(15):
        moment = now - timedelta(days=1 if index < 8 else 0, minutes=5 * (15 - index))
        samples.append({"delta": -0.15, "theta": round(theta * (0.9 + index * 0.007), 2), "vega": 14.5, "iv_vwap": 19.6,
                        "vanna": -0.01, "volga": 0.7, "veta": -0.5, "tgr": -24000.0, "tv": -0.3, "dtr": 0.03, "gamma": 0.0002,
                        "iv": 19.9, "oi": 20000, "timestamp": int(moment.timestamp()), "datetime_ist": moment.strftime("%Y-%m-%d %H:%M:%S")})
    return {"ind_key": key, "lookback": "7d", "count": len(samples), "greeks": samples}


# ── Watchlist scanner ───────────────────────────────────────────────────────────

def _sessions(end: date, count: int) -> list[date]:
    days, day = [], end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return days[::-1]


def candles(key: str, closes: list[float], *, end: date = date(2026, 10, 27), lows: list[float] | None = None,
            highs: list[float] | None = None) -> dict:
    """Daily candles ending on ``end``; highs/lows default to ±1% around each close."""
    days = _sessions(end, len(closes))
    rows = []
    for index, (day, close) in enumerate(zip(days, closes, strict=True)):
        high = highs[index] if highs else round(close * 1.01, 2)
        low = lows[index] if lows else round(close * 0.99, 2)
        rows.append({"datetime_ist": f"{day.isoformat()} 00:00:00", "open": round((high + low) / 2, 2), "high": high,
                     "low": low, "close": close, "volume": 100000 + index})
    return {"ind_key": key, "interval": "1day", "retrieved_at_ist": "2026-10-28 10:30:00",
            "query_end_ist": "2026-10-28 10:30:00", "count": len(rows), "candles": rows, "has_more_data": False}


def breakout_series(key: str) -> dict:
    """20 sessions trading 1000–1010, then a close of 1050 (above the prior 14-session high of 1020.1)."""
    closes = [1000 + (i % 3) * 5 for i in range(20)] + [1050]
    return candles(key, closes)


def support_series(key: str) -> dict:
    """A 14-session floor at 495 touched three times, the latest close 500 (1.01% above it)."""
    closes = [520, 515, 510, 505, 500, 510, 520, 515, 505, 500, 510, 515, 505, 500, 505, 510, 500, 505, 502, 508, 500]
    lows = [round(c * 0.99, 2) for c in closes]
    for index in (8, 13, 16):
        lows[index] = 495.0
    return candles(key, closes, lows=lows)


def flat_series(key: str) -> dict:
    closes = [800 + (i % 4) * 10 for i in range(21)]
    closes[-1] = 815
    return candles(key, closes)


def short_series(key: str) -> dict:
    return candles(key, [300, 301, 302, 303, 304])


def watchlist(rows: list[dict]) -> dict:
    return {"type": "indian", "watchlists": [{"name": "Swing", "watchlist_id": 1, "stocks": rows}, {"name": "Empty", "watchlist_id": 2, "stocks": []}]}


def analyst(key: str, *, price: float, mean: float | None, analysts: int = 30, sentiment: str = "Buy") -> dict:
    forecast = {"consensus": {"num_analysts": analysts, "target_prc": mean, "sentiment": sentiment, "buy_per": 60, "hold_per": 30, "sell_per": 10},
                "target_price": {"num_analysts": analysts, "low": (mean or 0) * 0.85, "high": (mean or 0) * 1.15, "mean": mean,
                                 "current": price, "upside_per": round(((mean or price) - price) * 100 / price, 2)}}
    if mean is None:
        forecast = {"consensus": {"num_analysts": 0}, "target_price": {}}
    return {key: {"entity_class": "STOCK", "entity_basic": {"ind_key": key, "name": key},
                  "entity_stats": {"live_price": price, "price_updated_at_ist": "2026-10-28T10:29:00+05:30"},
                  "analyst_forecast": forecast}}


# ── Health check (fund details, layout recorded 2026-10-26) ─────────────────────

def fund_details(funds: dict[str, dict]) -> dict:
    """``get_mf_funds_details`` with fund_performance and sector_allocation for {code: spec}."""
    data = []
    for code, spec in funds.items():
        periods = ("1M", "3M", "6M", "1Y", "3Y", "5Y")
        entry = {"fund_id": int(code), "data": {
            "fund_detail": {"name": spec["name"], "category": spec.get("category", "flexi cap"),
                            "benchmark_name": spec.get("benchmark", "Nifty 500 TR INR"), "expense_ratio": 0.6, "nav": 88.4,
                            "returns": {p: spec["returns"].get(p, 0) for p in periods}},
            "fund_performance": {
                "returns": {p: v for p, v in spec["returns"].items()},
                "benchmark_returns": dict(spec["benchmarkReturns"]),
                "category_returns": dict(spec.get("categoryReturns", {})),
                "category_rank": {p: "3/20" for p in spec["returns"]},
                "best_in_category": {}, "worst_in_category": {},
            },
        }}
        if "sectors" in spec:
            entry["data"]["sector_allocation"] = [
                {"name": "Equity", "perc": f"{spec['equityPct']}%", "PercentageVal": spec["equityPct"],
                 "sectors": [{"name": n, "perc": f"{v}%", "percValue": round(v)} for n, v in spec["sectors"].items()]},
                {"name": "Debt & Cash", "perc": f"{100 - spec['equityPct']}%", "PercentageVal": 100 - spec["equityPct"],
                 "sectors": [{"name": "Cash Equivalent", "perc": "100%", "percValue": 100}]},
            ]
        data.append(entry)
    return {"success": True, "data": data}


FUNDS = {
    # Trails its benchmark by 2.1 points over 3 years.
    "120503": {"name": "Parag Parikh Flexi Cap Fund", "returns": {"1Y": -6.68, "3Y": 10.2, "5Y": 10.47},
               "benchmarkReturns": {"1Y": -9.54, "3Y": 12.3, "5Y": 6.92}, "categoryReturns": {"1Y": -6.5, "3Y": 10.21},
               "equityPct": 90.0, "sectors": {"Financial Services": 30.0, "Tech": 12.5, "Health": 5.0}},
    # Within 0.5 points of its benchmark over 3 years (an index fund).
    "118989": {"name": "HDFC Index Fund Nifty 50", "returns": {"1Y": -8.1, "3Y": 11.9}, "benchmarkReturns": {"1Y": -7.9, "3Y": 12.2},
               "categoryReturns": {"3Y": 11.5}, "equityPct": 100.0, "sectors": {"Financial Services": 35.0, "Tech": 14.0}},
}


def history(points: list[tuple[str, float | None]]) -> dict:
    return {"days": 365, "points": [{"date": d, "capturedAt": f"{d}T03:30:00+00:00", "networthInr": v} for d, v in points]}


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)

"""Synthetic Kite MCP payloads in the documented field layout (made-up values, no real account)."""

from __future__ import annotations

import json

HOLDINGS = [
    {"tradingsymbol": "ACMEBANK", "exchange": "NSE", "instrument_token": 1001, "isin": "INE000A01011", "product": "CNC",
     "quantity": 10, "t1_quantity": 2, "average_price": 100.5, "last_price": 120.25, "close_price": 119.0,
     "pnl": 237.0, "day_change": 1.25, "day_change_percentage": 1.05},
    {"tradingsymbol": "SAMPLEIT", "exchange": "NSE", "instrument_token": 1002, "isin": "INE000B01012", "product": "CNC",
     "quantity": 5, "t1_quantity": 0, "average_price": 2000, "last_price": 2100.5, "close_price": 2090.0,
     "pnl": 502.5, "day_change": 10.5, "day_change_percentage": 0.5},
    {"tradingsymbol": "SOLDOUT", "exchange": "NSE", "instrument_token": 1003, "isin": "INE000C01013", "product": "CNC",
     "quantity": 0, "t1_quantity": 0, "average_price": 50, "last_price": 55, "close_price": 54.0,
     "pnl": 0, "day_change": 0, "day_change_percentage": 0},
]

MF_HOLDINGS = [
    {"folio": "1234/56", "fund": "Sample Flexi Cap Fund - Direct Growth", "tradingsymbol": "INF000X01AB1",
     "average_price": 40.0, "last_price": 52.5, "last_price_date": "2026-11-05", "pnl": 125.0, "quantity": 10.0},
]

PROFILE = {"user_id": "AB1234", "user_name": "Sample Person", "user_shortname": "Sample", "email": "sample@example.invalid",
           "broker": "ZERODHA", "products": ["CNC"], "exchanges": ["NSE"]}


def paged(items: list, *, start: int = 0, limit: int | None = None) -> str:
    """The ``{"data", "pagination"}`` shape Kite returns when a ``limit`` is passed."""
    page = items[start:start + (limit or len(items))]
    return json.dumps({"data": page, "pagination": {
        "from": start, "limit": limit or len(items), "total": len(items),
        "has_more": start + len(page) < len(items), "returned": len(page)}})


def text(value) -> str:
    return json.dumps(value)


LOGIN_TEXT = (
    "Please login to Kite: [Login to Kite](https://mcp.kite.trade/authorize?session_id=abc%7C1700000000.sig) "
    "IMPORTANT: show this link to the user."
)
LOGGED_IN_TEXT = "You are already logged in as Sample Person."
LOGIN_FIRST = "Please log in first using the login tool"

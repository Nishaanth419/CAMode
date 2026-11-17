"""Zerodha and FYERS as login-tool brokers, the way their servers worked before they moved to OAuth.

No real broker signs in through an MCP ``login`` tool any more (both answered 401 with an OAuth challenge on
2026-11-17). The login-tool flow stays in ``brokers`` as a mechanism, and these variants keep its tests running
against the recorded Kite layout. Use the ``login_tool_brokers`` fixture to put them in the registry.
"""

from __future__ import annotations

import dataclasses

import pytest

from app import brokers

# Kite's tools before it grouped them by mode (2026-11-17).
KITE_GET_TOOLS = (
    "get_profile", "get_margins", "get_holdings", "get_positions", "get_mf_holdings", "get_trades",
    "get_orders", "get_order_history", "get_order_trades", "get_gtts", "get_quotes", "get_ltp",
    "get_ohlc", "get_historical_data", "search_instruments",
)
LOGIN_TOOL_ZERODHA = dataclasses.replace(
    brokers.BROKERS["zerodha"], auth="mcp_login_tool", status_tool="get_profile",
    login_hosts=("mcp.kite.trade", "kite.zerodha.com"), read_modes={}, holdings_arguments={},
    read_tools=KITE_GET_TOOLS, holdings_tools={"get_holdings": "IND_STOCK", "get_mf_holdings": "MF"},
)
LOGIN_TOOL_FYERS = dataclasses.replace(
    brokers.BROKERS["fyers"], auth="mcp_login_tool", status_tool="get_funds",
    login_hosts=("mcp.fyers.in",), login_suffixes=("fyers.in",),
)


@pytest.fixture
def login_tool_brokers(monkeypatch):
    monkeypatch.setitem(brokers.BROKERS, "zerodha", LOGIN_TOOL_ZERODHA)
    monkeypatch.setitem(brokers.BROKERS, "fyers", LOGIN_TOOL_FYERS)

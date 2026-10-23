"""Synthetic INDmoney responses with the real field layout and made-up values.

Totals are consistent: holdings sum to each asset type's ``asset_summary`` and the
asset types sum to ``total_current_value``. Includes the known traps: a linked-broker
stock with ``invested_amount`` 0, US stocks with USD fields, MF rows without a broker, and
Indian stocks labelled ``STOCK`` in the summary and rows though the holdings call takes ``IND_STOCK``.
"""

from __future__ import annotations

import json


def _row(code, name, asset_type, invested, value, units, broker, **extra):
    return {
        "investment_code": code, "investment": name, "asset_type": asset_type, "assetclass_l2": "Equity",
        "invested_amount": invested, "market_value": value, "holding_percent": 0, "total_pnl": value - invested,
        "pnl_per": 0, "total_units": units, "unit_price": round(value / units, 2), "broker": broker,
        "market_cap": None, "one_day_change": 0, "one_day_change_percentage": 0, **extra,
    }


IND_STOCK = [
    _row("INFY", "Infosys", "STOCK", 150000, 180000, 100, "INDmoney"),
    _row("TCS", "Tata Consultancy Services", "STOCK", 200000, 190000, 50, "INDmoney"),
    _row("HDFCBANK", "HDFC Bank", "STOCK", 0, 130000, 80, "Zerodha"),  # linked broker: no cost reported
]
MF = [
    _row("120503", "Parag Parikh Flexi Cap Fund", "MF", 300000, 375000, 4000.5, None),
    _row("118989", "HDFC Index Fund Nifty 50", "MF", 100000, 125000, 500, None),
]
US_STOCK = [
    _row("AAPL", "Apple", "US_STOCK", 80000, 100000, 5, "INDmoney",
         invested_value_usd=960.0, current_value_usd=1200.0, one_day_change_usd=12.0),
]

SNAPSHOT = {
    "total_invested": 980000, "total_current_value": 1250000, "total_networth": 1200000,
    "investments": [
        {"asset_type": "MF", "invested_value": 400000, "current_value": 500000, "return": 100000,
         "return_percentage": 25, "progress_value_percentage": 40},
        {"asset_type": "STOCK", "invested_value": 350000, "current_value": 500000, "return": 150000,
         "return_percentage": 42.86, "progress_value_percentage": 40},
        {"asset_type": "US_STOCK", "invested_value": 80000, "current_value": 100000, "return": 20000,
         "return_percentage": 25, "progress_value_percentage": 8},
        {"asset_type": "EPF", "invested_value": 150000, "current_value": 150000, "return": 0,
         "return_percentage": 0, "progress_value_percentage": 12},
    ],
    "assets": [{"assetclass_l2": "Equity", "invested_value": 830000, "current_value": 1100000, "return": 270000,
                "return_percentage": 32.53, "progress_value_percentage": 88}],
    "sector": [],
    "market_cap": [{"market_cap": "Large Cap", "invested_value": 430000, "current_value": 600000, "return": 170000,
                    "return_percentage": 39.53, "progress_value_percentage": 100}],
    "liabilities": {"total_loan_balance": 50000, "total_credit_card_due": 0, "total": 50000,
                    "loans": [], "credit_cards": []},
    "data_notes": {"sector": "No sector analytics."},
}


def holdings(rows, *, usd=False):
    summary = {
        "total_value": sum(row["market_value"] for row in rows), "invested": sum(row["invested_amount"] for row in rows),
        "one_day_change": 0, "one_day_change_percentage": 0,
    }
    if usd:
        summary.update(total_value_usd=sum(r["current_value_usd"] for r in rows),
                       invested_usd=sum(r["invested_value_usd"] for r in rows), one_day_change_usd=12.0)
    return {"holdings": rows, "asset_summary": summary, "source_status": {"holdings": "ok"}}


def results() -> dict[str, str]:
    """Stored snapshot results, keyed as ``capture_snapshot`` stores them.

    EPF is held but has no holdings response, which exercises the "not fetched" note.
    """
    return {
        "networth_snapshot": json.dumps(SNAPSHOT),
        "networth_holdings:IND_STOCK": json.dumps(holdings(IND_STOCK)),
        "networth_holdings:MF": json.dumps(holdings(MF)),
        "networth_holdings:US_STOCK": json.dumps(holdings(US_STOCK, usd=True)),
    }

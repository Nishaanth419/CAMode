"""Build self-consistent INDmoney snapshot results for comparison tests.

Totals are derived from the holdings rows, so a snapshot built here reconciles unless
a test deliberately breaks it. Rows use ``indmoney_sample``'s real field layout.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from tests.fixtures.indmoney_sample import _row, holdings

IST = timezone(timedelta(hours=5, minutes=30))


def row(code: str, name: str, asset_type: str, invested: float, value: float, units: float,
        broker: str | None = "INDmoney", **extra: Any) -> dict[str, Any]:
    return _row(code, name, asset_type, invested, value, units, broker, **extra)


def build_results(
    rows_by_type: dict[str, list[dict[str, Any]]],
    *,
    liabilities: float = 50000,
    extra_types: dict[str, tuple[float, float]] | None = None,
) -> dict[str, str]:
    """Snapshot results whose summary totals equal their holdings.

    ``extra_types`` maps an asset type to ``(invested, value)`` that is held but has no
    holdings response (like EPF in the sample).
    """
    investments = [
        {"asset_type": asset_type, "invested_value": sum(r["invested_amount"] for r in rows),
         "current_value": sum(r["market_value"] for r in rows)}
        for asset_type, rows in rows_by_type.items()
    ]
    investments += [{"asset_type": name, "invested_value": cost, "current_value": value}
                    for name, (cost, value) in (extra_types or {}).items()]
    current = sum(item["current_value"] for item in investments)
    summary = {
        "total_invested": sum(item["invested_value"] for item in investments),
        "total_current_value": current,
        "total_networth": current - liabilities,
        "investments": investments,
        "liabilities": {"total": liabilities},
    }
    results = {"networth_snapshot": json.dumps(summary)}
    for asset_type, rows in rows_by_type.items():
        has_usd = any("current_value_usd" in r for r in rows)
        results[f"networth_holdings:{asset_type}"] = json.dumps(holdings(rows, usd=has_usd))
    return results


def stored(as_of: str, results: dict[str, str], *, errors: dict[str, str] | None = None,
           networth: float | None = None) -> dict[str, Any]:
    """A stored snapshot as ``snapshots.latest_snapshot`` returns it, captured at 09:00 IST."""
    captured = datetime.fromisoformat(as_of).replace(hour=9, tzinfo=IST)
    return {"asOfDate": as_of, "capturedAt": int(captured.timestamp() * 1000), "networthInr": networth,
            "results": results, "errors": errors or {}}


def captured_at(as_of: str) -> datetime:
    return datetime.fromisoformat(as_of).replace(hour=9, tzinfo=IST)


def mutate_summary(results: dict[str, str], **changes: Any) -> dict[str, str]:
    """Return a copy of ``results`` with top-level summary fields replaced (to break consistency)."""
    summary = json.loads(results["networth_snapshot"])
    summary.update(changes)
    return {**results, "networth_snapshot": json.dumps(summary)}


def baseline_rows() -> dict[str, list[dict[str, Any]]]:
    """The earlier snapshot's holdings."""
    return {
        "IND_STOCK": [
            row("INFY", "Infosys", "IND_STOCK", 150000, 170000, 100),
            row("TCS", "Tata Consultancy Services", "IND_STOCK", 200000, 200000, 50),
            row("HDFCBANK", "HDFC Bank", "IND_STOCK", 0, 130000, 80, "Zerodha"),
        ],
        "MF": [row("120503", "Parag Parikh Flexi Cap Fund", "MF", 300000, 360000, 4000, None)],
    }


def later_rows() -> dict[str, list[dict[str, Any]]]:
    """The later snapshot: INFY up on price, TCS down on price, 500 more MF units at a higher NAV, a new fund."""
    return {
        "IND_STOCK": [
            row("INFY", "Infosys", "IND_STOCK", 150000, 180000, 100),
            row("TCS", "Tata Consultancy Services", "IND_STOCK", 200000, 190000, 50),
            row("HDFCBANK", "HDFC Bank", "IND_STOCK", 0, 130000, 80, "Zerodha"),
        ],
        "MF": [
            row("120503", "Parag Parikh Flexi Cap Fund", "MF", 345000, 414000, 4500, None),
            row("118989", "HDFC Index Fund Nifty 50", "MF", 25000, 25000, 100, None),
        ],
    }

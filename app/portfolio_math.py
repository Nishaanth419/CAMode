"""Deterministic portfolio figures computed from INDmoney's snapshot and holdings responses.

The model quotes these numbers instead of doing arithmetic itself. All sums use
``Decimal``; results are rounded to two decimals only when returned.

Known traps in INDmoney's data, handled here:

- Linked (non-INDmoney) brokers often report ``invested_amount`` as 0. Such rows are
  left out of P&L instead of showing a 100% gain, and are listed in ``data_notes``.
- US stocks carry INR and USD amounts. INR figures are used for portfolio totals;
  USD figures are reported separately and never added to INR.
- ``holding_percent`` from INDmoney is a share of the holding's asset type only, so
  shares of the whole portfolio are computed here instead.
"""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final

HOLDINGS_PREFIX: Final = "networth_holdings:"
# Fields kept per row, under shorter names, when every holding is listed (small context budgets).
COMPACT_HOLDING_FIELDS: Final = {
    "name": "name", "asset_type": "type", "broker": "broker", "current_value_inr": "value_inr",
    "invested_inr": "invested_inr", "pnl_inr": "pnl_inr", "pnl_pct": "pnl_pct",
    "share_of_portfolio_pct": "share_of_portfolio_pct", "current_value_usd": "value_usd",
}
TOP_HOLDINGS: Final = 10
# A per-asset-type total that differs from INDmoney's own summary by more than this is flagged.
RECONCILE_TOLERANCE: Final = Decimal("0.01")
_ZERO = Decimal(0)


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _money(value: Decimal | None) -> float | None:
    return None if value is None else float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _pct(part: Decimal, whole: Decimal) -> float | None:
    if not whole:
        return None
    return float((part * 100 / whole).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def inr(value: Decimal | float) -> str:
    """Format rupees with Indian digit grouping: 1234567.5 -> "₹12,34,567.50"."""
    amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign, (whole, fraction) = "-" if amount < 0 else "", f"{abs(amount):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = [head[max(0, end - 2):end] for end in range(len(head), 0, -2)][::-1]
        whole = ",".join(groups) + "," + tail
    return f"{sign}₹{whole}.{fraction}"


def _json(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def asset_types_held(snapshot_text: str | None) -> list[str]:
    """Asset types with a non-zero current value in a ``networth_snapshot`` response."""
    snapshot = _json(snapshot_text) or {}
    held = []
    for item in snapshot.get("investments") or []:
        if isinstance(item, dict) and item.get("asset_type") and (_dec(item.get("current_value")) or _ZERO) > 0:
            held.append(str(item["asset_type"]))
    return held


def _holding_rows(results: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Flatten every ``networth_holdings:<TYPE>`` result into rows plus each type's asset_summary."""
    rows: list[dict[str, Any]] = []
    summaries: dict[str, dict[str, Any]] = {}
    for key, text in results.items():
        if not key.startswith(HOLDINGS_PREFIX):
            continue
        asset_type = key[len(HOLDINGS_PREFIX):]
        parsed = _json(text) or {}
        if isinstance(parsed.get("asset_summary"), dict):
            summaries[asset_type] = parsed["asset_summary"]
        for row in parsed.get("holdings") or []:
            if isinstance(row, dict):
                rows.append({**row, "asset_type": row.get("asset_type") or asset_type})
    return rows, summaries


def _holding_metrics(row: dict[str, Any], portfolio_value: Decimal) -> dict[str, Any]:
    value = _dec(row.get("market_value")) or _ZERO
    invested = _dec(row.get("invested_amount"))
    has_cost = invested is not None and invested > 0
    item: dict[str, Any] = {
        "name": row.get("investment"),
        "code": row.get("investment_code"),
        "asset_type": row.get("asset_type"),
        # INDmoney's class for the row (for example "Equity"); None when the response has none.
        "asset_class": row.get("assetclass_l2") or None,
        "broker": row.get("broker") or None,
        "units": _money(_dec(row.get("total_units"))),
        "unit_price_inr": _money(_dec(row.get("unit_price"))),
        "current_value_inr": _money(value),
        "invested_inr": _money(invested) if has_cost else None,
        "pnl_inr": _money(value - invested) if has_cost else None,
        "pnl_pct": _pct(value - invested, invested) if has_cost else None,
        "share_of_portfolio_pct": _pct(value, portfolio_value),
        "one_day_change_inr": _money(_dec(row.get("one_day_change"))),
    }
    if row.get("current_value_usd") is not None:
        item["current_value_usd"] = _money(_dec(row.get("current_value_usd")))
        item["invested_usd"] = _money(_dec(row.get("invested_value_usd")))
    return item


def _split(snapshot: dict[str, Any], key: str, label_key: str, total: Decimal) -> list[dict[str, Any]]:
    """A snapshot breakdown list (asset classes, market caps) with shares of ``total``."""
    rows = [item for item in snapshot.get(key) or [] if isinstance(item, dict)]
    return [
        {label_key: item.get(label_key), "current_value_inr": _money(_dec(item.get("current_value")) or _ZERO),
         "share_pct": _pct(_dec(item.get("current_value")) or _ZERO, total)}
        for item in sorted(rows, key=lambda entry: _dec(entry.get("current_value")) or _ZERO, reverse=True)
    ]


def _portfolio_value(investments: list[dict[str, Any]], rows: list[dict[str, Any]]) -> Decimal:
    """The total that holding shares are measured against: INDmoney's per-type values, else the holdings."""
    value = sum((_dec(item.get("current_value")) or _ZERO for item in investments), _ZERO)
    return value or sum((_dec(row.get("market_value")) or _ZERO for row in rows), _ZERO)


def all_holdings(results: dict[str, str]) -> list[dict[str, Any]]:
    """Every holding with its full computed fields, largest first (for the app's Investments page)."""
    snapshot = _json(results.get("networth_snapshot")) or {}
    rows, _ = _holding_rows(results)
    investments = [item for item in snapshot.get("investments") or [] if isinstance(item, dict)]
    holdings = [_holding_metrics(row, _portfolio_value(investments, rows)) for row in rows]
    return sorted(holdings, key=lambda item: item["current_value_inr"] or 0, reverse=True)


def compute_metrics(results: dict[str, str], *, include_all_holdings: bool = False) -> dict[str, Any]:
    """Compute portfolio figures from a snapshot's stored tool results (keyed by tool and asset type).

    ``top_holdings`` lists the largest holdings. ``include_all_holdings`` instead lists every
    row with only ``COMPACT_HOLDING_FIELDS`` (renamed, nulls dropped) and leaves out the
    per-type, class and market-cap splits, so the result fits Groq's context budget.
    """
    snapshot = _json(results.get("networth_snapshot")) or {}
    rows, summaries = _holding_rows(results)
    notes: list[str] = []

    investments = [item for item in snapshot.get("investments") or [] if isinstance(item, dict)]
    portfolio_value = _portfolio_value(investments, rows)

    by_type = []
    for item in sorted(investments, key=lambda entry: _dec(entry.get("current_value")) or _ZERO, reverse=True):
        value = _dec(item.get("current_value")) or _ZERO
        invested = _dec(item.get("invested_value")) or _ZERO
        by_type.append({
            "asset_type": item.get("asset_type"),
            "current_value_inr": _money(value),
            "invested_inr": _money(invested) if invested > 0 else None,
            "share_of_portfolio_pct": _pct(value, portfolio_value),
        })

    holdings = [_holding_metrics(row, portfolio_value) for row in rows]
    holdings.sort(key=lambda item: item["current_value_inr"] or 0, reverse=True)

    with_cost = [item for item in holdings if item["invested_inr"] is not None]
    without_cost = [item for item in holdings if item["invested_inr"] is None]
    cost_value = sum((Decimal(str(item["current_value_inr"])) for item in with_cost), _ZERO)
    cost_invested = sum((Decimal(str(item["invested_inr"])) for item in with_cost), _ZERO)
    if without_cost:
        notes.append(
            f"{len(without_cost)} holding(s) worth {inr(sum((Decimal(str(i['current_value_inr'])) for i in without_cost), _ZERO))} "
            "have no invested amount (usually linked brokers), so they are left out of P&L."
        )

    brokers: dict[str, dict[str, Decimal | int]] = defaultdict(lambda: {"value": _ZERO, "count": 0})
    for item in holdings:
        label = item["broker"] or ("Mutual funds (no broker listed)" if item["asset_type"] == "MF" else "Broker not listed")
        brokers[label]["value"] += Decimal(str(item["current_value_inr"] or 0))
        brokers[label]["count"] += 1
    holdings_value = sum((entry["value"] for entry in brokers.values()), _ZERO)
    by_broker = [
        {"broker": label, "holdings": entry["count"], "current_value_inr": _money(entry["value"]),
         "share_of_listed_holdings_pct": _pct(entry["value"], holdings_value)}
        for label, entry in sorted(brokers.items(), key=lambda pair: pair[1]["value"], reverse=True)
    ]

    for asset_type, summary in summaries.items():
        reported = _dec(summary.get("total_value"))
        summed = sum((_dec(row.get("market_value")) or _ZERO for row in rows if row["asset_type"] == asset_type), _ZERO)
        if reported and abs(summed - reported) > reported * RECONCILE_TOLERANCE:
            notes.append(f"{asset_type} holdings add up to {inr(summed)} but INDmoney reports {inr(reported)}.")
    held_types = {item.get("asset_type") for item in investments if (_dec(item.get("current_value")) or _ZERO) > 0}
    missing = sorted(str(t) for t in held_types - set(summaries))
    if missing:
        notes.append("Holdings were not fetched for: " + ", ".join(missing) + "; broker and per-holding figures exclude them.")

    usd = {
        asset_type: {"current_value_usd": _money(_dec(summary.get("total_value_usd"))),
                     "invested_usd": _money(_dec(summary.get("invested_usd")))}
        for asset_type, summary in summaries.items() if summary.get("total_value_usd") is not None
    }
    if usd:
        notes.append("USD amounts are shown separately and are never added to INR totals.")

    liabilities = snapshot.get("liabilities") if isinstance(snapshot.get("liabilities"), dict) else {}
    top = holdings[:TOP_HOLDINGS]
    stock_value = sum((_dec(item.get("current_value")) or _ZERO for item in snapshot.get("market_cap") or []
                       if isinstance(item, dict)), _ZERO)
    metrics = {
        "totals": {
            "net_worth_inr": _money(_dec(snapshot.get("total_networth"))),
            "current_value_inr": _money(_dec(snapshot.get("total_current_value"))),
            "invested_inr": _money(_dec(snapshot.get("total_invested"))),
            "liabilities_inr": _money(_dec(liabilities.get("total"))),
        },
        "pnl": {
            "basis": "holdings with a reported invested amount",
            "holdings_counted": len(with_cost),
            "invested_inr": _money(cost_invested),
            "current_value_inr": _money(cost_value),
            "pnl_inr": _money(cost_value - cost_invested),
            "pnl_pct": _pct(cost_value - cost_invested, cost_invested),
            "holdings_excluded": len(without_cost),
        },
        "by_asset_type": by_type,
        "by_asset_class": _split(snapshot, "assets", "assetclass_l2", portfolio_value),
        "by_market_cap_direct_stocks": _split(snapshot, "market_cap", "market_cap", stock_value),
        "by_broker": by_broker,
        "concentration": {
            "holdings_count": len(holdings),
            "largest_holding": top[0]["name"] if top else None,
            "largest_holding_pct": top[0]["share_of_portfolio_pct"] if top else None,
            "top_5_pct": _pct(sum((Decimal(str(i["current_value_inr"])) for i in holdings[:5]), _ZERO), portfolio_value),
            "top_10_pct": _pct(sum((Decimal(str(i["current_value_inr"])) for i in top), _ZERO), portfolio_value),
        },
        "top_holdings": top,
        "usd": usd,
        "data_notes": notes,
    }
    if include_all_holdings:
        for key in ("top_holdings", "by_asset_type", "by_asset_class", "by_market_cap_direct_stocks"):
            del metrics[key]
        metrics["all_holdings"] = [
            {short: item[field] for field, short in COMPACT_HOLDING_FIELDS.items() if item.get(field) is not None}
            for item in holdings
        ]
    return metrics

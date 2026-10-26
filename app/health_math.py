"""Deterministic figures for the portfolio health check.

Inputs are INDmoney responses (layouts recorded 2026-10-26): ``portfolio_math.compute_metrics()``
over ``networth_snapshot`` and ``networth_holdings:<TYPE>``, ``get_mf_funds_details`` with
``includes: ["fund_performance", "sector_allocation"]`` (MF holdings' ``investment_code`` is accepted
as its ``fund_ids``), and the local ``get_networth_history`` points.

- Concentration: each holding's share of the portfolio's total current value (INDmoney's per-asset-type
  values summed, the same base as ``share_of_portfolio_pct``). A share above the threshold is an alert
  about size, not a recommendation.
- Sectors: INDmoney has no sector data for holdings (its sector rows are always empty), so sector
  exposure is a mutual fund look-through only: fund value × its equity share × each sector's share of
  that equity. Directly held stocks are unclassified, so a sector share is a lower bound, and the same
  company can sit in several funds and in direct holdings (overlap is possible and not netted).
- Funds vs benchmark: ``fund_performance`` gives the fund's, its benchmark's and its category's return
  for the same periods. A fund is flagged only when its return trails its benchmark by more than
  ``UNDERPERFORMANCE_MARGIN_PP`` over ``PRIMARY_PERIOD`` (``FALLBACK_PERIOD`` when the primary is missing).
- Trend: only from saved snapshots. Current versus invested value is the unrealised gain or loss today,
  not a trend.
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from .options_math import HUNDRED, dec

CONCENTRATION_THRESHOLD_PCT: Final = Decimal(10)
UNDERPERFORMANCE_MARGIN_PP: Final = Decimal("0.5")
PRIMARY_PERIOD: Final = "3Y"
FALLBACK_PERIOD: Final = "1Y"
PERIODS: Final = ("1Y", "3Y", "5Y")


def _round(value: Decimal | None) -> float | None:
    return None if value is None else float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def concentration(metrics: dict[str, Any], threshold: Decimal = CONCENTRATION_THRESHOLD_PCT) -> dict[str, Any]:
    """Holdings and asset types above ``threshold`` % of the portfolio, from ``compute_metrics`` figures."""
    holdings = metrics.get("top_holdings") or []
    flagged = [
        {"name": item.get("name"), "assetType": item.get("asset_type"), "currentValueInr": item.get("current_value_inr"),
         "sharePct": item.get("share_of_portfolio_pct")}
        for item in holdings if (dec(item.get("share_of_portfolio_pct")) or 0) > threshold
    ]
    types = [
        {"assetType": item.get("asset_type"), "currentValueInr": item.get("current_value_inr"),
         "sharePct": item.get("share_of_portfolio_pct")}
        for item in metrics.get("by_asset_type") or []
    ]
    return {
        "thresholdPct": float(threshold),
        "denominator": "total current value of every asset type INDmoney reports (net worth before liabilities)",
        "denominatorInr": (metrics.get("totals") or {}).get("current_value_inr"),
        "holdingsAboveThreshold": flagged,
        "assetTypes": types,
        "top5Pct": (metrics.get("concentration") or {}).get("top_5_pct"),
        "holdingsCount": (metrics.get("concentration") or {}).get("holdings_count"),
    }


def _period_values(raw: Any) -> dict[str, Decimal]:
    return {key: value for key, value in ((k, dec(v)) for k, v in (raw or {}).items()) if value is not None} if isinstance(raw, dict) else {}


def parse_fund_details(text: str) -> dict[str, dict[str, Any]]:
    """``get_mf_funds_details`` as {fund id: {name, category, benchmark, returns, benchmarkReturns, categoryReturns,
    categoryRank, equityPct, sectors}}; a fund missing a section simply lacks those keys."""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    funds: dict[str, dict[str, Any]] = {}
    for item in (value.get("data") if isinstance(value, dict) else None) or []:
        if not isinstance(item, dict) or item.get("fund_id") is None:
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        detail = data.get("fund_detail") if isinstance(data.get("fund_detail"), dict) else {}
        performance = data.get("fund_performance") if isinstance(data.get("fund_performance"), dict) else {}
        fund: dict[str, Any] = {
            "name": detail.get("name"), "category": detail.get("category"), "benchmark": detail.get("benchmark_name"),
            "returns": _period_values(performance.get("returns")),
            "benchmarkReturns": _period_values(performance.get("benchmark_returns")),
            "categoryReturns": _period_values(performance.get("category_returns")),
            "categoryRank": {k: str(v) for k, v in (performance.get("category_rank") or {}).items()} if isinstance(performance.get("category_rank"), dict) else {},
        }
        groups = data.get("sector_allocation")
        if isinstance(groups, list):
            for group in groups:
                if not isinstance(group, dict) or str(group.get("name", "")).lower() != "equity":
                    continue
                # "perc" is the displayed text; "percValue" is sometimes rounded differently ("0.6%" vs 1).
                fund["equityPct"] = dec(group.get("perc")) if dec(group.get("perc")) is not None else dec(group.get("PercentageVal"))
                fund["sectors"] = {
                    str(sector.get("name")): share for sector in group.get("sectors") or []
                    if isinstance(sector, dict) and sector.get("name")
                    and (share := dec(sector.get("perc")) if dec(sector.get("perc")) is not None else dec(sector.get("percValue"))) is not None
                }
        funds[str(item["fund_id"])] = fund
    return funds


def fund_check(code: str, holding_value: Any, fund: dict[str, Any] | None) -> dict[str, Any]:
    """One fund against its benchmark and category over the same period, or why it could not be checked."""
    if fund is None:
        return {"code": code, "status": "not_checked", "reason": "INDmoney returned no details for this fund"}
    returns, benchmark, category = fund["returns"], fund["benchmarkReturns"], fund["categoryReturns"]
    period = next((p for p in (PRIMARY_PERIOD, FALLBACK_PERIOD) if p in returns and p in benchmark), None)
    base = {"code": code, "name": fund.get("name"), "category": fund.get("category"), "benchmark": fund.get("benchmark"),
            "currentValueInr": holding_value}
    if period is None:
        return {**base, "status": "not_checked",
                "reason": f"no fund and benchmark return for the same period ({PRIMARY_PERIOD} or {FALLBACK_PERIOD})"}
    gap = returns[period] - benchmark[period]
    status = "underperforming" if gap < -UNDERPERFORMANCE_MARGIN_PP else ("in_line" if gap <= UNDERPERFORMANCE_MARGIN_PP else "outperforming")
    by_period = {
        p: {"fundPct": _round(returns.get(p)), "benchmarkPct": _round(benchmark.get(p)), "categoryAveragePct": _round(category.get(p)),
            "categoryRank": fund["categoryRank"].get(p)}
        for p in PERIODS if p in returns
    }
    return {
        **base, "status": status, "period": period, "metric": "return over the period as INDmoney reports it",
        "fundPct": _round(returns[period]), "benchmarkPct": _round(benchmark[period]), "gapVsBenchmarkPp": _round(gap),
        "categoryAveragePct": _round(category.get(period)),
        "gapVsCategoryPp": _round(returns[period] - category[period]) if period in category else None,
        "byPeriod": by_period,
    }


def sector_lookthrough(holdings: list[dict[str, Any]], funds: dict[str, dict[str, Any]], total_value: Any,
                       threshold: Decimal = CONCENTRATION_THRESHOLD_PCT) -> dict[str, Any]:
    """Sector exposure through mutual funds (see the module docstring), as INR and % of the whole portfolio."""
    total = dec(total_value)
    exposure: dict[str, Decimal] = {}
    covered, uncovered = Decimal(0), []
    for row in holdings:
        if row.get("asset_type") != "MF":
            continue
        fund, value = funds.get(str(row.get("code"))), dec(row.get("current_value_inr"))
        if value is None:
            continue
        if not fund or fund.get("equityPct") is None or not fund.get("sectors"):
            uncovered.append(row.get("name"))
            continue
        covered += value
        for sector, share in fund["sectors"].items():
            exposure[sector] = exposure.get(sector, Decimal(0)) + value * fund["equityPct"] / HUNDRED * share / HUNDRED
    direct = sum((dec(row.get("current_value_inr")) or Decimal(0) for row in holdings if row.get("asset_type") in ("IND_STOCK", "US_STOCK")), Decimal(0))
    sectors = sorted(
        ({"sector": name, "exposureInr": _round(amount), "sharePct": _round(amount * HUNDRED / total) if total else None}
         for name, amount in exposure.items()),
        key=lambda item: item["exposureInr"] or 0, reverse=True,
    )
    return {
        "basis": "mutual fund look-through: fund value × equity share × sector share of equity",
        "denominatorInr": _round(total), "fundValueCoveredInr": _round(covered),
        "fundsWithoutSectorData": [name for name in uncovered if name],
        "directStocksUnclassifiedInr": _round(direct),
        "sectors": sectors[:12],
        "sectorsAboveThreshold": [item for item in sectors if (dec(item["sharePct"]) or 0) > threshold],
        "caveats": [
            "INDmoney returns no sector for directly held stocks, so these shares are lower bounds.",
            "The same company can be held by several funds and directly; overlapping exposure is not netted.",
        ],
    }


def unrealised(metrics: dict[str, Any]) -> dict[str, Any]:
    pnl = metrics.get("pnl") or {}
    return {
        "label": "unrealised gain or loss today (current value against invested amount), not a trend",
        "investedInr": pnl.get("invested_inr"), "currentValueInr": pnl.get("current_value_inr"),
        "gainInr": pnl.get("pnl_inr"), "gainPct": pnl.get("pnl_pct"),
        "holdingsCounted": pnl.get("holdings_counted"), "holdingsWithoutCost": pnl.get("holdings_excluded"),
    }


def history_trend(text: str | None) -> dict[str, Any]:
    """Net-worth change between the first and last saved snapshots with a readable value."""
    try:
        value = json.loads(text) if text else None
    except ValueError:
        value = None
    points = [p for p in (value.get("points") if isinstance(value, dict) else None) or []
              if isinstance(p, dict) and dec(p.get("networthInr")) is not None]
    if len(points) < 2:
        return {"available": False, "reason": "fewer than two saved snapshots with a readable net worth"}
    first, last = points[0], points[-1]
    start, end = dec(first["networthInr"]), dec(last["networthInr"])
    return {
        "available": True, "from": first.get("date"), "to": last.get("date"), "snapshots": len(points),
        "startInr": _round(start), "endInr": _round(end), "changeInr": _round(end - start),
        "changePct": _round((end - start) * HUNDRED / start) if start else None,
        "note": "Net worth includes new money added or withdrawn, so a change is not an investment return.",
    }

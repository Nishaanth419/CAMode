"""Workflow B: a portfolio health check from live INDmoney holdings and fund performance.

Waves: ``networth_snapshot`` and the saved net-worth history (when this profile has one) → holdings for
each asset type the snapshot shows as held → ``get_mf_funds_details`` (fund performance and sector split)
for the mutual funds held, ten per call. Figures come from ``portfolio_math`` and ``health_math``.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Final

from .. import health_math, portfolio_math
from .base import Calculation, Check, StepSpec, Workflow, WorkflowState, failure, ok_text, planned

# networth_holdings' asset types except INSURANCE and RD, which INDmoney does not serve (snapshots.HOLDINGS_ASSET_TYPES).
SERVED_TYPES: Final = ("IND_STOCK", "MF", "US_STOCK", "BOND", "EPF", "NPS", "SA", "FD", "CRYPTO", "VEHICLE", "RE", "AIF", "PMS", "PPF")
MAX_HOLDING_TYPES: Final = 8
FUNDS_PER_CALL: Final = 10
MAX_FUND_CALLS: Final = 3
HISTORY_DAYS: Final = 365


def _parse_inputs(message: str) -> dict[str, Any]:
    threshold = re.search(r"(?:above|over|more than|>)\s*(\d{1,2})\s*%", message.lower())
    return {"thresholdPct": threshold.group(1) if threshold else str(health_math.CONCENTRATION_THRESHOLD_PCT)}


class HealthCheck(Workflow):
    name = "portfolio_health_check"
    title = "Portfolio health check"
    required_tools = frozenset({"networth_snapshot", "networth_holdings"})
    needs_tools = frozenset({"networth_snapshot", "networth_holdings", "get_mf_funds_details"})
    required_outputs = ("concentration", "fund_checks", "unrealised", "trend")
    max_tool_calls = 2 + MAX_HOLDING_TYPES + MAX_FUND_CALLS
    max_waves = 4
    max_wave_steps = MAX_HOLDING_TYPES
    narration_rules = (
        "Workflow: portfolio health check. Lead with the priorities list in order. For concentration, give each share "
        "with its denominator and call it an alert about size, not a recommendation to sell. Call a fund underperforming "
        "only when its status is underperforming, and then name the period, the metric, its benchmark and both returns; "
        "list funds that could not be checked with the reason. Present sector shares as a mutual fund look-through and "
        "lower bound, noting possible overlap. Present current vs invested value as today's unrealised gain or loss, and "
        "a trend only from the saved-snapshot history (say so when it is unavailable)."
    )

    def inputs(self, message: str) -> dict[str, Any]:
        return _parse_inputs(message)

    def next_steps(self, state: WorkflowState) -> list[StepSpec]:
        evidence = state.get("evidence") or {}
        if not planned(state, "networth"):
            steps = [StepSpec("networth", "networth_snapshot", {}, purpose="Totals and the asset types held")]
            if "get_networth_history" in (state.get("offered") or []):
                steps.append(StepSpec("history", "get_networth_history", {"days": HISTORY_DAYS},
                                      purpose="Net worth from saved daily snapshots, for the trend"))
            return steps
        snapshot = ok_text(state, "networth")
        if snapshot is None:
            return []
        if not any(step["key"].startswith("holdings:") for step in state.get("steps") or []):
            held = [t for t in portfolio_math.asset_types_held(snapshot) if t in SERVED_TYPES][:MAX_HOLDING_TYPES]
            return [StepSpec(f"holdings:{t}", "networth_holdings", {"asset_type": t}, after=("networth",),
                             purpose=f"{t} holdings") for t in held]
        codes = evidence.get("fundCodes") or []
        if codes and not planned(state, "funds:1"):
            batches = [codes[i:i + FUNDS_PER_CALL] for i in range(0, len(codes), FUNDS_PER_CALL)][:MAX_FUND_CALLS]
            return [StepSpec(f"funds:{n}", "get_mf_funds_details",
                             {"fund_ids": ",".join(batch), "includes": ["fund_performance", "sector_allocation"]},
                             after=("holdings:MF",), purpose="Fund vs benchmark returns and sector split")
                    for n, batch in enumerate(batches, start=1)]
        return []

    def _results(self, state: WorkflowState) -> dict[str, str]:
        results: dict[str, str] = {}
        if (text := ok_text(state, "networth")) is not None:
            results["networth_snapshot"] = text
        for step in state.get("steps") or []:
            if step["key"].startswith("holdings:") and (text := ok_text(state, step["key"])) is not None:
                results[portfolio_math.HOLDINGS_PREFIX + step["key"].split(":", 1)[1]] = text
        return results

    def normalize(self, state: WorkflowState) -> tuple[dict[str, Any], list[Check]]:
        evidence: dict[str, Any] = {}
        results = self._results(state)
        if any(key.startswith(portfolio_math.HOLDINGS_PREFIX) for key in results):
            holdings = portfolio_math.all_holdings(results)
            evidence["holdings"] = holdings
            codes = [str(h["code"]) for h in holdings if h.get("asset_type") == "MF" and h.get("code")]
            evidence["fundCodes"] = list(dict.fromkeys(codes))
        funds: dict[str, dict[str, Any]] = {}
        for step in state.get("steps") or []:
            if step["key"].startswith("funds:") and (text := ok_text(state, step["key"])) is not None:
                funds.update(health_math.parse_fund_details(text))
        if funds:
            evidence["funds"] = funds
        return evidence, []

    def calculate(self, state: WorkflowState) -> list[Calculation]:
        results = self._results(state)
        if "networth_snapshot" not in results:
            return []
        evidence = state.get("evidence") or {}
        threshold = Decimal(state["inputs"]["thresholdPct"])
        metrics = portfolio_math.compute_metrics(results)
        holdings = evidence.get("holdings") or []
        funds = evidence.get("funds") or {}
        fund_checks = [health_math.fund_check(code, next((h["current_value_inr"] for h in holdings if str(h.get("code")) == code), None),
                                              funds.get(code)) for code in evidence.get("fundCodes") or []]
        total = (metrics.get("totals") or {}).get("current_value_inr")
        history_text = ok_text(state, "history")
        return [
            {"name": "concentration", "inputs": {"thresholdPct": str(threshold), "holdings": len(holdings)},
             "outputs": health_math.concentration(metrics, threshold)},
            {"name": "fund_vs_benchmark", "inputs": {"funds": len(fund_checks), "period": health_math.PRIMARY_PERIOD,
                                                     "marginPp": str(health_math.UNDERPERFORMANCE_MARGIN_PP)},
             "outputs": {"funds": fund_checks}},
            {"name": "sector_lookthrough", "inputs": {"funds": len(funds)},
             "outputs": health_math.sector_lookthrough(holdings, funds, total, threshold)},
            {"name": "unrealised_gain", "inputs": {}, "outputs": health_math.unrealised(metrics)},
            {"name": "networth_trend", "inputs": {"days": HISTORY_DAYS},
             "outputs": health_math.history_trend(history_text) if history_text is not None
             else {"available": False, "reason": failure(state, "history") or "this profile has no saved snapshot history"},
             "notes": metrics.get("data_notes") or []},
        ]

    def validate(self, state: WorkflowState) -> list[Check]:
        checks: list[Check] = []
        if failure(state, "networth"):
            return [{"check": "portfolio_data", "outcome": "failed", "detail": failure(state, "networth") or ""}]
        missing_types = [s["key"].split(":", 1)[1] for s in state.get("steps") or [] if s["key"].startswith("holdings:") and failure(state, s["key"])]
        checks.append({"check": "holdings_coverage", "outcome": "warning" if missing_types else "passed",
                       "detail": f"holdings not fetched for {missing_types}" if missing_types else "holdings fetched for every held type"})
        calcs = {c["name"]: c for c in state.get("calculations") or []}
        funds = ((calcs.get("fund_vs_benchmark") or {}).get("outputs") or {}).get("funds") or []
        unchecked = [f.get("name") or f["code"] for f in funds if f["status"] == "not_checked"]
        checks.append({"check": "benchmark_comparison", "outcome": "warning" if unchecked else "passed",
                       "detail": f"not compared (no matching period or no data): {unchecked}" if unchecked else f"{len(funds)} fund(s) compared over the same period"})
        codes = (state.get("evidence") or {}).get("fundCodes") or []
        if len(codes) > FUNDS_PER_CALL * MAX_FUND_CALLS:
            checks.append({"check": "fund_coverage", "outcome": "warning", "detail": f"only the first {FUNDS_PER_CALL * MAX_FUND_CALLS} of {len(codes)} funds were checked"})
        return checks

    def outputs(self, state: WorkflowState) -> dict[str, bool]:
        names = {c["name"] for c in state.get("calculations") or []}
        return {"concentration": "concentration" in names, "fund_checks": "fund_vs_benchmark" in names,
                "unrealised": "unrealised_gain" in names, "trend": "networth_trend" in names}

    def payload(self, state: WorkflowState) -> dict[str, Any]:
        calcs = {c["name"]: c.get("outputs") or {} for c in state.get("calculations") or []}
        concentration = calcs.get("concentration") or {}
        funds = (calcs.get("fund_vs_benchmark") or {}).get("funds") or []
        sectors = calcs.get("sector_lookthrough") or {}
        priorities: list[str] = []
        for item in concentration.get("holdingsAboveThreshold") or []:
            priorities.append(f"Concentration alert: {item['name']} is {item['sharePct']}% of total current value")
        lagging = sorted((f for f in funds if f["status"] == "underperforming"), key=lambda f: f["gapVsBenchmarkPp"])
        for fund in lagging:
            priorities.append(f"{fund['name']} trails {fund['benchmark']} by {abs(fund['gapVsBenchmarkPp'])} points over {fund['period']}")
        for item in sectors.get("sectorsAboveThreshold") or []:
            priorities.append(f"Sector exposure via funds: {item['sector']} at least {item['sharePct']}% of the portfolio")
        trend = calcs.get("networth_trend") or {}
        return {
            "workflow": self.title, "priorities": priorities or ["No holding, fund or sector crossed the alert rules."],
            "concentration": concentration,
            "fundsVsBenchmark": {"rule": f"underperforming = fund return below its benchmark by more than "
                                         f"{health_math.UNDERPERFORMANCE_MARGIN_PP} points over {health_math.PRIMARY_PERIOD} "
                                         f"({health_math.FALLBACK_PERIOD} when {health_math.PRIMARY_PERIOD} is missing)",
                                 "funds": [{k: v for k, v in f.items() if k != "byPeriod"} for f in funds]},
            "sectorLookthrough": sectors, "unrealised": calcs.get("unrealised_gain") or {},
            "networthTrend": trend, "dataNotes": next((c.get("notes") for c in state.get("calculations") or [] if c["name"] == "networth_trend"), []),
        }

    def fallback_report(self, payload: dict[str, Any]) -> str:
        lines = [f"**{self.title}**"] + [f"- {item}" for item in payload["priorities"]]
        unrealised = payload.get("unrealised") or {}
        if unrealised.get("gainInr") is not None:
            lines.append(f"- Unrealised gain/loss today: ₹{unrealised['gainInr']} ({unrealised['gainPct']}%) on ₹{unrealised['investedInr']} invested")
        trend = payload.get("networthTrend") or {}
        lines.append(f"- Net worth {trend['from']} to {trend['to']}: ₹{trend['changeInr']} ({trend['changePct']}%)" if trend.get("available")
                     else f"- No net-worth trend: {trend.get('reason', 'unavailable')}")
        if payload.get("missingData"):
            lines.append("- Unavailable: " + ", ".join(payload["missingData"]))
        lines.append("\nThis is information, not investment advice.")
        return "\n".join(lines)

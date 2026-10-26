"""Workflow D: a Nifty bear put spread for the next monthly expiry that hedges a stated downside move.

Waves: Nifty lookup and the live net-worth snapshot (the equity it can verify) → nearest chain → monthly
chain (when different) → the two legs' details.

- Legs: buy the put nearest the current Nifty level; sell the put nearest the level after the stated fall
  (5% unless the message says), so the spread pays off across the first part of that fall.
- Exposure: the amount the user states (₹50 lakh...) is used for sizing and kept apart from the equity
  INDmoney reports; without a stated amount, the reported equity value is used and labelled.
- Beta is an assumption no tool supplies; contract counts are shown for betas 0.8, 1.0 and 1.2 (or the
  one the user gives), each labelled. The hedge never tracks a portfolio of single stocks exactly.
"""

from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal
from typing import Any, Final

from .. import options_math as om
from .. import portfolio_math
from . import nifty
from .base import Calculation, Check, StepResult, StepSpec, Workflow, WorkflowState, failure, ok_text, planned

DEFAULT_MOVE_PCT: Final = Decimal(5)
DEFAULT_BETAS: Final = (Decimal("0.8"), Decimal("1.0"), Decimal("1.2"))
_UNITS: Final = {"lakh": Decimal(100000), "lakhs": Decimal(100000), "lac": Decimal(100000), "lacs": Decimal(100000),
                 "l": Decimal(100000), "crore": Decimal(10000000), "crores": Decimal(10000000), "cr": Decimal(10000000)}


def stated_exposure(message: str) -> Decimal | None:
    """The rupee amount the user says they hold ("₹50 lakh", "Rs 1.2 crore", "₹5,000,000"); ``None`` when absent."""
    match = re.search(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d+)?)\s*(lakhs?|lacs?|crores?|cr|l)?\b", message, re.IGNORECASE)
    if not match:
        match = re.search(r"\b([\d,]+(?:\.\d+)?)\s*(lakhs?|lacs?|crores?|cr)\b", message, re.IGNORECASE)
    if not match:
        return None
    amount = om.dec(match.group(1))
    if amount is None:
        return None
    return amount * _UNITS.get((match.group(2) or "").lower(), Decimal(1))


def _parse_inputs(message: str) -> dict[str, Any]:
    text = message.lower()
    move = re.search(r"(\d{1,2}(?:\.\d+)?)\s*%\s*(?:downside|down|fall|drop|decline|correction|move)", text)
    beta = re.search(r"beta\s*(?:of|=|is)?\s*(\d(?:\.\d+)?)", text)
    exposure = stated_exposure(message)
    return {"movePct": str(Decimal(move.group(1)) if move else DEFAULT_MOVE_PCT),
            "statedExposureInr": str(exposure) if exposure is not None else None,
            "betas": [beta.group(1)] if beta else [str(b) for b in DEFAULT_BETAS],
            "betaGiven": bool(beta)}


class PutSpreadHedge(Workflow):
    name = "nifty_put_spread_hedge"
    title = "Nifty put-spread hedge"
    required_tools = frozenset({"lookup_ind_keys", "get_indian_stocks_option_chain", "get_indian_stocks_details"})
    needs_tools = required_tools | {"networth_snapshot"}
    required_outputs = ("legs", "net_debit", "breakeven", "max_payoff", "scenario", "sizing")
    max_tool_calls = 7
    max_waves = 5
    narration_rules = (
        "Workflow: Nifty put-spread hedge. Give the two legs (strike, premium and how it was priced), net debit, "
        "breakeven, maximum payoff and maximum profit in points and rupees per lot, then what the spread pays at the "
        "stated fall at expiry. Show gross protection, hedge cost and net protection separately for each assumed beta, "
        "and say beta is an assumption the tools cannot supply. Keep the exposure the user stated apart from the equity "
        "INDmoney reports, and say sector claims could not be checked when sectorVerified is false. Say plainly that a "
        "Nifty hedge does not track a portfolio of individual stocks exactly, and that the maximum spread payoff is not "
        "the same as the portfolio loss it offsets."
    )

    def inputs(self, message: str) -> dict[str, Any]:
        return _parse_inputs(message)

    def next_steps(self, state: WorkflowState) -> list[StepSpec]:
        if not planned(state, "lookup"):
            return [nifty.lookup_spec(), StepSpec("networth", "networth_snapshot", {},
                                                  purpose="Equity the account holds, to compare with the stated exposure")]
        steps = nifty.chain_steps(state)
        if steps:
            return steps
        legs = (state.get("evidence") or {}).get("legs")
        if not legs or planned(state, "leg_details"):
            return []
        chain_key = "chain_monthly" if planned(state, "chain_monthly") else "chain_nearest"
        return [StepSpec("leg_details", "get_indian_stocks_details",
                         {"ind_keys": [legs[name].ind_key for name in ("longPut", "shortPut") if legs[name].ind_key]},
                         after=(chain_key,), purpose="Live bid/ask, expiry and underlying of both legs")]

    def fix(self, state: WorkflowState, result: StepResult) -> StepSpec | None:
        return nifty.fix_chain(state, result)

    def normalize(self, state: WorkflowState) -> tuple[dict[str, Any], list[Check]]:
        evidence, checks = nifty.normalize_chain(state)
        merged = {**(state.get("evidence") or {}), **evidence}
        if "portfolio" not in merged and (text := ok_text(state, "networth")) is not None:
            evidence["portfolio"] = portfolio_math.compute_metrics({"networth_snapshot": text})
        chain: om.Chain | None = merged.get("chain")
        if chain is not None and "legs" not in merged:
            move = Decimal(state["inputs"]["movePct"])
            if chain.spot is None:
                evidence["legs"] = None
                checks.append({"check": "leg_selection", "outcome": "failed", "detail": "the chain has no Nifty level"})
            else:
                long_put = om.nearest_strike(chain, "put", chain.spot)
                short_put = om.nearest_strike(chain, "put", chain.spot * (1 - move / om.HUNDRED))
                if long_put is None or short_put is None or short_put.strike >= long_put.strike:
                    evidence["legs"] = None
                    checks.append({"check": "leg_selection", "outcome": "failed",
                                   "detail": "the chain has no two priced puts for the spread"})
                else:
                    evidence["legs"] = {"longPut": long_put, "shortPut": short_put}
        if (text := ok_text(state, "leg_details")) is not None and "legDetails" not in merged:
            evidence["legDetails"] = om.parse_leg_details(text)
        return evidence, checks

    def _priced(self, state: WorkflowState) -> tuple[dict[str, om.Quote], dict[str, str]]:
        evidence = state["evidence"]
        details: dict[str, om.LegDetail] = evidence.get("legDetails") or {}
        legs, sources = {}, {}
        for name in ("longPut", "shortPut"):
            quote: om.Quote = evidence["legs"][name]
            detail = details.get(quote.ind_key or "")
            if detail is not None and detail.premium is not None:
                legs[name] = replace(quote, bid=detail.bid, ask=detail.ask, last=detail.last,
                                     delta=detail.delta if detail.delta is not None else quote.delta)
                sources[name] = f"leg details ({detail.premium_source})"
            else:
                legs[name], sources[name] = quote, f"option chain ({quote.premium_source})"
        return legs, sources

    def _exposure(self, state: WorkflowState) -> tuple[Decimal | None, str]:
        stated = state["inputs"].get("statedExposureInr")
        if stated:
            return Decimal(stated), "stated by you"
        equity = self._verified_equity(state)
        return (Decimal(str(equity)), "equity INDmoney reports") if equity else (None, "unknown")

    def _verified_equity(self, state: WorkflowState) -> float | None:
        portfolio = (state.get("evidence") or {}).get("portfolio") or {}
        return next((row["current_value_inr"] for row in portfolio.get("by_asset_class") or []
                     if str(row.get("assetclass_l2", "")).lower() == "equity"), None)

    def calculate(self, state: WorkflowState) -> list[Calculation]:
        evidence = state.get("evidence") or {}
        chain: om.Chain | None = evidence.get("chain")
        if not evidence.get("legs") or chain is None or chain.spot is None:
            return []
        inputs = state["inputs"]
        legs, sources = self._priced(state)
        lots = chain.lot_sizes()
        lot_size = next(iter(lots)) if len(lots) == 1 else None
        move = Decimal(inputs["movePct"])
        spread = om.put_spread(legs["longPut"], legs["shortPut"], spot=chain.spot, move_pct=move, lot_size=lot_size)
        calcs: list[Calculation] = [{
            "name": "put_spread",
            "inputs": {"spot": om.points(chain.spot), "movePct": inputs["movePct"], "lotSize": lot_size,
                       **{name: {"strike": om.points(q.strike), "premium": om.points(q.premium), "source": sources[name]}
                          for name, q in legs.items()}},
            "outputs": spread.figures, "problems": spread.problems, "notes": spread.notes,
        }]
        exposure, basis = self._exposure(state)
        if exposure is not None and not spread.problems:
            debit = legs["longPut"].premium - legs["shortPut"].premium
            gross = Decimal(str(spread.figures["grossPayoffAtScenarioPoints"]))
            sizing = om.hedge_sizing(exposure=exposure, spot=chain.spot, lot_size=lot_size, move_pct=move, debit=debit,
                                     gross_at_scenario=gross, betas=[Decimal(b) for b in inputs["betas"]])
            calcs.append({"name": "hedge_sizing", "inputs": {"exposureInr": om.rupees(exposure), "exposureBasis": basis,
                                                             "betas": inputs["betas"], "lotSize": lot_size},
                          "outputs": sizing.figures, "problems": sizing.problems, "notes": sizing.notes})
        else:
            calcs.append({"name": "hedge_sizing", "inputs": {}, "outputs": {},
                          "problems": ["no exposure was stated and INDmoney reported no equity value"] if exposure is None
                          else ["the spread itself failed its checks"], "notes": []})
        return calcs

    def validate(self, state: WorkflowState) -> list[Check]:
        evidence = state.get("evidence") or {}
        chain: om.Chain | None = evidence.get("chain")
        if chain is None:
            return [{"check": "option_chain", "outcome": "failed", "detail": nifty.chain_failure(state) or "no chain for the monthly expiry"}]
        checks = nifty.chain_checks(state, chain)
        details: dict[str, om.LegDetail] = evidence.get("legDetails") or {}
        if details:
            wrong = [d.ind_key for d in details.values()
                     if (d.underlying_key and d.underlying_key != evidence.get("niftyKey")) or (d.expiry and d.expiry != chain.expiry)]
            checks.append({"check": "leg_instrument", "outcome": "failed" if wrong else "passed",
                           "detail": f"legs on another underlying or expiry: {wrong}" if wrong else f"both legs are Nifty 50 puts expiring {chain.expiry}"})
            oldest = min((d.updated_at for d in details.values() if d.updated_at), default=None)
            checks.append(nifty.freshness(oldest, state["now"], "oldest leg quote"))
        elif evidence.get("legs"):
            checks.append({"check": "leg_quotes", "outcome": "warning",
                           "detail": (failure(state, "leg_details") or "leg details not fetched") + "; chain quotes used"})
        if failure(state, "networth"):
            checks.append({"check": "portfolio_equity", "outcome": "warning", "detail": failure(state, "networth") or ""})
        for calc in state.get("calculations") or []:
            checks.append({"check": f"arithmetic_{calc['name']}", "outcome": "failed" if calc.get("problems") else "passed",
                           "detail": "; ".join(calc.get("problems") or []) or "checked"})
        return checks

    def outputs(self, state: WorkflowState) -> dict[str, bool]:
        calcs = {c["name"]: c for c in state.get("calculations") or []}
        spread = (calcs.get("put_spread") or {}).get("outputs") or {}
        sizing = calcs.get("hedge_sizing") or {}
        return {
            "legs": bool((state.get("evidence") or {}).get("legs")), "net_debit": spread.get("netDebitPoints") is not None,
            "breakeven": spread.get("breakeven") is not None, "max_payoff": spread.get("maxPayoffPoints") is not None,
            "scenario": spread.get("grossPayoffAtScenarioPoints") is not None,
            "sizing": bool(sizing.get("outputs")) or bool(sizing.get("problems")),
        }

    def payload(self, state: WorkflowState) -> dict[str, Any]:
        evidence = state.get("evidence") or {}
        chain: om.Chain | None = evidence.get("chain")
        calcs = {c["name"]: c for c in state.get("calculations") or []}
        inputs = state["inputs"]
        exposure, basis = self._exposure(state)
        portfolio = evidence.get("portfolio") or {}
        payload: dict[str, Any] = {
            "workflow": self.title, "underlying": chain.describe() if chain else None,
            "expiry": evidence["expiry"].isoformat() if evidence.get("expiry") else None,
            "construction": f"buy the put nearest the Nifty level, sell the put nearest the level after a {inputs['movePct']}% fall",
            "exposure": {
                "usedForSizingInr": om.rupees(exposure), "basis": basis,
                "statedInr": om.rupees(Decimal(inputs["statedExposureInr"])) if inputs.get("statedExposureInr") else None,
                "indmoneyEquityInr": self._verified_equity(state),
                "indmoneyEquityNote": "equity across every asset type INDmoney reports (stocks and equity funds); not reconciled with the stated amount",
                "directStockMarketCap": portfolio.get("by_market_cap_direct_stocks") or [],
                "sectorVerified": False,
                "sectorNote": "INDmoney returns no sector data for holdings, so stated sectors (e.g. banks, IT) cannot be checked",
            },
        }
        if evidence.get("legs"):
            priced, sources = self._priced(state)
            lots = chain.lot_sizes() if chain else set()
            lot_size = next(iter(lots)) if len(lots) == 1 else None
            payload["legs"] = [{"leg": name, "position": "buy" if name == "longPut" else "sell", **priced[name].describe(),
                                "premiumFrom": sources[name],
                                "premiumPerLotRupees": om.rupees(priced[name].premium * lot_size) if lot_size and priced[name].premium else None}
                               for name in ("longPut", "shortPut")]
        if "put_spread" in calcs:
            payload["spread"] = {**calcs["put_spread"]["outputs"], "problems": calcs["put_spread"]["problems"]}
        if "hedge_sizing" in calcs:
            payload["sizing"] = {**calcs["hedge_sizing"]["outputs"], "problems": calcs["hedge_sizing"]["problems"],
                                 "notes": calcs["hedge_sizing"]["notes"],
                                 "betaAssumption": "given by you" if inputs["betaGiven"] else "assumed: 0.8, 1.0 and 1.2 shown; no tool supplies beta"}
        payload["assumptions"] = [
            "Portfolio loss in the scenario = exposure × beta × fall; real losses depend on each stock, not only on Nifty.",
            "Payoffs are at expiry; before expiry the spread is worth less than its intrinsic value plus time value changes.",
            "Premiums are mid prices of the quoted bid/ask; brokerage, taxes and margin are not included.",
        ]
        return payload

    def fallback_report(self, payload: dict[str, Any]) -> str:
        lines = [f"**{self.title}**, expiry {payload.get('expiry') or 'unavailable'}: {payload['construction']}"]
        for leg in payload.get("legs") or []:
            lines.append(f"- {leg['position']} put {leg['strike']}: premium {leg['premium']} ({leg['premiumFrom']})")
        spread = payload.get("spread") or {}
        if spread.get("netDebitPoints") is not None:
            lines.append(f"- Net debit {spread['netDebitPoints']} points, breakeven {spread['breakeven']}, max payoff "
                         f"{spread['maxPayoffPoints']}, max profit {spread['maxProfitPoints']}")
        for row in (payload.get("sizing") or {}).get("byBeta") or []:
            lines.append(f"- Beta {row['beta']} (assumed): {row['lots']} lot(s); gross ₹{row['grossProtectionRupees']}, "
                         f"cost ₹{row['hedgeCostRupees']}, net ₹{row['netProtectionRupees']}")
        if payload.get("missingData"):
            lines.append("- Unavailable: " + ", ".join(payload["missingData"]))
        lines.append("\nThis is information, not investment advice.")
        return "\n".join(lines)

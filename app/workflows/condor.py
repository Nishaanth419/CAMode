"""Workflow A: a short Nifty iron condor for the next monthly expiry, from INDmoney's chain and leg quotes.

Waves: Nifty lookup → nearest chain (expiry list) → monthly chain (when different) → the four legs'
details and the two short legs' 7-day greeks history, in parallel.

- Short legs: the out-of-the-money call and put whose |delta| (as INDmoney returned it) is nearest the
  target (0.15 unless the message says otherwise). Without deltas no leg is picked and the answer says why.
- Wings: the priced strike 200–300 points beyond each short leg, nearest 250 (a preference, not a rule:
  the nearest available distance is used, with a note, when none falls in the range).
- Premiums: the mid of each leg's bid/ask from ``get_indian_stocks_details``; the chain's quote when the
  details call failed. Rupee amounts use the chain's lot size × lots (1 lot unless the message says).
- Theta: today's theta of each leg × the trading days left in the window (at most 5); an estimate.
"""

from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal
from typing import Any, Final

from .. import options_math as om
from . import nifty
from .base import Calculation, Check, StepResult, StepSpec, Workflow, WorkflowState, failure, ok_text, planned

DEFAULT_DELTA: Final = Decimal("0.15")
DEFAULT_WINGS: Final = (Decimal(200), Decimal(300))
DEFAULT_THETA_DAYS: Final = 5
DELTA_TOLERANCE: Final = Decimal("0.05")
LEG_NAMES: Final = ("longPut", "shortPut", "shortCall", "longCall")
LEG_QUANTITY: Final = {"longPut": 1, "shortPut": -1, "shortCall": -1, "longCall": 1}


def _parse_inputs(message: str) -> dict[str, Any]:
    text = message.lower()
    delta = re.search(r"(\d{1,2}(?:\.\d+)?)\s*-?\s*delta|delta\s*(?:of\s*)?(0?\.\d+)", text)
    if delta:
        raw = Decimal(delta.group(1) or delta.group(2))
        target = raw / 100 if raw >= 1 else raw
    else:
        target = DEFAULT_DELTA
    wings = re.search(r"(\d{2,4})\s*(?:-|–|to)\s*(\d{2,4})\s*(?:-\s*)?(?:point|pt)", text)
    low, high = (Decimal(wings.group(1)), Decimal(wings.group(2))) if wings else DEFAULT_WINGS
    days = re.search(r"next\s+(\d{1,2})\s+(?:trading\s+)?(?:days|sessions)", text)
    lots = re.search(r"(\d{1,3})\s+lots?\b", text)
    return {"deltaTarget": str(target), "wingPreference": [str(min(low, high)), str(max(low, high))],
            "thetaDays": int(days.group(1)) if days else DEFAULT_THETA_DAYS, "lots": int(lots.group(1)) if lots else 1}


class IronCondor(Workflow):
    name = "nifty_iron_condor"
    title = "Nifty iron condor"
    required_tools = frozenset({"lookup_ind_keys", "get_indian_stocks_option_chain", "get_indian_stocks_details"})
    needs_tools = required_tools | {"get_indian_stocks_greeks_history"}
    required_outputs = ("legs", "premiums", "net_credit", "max_profit", "max_loss", "breakevens", "theta_estimate")
    max_tool_calls = 8
    max_waves = 5
    narration_rules = (
        "Workflow: Nifty iron condor. Present the four legs (strike, premium and how it was priced, delta), then net "
        "credit, maximum profit, maximum loss and both breakevens in points and, where given, rupees for the stated lot "
        "size and lots. Say the short legs are the strikes nearest the requested delta from INDmoney's own deltas, and "
        "give their actual deltas. Call the theta figure an estimate that assumes today's theta holds, not expected "
        "profit or realised decay. Mention the wing distances and any note when they fall outside the preference."
    )

    def inputs(self, message: str) -> dict[str, Any]:
        return _parse_inputs(message)

    def next_steps(self, state: WorkflowState) -> list[StepSpec]:
        steps = nifty.chain_steps(state)
        if steps:
            return steps
        legs = (state.get("evidence") or {}).get("legs")
        if not legs or planned(state, "leg_details"):
            return []
        keys = [legs[name].ind_key for name in LEG_NAMES if legs[name].ind_key]
        chain_key = "chain_monthly" if planned(state, "chain_monthly") else "chain_nearest"
        specs = [StepSpec("leg_details", "get_indian_stocks_details", {"ind_keys": keys}, after=(chain_key,),
                          purpose="Live bid/ask, greeks, expiry and underlying of the four legs")]
        for name in ("shortPut", "shortCall"):
            if legs[name].ind_key:
                specs.append(StepSpec(f"greeks_{name}", "get_indian_stocks_greeks_history",
                                      {"ind_key": legs[name].ind_key, "lookback": "7d"}, after=(chain_key,),
                                      purpose=f"Recent theta of the {name} leg"))
        return specs

    def fix(self, state: WorkflowState, result: StepResult) -> StepSpec | None:
        return nifty.fix_chain(state, result)

    def normalize(self, state: WorkflowState) -> tuple[dict[str, Any], list[Check]]:
        evidence, checks = nifty.normalize_chain(state)
        merged = {**(state.get("evidence") or {}), **evidence}
        chain: om.Chain | None = merged.get("chain")
        inputs = state["inputs"]
        if chain is not None and "legs" not in merged:
            target = Decimal(inputs["deltaTarget"])
            preference = tuple(Decimal(x) for x in inputs["wingPreference"])
            short_call, call_notes = om.nearest_delta(chain, "call", target)
            short_put, put_notes = om.nearest_delta(chain, "put", target)
            notes = [*call_notes, *put_notes]
            long_call = long_put = None
            if short_call is not None:
                long_call, wing_notes = om.pick_wing(chain, "call", short_call.strike, preference)
                notes += wing_notes
            if short_put is not None:
                long_put, wing_notes = om.pick_wing(chain, "put", short_put.strike, preference)
                notes += wing_notes
            legs = {"longPut": long_put, "shortPut": short_put, "shortCall": short_call, "longCall": long_call}
            evidence["selectionNotes"] = notes
            if all(legs.values()):
                evidence["legs"] = legs
            else:
                evidence["legs"] = None
                checks.append({"check": "leg_selection", "outcome": "failed", "detail": "; ".join(notes) or "no legs could be chosen"})
        if (text := ok_text(state, "leg_details")) is not None and "legDetails" not in merged:
            evidence["legDetails"] = om.parse_leg_details(text)
        for name in ("shortPut", "shortCall"):
            if (text := ok_text(state, f"greeks_{name}")) is not None and f"greeks_{name}" not in merged:
                evidence[f"greeks_{name}"] = om.parse_greeks_history(text)
        return evidence, checks

    def _priced_legs(self, state: WorkflowState) -> tuple[dict[str, om.Quote], dict[str, str]]:
        """Each leg with its premium from the leg details when they were returned, else from the chain."""
        evidence = state["evidence"]
        details: dict[str, om.LegDetail] = evidence.get("legDetails") or {}
        legs, sources = {}, {}
        for name in LEG_NAMES:
            quote: om.Quote = evidence["legs"][name]
            detail = details.get(quote.ind_key or "")
            if detail is not None and detail.premium is not None:
                legs[name] = replace(quote, bid=detail.bid, ask=detail.ask, last=detail.last,
                                     delta=detail.delta if detail.delta is not None else quote.delta,
                                     theta=detail.theta if detail.theta is not None else quote.theta)
                sources[name] = f"leg details ({detail.premium_source})"
            else:
                legs[name] = quote
                sources[name] = f"option chain ({quote.premium_source})"
        return legs, sources

    def calculate(self, state: WorkflowState) -> list[Calculation]:
        evidence = state.get("evidence") or {}
        chain: om.Chain | None = evidence.get("chain")
        if not evidence.get("legs") or chain is None:
            return []
        inputs = state["inputs"]
        legs, sources = self._priced_legs(state)
        lots = chain.lot_sizes()
        lot_size = next(iter(lots)) if len(lots) == 1 else None
        condor = om.iron_condor(legs["longPut"], legs["shortPut"], legs["shortCall"], legs["longCall"],
                                lot_size=lot_size, lots=inputs["lots"])
        days_left = nifty.trading_days_until(state["today"], chain.expiry) if chain.expiry else 0
        sessions = min(inputs["thetaDays"], days_left)
        theta = om.theta_estimate([(legs[name], LEG_QUANTITY[name]) for name in LEG_NAMES], sessions,
                                  lot_size=lot_size, lots=inputs["lots"])
        if sessions < inputs["thetaDays"]:
            theta.notes.append(f"Only {days_left} trading day(s) remain to expiry, so the window is {sessions} day(s), not {inputs['thetaDays']}.")
        leg_inputs = {name: {"strike": om.points(q.strike), "premium": om.points(q.premium), "source": sources[name]} for name, q in legs.items()}
        return [
            {"name": "iron_condor", "inputs": {**leg_inputs, "lotSize": lot_size, "lots": inputs["lots"]},
             "outputs": condor.figures, "problems": condor.problems, "notes": condor.notes},
            {"name": "theta_estimate", "inputs": {name: om.points(q.theta) for name, q in legs.items()} | {"days": sessions},
             "outputs": theta.figures, "problems": theta.problems, "notes": theta.notes},
        ]

    def validate(self, state: WorkflowState) -> list[Check]:
        evidence = state.get("evidence") or {}
        chain: om.Chain | None = evidence.get("chain")
        checks: list[Check] = []
        if chain is None:
            return [{"check": "option_chain", "outcome": "failed", "detail": nifty.chain_failure(state) or "no chain for the monthly expiry"}]
        checks += nifty.chain_checks(state, chain)
        legs = evidence.get("legs")
        if not legs:
            return checks
        target = Decimal(state["inputs"]["deltaTarget"])
        for name in ("shortPut", "shortCall"):
            delta = legs[name].delta
            off = abs(abs(delta) - target) if delta is not None else None
            checks.append({"check": "delta_target", "outcome": "passed" if off is not None and off <= DELTA_TOLERANCE else "warning",
                           "detail": f"{name} delta {delta} vs target {target}"})
        details: dict[str, om.LegDetail] = evidence.get("legDetails") or {}
        if details:
            mismatched = [d.ind_key for d in details.values() if d.underlying_key and d.underlying_key != evidence.get("niftyKey")]
            wrong_expiry = [d.ind_key for d in details.values() if d.expiry and d.expiry != chain.expiry]
            checks.append({"check": "leg_instrument", "outcome": "failed" if mismatched else "passed",
                           "detail": f"legs on another underlying: {mismatched}" if mismatched else "every leg is a Nifty 50 option"})
            checks.append({"check": "leg_expiry", "outcome": "failed" if wrong_expiry else "passed",
                           "detail": f"legs with another expiry: {wrong_expiry}" if wrong_expiry else f"every leg expires {chain.expiry}"})
            missing = [name for name in LEG_NAMES if (legs[name].ind_key or "") not in details]
            if missing:
                checks.append({"check": "leg_quotes", "outcome": "warning", "detail": f"no leg details for {missing}; chain quotes used"})
            oldest = min((d.updated_at for d in details.values() if d.updated_at), default=None)
            checks.append(nifty.freshness(oldest, state["now"], "oldest leg quote"))
        else:
            checks.append({"check": "leg_quotes", "outcome": "warning",
                           "detail": (failure(state, "leg_details") or "leg details not fetched") + "; chain quotes used"})
        priced, _ = self._priced_legs(state)
        wide = [f"{name} {om.points(q.spread_pct())}%" for name, q in priced.items() if (q.spread_pct() or 0) > om.WIDE_SPREAD_PCT]
        if wide:
            checks.append({"check": "liquidity", "outcome": "warning", "detail": "bid/ask spread above 10% of mid: " + ", ".join(wide)})
        for calc in state.get("calculations") or []:
            checks.append({"check": f"arithmetic_{calc['name']}", "outcome": "failed" if calc.get("problems") else "passed",
                           "detail": "; ".join(calc.get("problems") or []) or "payoff checked at the breakevens"})
        for name in ("shortPut", "shortCall"):
            history: om.GreeksHistory | None = evidence.get(f"greeks_{name}")
            if history and history.samples and priced[name].theta is not None:
                latest = history.samples[-1][1]
                used = priced[name].theta
                drift = abs(latest - used) / abs(used) if used else None
                checks.append({"check": "theta_consistency", "outcome": "warning" if drift is not None and drift > Decimal("0.2") else "passed",
                               "detail": f"{name}: theta used {used}, latest greeks-history sample {latest}"})
        return checks

    def outputs(self, state: WorkflowState) -> dict[str, bool]:
        calcs = {c["name"]: c for c in state.get("calculations") or []}
        condor = (calcs.get("iron_condor") or {}).get("outputs") or {}
        theta = calcs.get("theta_estimate") or {}
        legs = (state.get("evidence") or {}).get("legs")
        return {
            "legs": bool(legs), "premiums": bool(calcs.get("iron_condor")) and not calcs["iron_condor"].get("problems"),
            "net_credit": condor.get("netCreditPoints") is not None, "max_profit": condor.get("maxProfitPoints") is not None,
            "max_loss": condor.get("maxLossPoints") is not None,
            "breakevens": condor.get("lowerBreakeven") is not None and condor.get("upperBreakeven") is not None,
            # An estimate, or a stated reason it could not be made, both satisfy the contract.
            "theta_estimate": bool(theta.get("outputs")) or bool(theta.get("problems")),
        }

    def payload(self, state: WorkflowState) -> dict[str, Any]:
        evidence = state.get("evidence") or {}
        chain: om.Chain | None = evidence.get("chain")
        calcs = {c["name"]: c for c in state.get("calculations") or []}
        inputs = state["inputs"]
        payload: dict[str, Any] = {
            "workflow": self.title, "underlying": chain.describe() if chain else None,
            "expiry": evidence["expiry"].isoformat() if evidence.get("expiry") else None,
            "expiryRule": "next monthly expiry: the last listed expiry of its calendar month, after today",
            "daysToExpiry": nifty.trading_days_until(state["today"], chain.expiry) if chain and chain.expiry else None,
            "selection": {"shortLegDeltaTarget": inputs["deltaTarget"], "deltaSource": "INDmoney option chain",
                          "wingPreferencePoints": inputs["wingPreference"], "notes": evidence.get("selectionNotes") or []},
        }
        if evidence.get("legs"):
            priced, sources = self._priced_legs(state)
            lots = chain.lot_sizes() if chain else set()
            lot_size = next(iter(lots)) if len(lots) == 1 else None
            payload["legs"] = [{"leg": name, "position": "sell" if LEG_QUANTITY[name] < 0 else "buy",
                                **priced[name].describe(), "premiumFrom": sources[name],
                                "premiumPerLotRupees": om.rupees(priced[name].premium * lot_size) if lot_size and priced[name].premium else None}
                               for name in LEG_NAMES]
        if "iron_condor" in calcs:
            payload["condor"] = {**calcs["iron_condor"]["outputs"], "problems": calcs["iron_condor"]["problems"],
                                 "notes": calcs["iron_condor"]["notes"]}
        if "theta_estimate" in calcs:
            payload["thetaEstimate"] = {**calcs["theta_estimate"]["outputs"], "problems": calcs["theta_estimate"]["problems"],
                                        "notes": calcs["theta_estimate"]["notes"]}
        histories = {name: evidence[f"greeks_{name}"].describe() for name in ("shortPut", "shortCall") if evidence.get(f"greeks_{name}")}
        if histories:
            payload["greeksHistory"] = histories
        payload["assumptions"] = [
            "Premiums are mid prices of the quoted bid/ask; a real fill can be worse.",
            f"Rupee figures are per {inputs['lots']} lot(s) of the chain's lot size; margin is not included.",
            "Maximum loss assumes the position is held to expiry; brokerage, taxes and fees are not included.",
        ]
        return payload

    def fallback_report(self, payload: dict[str, Any]) -> str:
        lines = [f"**{self.title}**, expiry {payload.get('expiry') or 'unavailable'}"]
        for leg in payload.get("legs") or []:
            lines.append(f"- {leg['position']} {leg['side']} {leg['strike']}: premium {leg['premium']} ({leg['premiumFrom']}), delta {leg['delta']}")
        condor = payload.get("condor") or {}
        if condor.get("netCreditPoints") is not None:
            lines.append(f"- Net credit {condor['netCreditPoints']} points; max profit {condor['maxProfitPoints']}, max loss "
                         f"{condor['maxLossPoints']}; breakevens {condor['lowerBreakeven']} and {condor['upperBreakeven']}")
            if condor.get("netCreditRupees") is not None:
                lines.append(f"- For {condor['lots']} lot(s) of {condor['lotSize']}: credit ₹{condor['netCreditRupees']}, "
                             f"max loss ₹{condor['maxLossRupees']}")
        theta = payload.get("thetaEstimate") or {}
        if theta.get("estimatedThetaPoints") is not None:
            lines.append(f"- Theta estimate over {theta['days']} day(s): {theta['estimatedThetaPoints']} points (assumes today's theta holds)")
        if payload.get("missingData"):
            lines.append("- Unavailable: " + ", ".join(payload["missingData"]))
        lines.append("\nThis is information, not investment advice.")
        return "\n".join(lines)

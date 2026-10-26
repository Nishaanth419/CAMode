"""Steps shared by the Nifty option workflows: resolve the index, read its expiries, fetch the monthly chain.

Waves: ``lookup`` (``lookup_ind_keys``) → ``chain_nearest`` (nearest expiry, which also lists every expiry) →
``chain_monthly`` (only when the next monthly expiry is not the nearest one). A monthly chain call the tool
rejects (an expiry it does not list) is corrected once from the dates its error lists.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Final

from .. import options_math as om
from .base import Check, StepResult, StepSpec, WorkflowState, failure, latest, market_open, ok_text, planned

NIFTY_NAME: Final = "NIFTY 50"
CHAIN_STRIKES: Final = om.MAX_STRIKES_AROUND_ATM
# The other indices Nifty shares a name with; a request naming one of these is not a Nifty 50 workflow.
OTHER_INDEX: Final = re.compile(r"bank\s*nifty|banknifty|fin\s*nifty|finnifty|nifty\s*(?:next|midcap|smallcap|it|bank|fin|pharma|auto|metal)", re.IGNORECASE)
NIFTY: Final = re.compile(r"\bnifty(?:\s*50)?\b", re.IGNORECASE)


def mentions_nifty(message: str) -> bool:
    return bool(NIFTY.search(message)) and not OTHER_INDEX.search(message)


def lookup_spec() -> StepSpec:
    return StepSpec("lookup", "lookup_ind_keys", {"names": [NIFTY_NAME], "filter_type": "IN_STOCKS"},
                    purpose="Resolve Nifty 50 to its INDmoney key")


def nifty_key(text: str) -> str | None:
    """The key of the result named exactly "NIFTY 50" (the lookup also returns Nifty Next 50 and others)."""
    value = om.load_json(text)
    rows = value if isinstance(value, list) else (value.get("results") or value.get("data") if isinstance(value, dict) else None)
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and str(row.get("name", "")).strip().upper() == NIFTY_NAME and row.get("ind_key"):
            return str(row["ind_key"])
    return None


def chain_steps(state: WorkflowState) -> list[StepSpec]:
    """The next chain call this run needs, if any (see the module docstring)."""
    evidence = state.get("evidence") or {}
    if not planned(state, "lookup"):
        return [lookup_spec()]
    key = evidence.get("niftyKey")
    if not key:
        return []
    if not planned(state, "chain_nearest"):
        return [StepSpec("chain_nearest", "get_indian_stocks_option_chain",
                         {"ind_key": key, "use_expiry_date": False, "strikes_around_atm": CHAIN_STRIKES},
                         after=("lookup",), purpose="Nearest-expiry chain and the list of every expiry")]
    nearest: om.Chain | None = evidence.get("chainNearest")
    monthly: date | None = evidence.get("expiry")
    if nearest is None or monthly is None or nearest.expiry == monthly:
        return []
    if not planned(state, "chain_monthly"):
        return [StepSpec("chain_monthly", "get_indian_stocks_option_chain",
                         {"ind_key": key, "use_expiry_date": True, "expiry_date": monthly.isoformat(),
                          "strikes_around_atm": CHAIN_STRIKES},
                         after=("chain_nearest",), purpose="Chain for the next monthly expiry")]
    return []


def fix_chain(state: WorkflowState, result: StepResult) -> StepSpec | None:
    """A rejected monthly-chain call, sent again with the monthly expiry recomputed from the dates the tool listed."""
    if result.get("key") != "chain_monthly":
        return None
    listed = om.expiries_listed(result.get("error") or "")
    monthly = om.next_monthly_expiry(listed, state["today"]) if listed else None
    if monthly is None or monthly.isoformat() == str(result["arguments"].get("expiry_date")):
        return None
    return StepSpec("chain_monthly", result["tool"], {**result["arguments"], "expiry_date": monthly.isoformat()},
                    after=("chain_nearest",), purpose="Chain for the next monthly expiry (corrected expiry)")


def normalize_chain(state: WorkflowState) -> tuple[dict[str, Any], list[Check]]:
    evidence: dict[str, Any] = {}
    checks: list[Check] = []
    previous = state.get("evidence") or {}
    if "niftyKey" not in previous and (text := ok_text(state, "lookup")) is not None:
        key = nifty_key(text)
        evidence["niftyKey"] = key
        checks.append({"check": "instrument", "outcome": "passed" if key else "failed",
                       "detail": f"{NIFTY_NAME} → {key}" if key else f"lookup_ind_keys returned no exact {NIFTY_NAME} match"})
    if "chainNearest" not in previous and (text := ok_text(state, "chain_nearest")) is not None:
        try:
            nearest = om.parse_chain(text)
        except om.ChainError as exc:
            checks.append({"check": "option_chain", "outcome": "failed", "detail": str(exc)})
        else:
            evidence["chainNearest"] = nearest
            monthly = om.next_monthly_expiry(nearest.expiries, state["today"])
            evidence["expiry"] = monthly
            evidence["monthlyExpiries"] = [d.isoformat() for d in om.monthly_expiries(nearest.expiries)]
            checks.append({"check": "expiry", "outcome": "passed" if monthly else "failed", "detail": (
                f"next monthly expiry {monthly.isoformat()} (last listed expiry of its month; listed: "
                f"{', '.join(d.isoformat() for d in nearest.expiries)})" if monthly else "no monthly expiry after today is listed")})
            if monthly is not None and nearest.expiry == monthly:
                evidence["chain"] = nearest
    if "chain" not in previous and "chain" not in evidence and (text := ok_text(state, "chain_monthly")) is not None:
        try:
            evidence["chain"] = om.parse_chain(text)
        except om.ChainError as exc:
            checks.append({"check": "option_chain", "outcome": "failed", "detail": str(exc)})
        requested = om.parse_expiry((latest(state, "chain_monthly") or {}).get("arguments", {}).get("expiry_date"))
        if requested is not None and requested != previous.get("expiry"):
            # The first expiry was rejected and corrected from the tool's own list (fix_chain).
            evidence["expiry"] = requested
            checks.append({"check": "expiry", "outcome": "warning", "detail": (
                f"{previous.get('expiry')} was not accepted; next monthly expiry from the dates the tool listed: {requested}")})
    return evidence, checks


def chain_checks(state: WorkflowState, chain: om.Chain) -> list[Check]:
    """Instrument, expiry, lot size and freshness checks on the chain the legs come from."""
    evidence = state.get("evidence") or {}
    checks: list[Check] = []
    key, expiry = evidence.get("niftyKey"), evidence.get("expiry")
    checks.append({"check": "instrument_match", "outcome": "passed" if chain.underlying_key == key else "failed",
                   "detail": f"chain underlying {chain.underlying_key}, requested {key}"})
    checks.append({"check": "expiry_match", "outcome": "passed" if chain.expiry == expiry else "failed",
                   "detail": f"chain expiry {chain.expiry}, requested {expiry}"})
    lots = chain.lot_sizes()
    checks.append({"check": "lot_size", "outcome": "passed" if len(lots) == 1 else ("failed" if not lots else "warning"),
                   "detail": f"lot sizes in the chain: {sorted(lots) or 'none'}"})
    checks.append(freshness(chain.spot_time, state["now"], "underlying price", "quote_freshness"))
    return checks


def freshness(updated: datetime | None, now: datetime, what: str, name: str = "leg_quote_freshness") -> Check:
    """A quote is current during the session when under ``STALE_QUOTE_MINUTES`` old; outside it, from the last session."""
    if updated is None:
        return {"check": name, "outcome": "warning", "detail": f"{what}: no update time was returned"}
    age = om.quote_age_minutes(updated, now)
    if market_open(now):
        stale = age is not None and age > om.STALE_QUOTE_MINUTES
        return {"check": name, "outcome": "warning" if stale else "passed",
                "detail": f"{what} updated {updated.isoformat(sep=' ')} IST ({age} min before this check)"}
    return {"check": name, "outcome": "warning",
            "detail": f"{what} updated {updated.isoformat(sep=' ')} IST; the market is closed, so prices are from the last session"}


def trading_days_until(today: date, expiry: date) -> int:
    """Weekdays after ``today`` up to and including ``expiry`` (exchange holidays are not known here)."""
    days, day = 0, today
    while day < expiry:
        day += timedelta(days=1)
        if day.weekday() < 5:
            days += 1
    return days


def chain_failure(state: WorkflowState) -> str | None:
    return failure(state, "chain_monthly") or failure(state, "chain_nearest") or failure(state, "lookup")

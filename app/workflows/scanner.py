"""Workflow C: scan the user's Indian watchlist for 14-session breakouts and multi-session support.

Waves: ``user_watchlist`` → ``lookup_ind_keys`` for rows without a key (ten names per call) → daily candles
for each instrument (``1day`` over ``1y``: the ``14d`` lookback is calendar days, too few sessions) →
``get_indian_stocks_details`` with analyst data for the top candidates. Rules are in ``price_action``.

The watchlist row layout could not be recorded (the account's watchlists were empty on 2026-10-26), so
rows are read leniently: a key from ``ind_key``-like fields, else the name is resolved with a lookup.
"""

from __future__ import annotations

import re
from typing import Any, Final

from .. import options_math as om
from .. import price_action as pa
from .base import Calculation, Check, StepSpec, Workflow, WorkflowState, failure, ok_text, planned

MAX_INSTRUMENTS: Final = 20
NAMES_PER_LOOKUP: Final = 10
MAX_LOOKUPS: Final = 2
DEFAULT_TOP: Final = 3
_KEY_FIELDS: Final = ("ind_key", "ind_stock_id", "indKey", "stock_id", "instrument_key", "investment_code")
_NAME_FIELDS: Final = ("name", "display_name", "short_name", "stock_name", "company_name", "symbol", "ticker")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\b(ltd|limited|inc|corp|corporation)\b\.?", "", text.lower()))


def watchlist_rows(text: str) -> list[dict[str, str | None]]:
    """``{"name", "key"}`` per watchlisted instrument (deduplicated across watchlists, first seen first)."""
    value = om.load_json(text)
    lists = value.get("watchlists") if isinstance(value, dict) else None
    rows: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for watchlist in lists if isinstance(lists, list) else []:
        for row in (watchlist.get("stocks") or []) if isinstance(watchlist, dict) else []:
            if not isinstance(row, dict):
                continue
            key = next((str(row[f]) for f in _KEY_FIELDS if isinstance(row.get(f), str) and row[f].upper().startswith("IND")), None)
            name = next((str(row[f]) for f in _NAME_FIELDS if isinstance(row.get(f), str) and row[f].strip()), None)
            ident = key or (_norm(name) if name else "")
            if not ident or ident in seen:
                continue
            seen.add(ident)
            rows.append({"name": name or key, "key": key})
    return rows


def match_lookup(names: list[str], text: str) -> dict[str, str]:
    """Each name's key from a batched lookup (results come back as one flat list, so names are matched in code)."""
    value = om.load_json(text)
    results = [r for r in (value if isinstance(value, list) else []) if isinstance(r, dict) and r.get("ind_key")]
    found: dict[str, str] = {}
    for name in names:
        wanted = _norm(name)
        exact = next((r for r in results if _norm(str(r.get("name", ""))) == wanted), None)
        partial = next((r for r in results if wanted and _norm(str(r.get("name", ""))).startswith(wanted)), None)
        if (chosen := exact or partial) is not None:
            found[name] = str(chosen["ind_key"])
    return found


class WatchlistScanner(Workflow):
    name = "watchlist_swing_scanner"
    title = "Watchlist swing-trade scan"
    # Candles are needed only when the watchlist has instruments; the "scan" output covers that case.
    required_tools = frozenset({"user_watchlist"})
    needs_tools = frozenset({"user_watchlist", "get_indian_stocks_ohlc", "lookup_ind_keys", "get_indian_stocks_details"})
    required_outputs = ("scan", "ranking", "analyst")
    max_tool_calls = 1 + MAX_LOOKUPS + MAX_INSTRUMENTS + 1
    max_waves = 5
    max_wave_steps = MAX_INSTRUMENTS
    narration_rules = (
        "Workflow: watchlist swing-trade scan. State the breakout and support rules exactly as given, then the flagged "
        "instruments in rank order with their close, prior high or support and the percentage. Give analyst consensus "
        "and target upside only for the candidates that have them, and say how many candidates there actually are when "
        "fewer than asked. List instruments that could not be scanned and why. Never say a breakout or support level "
        "means the price will rise."
    )

    def inputs(self, message: str) -> dict[str, Any]:
        text = message.lower()
        top = re.search(r"top\s+(\d{1,2})", text)
        return {"top": int(top.group(1)) if top else DEFAULT_TOP, "lookbackSessions": pa.LOOKBACK_SESSIONS}

    def next_steps(self, state: WorkflowState) -> list[StepSpec]:
        evidence = state.get("evidence") or {}
        if not planned(state, "watchlist"):
            return [StepSpec("watchlist", "user_watchlist", {"type": "indian"}, purpose="Instruments on your Indian watchlist")]
        rows = evidence.get("rows")
        if not rows:
            return []
        unresolved = [r["name"] for r in rows if not r["key"] and r["name"]]
        if unresolved and not planned(state, "lookup:1"):
            batches = [unresolved[i:i + NAMES_PER_LOOKUP] for i in range(0, len(unresolved), NAMES_PER_LOOKUP)][:MAX_LOOKUPS]
            return [StepSpec(f"lookup:{n}", "lookup_ind_keys", {"names": batch, "filter_type": "IN_STOCKS"},
                             after=("watchlist",), purpose="Resolve watchlist names to INDmoney keys")
                    for n, batch in enumerate(batches, start=1)]
        keys = evidence.get("instruments") or []
        if keys and not any(s["key"].startswith("ohlc:") for s in state.get("steps") or []):
            after = ("lookup:1",) if planned(state, "lookup:1") else ("watchlist",)
            return [StepSpec(f"ohlc:{item['key']}", "get_indian_stocks_ohlc",
                             {"ind_key": item["key"], "interval": "1day", "lookback": "1y"}, after=after,
                             purpose=f"Daily candles for {item['name']}") for item in keys[:MAX_INSTRUMENTS]]
        top = evidence.get("top")
        if top and not planned(state, "analyst"):
            return [StepSpec("analyst", "get_indian_stocks_details", {"ind_keys": [s.ind_key or "" for s in top], "segments": ["analyst"]},
                             after=tuple(f"ohlc:{s.ind_key}" for s in top), purpose="Analyst consensus and targets for the top candidates")]
        return []

    def normalize(self, state: WorkflowState) -> tuple[dict[str, Any], list[Check]]:
        previous = state.get("evidence") or {}
        evidence: dict[str, Any] = {}
        checks: list[Check] = []
        if "rows" not in previous and (text := ok_text(state, "watchlist")) is not None:
            rows = watchlist_rows(text)
            evidence["rows"] = rows
            checks.append({"check": "watchlist", "outcome": "passed" if rows else "warning",
                           "detail": f"{len(rows)} instrument(s) on the Indian watchlist" if rows else "the Indian watchlist is empty"})
        rows = evidence.get("rows", previous.get("rows")) or []
        if rows and "instruments" not in previous:
            resolved: dict[str, str] = {}
            lookups = [s for s in state.get("steps") or [] if s["key"].startswith("lookup:")]
            needs_lookup = any(not r["key"] for r in rows)
            if not needs_lookup or (lookups and all(s["id"] in (state.get("results") or {}) for s in lookups)):
                for step in lookups:
                    if (text := ok_text(state, step["key"])) is not None:
                        resolved.update(match_lookup(step["arguments"]["names"], text))
                instruments = [{"name": r["name"], "key": r["key"] or resolved.get(r["name"] or "")} for r in rows]
                evidence["instruments"] = [i for i in instruments if i["key"]]
                evidence["unresolved"] = [i["name"] for i in instruments if not i["key"]]
                if len(instruments) > MAX_INSTRUMENTS:
                    checks.append({"check": "watchlist_cap", "outcome": "warning",
                                   "detail": f"only the first {MAX_INSTRUMENTS} of {len(instruments)} instruments are scanned"})
        instruments = evidence.get("instruments", previous.get("instruments")) or []
        ohlc_steps = [s for s in state.get("steps") or [] if s["key"].startswith("ohlc:")]
        if ohlc_steps and "scans" not in previous and all(s["id"] in (state.get("results") or {}) for s in ohlc_steps):
            names = {i["key"]: i["name"] for i in instruments}
            scans, failed = [], []
            for step in ohlc_steps:
                key = step["arguments"]["ind_key"]
                if (text := ok_text(state, step["key"])) is None:
                    failed.append(f"{names.get(key, key)}: {failure(state, step['key'])}")
                    continue
                result = pa.scan(names.get(key, key), pa.parse_candles(text), state["now"], state["inputs"]["lookbackSessions"])
                result.ind_key = key  # The key the candles were asked for, whatever the response echoes.
                scans.append(result)
            evidence["scans"], evidence["scanFailures"] = scans, failed
            evidence["ranked"] = pa.rank(scans)
            evidence["top"] = evidence["ranked"][: state["inputs"]["top"]]
        if (text := ok_text(state, "analyst")) is not None and "analyst" not in previous:
            evidence["analyst"] = pa.parse_analyst(text)
        return evidence, checks

    def calculate(self, state: WorkflowState) -> list[Calculation]:
        evidence = state.get("evidence") or {}
        if "scans" not in evidence:
            return []
        analysts: dict[str, pa.Analyst] = evidence.get("analyst") or {}
        candidates = []
        for item in evidence.get("top") or []:
            row: dict[str, Any] = {"name": item.name, "indKey": item.ind_key, "signal": "breakout" if item.breakout else "support"}
            analyst = analysts.get(item.ind_key or "")
            if analyst is None:
                row["analyst"] = {"available": False, "reason": failure(state, "analyst") or "no analyst data returned"}
            else:
                upside, notes = pa.target_upside(analyst)
                row["analyst"] = {
                    "available": bool(analyst.analysts), "analysts": analyst.analysts, "consensus": analyst.sentiment,
                    "buyPct": om.points(analyst.buy_pct), "holdPct": om.points(analyst.hold_pct), "sellPct": om.points(analyst.sell_pct),
                    "targetMean": om.points(analyst.target_mean), "targetLow": om.points(analyst.target_low),
                    "targetHigh": om.points(analyst.target_high), "livePrice": om.points(analyst.price),
                    "priceUpdatedAt": analyst.price_updated.isoformat(sep=" ") if analyst.price_updated else None,
                    "targetUpsidePct": upside, "notes": notes,
                }
            candidates.append(row)
        return [
            {"name": "price_action_scan", "inputs": {"instruments": len(evidence["scans"]), **pa.rules()},
             "outputs": {"scans": [s.describe() for s in evidence["scans"]],
                         "ranked": [s.name for s in evidence.get("ranked") or []]}},
            {"name": "target_upside", "inputs": {"top": state["inputs"]["top"]}, "outputs": {"candidates": candidates}},
        ]

    def validate(self, state: WorkflowState) -> list[Check]:
        evidence = state.get("evidence") or {}
        checks: list[Check] = []
        if failure(state, "watchlist"):
            return [{"check": "watchlist", "outcome": "failed", "detail": failure(state, "watchlist") or ""}]
        if evidence.get("unresolved"):
            checks.append({"check": "instrument", "outcome": "warning", "detail": f"could not resolve: {evidence['unresolved']}"})
        scans: list[pa.Scan] = evidence.get("scans") or []
        short = [s.name for s in scans if s.status != "ok"]
        if short or evidence.get("scanFailures"):
            checks.append({"check": "price_history", "outcome": "warning",
                           "detail": "; ".join([*(f"{n}: too few sessions" for n in short), *(evidence.get("scanFailures") or [])])})
        if any(s.excluded_running_session for s in scans):
            checks.append({"check": "quote_freshness", "outcome": "passed",
                           "detail": "today's session is still open, so its candle was left out of the rules"})
        top, wanted = evidence.get("top") or [], state["inputs"]["top"]
        if scans and len(top) < wanted:
            checks.append({"check": "candidates", "outcome": "warning", "detail": f"{len(top)} candidate(s) met a rule, fewer than the {wanted} asked for"})
        return checks

    def outputs(self, state: WorkflowState) -> dict[str, bool]:
        evidence = state.get("evidence") or {}
        names = {c["name"] for c in state.get("calculations") or []}
        empty = evidence.get("rows") == []
        no_top = "scans" in evidence and not evidence.get("top")
        return {"scan": "price_action_scan" in names or empty, "ranking": "ranked" in evidence or empty,
                "analyst": "target_upside" in names and (no_top or "analyst" in evidence or bool(failure(state, "analyst"))) or empty}

    def payload(self, state: WorkflowState) -> dict[str, Any]:
        evidence = state.get("evidence") or {}
        calcs = {c["name"]: c.get("outputs") or {} for c in state.get("calculations") or []}
        scans = (calcs.get("price_action_scan") or {}).get("scans") or []
        flagged = [s for s in scans if s["breakout"] or s["atSupport"]]
        return {
            "workflow": self.title, "rules": pa.rules(), "dataUsed": "daily candles over the last year; the rules use the last 15 completed sessions",
            "watchlistInstruments": len(evidence.get("rows") or []), "scanned": len(scans),
            "flaggedCount": len(flagged), "candidatesAsked": state["inputs"]["top"],
            "candidates": (calcs.get("target_upside") or {}).get("candidates") or [],
            "flagged": flagged,
            "notFlagged": [{"name": s["name"], "close": s["close"], "priorHigh": s["priorHigh"], "support": s["support"]}
                           for s in scans if not (s["breakout"] or s["atSupport"]) and s["status"] == "ok"][:10],
            "notScanned": [*(evidence.get("unresolved") or []), *(s["name"] for s in scans if s["status"] != "ok"), *(evidence.get("scanFailures") or [])],
        }

    def fallback_report(self, payload: dict[str, Any]) -> str:
        count = len(payload["candidates"])
        shortfall = (f"; only {count} candidate(s) met a rule, fewer than the {payload['candidatesAsked']} asked for"
                     if count < payload["candidatesAsked"] else "")
        lines = [f"**{self.title}**: {payload['scanned']} instrument(s) scanned, {payload['flaggedCount']} flagged{shortfall}",
                 f"- Breakout: {payload['rules']['breakout']}", f"- Support: {payload['rules']['support']}"]
        for item in payload["flagged"]:
            lines.append(f"- {item['name']}: close {item['close']}, prior high {item['priorHigh']}, support {item['support']}")
        for item in payload["candidates"]:
            analyst = item["analyst"]
            if analyst.get("targetUpsidePct") is not None:
                lines.append(f"- {item['name']}: {analyst.get('consensus')}, mean target {analyst['targetMean']} ({analyst['targetUpsidePct']}% upside)")
        if payload.get("missingData"):
            lines.append("- Unavailable: " + ", ".join(payload["missingData"]))
        lines.append("\nThis is information, not investment advice.")
        return "\n".join(lines)

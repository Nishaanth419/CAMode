"""Mutual fund returns per scheme and for the whole portfolio, from the ledger and today's NAV.

For each scheme in ``mf_ledger`` the NAV is AMFI's (``amfi.navs_for``) unless the
statement's own valuation is newer or AMFI has no row; ``navSource`` says which. A NAV
older than ``fund_data.stale_days()`` makes an otherwise clean scheme ``nav_stale``
(still counted, labelled). Current value is shown for every scheme; invested amount,
gains and returns only for ``ok`` and ``nav_stale`` schemes, the others carry the
status reason instead. Missing is never zero.

A scheme with no units left is worth exactly zero and needs no NAV; one with units
but no NAV from either source is ``no_nav`` (left out, and the coverage is then unknown).

Returns: XIRR over the scheme's cash flows plus its value on the NAV date; under 365
days from the first flow, ``absoluteReturnPct`` instead (``returnReason: under_one_year``).
The portfolio pools the included schemes' flows, so a switch between two of them nets out.

Benchmarks (``benchmarks``): each included scheme is compared with its category's index
fund and the portfolio with the chosen one (``nifty50`` by default). ``mf_math.replay``
buys and sells the benchmark with the same cash flows and values what is left on the same
date as the fund's figures; the comparison carries the benchmark's XIRR (or absolute
return under a year, by the same rule) and ``differenceInr`` (the fund's value minus the
benchmark's). A scheme left out of the totals, or with no benchmark for its category, says why.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final, NamedTuple

from . import amfi, benchmarks, fund_data, mf_ledger, mf_math
from .amfi import NavRow
from .mf_ledger import LedgerScheme
from .sealing import ProfileKeys

INCLUDED: Final = frozenset({"ok", "nav_stale"})
ONE_YEAR_DAYS: Final = 365
_ZERO: Final = Decimal(0)

Flows = list[tuple[date, Decimal]]
Histories = dict[str, list[tuple[date, Decimal]]]
EXCLUDED_REASON: Final = "Left out of the totals, so not compared."
NOT_DOWNLOADED_REASON: Final = "Benchmark NAV history has not been downloaded yet."


class _Computed(NamedTuple):
    item: dict[str, Any]
    flows: Flows  # Including the terminal value; empty when the scheme is excluded.
    value: Decimal | None
    invested: Decimal | None
    realised: Decimal | None
    cashflows: Sequence[tuple[date, Decimal]] = ()  # The investor's own flows, without the terminal value.
    end: date | None = None  # The valuation date: the NAV date, or the last flow when nothing is left.


def _round(value: Decimal | None, places: str = "0.01") -> float | None:
    return None if value is None else float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def _pct(rate: Decimal | None) -> float | None:
    return None if rate is None else _round(rate * 100)


def nav_for(scheme: LedgerScheme, navs: dict[str, NavRow]) -> tuple[Decimal | None, date | None, str | None]:
    row = navs.get(scheme.isin) if scheme.isin else None
    statement = (scheme.valuation_nav, scheme.valuation_date) if scheme.valuation_nav and scheme.valuation_date else None
    if row and (statement is None or row.nav_date >= statement[1]):
        return row.nav, row.nav_date, "amfi"
    if statement:
        return statement[0], statement[1], "statement"
    return None, None, None


def _returns(flows: Flows, end: date | None) -> dict[str, Any]:
    """XIRR, or the absolute return for under a year, with the reason when there is none."""
    if not flows:
        return {"xirr": None, "absoluteReturnPct": None, "returnReason": "no_flows"}
    first = min(day for day, _ in flows)
    last = end or max(day for day, _ in flows)
    if (last - first).days < ONE_YEAR_DAYS:
        return {"xirr": None, "absoluteReturnPct": _round(mf_math.absolute_return_pct(flows)), "returnReason": "under_one_year"}
    result = mf_math.xirr(flows)
    return {"xirr": _pct(result.rate), "absoluteReturnPct": None, "returnReason": result.reason}


def _scheme(scheme: LedgerScheme, navs: dict[str, NavRow], today: date) -> _Computed:
    """The scheme's figures with the exact amounts the portfolio adds up."""
    nav, nav_date, source = nav_for(scheme, navs)
    # A scheme with no units is worth exactly zero: it needs no NAV.
    value = _ZERO if scheme.close == 0 else (scheme.close * nav if nav is not None else None)
    status, reason = scheme.status, scheme.reason
    lots = mf_math.fifo(scheme.transactions)
    if status == "ok":
        held = sum((lot.units for lot in lots.open_lots), _ZERO)
        if abs(held - scheme.close) > mf_math.UNIT_TOLERANCE:
            status, reason = "unreconciled", (
                f"Units bought and sold ({held}) do not match the closing units ({scheme.close})."
            )
        elif value is None:
            status, reason = "no_nav", "No NAV is available for this fund from AMFI or the statement."
        elif scheme.close and (today - nav_date).days > fund_data.stale_days():
            status, reason = "nav_stale", f"The latest NAV is from {nav_date:%d %b %Y}; figures use it."
    row = navs.get(scheme.isin) if scheme.isin else None
    result: dict[str, Any] = {
        "name": scheme.name, "isin": scheme.isin, "amc": scheme.amc, "amfiCategory": row.amfi_category if row else None,
        "folioKey": scheme.folio_key, "status": status, "reason": reason,
        "units": _round(scheme.close, "0.001"), "nav": _round(nav, "0.0001"),
        "navDate": nav_date.isoformat() if nav_date else None, "navSource": source, "currentValue": _round(value),
        "invested": None, "unrealisedGain": None, "realisedGain": None,
        "xirr": None, "absoluteReturnPct": None, "returnReason": status,
        "heldSince": None, "problems": lots.problems,
    }
    if status not in INCLUDED:
        return _Computed(result, [], value, None, None)
    invested = sum((lot.cost for lot in lots.open_lots), _ZERO)
    realised = sum((d.sale_value - d.cost for d in lots.disposals), _ZERO)
    flows = list(lots.cashflows)
    if value:
        flows.append((nav_date, value))
    result.update(
        invested=_round(invested), unrealisedGain=_round(value - invested), realisedGain=_round(realised),
        heldSince=min(day for day, _ in lots.cashflows).isoformat() if lots.cashflows else None,
        **_returns(flows, nav_date if value else None),
    )
    end = nav_date if value else max((day for day, _ in lots.cashflows), default=None)
    return _Computed(result, flows, value, invested, realised, list(lots.cashflows), end)


def scheme_returns(scheme: LedgerScheme, navs: dict[str, NavRow], today: date) -> dict[str, Any]:
    return _scheme(scheme, navs, today).item


def _portfolio(items: Sequence[_Computed]) -> tuple[dict[str, Any], dict[str, Any]]:
    total = sum((c.value for c in items if c.value is not None), _ZERO)
    included = [c for c in items if c.item["status"] in INCLUDED]
    excluded = [{"name": c.item["name"], "status": c.item["status"], "reason": c.item["reason"]}
                for c in items if c.item["status"] not in INCLUDED]
    if included:
        value = sum((c.value or _ZERO for c in included), _ZERO)
        invested = sum((c.invested for c in included), _ZERO)
        end = max((date.fromisoformat(c.item["navDate"]) for c in included if c.item["navDate"]), default=None)
        portfolio = {
            "totalValue": _round(total), "includedValue": _round(value), "invested": _round(invested),
            "unrealisedGain": _round(value - invested),
            "realisedGain": _round(sum((c.realised for c in included), _ZERO)),
            **_returns([flow for c in included for flow in c.flows], end),
        }
    else:
        value = _ZERO
        portfolio = {"totalValue": _round(total), "includedValue": 0.0, "invested": None, "unrealisedGain": None,
                     "realisedGain": None, "xirr": None, "absoluteReturnPct": None, "returnReason": "no_schemes"}
    unknown = sum(1 for c in items if c.value is None)
    if unknown:
        coverage_pct: float | None = None  # The whole is not known, so neither is the share of it.
    elif not excluded:
        coverage_pct = 100.0
    else:
        coverage_pct = _round(value / total * 100) if total else None
    return portfolio, {"pct": coverage_pct, "excluded": excluded, "valueUnknown": unknown}


def _comparison(benchmark_id: str | None, status: str, reason: str | None, **figures: Any) -> dict[str, Any]:
    benchmark = benchmarks.get(benchmark_id) if benchmark_id else None
    return {
        "id": benchmark_id, "label": benchmark.label if benchmark else None, "status": status, "reason": reason,
        "xirr": None, "absoluteReturnPct": None, "returnReason": None, "value": None, "differenceInr": None,
        "navDate": None, **figures,
    }


def _compare(cashflows: Sequence[tuple[date, Decimal]], end: date | None, value: Decimal, benchmark_id: str,
             histories: Histories) -> dict[str, Any]:
    """The fund's flows replayed in one benchmark, valued on the fund's own valuation date."""
    history = histories.get(benchmark_id) or []
    if not history:
        return _comparison(benchmark_id, "unavailable", NOT_DOWNLOADED_REASON)
    if end is None:
        return _comparison(benchmark_id, "unavailable", "No cash flows to compare.")
    label = benchmarks.get(benchmark_id).label
    result = mf_math.replay(cashflows, history, end, value, label=label)
    if result.status != "ok":
        return _comparison(benchmark_id, result.status, result.reason)
    return _comparison(
        benchmark_id, "ok", None, **_returns(result.flows, end), value=_round(result.value),
        differenceInr=_round(result.difference), navDate=result.nav_date.isoformat(),
    )


def _scheme_comparison(c: _Computed, histories: Histories) -> dict[str, Any]:
    if c.item["status"] not in INCLUDED:
        return _comparison(None, "excluded", EXCLUDED_REASON)
    benchmark_id, reason = benchmarks.benchmark_for(c.item["amfiCategory"], c.item["name"])
    if benchmark_id is None:
        return _comparison(None, "none", reason)
    return _compare(c.cashflows, c.end, c.value or _ZERO, benchmark_id, histories)


def _portfolio_comparison(items: Sequence[_Computed], benchmark_id: str, histories: Histories) -> dict[str, Any]:
    included = [c for c in items if c.item["status"] in INCLUDED]
    if not included:
        return _comparison(benchmark_id, "unavailable", "No funds are included in the totals.")
    ends = [c.end for c in included if c.end is not None]
    nav_dates = [date.fromisoformat(c.item["navDate"]) for c in included if c.item["navDate"] and c.value]
    end = max(nav_dates) if nav_dates else max(ends, default=None)
    flows = [flow for c in included for flow in c.cashflows]
    return _compare(flows, end, sum((c.value or _ZERO for c in included), _ZERO), benchmark_id, histories)


def _benchmark_info(benchmark_id: str, histories: Histories) -> dict[str, Any]:
    benchmark = benchmarks.get(benchmark_id)
    history = histories.get(benchmark_id) or []
    return {
        "id": benchmark.id, "label": benchmark.label, "schemeName": benchmark.scheme_name,
        "schemeCode": benchmark.scheme_code, "navDate": history[-1][0].isoformat() if history else None,
        "note": benchmarks.INDEX_NOTE, "options": [{"id": b.id, "label": b.label} for b in benchmarks.BENCHMARKS],
    }


def returns_from(schemes: Sequence[LedgerScheme], navs: dict[str, NavRow], today: date, *,
                 benchmark: str = benchmarks.DEFAULT_BENCHMARK, benchmark_navs: Histories | None = None) -> dict[str, Any]:
    """Figures for the stored schemes; ``benchmark_navs`` maps benchmark ids to their ascending NAV history."""
    if benchmarks.get(benchmark) is None:
        raise ValueError(f"Unknown benchmark {benchmark!r}.")
    if not schemes:
        return {"available": False, "hint": mf_ledger.HINT}
    histories = benchmark_navs or {}
    items = [_scheme(scheme, navs, today) for scheme in schemes]
    items.sort(key=lambda c: (-(c.value or _ZERO), c.item["name"]))
    for c in items:
        c.item["benchmark"] = _scheme_comparison(c, histories)
    portfolio, coverage = _portfolio(items)
    portfolio["benchmark"] = _portfolio_comparison(items, benchmark, histories)
    nav_dates = [c.item["navDate"] for c in items if c.item["navDate"]]
    starts = [s.period_from for s in schemes if s.period_from]
    ends = [s.period_to for s in schemes if s.period_to]
    return {
        "available": True,
        "schemes": [c.item for c in items],
        "portfolio": portfolio,
        "benchmark": _benchmark_info(benchmark, histories),
        "coverage": coverage,
        "navDate": max(nav_dates) if nav_dates else None,
        "statementPeriod": {"from": min(starts).isoformat() if starts else None, "to": max(ends).isoformat() if ends else None},
    }


def returns_for(user_id: str, keys: ProfileKeys, today: date,
                benchmark: str = benchmarks.DEFAULT_BENCHMARK) -> dict[str, Any]:
    """Figures from the profile's stored statements. Raises ``sealing.LockedError`` when locked."""
    if benchmarks.get(benchmark) is None:
        raise ValueError(f"Unknown benchmark {benchmark!r}.")
    schemes = mf_ledger.ledger_for(user_id, keys)
    navs = amfi.navs_for([s.isin for s in schemes if s.isin])
    wanted = {benchmark} | {
        benchmark_id for s in schemes
        if (benchmark_id := benchmarks.benchmark_for(navs[s.isin].amfi_category if s.isin in navs else None, s.name)[0])
    }
    histories = {benchmark_id: benchmarks.nav_history(benchmark_id) for benchmark_id in wanted}
    return returns_from(schemes, navs, today, benchmark=benchmark, benchmark_navs=histories)

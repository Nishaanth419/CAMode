"""Mutual fund metrics from INDmoney for the categories that suit a goal, each with its source and date.

INDmoney's ``get_mf_by_category`` returns one row per scheme with a ``nav_date``
("15 Oct 2026"), expense ratio, risk label and 1/3/5-year returns; ``get_mf_funds_details``
adds the benchmark but carries no date at all (layout recorded with
``scripts/inspect_indmoney_shapes.py --funds``; ``tests/fixtures/indmoney_funds.py``).
So the returns are dated by the row's NAV date, and the expense ratio, risk label
and benchmark are reported as undated, with the time CA Mode fetched them. A figure
is ``current`` only when it has a date no older than ``FUND_STALE_DAYS``; ``stale`` and
``undated`` figures are labelled as such and never presented as current.

Missing is never zero: an absent, unreadable or implausible value (a return of
exactly 0, which INDmoney also sends for "no figure"; a return for a period longer
than the scheme has existed; an expense ratio outside 0.01–3%) is listed under
``missing`` with the reason, and no value is shown for it.

Only INDmoney categories in ``INDMONEY_CATEGORIES`` are compared, each mapped to one
``goal_suitability.CATEGORIES`` id, and a row is kept only when the category the
response gives maps to the category asked for. INDmoney's ratings and ranks are
dropped; the listing asks for the largest schemes by assets and shows them by name,
so nothing here ranks funds. Nothing is stored: results are cached in memory for
``CACHE_SECONDS`` and nothing is bought or sold. Not advice.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final

import httpx

from . import goal_suitability, memory, snapshots, users
from .agent import LocalTool
from .mcp_client import McpToolError, call_mcp_tool, fetch_mcp_session

logger = logging.getLogger(__name__)

SOURCE_LISTING: Final = "INDmoney get_mf_by_category"
SOURCE_DETAILS: Final = "INDmoney get_mf_funds_details"
NOTE: Final = (
    "Fund figures as INDmoney reports them, for categories that suit this goal under CA Mode's rules. Shown by name, "
    "not ranked; INDmoney's ratings and ranks are left out. Past returns do not predict future returns. Not advice or "
    "a recommendation of any fund."
)
SELECTION: Final = "The largest schemes by assets in INDmoney's listing for each category, shown alphabetically."
DATED_BY: Final = (
    "Returns are as of each scheme's NAV date in INDmoney's listing. INDmoney gives no date for the expense ratio, "
    "risk label or benchmark, so they are shown as undated with the time CA Mode fetched them."
)

FUNDS_PER_CATEGORY: Final = 5
CACHE_SECONDS: Final = 6 * 60 * 60
MAX_PARALLEL_CALLS: Final = 4
CALL_TIMEOUT_SECONDS: Final = 45
FETCH_DEADLINE_SECONDS: Final = 60

# INDmoney category slugs (the get_mf_by_category enum) that clearly fall in one CA Mode category. Anything else
# (aggressive hybrid, multi-cap, sector, thematic, credit-risk, long-duration, generic "index-funds"...) is not compared.
INDMONEY_CATEGORIES: Final = {
    "liquid_overnight": ("liquid", "overnight"),
    "short_duration_debt": ("ultra-short-duration", "low-duration", "money-market", "short-duration"),
    "conservative_hybrid": ("conservative-allocation",),
    "balanced_multi_asset": ("dynamic-asset-allocation", "balanced-allocation", "multi-asset-allocation"),
    "large_flexi_equity": ("large-cap", "flexi-cap", "large-cap-index-funds", "nifty-50-index-funds"),
    "mid_small_equity": (
        "mid-cap", "small-cap", "mid-cap-index-funds", "small-cap-index-funds", "nifty-midcap-index-funds",
        "nifty-smallcap-index-funds",
    ),
}
_SLUG_TO_CATEGORY: Final = {slug: category for category, slugs in INDMONEY_CATEGORIES.items() for slug in slugs}

# SEBI riskometer levels, lowest first.
RISK_LEVELS: Final = ("Low", "Low to Moderate", "Moderate", "Moderately High", "High", "Very High")
RETURN_PERIODS: Final = (("return1yPct", "return_1yr", 1), ("return3yPct", "return_3yr", 3), ("return5yPct", "return_5yr", 5))
METRIC_LABELS: Final = {
    "expenseRatioPct": "expense ratio", "return1yPct": "1-year return", "return3yPct": "3-year return",
    "return5yPct": "5-year return", "risk": "risk level", "benchmark": "benchmark",
}
_MIN_EXPENSE: Final = Decimal("0.01")
_MAX_EXPENSE: Final = Decimal(3)
_CENT: Final = Decimal("0.01")


class FundDataError(Exception):
    """A response CA Mode cannot read; the message is safe to show."""


def stale_days() -> int:
    """How many days old a NAV date may be and still count as current (weekends and holidays need a few)."""
    try:
        return max(1, int(os.getenv("FUND_STALE_DAYS", "5")))
    except ValueError:
        return 5


# ── Parsing (pure) ─────────────────────────────────────────────────────────────

def slug(text: Any) -> str | None:
    """INDmoney's category text ("flexi cap") as its slug ("flexi-cap"); None when there is none."""
    if not isinstance(text, str) or not text.strip():
        return None
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-") or None


def category_for(text: Any) -> str | None:
    """The CA Mode category id for INDmoney's category text or slug, or None when it is not mapped."""
    return _SLUG_TO_CATEGORY.get(slug(text) or "")


def parse_date(value: Any) -> date | None:
    """A date as INDmoney writes it ("15 Oct 2026", "12 June 2023", "2026-10-15"); None when absent or unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = re.sub(r"\bSept\b", "Sep", value.strip())
    for pattern in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def to_decimal(value: Any) -> Decimal | None:
    """A number from the response as ``Decimal``; None for anything else (booleans, blanks, text)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str) and re.fullmatch(r"\s*-?\d[\d,]*(?:\.\d+)?\s*%?\s*", value):
        try:
            return Decimal(value.strip().rstrip("%").replace(",", ""))
        except InvalidOperation:
            return None
    return None


def _number_text(value: Decimal) -> str:
    return format(value.quantize(_CENT, rounding=ROUND_HALF_UP), "f")


def _load(text: str, what: str) -> Any:
    """A tool result as JSON with every number a ``Decimal``, unwrapping a JSON string under ``result``."""
    try:
        value = json.loads(text, parse_float=Decimal)
        if isinstance(value, dict) and isinstance(value.get("result"), str):
            value = json.loads(value["result"], parse_float=Decimal)
    except (TypeError, ValueError) as exc:
        raise FundDataError(f"INDmoney's {what} was not in a readable form.") from exc
    if not isinstance(value, dict) or value.get("success") is False:
        raise FundDataError(f"INDmoney's {what} reported no data.")
    return value


def risk_label(value: Any) -> tuple[str, int | None] | None:
    """INDmoney's risk text ("Very High Risk") as a riskometer label and level 1–6; unknown text is kept as given."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = re.sub(r"\s+risk$", "", value.strip(), flags=re.IGNORECASE).strip()
    for level, label in enumerate(RISK_LEVELS, start=1):
        if text.lower() == label.lower():
            return label, level
    return value.strip(), None


def parse_benchmarks(text: str) -> dict[str, str]:
    """``get_mf_funds_details`` as {scheme id: benchmark name}; schemes without a benchmark are left out."""
    body = _load(text, "fund details")
    found: dict[str, str] = {}
    for item in body.get("data") or []:
        if not isinstance(item, dict):
            continue
        detail = ((item.get("data") or {}).get("fund_detail") or {}) if isinstance(item.get("data"), dict) else {}
        name = detail.get("benchmark_name") if isinstance(detail, dict) else None
        if item.get("fund_id") is not None and isinstance(name, str) and name.strip():
            found[str(item["fund_id"])] = name.strip()
    return found


def _years_after(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # 29 February
        return day.replace(year=day.year + years, day=28)


def _metric(value: Any, as_of: date | None, source: str, today: date, retrieved: str, *, level: int | None = None) -> dict[str, Any]:
    """One figure with where it came from and how current it is."""
    if as_of is None:
        status = "undated"
    elif as_of > today:
        status, as_of = "undated", None  # a future date cannot be right, so the figure is treated as undated
    else:
        status = "current" if (today - as_of).days <= stale_days() else "stale"
    item = {"value": value, "asOf": as_of.isoformat() if as_of else None, "status": status, "source": source}
    if status == "undated":
        item["retrievedAt"] = retrieved
    if level is not None:
        item["level"] = level
    return item


def parse_scheme(
    row: Mapping[str, Any], benchmarks: Mapping[str, str], today: date, retrieved: str, details_failed: bool = False,
) -> dict[str, Any]:
    """One ``get_mf_by_category`` row as a scheme with sourced, dated metrics and the reason for each missing one."""
    scheme_id = str(row["id"]) if row.get("id") is not None else None
    nav_date = parse_date(row.get("nav_date"))
    launched = parse_date(row.get("launch_date"))
    metrics: dict[str, Any] = {}
    missing: list[dict[str, str]] = []

    def gone(name: str, reason: str) -> None:
        missing.append({"field": name, "label": METRIC_LABELS[name], "reason": reason})

    expense = to_decimal(row.get("expense_ratio"))
    if expense is None:
        gone("expenseRatioPct", "INDmoney did not give it.")
    elif not _MIN_EXPENSE <= expense <= _MAX_EXPENSE:
        gone("expenseRatioPct", "INDmoney's figure is outside the range an expense ratio can take, so it is not shown.")
    else:
        metrics["expenseRatioPct"] = _metric(_number_text(expense), None, SOURCE_LISTING, today, retrieved)

    for name, key, years in RETURN_PERIODS:
        value = to_decimal(row.get(key))
        if launched and nav_date and _years_after(launched, years) > nav_date:
            gone(name, f"The scheme is younger than {years} year{'s' if years > 1 else ''} (launched {launched.isoformat()}).")
        elif value is None:
            gone(name, "INDmoney did not give it.")
        elif value == 0:
            gone(name, "INDmoney reported 0, which it also sends when it has no figure, so it is not shown.")
        elif not Decimal(-100) < value <= Decimal(1000):
            gone(name, "INDmoney's figure is not a possible return, so it is not shown.")
        else:
            metrics[name] = _metric(_number_text(value), nav_date, SOURCE_LISTING, today, retrieved)
    if nav_date is None and any(name in metrics for name, _, _ in RETURN_PERIODS):
        note = "INDmoney gave no readable NAV date, so these returns are undated and not shown as current."
        missing.append({"field": "navDate", "label": "NAV date", "reason": note})

    risk = risk_label(row.get("risk"))
    if risk is None:
        gone("risk", "INDmoney did not give it.")
    else:
        metrics["risk"] = _metric(risk[0], None, SOURCE_LISTING, today, retrieved, level=risk[1])

    benchmark = benchmarks.get(scheme_id or "")
    if benchmark:
        metrics["benchmark"] = _metric(benchmark, None, SOURCE_DETAILS, today, retrieved)
    else:
        gone("benchmark", "INDmoney's fund details could not be fetched." if details_failed else "INDmoney did not give it.")

    return {
        "schemeId": scheme_id, "name": str(row.get("name") or "").strip() or None,
        "category": row.get("category") if isinstance(row.get("category"), str) else None,
        "navDate": nav_date.isoformat() if nav_date else None, "metrics": metrics, "missing": missing,
    }


def parse_category(
    category_id: str, listing: str, details: Sequence[str], today: date, retrieved: str, *, details_failed: bool = False,
) -> dict[str, Any]:
    """The schemes in one ``get_mf_by_category`` response that INDmoney itself places in ``category_id``.

    Rows whose category is missing or maps elsewhere are listed in ``excluded`` with the reason, never shown.
    """
    body = _load(listing, "fund listing")
    benchmarks: dict[str, str] = {}
    for text in details:
        try:
            benchmarks.update(parse_benchmarks(text))
        except FundDataError:
            details_failed = True
    funds: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for row in body.get("data") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        given = row.get("category")
        mapped = category_for(given)
        if not name:
            excluded.append({"name": "A scheme without a name", "reason": "INDmoney gave no scheme name."})
        elif mapped is None:
            reason = (f"INDmoney lists its category as \"{given}\", which CA Mode does not map to a goal category."
                      if isinstance(given, str) and given.strip() else "INDmoney gave no category for it.")
            excluded.append({"name": name, "reason": reason})
        elif mapped != category_id:
            excluded.append({"name": name, "reason": f"INDmoney lists its category as \"{given}\", which belongs to another goal category."})
        else:
            funds.append(parse_scheme(row, benchmarks, today, retrieved, details_failed))
    funds.sort(key=lambda item: (item["name"] or "").casefold())
    return {"funds": funds, "excluded": excluded}


def _readable(listing: str | None) -> bool:
    try:
        _load(listing or "", "fund listing")
    except FundDataError:
        return False
    return True


def scheme_ids(listing: str) -> list[str]:
    """The scheme ids in a listing, for the details call."""
    try:
        body = _load(listing, "fund listing")
    except FundDataError:
        return []
    return [str(row["id"]) for row in body.get("data") or [] if isinstance(row, dict) and row.get("id") is not None]


# ── Fetching ───────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class CategoryFetch:
    """The raw INDmoney responses for one CA Mode category, kept in memory only."""

    listing: str | None
    details: list[str] = field(default_factory=list)
    details_failed: bool = False
    retrieved_at: float = 0.0
    error: str | None = None


_cache: dict[tuple[str, str], CategoryFetch] = {}


def clear_cache(user_id: str | None = None) -> None:
    """Forget cached fund responses (for one profile, or all)."""
    for key in [key for key in _cache if user_id is None or key[0] == user_id]:
        del _cache[key]


def _is_auth_failure(error: Exception) -> bool:
    text = str(error).lower()
    return "401" in text or "invalid_token" in text or "unauthorized" in text


async def _fetch_category(
    category_id: str, call: Any, limit: asyncio.Semaphore,
) -> CategoryFetch:
    arguments = {"categories": list(INDMONEY_CATEGORIES[category_id]), "size": FUNDS_PER_CATEGORY,
                 "sort_key": "aum", "sort_asc": False}
    retrieved = time.time()
    try:
        async with limit:
            listing = await call("get_mf_by_category", arguments)
    except McpToolError:
        logger.warning("fund_listing_failed", extra={"category": category_id})
        return CategoryFetch(None, retrieved_at=retrieved, error="INDmoney's fund listing failed for this category.")
    fetch = CategoryFetch(listing if isinstance(listing, str) else json.dumps(listing), retrieved_at=retrieved)
    ids = scheme_ids(fetch.listing)
    if ids:
        try:
            async with limit:
                detail = await call("get_mf_funds_details", {"fund_ids": ",".join(ids[:10])})
            fetch.details.append(detail if isinstance(detail, str) else json.dumps(detail))
        except McpToolError:
            fetch.details_failed = True
    return fetch


async def fetch_categories(user_id: str, category_ids: Sequence[str]) -> dict[str, CategoryFetch] | str:
    """The INDmoney responses for each category (cached for ``CACHE_SECONDS``), or a reason none could be fetched."""
    now = time.time()
    found = {cid: _cache[(user_id, cid)] for cid in category_ids
             if (user_id, cid) in _cache and now - _cache[(user_id, cid)].retrieved_at < CACHE_SECONDS}
    wanted = [cid for cid in category_ids if cid not in found]
    if not wanted:
        return found
    token = await users.user_access_token(user_id)
    if not token:
        return "INDmoney is not connected, or its session has expired. Connect it in your profile to see fund data."
    for attempt in range(2):
        try:
            async with asyncio.timeout(FETCH_DEADLINE_SECONDS), httpx.AsyncClient(timeout=CALL_TIMEOUT_SECONDS) as client:
                session = await fetch_mcp_session(users.MCP_RESOURCE, token)

                async def call(name: str, arguments: dict[str, Any]) -> Any:
                    return await call_mcp_tool(
                        users.MCP_RESOURCE, token, session["protocolVersion"], session["sessionId"], name, arguments,
                        client=client,
                    )

                limit = asyncio.Semaphore(MAX_PARALLEL_CALLS)
                results = await asyncio.gather(*(_fetch_category(cid, call, limit) for cid in wanted))
            break
        except TimeoutError:
            logger.warning("fund_data_fetch_timed_out")
            return "INDmoney's fund data took too long to arrive. Try again later."
        except (RuntimeError, httpx.HTTPError) as exc:
            if attempt == 0 and _is_auth_failure(exc):
                token = await users.user_access_token(user_id, force_refresh=True)
                if token:
                    continue
                return "INDmoney rejected the saved session. Reconnect it in your profile to see fund data."
            logger.warning("fund_data_fetch_failed", extra={"error_type": type(exc).__name__})
            return "INDmoney's fund data could not be reached. Try again later."
    for cid, fetch in zip(wanted, results, strict=True):
        # Only complete, readable responses are kept, so a failure is retried on the next request.
        if fetch.error is None and not fetch.details_failed and _readable(fetch.listing):
            _cache[(user_id, cid)] = fetch
        found[cid] = fetch
    return found


# ── Comparison for a goal ──────────────────────────────────────────────────────

def _retrieved_text(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_comparison(
    suitability: Mapping[str, Any], fetched: Mapping[str, CategoryFetch] | str, today: date,
) -> dict[str, Any]:
    """The comparison for the categories ``suitability`` does not rule out, from fetched responses. Pure."""
    eligible = [item for item in suitability["categories"] if item["verdict"] != "unsuitable"]
    left_out = [item["label"] for item in suitability["categories"] if item["verdict"] == "unsuitable"]
    head = {
        "source": f"{SOURCE_LISTING} and {SOURCE_DETAILS}", "selection": SELECTION, "datedBy": DATED_BY,
        "staleAfterDays": stale_days(), "asOf": today.isoformat(), "note": NOTE,
        "unsuitableCategories": left_out,
    }
    if isinstance(fetched, str):
        return {"available": False, "reason": fetched, **head, "categories": []}
    categories = []
    for item in eligible:
        entry = {
            "id": item["id"], "label": item["label"], "verdict": item["verdict"],
            "indmoneyCategories": list(INDMONEY_CATEGORIES[item["id"]]),
        }
        fetch = fetched.get(item["id"])
        if fetch is None or fetch.listing is None:
            entry.update(available=False, reason=(fetch.error if fetch else None) or "Not fetched.", funds=[], excluded=[])
        else:
            retrieved = _retrieved_text(fetch.retrieved_at)
            try:
                parsed = parse_category(item["id"], fetch.listing, fetch.details, today, retrieved,
                                        details_failed=fetch.details_failed)
            except FundDataError as exc:
                entry.update(available=False, reason=str(exc), funds=[], excluded=[])
            else:
                entry.update(available=bool(parsed["funds"]), retrievedAt=retrieved, **parsed)
                if not parsed["funds"]:
                    entry["reason"] = "INDmoney listed no scheme in this category."
        categories.append(entry)
    available = any(entry["available"] for entry in categories)
    result = {"available": available, **head, "categories": categories}
    if not available:
        result["reason"] = "INDmoney returned no fund data for the categories that suit this goal."
    return result


async def goal_comparison(user: dict[str, Any], goal: dict[str, Any], today: date, category_id: str | None = None) -> dict[str, Any]:
    """Fetch and build the comparison for a saved goal, optionally for one category."""
    suitability = goal_suitability.evaluate(goal, today)
    if category_id is not None:
        verdicts = {item["id"]: item["verdict"] for item in suitability["categories"]}
        if category_id not in verdicts:
            raise FundDataError("Unknown category.")
        if verdicts[category_id] == "unsuitable":
            raise FundDataError("That category is unsuitable for this goal under CA Mode's rules, so it is not compared.")
        suitability = {**suitability, "categories": [item for item in suitability["categories"]
                                                     if item["id"] == category_id or item["verdict"] == "unsuitable"]}
    wanted = [item["id"] for item in suitability["categories"] if item["verdict"] != "unsuitable"]
    fetched = await fetch_categories(user["id"], wanted) if wanted else {}
    result = build_comparison(suitability, fetched, today)
    return {"goalId": goal["id"], "goalName": goal["name"], **result}


# ── Agent tool ─────────────────────────────────────────────────────────────────

MAX_TOOL_PAYLOAD_CHARS: Final = 5500


def _tool_fund(fund: Mapping[str, Any]) -> dict[str, Any]:
    metrics = fund["metrics"]
    item: dict[str, Any] = {"name": fund["name"], "indmoneyCategory": fund["category"]}
    for name in ("expenseRatioPct", "return1yPct", "return3yPct", "return5yPct"):
        if name in metrics:
            item[name] = metrics[name]["value"]
    returns = [metrics[name] for name, _, _ in RETURN_PERIODS if name in metrics]
    if returns:
        item["returnsAsOf"] = returns[0]["asOf"]
        item["returnsStatus"] = returns[0]["status"]
    if "risk" in metrics:
        item["risk"] = metrics["risk"]["value"]
    if "benchmark" in metrics:
        item["benchmark"] = metrics["benchmark"]["value"]
    if fund["missing"]:
        item["missing"] = {entry["label"]: entry["reason"] for entry in fund["missing"]}
    return item


def tool_payload(result: Mapping[str, Any]) -> dict[str, Any]:
    """The comparison for the model: compact, every figure next to its date status, under the Groq limit."""
    head = {key: result[key] for key in ("goalName", "available", "source", "selection", "datedBy", "staleAfterDays",
                                          "asOf", "note", "unsuitableCategories") if key in result}
    if not result["available"]:
        head["reason"] = result.get("reason")
    categories = []
    for entry in result["categories"]:
        item: dict[str, Any] = {"category": entry["label"], "verdict": entry["verdict"]}
        if entry.get("retrievedAt"):
            item["retrievedAt"] = entry["retrievedAt"]
        if entry["available"]:
            item["funds"] = [_tool_fund(fund) for fund in entry["funds"]]
        else:
            item["unavailable"] = entry.get("reason")
        if entry.get("excluded"):
            item["leftOut"] = len(entry["excluded"])
        categories.append(item)
    payload = {**head, "categories": categories}
    # Too long for Groq: keep only the names of missing fields, then drop funds from the largest category (never a
    # whole category).
    if len(json.dumps(payload, ensure_ascii=False)) > MAX_TOOL_PAYLOAD_CHARS:
        for item in categories:
            for fund in item.get("funds", []):
                if "missing" in fund:
                    fund["missing"] = sorted(fund["missing"])
    while len(json.dumps(payload, ensure_ascii=False)) > MAX_TOOL_PAYLOAD_CHARS:
        largest = max(categories, key=lambda item: len(item.get("funds", [])), default=None)
        if not largest or len(largest.get("funds", [])) <= 1:
            break
        largest["funds"].pop()
        largest["truncated"] = "More schemes are on the Goals page; ask about this category alone to see them."
    return payload


def fund_comparison_tool(user: dict[str, Any], *, today: date | None = None) -> LocalTool:
    """Read-only fund figures for a saved goal's suitable categories (Portfolio Analyzer only)."""

    async def get_goal_fund_comparison(arguments: dict[str, Any]) -> str:
        day = today or snapshots.local_now().date()
        goals = await asyncio.to_thread(memory.list_goals, user["id"], user["keys"])
        wanted = str(arguments.get("goalName") or "").strip().lower()
        chosen = [goal for goal in goals if goal["name"].lower() == wanted] or (
            [goal for goal in goals if wanted and wanted in goal["name"].lower()] if wanted else goals
        )
        if len(chosen) != 1:
            reason = "Name one goal." if chosen else "No goal has that name."
            return json.dumps({"error": reason, "goalNames": [goal["name"] for goal in goals]})
        try:
            result = await goal_comparison(user, chosen[0], day, arguments.get("categoryId"))
        except FundDataError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(tool_payload(result), ensure_ascii=False, separators=(",", ":"))

    return LocalTool(
        name="get_goal_fund_comparison",
        description=(
            "Mutual fund figures from INDmoney (expense ratio, 1/3/5-year returns, risk level, benchmark) for the "
            "investment categories that suit one saved goal, with the date and source of each and what is missing. "
            "Schemes are listed by name, not ranked. Quote figures with their date as returned; say when they are "
            "stale or undated; never rank, pick or recommend a fund."
        ),
        input_schema={"type": "object", "properties": {
            "goalName": {"type": "string", "description": "The saved goal; may be omitted when there is only one."},
            "categoryId": {"type": "string", "enum": [item["id"] for item in goal_suitability.CATEGORIES],
                           "description": "Only this category; omit for every category that suits the goal."},
        }, "additionalProperties": False},
        handler=get_goal_fund_comparison,
    )

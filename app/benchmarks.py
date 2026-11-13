"""Benchmarks for mutual fund returns: five public index and liquid funds and their NAV history.

Each benchmark is a direct-growth fund whose NAV includes reinvested dividends and is
net of a small expense ratio, so it approximates the total-return index. Scheme codes
and ISINs were checked against AMFI's live ``NAVAll.txt`` on 2026-11-13; each is the
direct-growth fund with the longest history on its index.

History lives in the plain ``benchmark_navs`` table. The scheduler (``refresh``) seeds a
benchmark's full history from mfapi.in (``MFAPI_URL``, ``/mf/<scheme code>``) when it has
none, or when its latest NAV is over ``RESEED_AFTER_DAYS`` old (at most once a day), and adds each day's NAV
from the AMFI file ``amfi`` already stores. The requests name only these five fixed
funds: nothing about any user or their holdings is sent. Off unless ``MF_DATA_ENABLED``.

``benchmark_for()`` picks a fund's default benchmark from its AMFI category
(``CATEGORY_BENCHMARK``) or, for an index fund, from the index in its name; a fund with
none gets a reason instead.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

import httpx

from . import amfi
from .db import connect_db

logger = logging.getLogger("ca_mode.benchmarks")


@dataclass(frozen=True)
class Benchmark:
    id: str
    label: str
    scheme_code: str
    isin: str
    scheme_name: str


BENCHMARKS: Final = (
    Benchmark("nifty50", "Nifty 50", "120716", "INF789F01XA0", "UTI Nifty 50 Index Fund - Direct Plan - Growth"),
    Benchmark("nifty500", "Nifty 500", "147625", "INF247L01957",
              "Motilal Oswal Nifty 500 Index Fund - Direct Plan - Growth"),
    Benchmark("midcap150", "Nifty Midcap 150", "147622", "INF247L01916",
              "Motilal Oswal Nifty Midcap 150 Index Fund - Direct Plan - Growth"),
    Benchmark("smallcap250", "Nifty Smallcap 250", "147623", "INF247L01932",
              "Motilal Oswal Nifty Smallcap 250 Index Fund - Direct Plan - Growth"),
    Benchmark("liquid", "Liquid fund", "119800", "INF200K01UT4", "SBI LIQUID FUND - Direct Plan - Growth"),
)
BENCHMARK_IDS: Final = tuple(b.id for b in BENCHMARKS)
DEFAULT_BENCHMARK: Final = "nifty50"
INDEX_NOTE: Final = (
    "Benchmarks use a direct-growth index fund's NAV: it includes reinvested dividends and is net of a small "
    "expense ratio, so it approximates the total-return index."
)

DEFAULT_MFAPI_URL: Final = "https://api.mfapi.in"
# Over 4 days old (a Friday NAV on a Tuesday after a Monday holiday is still current) means AMFI downloads failed:
# the full history fills the gap before it is wider than mf_math's 5-day rule.
RESEED_AFTER_DAYS: Final = 4
RETRY_SECONDS: Final = amfi.RETRY_SECONDS
# After a download, the same benchmark is not asked for again within a day, even if mfapi.in's own data is old.
SEED_AGAIN_SECONDS: Final = 24 * 3600
TIMEOUT_SECONDS: Final = 30

# Normalised AMFI sub-category (see _sub_category) → benchmark. Exact matches only.
CATEGORY_BENCHMARK: Final[dict[str, str]] = {
    "large cap": "nifty50", "flexi cap": "nifty50", "elss": "nifty50", "focused": "nifty50", "value": "nifty50",
    "contra": "nifty50", "dividend yield": "nifty50",
    "multi cap": "nifty500", "large & mid cap": "nifty500",
    "mid cap": "midcap150",
    "small cap": "smallcap250",
    "liquid": "liquid", "overnight": "liquid", "money market": "liquid",
}
# An index fund is matched on the index in its (normalised) name, first match wins;
# any of the variant words means a different index.
INDEX_NAME_BENCHMARK: Final = (
    ("nifty 500", "nifty500"), ("midcap 150", "midcap150"), ("smallcap 250", "smallcap250"),
    ("nifty 50", "nifty50"), ("sensex", "nifty50"),
)
INDEX_VARIANT_WORDS: Final = ("next", "equal", "momentum", "quality", "value", "alpha", "low vol")
# Why a category has no benchmark, by a word in the category; first match wins.
NO_BENCHMARK_REASONS: Final = (
    (("hybrid",), "Hybrid funds mix asset classes, so no single index fits."),
    (("sectoral", "thematic"), "Sectoral and thematic funds have no broad index benchmark here."),
    (("fof", "fund of funds", "overseas", "gold", "silver", "etf"),
     "Funds of funds, international and gold funds have no benchmark here."),
    (("debt", "income"), "Only liquid, overnight and money market debt funds have a benchmark here (a liquid fund)."),
)
INDEX_VARIANT_REASON: Final = "This index fund tracks an index that has no benchmark here."
UNKNOWN_CATEGORY_REASON: Final = "The fund's AMFI category is not known."

# In memory: when each benchmark was last downloaded and whether that failed.
_attempts: dict[str, tuple[datetime, bool]] = {}


def reset_state() -> None:
    _attempts.clear()


def get(benchmark_id: str) -> Benchmark | None:
    return next((b for b in BENCHMARKS if b.id == benchmark_id), None)


def mfapi_url() -> str:
    return (os.getenv("MFAPI_URL", "").strip() or DEFAULT_MFAPI_URL).rstrip("/")


def _sub_category(category: str) -> str:
    sub = category.split(" - ", 1)[-1].strip().lower()
    sub = re.sub(r"\s+fund$", "", sub)
    return "elss" if sub.startswith("elss") else sub


def _index_name(name: str) -> str:
    text = re.sub(r"\s+", " ", name.lower())
    text = re.sub(r"nifty\s*50(?!\d)", "nifty 50", text)
    return text.replace("mid cap", "midcap").replace("small cap", "smallcap")


def benchmark_for(amfi_category: str | None, scheme_name: str) -> tuple[str | None, str | None]:
    """The fund's default benchmark id, or ``None`` with the reason it has none."""
    if not amfi_category:
        return None, UNKNOWN_CATEGORY_REASON
    lowered = amfi_category.lower()
    if "index fund" in lowered:
        name = _index_name(scheme_name)
        if not any(word in name for word in INDEX_VARIANT_WORDS):
            for needle, benchmark_id in INDEX_NAME_BENCHMARK:
                if needle in name:
                    return benchmark_id, None
        return None, INDEX_VARIANT_REASON
    if benchmark_id := CATEGORY_BENCHMARK.get(_sub_category(amfi_category)):
        return benchmark_id, None
    for words, reason in NO_BENCHMARK_REASONS:
        if any(word in lowered for word in words):
            return None, reason
    return None, f"No benchmark is set for the AMFI category {amfi_category}."


def _positive(text: Any) -> Decimal | None:
    try:
        value = Decimal(str(text).strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() and value > 0 else None


def parse_mfapi(payload: dict[str, Any], scheme_code: str) -> list[tuple[date, Decimal]]:
    """mfapi.in's ``/mf/<code>`` history, ascending; rows without a readable date or positive NAV are skipped."""
    if payload.get("status") != "SUCCESS":
        raise ValueError("mfapi.in did not return the history.")
    if str((payload.get("meta") or {}).get("scheme_code")) != scheme_code:
        raise ValueError("mfapi.in returned another scheme.")
    rows: dict[date, Decimal] = {}
    for row in payload.get("data") or []:
        try:
            day = datetime.strptime(str(row.get("date")), "%d-%m-%Y").date()
        except (ValueError, AttributeError):
            continue
        if (nav := _positive(row.get("nav"))) is not None:
            rows[day] = nav
    return sorted(rows.items())


def store_navs(benchmark_id: str, rows: Iterable[tuple[date, Decimal]]) -> int:
    values = [(benchmark_id, day.isoformat(), str(nav)) for day, nav in rows]
    with connect_db() as connection:
        connection.executemany(
            "INSERT OR REPLACE INTO benchmark_navs(benchmark_id, nav_date, nav) VALUES (?, ?, ?)", values,
        )
    return len(values)


def nav_history(benchmark_id: str) -> list[tuple[date, Decimal]]:
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT nav_date, nav FROM benchmark_navs WHERE benchmark_id = ? ORDER BY nav_date", (benchmark_id,),
        ).fetchall()
    return [(date.fromisoformat(row["nav_date"]), Decimal(row["nav"])) for row in rows]


def latest_nav_date(benchmark_id: str) -> date | None:
    with connect_db() as connection:
        row = connection.execute(
            "SELECT MAX(nav_date) AS latest FROM benchmark_navs WHERE benchmark_id = ?", (benchmark_id,),
        ).fetchone()
    return date.fromisoformat(row["latest"]) if row and row["latest"] else None


async def _download(scheme_code: str, *, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """GET one benchmark's full history. Only the fixed scheme code is sent; redirects are not followed."""
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, headers={"User-Agent": amfi.USER_AGENT},
                                 transport=transport) as client:
        response = await client.get(f"{mfapi_url()}/mf/{scheme_code}")
        response.raise_for_status()
        return response.json()


def _due(benchmark: Benchmark, today: date) -> bool:
    latest = latest_nav_date(benchmark.id)
    return latest is None or latest < today - timedelta(days=RESEED_AFTER_DAYS)


async def seed_if_needed(now: datetime, *, fetch: Callable[[str], Awaitable[dict[str, Any]]] | None = None) -> list[str]:
    """Fetch the history of each benchmark with none, or none recent; a failed one waits ``RETRY_SECONDS``.

    Returns the ids stored.
    """
    if not amfi.is_enabled():
        return []
    stored: list[str] = []
    for benchmark in BENCHMARKS:
        if attempt := _attempts.get(benchmark.id):
            at, failed = attempt
            if (now - at).total_seconds() < (RETRY_SECONDS if failed else SEED_AGAIN_SECONDS):
                continue
        if not await asyncio.to_thread(_due, benchmark, now.date()):
            continue
        try:
            payload = await (fetch or _download)(benchmark.scheme_code)
            rows = await asyncio.to_thread(parse_mfapi, payload, benchmark.scheme_code)
            if not rows:
                raise ValueError("no NAV rows in the history")
            count = await asyncio.to_thread(store_navs, benchmark.id, rows)
        except Exception as exc:
            _attempts[benchmark.id] = (now, True)
            logger.warning("benchmark_seed_failed", extra={"benchmark": benchmark.id, "error": type(exc).__name__})
            continue
        _attempts[benchmark.id] = (now, False)
        stored.append(benchmark.id)
        logger.info("benchmark_seeded", extra={"benchmark": benchmark.id, "rows": count})
    return stored


def append_from_amfi() -> int:
    """Copy today's NAV from ``amfi_navs`` into each benchmark that already has history; returns rows added."""
    added = 0
    with connect_db() as connection:
        for benchmark in BENCHMARKS:
            seeded = connection.execute(
                "SELECT 1 FROM benchmark_navs WHERE benchmark_id = ? LIMIT 1", (benchmark.id,),
            ).fetchone()
            row = connection.execute("SELECT nav, nav_date FROM amfi_navs WHERE isin = ?", (benchmark.isin,)).fetchone()
            if not seeded or not row:
                continue
            connection.execute(
                "INSERT OR REPLACE INTO benchmark_navs(benchmark_id, nav_date, nav) VALUES (?, ?, ?)",
                (benchmark.id, row["nav_date"], row["nav"]),
            )
            added += 1
    return added


async def refresh(now: datetime, *, fetch: Callable[[str], Awaitable[dict[str, Any]]] | None = None) -> None:
    """The scheduler's job: seed what is missing or stale, then add today's NAVs from the AMFI file."""
    if not amfi.is_enabled():
        return
    await seed_if_needed(now, fetch=fetch)
    await asyncio.to_thread(append_from_amfi)

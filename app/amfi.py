"""Today's mutual fund NAVs from AMFI's public ``NAVAll.txt``.

The scheduler downloads the file once a day (``refresh_if_due``) and keeps one row
per ISIN in the plain ``amfi_navs`` table. It is public data, the same request for
every user: nothing personal is sent, sealed, exported or deleted with a profile.

File layout: a header line, then section headings such as
``Open Ended Schemes(Equity Scheme - Large Cap Fund)`` (the text in brackets is the
scheme's AMFI category), AMC name lines, and data lines
``code;ISIN payout or growth;ISIN reinvestment;name;[plan;option;]NAV;dd-Mon-yyyy`` (the plan and
option columns appeared in 2026; they are joined to the name). Both ISINs of a
line map to its NAV; ``-`` is no ISIN, and ``N.A.`` or other non-numbers are skipped.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final

import httpx

from .db import connect_db

logger = logging.getLogger("ca_mode.amfi")

DEFAULT_NAV_URL: Final = "https://www.amfiindia.com/spages/NAVAll.txt"
USER_AGENT: Final = "CA-Mode/1.0"
RETRY_SECONDS: Final = 3600
TIMEOUT_SECONDS: Final = 30
MAX_REDIRECTS: Final = 3

_HEADING_RE: Final = re.compile(r"^(?:Open|Close|Interval)[^()]*\((.+)\)\s*$")

# In memory: the local date of the last successful download and the time of the last failed try.
_state: dict[str, object] = {"succeeded_on": None, "failed_at": None}


@dataclass(frozen=True)
class NavRow:
    isin: str
    scheme_code: str
    scheme_name: str
    amfi_category: str | None
    nav: Decimal
    nav_date: date


def is_enabled() -> bool:
    return os.getenv("MF_DATA_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


def nav_url() -> str:
    return os.getenv("AMFI_NAV_URL", "").strip() or DEFAULT_NAV_URL


def reset_state() -> None:
    _state.update(succeeded_on=None, failed_at=None)


def _nav(text: str) -> Decimal | None:
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() and value > 0 else None


def parse_nav_file(text: str) -> list[NavRow]:
    """One row per ISIN; lines without a readable NAV or date are skipped."""
    rows: list[NavRow] = []
    category: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if ";" not in line:
            if match := _HEADING_RE.match(line):
                category = match.group(1).strip()
            continue  # An AMC name line.
        parts = [part.strip() for part in line.split(";")]
        if len(parts) < 6 or not parts[0].isdigit():
            continue  # The header line or a malformed line.
        # Six columns until 2026; since then Plan and Option sit between the name and the NAV.
        code, growth, reinvest = parts[:3]
        name = " - ".join(part for part in parts[3:-2] if part)
        nav_text, date_text = parts[-2], parts[-1]
        nav = _nav(nav_text)
        try:
            nav_date = datetime.strptime(date_text, "%d-%b-%Y").date()
        except ValueError:
            continue
        if nav is None:
            continue
        for isin in (growth, reinvest):
            if isin and isin != "-":
                rows.append(NavRow(isin, code, name, category, nav, nav_date))
    return rows


def store_navs(rows: list[NavRow]) -> int:
    with connect_db() as connection:
        connection.executemany(
            "INSERT OR REPLACE INTO amfi_navs(isin, scheme_code, scheme_name, amfi_category, nav, nav_date) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(r.isin, r.scheme_code, r.scheme_name, r.amfi_category, str(r.nav), r.nav_date.isoformat()) for r in rows],
        )
    return len(rows)


def navs_for(isins: Collection[str]) -> dict[str, NavRow]:
    wanted = sorted({isin for isin in isins if isin})
    if not wanted:
        return {}
    with connect_db() as connection:
        rows = connection.execute(
            f"SELECT * FROM amfi_navs WHERE isin IN ({','.join('?' * len(wanted))})", wanted,
        ).fetchall()
    return {
        row["isin"]: NavRow(row["isin"], row["scheme_code"], row["scheme_name"], row["amfi_category"],
                            Decimal(row["nav"]), date.fromisoformat(row["nav_date"]))
        for row in rows
    }


def _check_amfi_url(url: httpx.URL) -> None:
    if url.scheme != "https" or not (url.host == "amfiindia.com" or url.host.endswith(".amfiindia.com")):
        raise ValueError("The NAV file must come from https://*.amfiindia.com.")


async def _download(*, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """GET the NAV file, following AMFI's redirect (to portal.amfiindia.com) but never off amfiindia.com."""
    url = httpx.URL(nav_url())
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}, transport=transport) as client:
        for _ in range(MAX_REDIRECTS + 1):
            _check_amfi_url(url)
            response = await client.get(url)
            if not response.is_redirect:
                response.raise_for_status()
                return response.text
            url = url.join(response.headers["Location"])
    raise ValueError("Too many redirects for the NAV file on amfiindia.com.")


async def refresh_if_due(now: datetime, *, fetch: Callable[[], Awaitable[str]] | None = None) -> bool:
    """Download and store the NAV file once per local date; a failed try waits ``RETRY_SECONDS``.

    ``now`` is the scheduler's local time. Returns True when rows were stored.
    """
    if not is_enabled() or _state["succeeded_on"] == now.date():
        return False
    failed_at = _state["failed_at"]
    if isinstance(failed_at, datetime) and (now - failed_at).total_seconds() < RETRY_SECONDS:
        return False
    try:
        text = await (fetch or _download)()
        rows = await asyncio.to_thread(parse_nav_file, text)
        if not rows:
            raise ValueError("no NAV rows in the file")
        count = await asyncio.to_thread(store_navs, rows)
    except Exception as exc:
        _state["failed_at"] = now
        logger.warning("amfi_refresh_failed", extra={"error": type(exc).__name__})
        return False
    _state.update(succeeded_on=now.date(), failed_at=None)
    logger.info("amfi_navs_refreshed", extra={"rows": count})
    return True

"""Anonymous, local-only page-view counts.

One row per local day and app page holds a number and nothing else: no profile, session,
IP address, browser details or URL beyond the page name (``#/activity/<id>`` counts as
``activity``). Counts are taken only for profiles that accepted the current notice and save
history on this computer (``snapshots.snapshots_allowed()``), are kept ``RETENTION_DAYS``
days, are never sent anywhere, and are read with ``python -m scripts.page_views``. Because
they are not linked to a profile, export, history clearing and profile deletion leave them.
``PAGE_VIEWS_ENABLED=false`` turns counting off.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from .db import connect_db

RETENTION_DAYS: Final = 90
MAX_REPORT_DAYS: Final = RETENTION_DAYS

# The hash routes in frontend/js/pages.js (ROUTE_META), including its not-found page.
Route = Literal[
    "home", "chat", "portfolio", "investments", "planning", "markets",
    "goals", "reports", "activity", "chats", "not-found",
]


class PageViewInput(BaseModel):
    """The app page that was opened."""

    model_config = ConfigDict(extra="forbid")

    route: Route


def is_enabled() -> bool:
    return os.getenv("PAGE_VIEWS_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


def record(route: Route, *, today: date) -> None:
    """Add one view of ``route`` on ``today`` and drop counts older than the retention period."""
    cutoff = (today - timedelta(days=RETENTION_DAYS - 1)).isoformat()
    with connect_db() as connection:
        connection.execute(
            "INSERT INTO page_views(day, route, views) VALUES (?, ?, 1) "
            "ON CONFLICT(day, route) DO UPDATE SET views = views + 1",
            (today.isoformat(), route),
        )
        connection.execute("DELETE FROM page_views WHERE day < ?", (cutoff,))


def counts(*, days: int, today: date) -> list[dict[str, str | int]]:
    """Return ``{day, route, views}`` rows for the last ``days`` days (today included), newest first."""
    days = max(1, min(days, MAX_REPORT_DAYS))
    since = (today - timedelta(days=days - 1)).isoformat()
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT day, route, views FROM page_views WHERE day >= ? ORDER BY day DESC, views DESC, route",
            (since,),
        ).fetchall()
    return [{"day": row["day"], "route": row["route"], "views": row["views"]} for row in rows]


def totals(rows: list[dict[str, str | int]]) -> dict[str, int]:
    """Sum ``counts()`` rows per page, most viewed first."""
    summed: dict[str, int] = {}
    for row in rows:
        summed[str(row["route"])] = summed.get(str(row["route"]), 0) + int(row["views"])
    return dict(sorted(summed.items(), key=lambda item: (-item[1], item[0])))

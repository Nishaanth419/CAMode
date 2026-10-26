"""Monthly credits for paid plans that have them (the Individual plan: ``plans.Plan.monthly_credits``).

One credit is ``TOKENS_PER_CREDIT`` model tokens (prompt plus completion, as the provider reports them).
An answered chat question costs its tokens rounded up to whole credits, and at least one credit. A
question that gets no answer costs nothing, like the Free plan's daily count.

The month runs from the day the plan started: a plan started on the 14th renews on the 14th of each
month (on the last day of a shorter month). Monthly credits are used first and do not carry over;
top-up credits (``python -m scripts.plans topup``) are used once the month's credits run out and are
kept until used. A question is refused with ``CreditsExhausted`` only when nothing is left, so the last
answer of a month may cost more than what remained; the rest of its cost is not charged.

State lives on the ``users`` row: ``plan_started_at`` (local date), ``credit_period_start`` (the month
being counted), ``credits_used`` (monthly credits used in it) and ``topup_credits``.
"""

from __future__ import annotations

import calendar
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final

from .db import connect_db
from .snapshots import SNAPSHOT_TIMEZONE

TOKENS_PER_CREDIT: Final = 1_000


class CreditsExhausted(Exception):
    """The profile's monthly and top-up credits are used up."""

    def __init__(self, renews_on: date) -> None:
        self.renews_on = renews_on
        super().__init__(
            f"You've used this month's credits. They renew on {renews_on.day} {renews_on:%B}, or top up to keep asking."
        )


@dataclass(frozen=True, slots=True)
class Balance:
    monthly: int
    used: int
    top_up: int
    period_start: date
    renews_on: date

    @property
    def monthly_left(self) -> int:
        return max(0, self.monthly - self.used)

    @property
    def remaining(self) -> int:
        return self.monthly_left + self.top_up

    def as_json(self) -> dict[str, Any]:
        return {
            "monthly": self.monthly, "used": self.used, "monthlyRemaining": self.monthly_left,
            "topUp": self.top_up, "remaining": self.remaining, "renewsOn": self.renews_on.isoformat(),
        }


def _on_day(year: int, month: int, day: int) -> date:
    """``day`` of the month, or the month's last day when it is shorter."""
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _add_months(year: int, month: int, months: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + months
    return index // 12, index % 12 + 1


def period_bounds(started: date, today: date) -> tuple[date, date]:
    """The month containing ``today`` for a plan started on ``started``: (its first day, the renewal day)."""
    year, month = today.year, today.month
    start = _on_day(year, month, started.day)
    if start > today:
        year, month = _add_months(year, month, -1)
        start = _on_day(year, month, started.day)
    next_year, next_month = _add_months(year, month, 1)
    return start, _on_day(next_year, next_month, started.day)


def local_today(now: datetime) -> date:
    return now.astimezone(SNAPSHOT_TIMEZONE).date()


def _current(connection: Any, user_id: str, monthly: int, today: date) -> Balance:
    """Read the balance, starting the plan today when it has no start and rolling into a new month."""
    row = connection.execute(
        "SELECT plan_started_at, credit_period_start, credits_used, topup_credits FROM users WHERE id = ?", (user_id,),
    ).fetchone()
    if row is None:
        raise LookupError(user_id)
    started = date.fromisoformat(row["plan_started_at"]) if row["plan_started_at"] else today
    period_start, renews_on = period_bounds(started, today)
    same_month = row["credit_period_start"] == period_start.isoformat()
    used = int(row["credits_used"]) if same_month else 0
    if row["plan_started_at"] is None or not same_month:
        connection.execute(
            "UPDATE users SET plan_started_at = ?, credit_period_start = ?, credits_used = ? WHERE id = ?",
            (started.isoformat(), period_start.isoformat(), used, user_id),
        )
    return Balance(
        monthly=monthly, used=used, top_up=int(row["topup_credits"]), period_start=period_start, renews_on=renews_on,
    )


def balance(user_id: str, monthly: int, *, now: datetime) -> Balance:
    with connect_db() as connection:
        return _current(connection, user_id, monthly, local_today(now))


def ensure_available(user_id: str, monthly: int, *, now: datetime) -> Balance:
    """Return the balance, or raise ``CreditsExhausted`` when nothing is left to answer with."""
    current = balance(user_id, monthly, now=now)
    if current.remaining <= 0:
        raise CreditsExhausted(current.renews_on)
    return current


def credits_for_tokens(tokens: int | None) -> int:
    """Whole credits for an answer's tokens; one when the provider reported none."""
    if not tokens or tokens <= 0:
        return 1
    return max(1, math.ceil(tokens / TOKENS_PER_CREDIT))


def tokens_used(events: list[dict[str, Any]] | None) -> int | None:
    """Prompt plus completion tokens across an answer's model calls (``RunRecorder`` events); None when none reported."""
    total, seen = 0, False
    for event in events or []:
        if not isinstance(event, dict) or event.get("kind") != "model_result":
            continue
        for key in ("promptTokens", "completionTokens"):
            value = event.get(key)
            if isinstance(value, int) and value >= 0:
                total += value
                seen = True
    return total if seen else None


def charge(user_id: str, monthly: int, credits: int, *, now: datetime) -> Balance:
    """Take ``credits`` for an answer: monthly credits first, then top-ups; never below zero."""
    today = local_today(now)
    with connect_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = _current(connection, user_id, monthly, today)
        from_monthly = min(credits, current.monthly_left)
        from_top_up = min(credits - from_monthly, current.top_up)
        connection.execute(
            "UPDATE users SET credits_used = credits_used + ?, topup_credits = topup_credits - ? WHERE id = ?",
            (from_monthly, from_top_up, user_id),
        )
    return Balance(
        monthly=monthly, used=current.used + from_monthly, top_up=current.top_up - from_top_up,
        period_start=current.period_start, renews_on=current.renews_on,
    )


def start_plan(connection: Any, user_id: str, *, today: date) -> None:
    """A fresh month of credits from ``today`` (a paid plan was just given); top-ups are kept."""
    connection.execute(
        "UPDATE users SET plan_started_at = ?, credit_period_start = ?, credits_used = 0 WHERE id = ?",
        (today.isoformat(), today.isoformat(), user_id),
    )


def add_top_up(user_id: str, credits: int) -> int:
    """Add top-up credits; return the profile's top-up balance."""
    if credits <= 0:
        raise ValueError("Top up a positive number of credits.")
    with connect_db() as connection:
        connection.execute("UPDATE users SET topup_credits = topup_credits + ? WHERE id = ?", (credits, user_id))
        row = connection.execute("SELECT topup_credits FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise LookupError(user_id)
    return int(row["topup_credits"])

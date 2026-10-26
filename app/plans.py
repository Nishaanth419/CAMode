"""Plans and what each one allows: Free (a daily question cap), Individual and Team (no cap).

There is no checkout: whoever runs the copy sets a profile's plan with
``python -m scripts.plans set <profile> <plan>`` after the person pays, and the pricing
page's buttons open an email to ``PRICING_CONTACT_EMAIL``. Free profiles are capped by
``DAILY_QUESTION_LIMIT`` (``limits.py``; never above ``FREE_DAILY_QUESTIONS``),
so a copy on one computer stays unlimited unless its owner sets one. Paid plans skip the
daily cap; the Individual plan has monthly credits instead (``credits.py``), and the request rate
limits still apply to everyone.

Prices live in ``PLANS`` and are shown on ``/pricing`` (``public_pages.pricing_page()``).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final, Literal, get_args

from . import credits, limits
from .db import connect_db

PlanId = Literal["free", "individual", "team"]
PLAN_IDS: Final[tuple[PlanId, ...]] = get_args(PlanId)
DEFAULT_PLAN: Final[PlanId] = "free"
# The free allowance the pricing page offers; kept in limits.py, which caps DAILY_QUESTION_LIMIT at it.
FREE_DAILY_QUESTIONS: Final = limits.FREE_DAILY_QUESTIONS
# Shown as the pricing page's sitemap date; change it with the prices.
PRICES_UPDATED: Final = date(2026, 10, 26)
_EMAIL = re.compile(r"^[^@\s<>\"']+@[^@\s<>\"']+\.[^@\s<>\"']+$")


@dataclass(frozen=True, slots=True)
class Plan:
    id: PlanId
    name: str
    price: str
    period: str
    summary: str
    features: tuple[str, ...]
    daily_cap: bool
    # Credits each month (credits.py); None: not counted.
    monthly_credits: int | None = None


PLANS: Final[dict[PlanId, Plan]] = {
    "free": Plan(
        id="free",
        name="Free",
        price="₹0",
        period="",
        summary="Try CA Mode on the shared copy.",
        features=(
            "{free_questions} questions a day, starting again at midnight {zone}",
            "Finance Agent and Portfolio Analyzer",
            "INDmoney connection, goals and daily portfolio snapshots",
            "History encrypted with your PIN",
        ),
        daily_cap=True,
    ),
    "individual": Plan(
        id="individual",
        name="Individual",
        price="₹599",
        period="per month, excluding GST",
        summary="For one person. Answers come from a privately hosted language model, and each month comes with its own credits.",
        features=(
            "{monthly_credits} credits a month (1 credit = 1,000 tokens), renewed on the day your plan started",
            "Top up any time once your monthly credits are used",
            "Answers from a privately hosted LLM, not a shared cloud AI service",
            "Easy access: sign in and ask, no AI keys to buy or manage",
            "Everything in Free",
            "Full refund within 30 days of paying",
        ),
        daily_cap=False,
        monthly_credits=3_000,
    ),
    "team": Plan(
        id="team",
        name="Team or organisation",
        price="Contact us",
        period="",
        summary="A copy hosted for your team alone, so the price depends on its size.",
        features=(
            "Its own server, database and model, run by us",
            "Each member signs in with their own Google account",
            "Unlimited questions for every member",
            "Daily backups and updates",
        ),
        daily_cap=False,
    ),
}


class UnknownPlan(ValueError):
    """Not one of ``PLAN_IDS``."""


def plan_of(user: dict[str, Any]) -> Plan:
    """Return the profile's plan; an unknown stored value counts as Free."""
    return PLANS.get(user.get("plan") or DEFAULT_PLAN, PLANS[DEFAULT_PLAN])


def has_daily_cap(user: dict[str, Any]) -> bool:
    return plan_of(user).daily_cap


def reset_zone() -> str:
    """The name of the time zone whose midnight starts the daily count again (IST by default)."""
    return limits.next_reset(limits.local_now()).tzname() or "local time"


def free_daily_questions() -> int:
    """The free allowance to advertise: this copy's cap, else ``FREE_DAILY_QUESTIONS``."""
    return limits.daily_question_limit() or FREE_DAILY_QUESTIONS


def usage(user: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """The profile's plan, today's questions and, on a plan with them, its credits.

    ``limit`` is None when no daily cap applies; ``credits`` is None on a plan without monthly credits.
    """
    plan = plan_of(user)
    limit = limits.daily_question_limit() if plan.daily_cap else None
    if limit is None:
        balance = credits.balance(user["id"], plan.monthly_credits, now=now) if plan.monthly_credits else None
        return {
            "plan": plan.id, "planName": plan.name, "limit": None, "used": None, "remaining": None, "resetsAt": None,
            "credits": balance.as_json() if balance else None,
        }
    used = min(limits.questions_used(user["id"], now=now), limit)
    return {
        "plan": plan.id, "planName": plan.name, "limit": limit, "used": used, "remaining": limit - used,
        "resetsAt": limits.next_reset(now).isoformat(timespec="minutes"), "credits": None,
    }


def set_plan(user_id: str, plan: str) -> bool:
    """Give the profile ``plan``; return False when no such profile exists."""
    if plan not in PLAN_IDS:
        raise UnknownPlan(f"Unknown plan {plan!r}; choose one of {', '.join(PLAN_IDS)}.")
    with connect_db() as connection:
        previous = connection.execute("SELECT plan FROM users WHERE id = ?", (user_id,)).fetchone()
        changed = connection.execute("UPDATE users SET plan = ? WHERE id = ?", (plan, user_id)).rowcount
        # A move onto a plan with credits starts a fresh month from today; setting the same plan again does not.
        if changed and PLANS[plan].monthly_credits and (previous is None or previous["plan"] != plan):
            credits.start_plan(connection, user_id, today=credits.local_today(limits.local_now()))
    return changed == 1


def contact_email() -> str | None:
    """The address the pricing page's buttons write to (``PRICING_CONTACT_EMAIL``), when it looks like one."""
    value = os.getenv("PRICING_CONTACT_EMAIL", "").strip()
    return value if _EMAIL.match(value) else None

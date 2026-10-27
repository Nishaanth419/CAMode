"""The operator's admin page: every profile with its plan and usage, and a few account actions.

Off (every admin path is a 404) unless ``ADMIN_EMAILS`` lists one or more Google accounts. The operator signs
in with Google through the same client and callback as account sign-in, so ``GOOGLE_CLIENT_ID`` and
``GOOGLE_CLIENT_SECRET`` are needed even when ``AUTH_MODE`` is ``local``. Admin sign-in uses its own cookie
(``ADMIN_COOKIE``) and session row, never bound to a profile: being an admin gives no access to any profile,
and signing in to a profile gives no admin access. The address is checked against ``ADMIN_EMAILS`` on every
request, so removing it ends that admin's access at once; a session lasts ``SESSION_SECONDS``.

The page shows only what the database holds outside the sealed history: names, Google email addresses,
plans, today's question counts, consent, whether INDmoney is connected, snapshot, sign-in and last-seen times,
and PIN lockouts. No admin route opens chats, goals, decisions, profile details or snapshot contents.
"""

from __future__ import annotations

import hmac
import os
from datetime import date, datetime, timedelta
from typing import Any, Final

from . import accounts, backup, chats, credits, limits, page_views, plans, privacy, unlock, users, vault
from .db import connect_db

ADMIN_COOKIE: Final = "ca_admin"
ADMIN_KEY: Final = "admin"
START_PATH: Final = "/auth/admin/start"
PAGE_PATH: Final = "/admin"
SESSION_SECONDS: Final = 8 * 60 * 60
# A snapshot attempt this much newer than the last snapshot taken is shown as failing.
SNAPSHOT_FAILURE_GRACE_MS: Final = 15 * 60 * 1000
ACTIVE_DAYS: Final = 7
PAGE_VIEW_DAYS: Final = 7


class NotAnAdmin(Exception):
    """The Google account that signed in is not in ``ADMIN_EMAILS``."""


# ── Configuration ──────────────────────────────────────────────────────────────

def admin_emails() -> frozenset[str]:
    return frozenset(
        email.strip().casefold() for email in os.getenv("ADMIN_EMAILS", "").split(",") if email.strip()
    )


def is_enabled() -> bool:
    return bool(admin_emails())


def is_admin_email(email: str) -> bool:
    return email.strip().casefold() in admin_emails()


def check_config() -> None:
    """Fail at startup when ``ADMIN_EMAILS`` is set without a Google client to sign in with."""
    if is_enabled():
        accounts.google_client()


# ── Admin sign-in ──────────────────────────────────────────────────────────────

def is_signing_in(sid: str | None, state: str) -> bool:
    """Whether the Google callback carrying ``state`` belongs to an admin sign-in started on this admin session."""
    if not sid or not state:
        return False
    stored = users.get_session(sid).get(accounts.SIGN_IN_KEY)
    return isinstance(stored, dict) and hmac.compare_digest(str(stored.get("state", "")), state)


async def finish_sign_in(sid: str, params: dict[str, str]) -> tuple[str, str]:
    """Check the Google callback and start an admin session; return its new id and the admin's email.

    Raises ``accounts.SignInError`` for a failed sign-in and ``NotAnAdmin`` for an address not in
    ``ADMIN_EMAILS``. Either way the sign-in in progress is cleared.
    """
    stored = users.get_session(sid).get(accounts.SIGN_IN_KEY)
    try:
        identity = await accounts.finish_sign_in(stored, params)
        if not is_admin_email(identity.email):
            raise NotAnAdmin(identity.email)
    except (accounts.SignInError, NotAnAdmin):
        users.save_session(sid, {})
        raise
    values = {ADMIN_KEY: {"email": identity.email, "subject": identity.subject, "at": users.now_ms()}}
    return users.start_unbound_session(sid, values), identity.email


def admin_for_session(sid: str | None) -> dict[str, Any] | None:
    """The admin signed in on this admin session, unless it expired or the address left ``ADMIN_EMAILS``."""
    if not sid or not is_enabled():
        return None
    entry = users.get_session(sid).get(ADMIN_KEY)
    if not isinstance(entry, dict) or not isinstance(entry.get("email"), str):
        return None
    signed_in_at = int(entry.get("at", 0))
    if users.now_ms() - signed_in_at > SESSION_SECONDS * 1000 or not is_admin_email(entry["email"]):
        return None
    return {"email": entry["email"], "signedInAt": signed_in_at}


def sign_out(sid: str) -> None:
    with connect_db() as connection:
        connection.execute("DELETE FROM oauth_sessions WHERE id = ? AND user_id IS NULL", (sid,))


# ── What the page shows ────────────────────────────────────────────────────────

_PROFILES_QUERY: Final = """
    SELECT u.id, u.display_name, u.plan, u.storage_mode, u.created_at, u.failed_logins, u.locked_until,
           u.plan_started_at, u.credit_period_start, u.credits_used, u.topup_credits,
           u.snapshot_attempted_at, u.indmoney <> '{}' AS has_indmoney,
           a.email AS account_email, a.last_sign_in_at,
           (SELECT MAX(s.updated_at) FROM oauth_sessions s WHERE s.user_id = u.id) AS last_seen_at,
           (SELECT MAX(p.captured_at) FROM portfolio_snapshots p WHERE p.user_id = u.id) AS last_snapshot_at,
           (SELECT q.questions FROM question_counts q WHERE q.user_id = u.id AND q.day = ?) AS questions_today,
           (SELECT c.policy_version FROM consents c WHERE c.user_id = u.id ORDER BY c.id DESC LIMIT 1) AS policy_version
    FROM users u LEFT JOIN accounts a ON a.user_id = u.id
    ORDER BY u.created_at DESC
"""


def _email(stored: str | None) -> str | None:
    if stored is None:
        return None
    try:
        return vault.decrypt(stored)
    except vault.VaultError:
        return ""


def _profile_row(row: Any, *, daily_limit: int | None, now_ms: int, today: date) -> dict[str, Any]:
    plan = plans.plan_of({"plan": row["plan"]})
    balance = credits.from_row(row, plan.monthly_credits, today) if plan.monthly_credits else None
    last_snapshot = row["last_snapshot_at"]
    attempted = row["snapshot_attempted_at"] or 0
    return {
        "id": row["id"],
        "name": row["display_name"],
        "email": _email(row["account_email"]),
        "plan": plan.id,
        "planName": plan.name,
        "createdAt": row["created_at"],
        "lastSeenAt": row["last_seen_at"],
        "lastSignInAt": row["last_sign_in_at"],
        "storageMode": row["storage_mode"],
        "consentCurrent": row["policy_version"] == privacy.POLICY_VERSION,
        "indmoneyConnected": bool(row["has_indmoney"]),
        "lastSnapshotAt": last_snapshot,
        "snapshotFailing": attempted > (last_snapshot or 0) + SNAPSHOT_FAILURE_GRACE_MS,
        "questionsToday": row["questions_today"] or 0,
        "dailyLimit": daily_limit if plan.daily_cap else None,
        # This month's credits on a plan that has them (credits.py); None otherwise.
        "credits": balance.as_json() if balance else None,
        "failedPins": row["failed_logins"],
        "lockedUntil": row["locked_until"] if row["locked_until"] > now_ms else None,
    }


def profiles(*, now: datetime) -> list[dict[str, Any]]:
    """Every profile, newest first, with only the fields kept outside its sealed history."""
    today = now.astimezone(limits.SNAPSHOT_TIMEZONE).date()
    daily_limit, now_ms = limits.daily_question_limit(), users.now_ms()
    with connect_db() as connection:
        rows = connection.execute(_PROFILES_QUERY, (today.isoformat(),)).fetchall()
    return [_profile_row(row, daily_limit=daily_limit, now_ms=now_ms, today=today) for row in rows]


def overview(*, now: datetime) -> dict[str, Any]:
    """The admin page's data: totals, every profile, backups and this week's anonymous page views."""
    rows = profiles(now=now)
    active_since = int((now - timedelta(days=ACTIVE_DAYS)).timestamp() * 1000)
    by_plan = {plan_id: 0 for plan_id in plans.PLAN_IDS}
    for row in rows:
        by_plan[row["plan"]] += 1
    views = page_views.counts(days=PAGE_VIEW_DAYS, today=now.astimezone(limits.SNAPSHOT_TIMEZONE).date())
    return {
        "totals": {
            "profiles": len(rows),
            "byPlan": by_plan,
            "activeLastWeek": sum(1 for row in rows if (row["lastSeenAt"] or 0) >= active_since),
            "questionsToday": sum(row["questionsToday"] for row in rows),
            "creditsUsedThisMonth": sum(row["credits"]["used"] for row in rows if row["credits"]),
            "indmoneyConnected": sum(1 for row in rows if row["indmoneyConnected"]),
            "locked": sum(1 for row in rows if row["lockedUntil"]),
        },
        "plans": [{"id": plan.id, "name": plan.name} for plan in plans.PLANS.values()],
        "dailyLimit": limits.daily_question_limit(),
        "resetsAt": limits.next_reset(now).isoformat(timespec="minutes"),
        "authMode": accounts.auth_mode(),
        "backups": backup.status(),
        "pageViews": {"days": PAGE_VIEW_DAYS, "byPage": page_views.totals(views)},
        "profiles": rows,
    }


# ── Account actions ────────────────────────────────────────────────────────────

def clear_lockout(user_id: str) -> bool:
    """Clear a profile's wrong-PIN count and lock; return False when no such profile exists."""
    with connect_db() as connection:
        changed = connection.execute(
            "UPDATE users SET failed_logins = 0, locked_until = 0 WHERE id = ?", (user_id,),
        ).rowcount
    return changed == 1


def sign_out_everywhere(user_id: str) -> int:
    """End every browser session signed in to the profile (and its unlocked keys); return how many."""
    with connect_db() as connection:
        ended = connection.execute("DELETE FROM oauth_sessions WHERE user_id = ?", (user_id,)).rowcount
    unlock.forget_user(user_id)
    chats.forget_private_chats(user_id)
    return ended

"""Abuse and cost limits for a hosted copy of CA Mode.

Three independent protections, each off unless configured, so a copy on one computer
behaves exactly as before:

- ``DAILY_QUESTION_LIMIT``: answered chat questions per Free-plan profile per local day (in
  ``SNAPSHOT_TIMEZONE``; paid plans in ``plans.py`` are not capped), counted in the ``question_counts`` table.
  The cap is never above ``FREE_DAILY_QUESTIONS``, the Free plan's allowance in code: a larger number
  in a server's settings is lowered to it, so changing the allowance in code reaches a hosted copy with
  the next image. A smaller number still lowers the cap for that copy. A slot is reserved
  before the model runs (in a ``BEGIN IMMEDIATE`` transaction, so concurrent requests
  cannot overshoot) and released when no answer comes back. Each reservation is also a
  ``question_reservations`` row until it is confirmed or released, so a request killed mid-answer
  (a deploy restart) gives its slot back: at the next start, or ``RESERVATION_TTL_SECONDS`` later.
- ``RATE_LIMITS_ENABLED``: sliding-window request limits per client IP and per profile
  on chat, sign-up, sign-in and investigation routes (``RATE_RULES``). Kept in process
  memory: they reset on restart and assume one app process, which is how CA Mode runs.
- ``TURNSTILE_SITE_KEY`` + ``TURNSTILE_SECRET_KEY``: a Cloudflare Turnstile check on
  sign-up, verified server side.

The client IP is the TCP peer unless ``TRUSTED_PROXY`` names the proxy in front of the app:
``cloudflare`` takes it from ``CF-Connecting-IP``, ``caddy`` from the last ``X-Forwarded-For``
entry (the address Caddy saw; Caddy replaces a client's own header). Set it only when that proxy
is the only way in (a Tunnel, or Caddy with the app's port unpublished); otherwise anyone could
pick their own IP.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import os
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from typing import Final, Literal

import httpx
from fastapi import Request

from .db import connect_db
from .snapshots import SNAPSHOT_TIMEZONE

logger = logging.getLogger("ca_mode.limits")

RateGroup = Literal["chat", "sign_up", "sign_in", "investigation", "broker", "backup"]

# The Free plan's daily allowance, offered on the pricing page; DAILY_QUESTION_LIMIT is never above it.
FREE_DAILY_QUESTIONS: Final = 10

TURNSTILE_VERIFY_URL: Final = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TURNSTILE_TIMEOUT_SECONDS: Final = 10
# Distinct client keys kept in memory before the least recently seen are dropped.
MAX_TRACKED_KEYS: Final = 10_000
# How often request times older than their window are swept out of memory.
SWEEP_SECONDS: Final = 60


def _flag(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in {"1", "true", "yes", "on"}


# ── Daily question cap ──────────────────────────────────────────────────────────

class DailyLimitReached(Exception):
    """The profile has used every answered question allowed today."""

    def __init__(self, limit: int, resets_at: datetime) -> None:
        self.limit = limit
        self.resets_at = resets_at
        super().__init__(
            f"You've reached today's limit of {limit} question{'' if limit == 1 else 's'}. "
            f"It resets at midnight {resets_at.tzname() or 'local time'}."
        )

    def retry_after_seconds(self, now: datetime) -> int:
        return max(1, math.ceil((self.resets_at - now).total_seconds()))


def daily_question_limit() -> int | None:
    """Return the cap, or None when questions are unlimited (unset, empty, 0 or invalid).

    Never more than ``FREE_DAILY_QUESTIONS``: the pricing page promises that allowance, and a hosted
    copy's older, larger setting must not outlive a change to it in code.
    """
    raw = os.getenv("DAILY_QUESTION_LIMIT", "").strip()
    if not raw:
        return None
    try:
        limit = int(raw)
    except ValueError:
        logger.warning("Ignoring DAILY_QUESTION_LIMIT=%r: not a whole number", raw)
        return None
    return min(limit, FREE_DAILY_QUESTIONS) if limit > 0 else None


def local_now() -> datetime:
    return datetime.now(SNAPSHOT_TIMEZONE)


def next_reset(now: datetime) -> datetime:
    """Return the next local midnight after ``now`` (when the daily count starts again)."""
    local = now.astimezone(SNAPSHOT_TIMEZONE)
    return datetime.combine(local.date() + timedelta(days=1), dt_time(0), tzinfo=SNAPSHOT_TIMEZONE)


# A reservation older than this belongs to a request that can no longer be running (the chat budget is far
# shorter), so its slot is given back.
RESERVATION_TTL_SECONDS: Final = 15 * 60


@dataclass(frozen=True, slots=True)
class Reservation:
    """One counted question waiting for its answer: confirm it when answered, release it otherwise."""

    id: str
    day: date


def _release_stale(connection, cutoff_ms: int, user_id: str | None = None) -> int:
    """Give back the slots of reservations made before ``cutoff_ms`` (all profiles when ``user_id`` is None)."""
    query = "SELECT id, user_id, day FROM question_reservations WHERE reserved_at < ?"
    params: tuple = (cutoff_ms,)
    if user_id is not None:
        query += " AND user_id = ?"
        params += (user_id,)
    stale = connection.execute(query, params).fetchall()
    for row in stale:
        connection.execute(
            "UPDATE question_counts SET questions = questions - 1 WHERE user_id = ? AND day = ? AND questions > 0",
            (row["user_id"], row["day"]),
        )
        connection.execute("DELETE FROM question_reservations WHERE id = ?", (row["id"],))
    return len(stale)


def release_stale_reservations(*, older_than_seconds: float = RESERVATION_TTL_SECONDS) -> int:
    """Give back every slot still reserved after ``older_than_seconds``; return how many.

    At startup it runs with 0: no request survives a restart, so every reservation left is an orphan.
    """
    cutoff = int((time.time() - older_than_seconds) * 1000)
    with connect_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        released = _release_stale(connection, cutoff)
    if released:
        logger.info("question_reservations_released", extra={"count": released})
    return released


def reserve_question(user_id: str, *, now: datetime, capped: bool = True) -> Reservation | None:
    """Count one question for today, or raise ``DailyLimitReached``; return the reservation (None: no cap).

    ``capped=False`` (a paid plan, ``plans.has_daily_cap()``) skips the cap and the count.
    Pass the reservation to ``confirm_question()`` once answered, else to ``release_question()``.
    """
    limit = daily_question_limit() if capped else None
    if limit is None:
        return None
    today = now.astimezone(SNAPSHOT_TIMEZONE).date()
    reservation = Reservation(secrets.token_hex(8), today)
    with connect_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _release_stale(connection, int((time.time() - RESERVATION_TTL_SECONDS) * 1000), user_id)
        row = connection.execute(
            "SELECT questions FROM question_counts WHERE user_id = ? AND day = ?", (user_id, today.isoformat()),
        ).fetchone()
        if row is not None and row["questions"] >= limit:
            raise DailyLimitReached(limit, next_reset(now))
        connection.execute(
            "INSERT INTO question_counts(user_id, day, questions) VALUES (?, ?, 1) "
            "ON CONFLICT(user_id, day) DO UPDATE SET questions = questions + 1",
            (user_id, today.isoformat()),
        )
        connection.execute(
            "INSERT INTO question_reservations(id, user_id, day, reserved_at) VALUES (?, ?, ?, ?)",
            (reservation.id, user_id, today.isoformat(), int(time.time() * 1000)),
        )
        # Only today's count matters; yesterday's is kept so a request spanning midnight can release it.
        connection.execute("DELETE FROM question_counts WHERE day < ?", ((today - timedelta(days=1)).isoformat(),))
    return reservation


def confirm_question(reservation: Reservation | None) -> None:
    """The question was answered: its slot stays counted and no longer waits to be given back."""
    if reservation is None:
        return
    with connect_db() as connection:
        connection.execute("DELETE FROM question_reservations WHERE id = ?", (reservation.id,))


def release_question(user_id: str, reservation: Reservation | None) -> None:
    """Give back a slot taken by ``reserve_question()`` for a question that was not answered (once only)."""
    if reservation is None:
        return
    with connect_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        pending = connection.execute(
            "DELETE FROM question_reservations WHERE id = ? AND user_id = ?", (reservation.id, user_id),
        ).rowcount
        if pending:
            connection.execute(
                "UPDATE question_counts SET questions = questions - 1 WHERE user_id = ? AND day = ? AND questions > 0",
                (user_id, reservation.day.isoformat()),
            )


def questions_used(user_id: str, *, now: datetime) -> int:
    today = now.astimezone(SNAPSHOT_TIMEZONE).date().isoformat()
    with connect_db() as connection:
        row = connection.execute(
            "SELECT questions FROM question_counts WHERE user_id = ? AND day = ?", (user_id, today),
        ).fetchone()
    return int(row["questions"]) if row else 0


# ── Request rate limits ─────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Rule:
    """At most ``limit`` requests in any ``window_seconds``."""

    limit: int
    window_seconds: int


@dataclass(frozen=True, slots=True)
class GroupRules:
    per_ip: Rule
    per_profile: Rule | None


# Chat and investigations cost model or database time; sign-up and sign-in each run a
# deliberately slow scrypt hash. Sign-in also covers unlocking and recovery (PIN guesses);
# its per-profile rule is keyed by the profile being signed in to, on top of the
# account lockout in users.check_pin().
RATE_RULES: Final[dict[RateGroup, GroupRules]] = {
    "chat": GroupRules(per_ip=Rule(30, 60), per_profile=Rule(10, 60)),
    "sign_up": GroupRules(per_ip=Rule(5, 3600), per_profile=None),
    "sign_in": GroupRules(per_ip=Rule(20, 600), per_profile=Rule(10, 600)),
    "investigation": GroupRules(per_ip=Rule(20, 600), per_profile=Rule(10, 600)),
    "broker": GroupRules(per_ip=Rule(60, 60), per_profile=Rule(10, 60)),
    # A whole-database copy each time; manual copies are pruned apart from the daily ones (backup.py).
    "backup": GroupRules(per_ip=Rule(5, 3600), per_profile=Rule(3, 3600)),
}


class RateLimited(Exception):
    """Too many requests from this client or profile."""

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        minutes = math.ceil(retry_after_seconds / 60)
        wait = f"{retry_after_seconds} seconds" if retry_after_seconds < 60 else (
            f"{minutes} minute{'' if minutes == 1 else 's'}"
        )
        super().__init__(f"Too many requests. Try again in {wait}.")


class RateLimiter:
    """Sliding-window counters keyed by string, safe to share between threads."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, max_keys: int = MAX_TRACKED_KEYS) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._hits: dict[str, tuple[int, deque[float]]] = {}
        self._lock = threading.Lock()
        self._last_sweep = clock()

    def __repr__(self) -> str:
        return f"RateLimiter(keys={len(self._hits)})"

    def hit(self, key: str, rule: Rule) -> int:
        """Record a request for ``key``; return 0 when allowed, else the seconds until one is."""
        now = self._clock()
        with self._lock:
            if now - self._last_sweep >= SWEEP_SECONDS:
                self._sweep(now)
            _, hits = self._hits.get(key, (rule.window_seconds, deque()))
            while hits and hits[0] <= now - rule.window_seconds:
                hits.popleft()
            if len(hits) >= rule.limit:
                return max(1, math.ceil(hits[0] + rule.window_seconds - now))
            hits.append(now)
            self._hits[key] = (rule.window_seconds, hits)
            if len(self._hits) > self._max_keys:
                self._sweep(now)
            return 0

    def _sweep(self, now: float) -> None:
        """Drop request times past their window, so none outlives its window by more than ``SWEEP_SECONDS``."""
        self._last_sweep = now
        for key, (window, hits) in list(self._hits.items()):
            while hits and hits[0] <= now - window:
                hits.popleft()
            if not hits:
                del self._hits[key]
        if len(self._hits) > self._max_keys:
            # Still full of active keys (many addresses at once): forget the least recently seen half.
            by_last_hit = sorted(self._hits, key=lambda key: self._hits[key][1][-1])
            for key in by_last_hit[: len(by_last_hit) // 2]:
                del self._hits[key]

    def __len__(self) -> int:
        return len(self._hits)

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def rate_limits_enabled() -> bool:
    return _flag("RATE_LIMITS_ENABLED")


def check_rate(group: RateGroup, *, ip: str, profile_id: str | None = None) -> None:
    """Count one request in ``group`` for this IP (and profile); raise ``RateLimited`` when over a rule."""
    if not rate_limits_enabled():
        return
    rules = RATE_RULES[group]
    keys = [(f"{group}|ip|{ip}", rules.per_ip)]
    if profile_id and rules.per_profile:
        keys.append((f"{group}|profile|{profile_id}", rules.per_profile))
    for key, rule in keys:
        if retry_after := limiter.hit(key, rule):
            logger.warning("rate_limited", extra={"group": group, "scope": key.split("|")[1]})
            raise RateLimited(retry_after)


TRUSTED_PROXIES: Final = ("cloudflare", "caddy")


def _trusted_proxy() -> str | None:
    value = os.getenv("TRUSTED_PROXY", "").strip().lower()
    if not value:
        return None
    if value not in TRUSTED_PROXIES:
        logger.warning("Ignoring TRUSTED_PROXY=%r: use one of %s", value, ", ".join(TRUSTED_PROXIES))
        return None
    return value


def _client_key(address: str) -> str | None:
    """Normalise an address; IPv6 clients are grouped by /64, the block one subscriber usually gets."""
    try:
        ip = ipaddress.ip_address(address.strip())
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def client_address(request: Request) -> str:
    """Return the client's address as the trusted proxy reports it (``TRUSTED_PROXY``), else the TCP peer."""
    proxy = _trusted_proxy()
    forwarded = ""
    if proxy == "cloudflare":
        forwarded = request.headers.get("cf-connecting-ip", "").strip()
    elif proxy == "caddy":
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
    if _client_key(forwarded):
        return forwarded
    return request.client.host if request.client else ""


def client_ip(request: Request) -> str:
    """Return the rate-limit key for the client's address (see ``client_address()``)."""
    address = client_address(request)
    return _client_key(address) or address or "unknown"


# ── Cloudflare Turnstile on sign-up ─────────────────────────────────────────────

class TurnstileUnavailable(Exception):
    """Cloudflare could not be asked whether the token is valid."""


def turnstile_site_key() -> str | None:
    """Return the public site key when Turnstile is configured (both keys set), else None."""
    site_key = os.getenv("TURNSTILE_SITE_KEY", "").strip()
    secret = os.getenv("TURNSTILE_SECRET_KEY", "").strip()
    return site_key if site_key and secret else None


async def verify_turnstile(
    token: str | None, *, remote_ip: str | None, transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """Ask Cloudflare whether ``token`` is a valid, unused Turnstile response for this site."""
    if not token or len(token) > 2048:
        return False
    data = {"secret": os.getenv("TURNSTILE_SECRET_KEY", "").strip(), "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        async with httpx.AsyncClient(timeout=TURNSTILE_TIMEOUT_SECONDS, transport=transport) as client:
            response = await client.post(TURNSTILE_VERIFY_URL, data=data)
            response.raise_for_status()
            outcome = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("turnstile_unavailable", extra={"error": type(exc).__name__})
        raise TurnstileUnavailable from exc
    if not outcome.get("success"):
        logger.info("turnstile_rejected", extra={"codes": outcome.get("error-codes", [])})
        return False
    return True

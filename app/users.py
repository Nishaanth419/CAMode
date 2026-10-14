"""Local user profiles: PIN sign-in, browser sessions, profile fields, INDmoney tokens.

Profiles are a *local* lock for people sharing one machine, not internet-grade
authentication. Replace with real authentication before deploying CA Mode anywhere
other people can reach it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .db import connect_db

logger = logging.getLogger("ca_mode.users")

MCP_RESOURCE = os.getenv("MCP_SERVER_URL", "https://mcp.indmoney.com/mcp")
INDMONEY_TOKEN_URL = "https://mcp.indmoney.com/token"
INDMONEY_REVOKE_URL = "https://mcp.indmoney.com/revoke"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60

PIN_MIN_LENGTH = 4
PIN_MAX_LENGTH = 64
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 5 * 60
# scrypt work factor (N=2^17, r=8, p=1 needs ~128 MiB). Parameters are stored in
# each hash, so lowering SCRYPT_N (tests) never breaks verifying existing hashes.
SCRYPT_N = 2**17
SCRYPT_R = 8
SCRYPT_P = 1
_SCRYPT_MAXMEM = 256 * 1024 * 1024


class ProfileError(Exception):
    """A profile operation failed for a reason the user can act on."""


class DuplicateProfileError(ProfileError):
    """Another profile already uses this display name."""


class ProfileLockedError(ProfileError):
    """Too many wrong PINs; sign-in is paused."""

    def __init__(self, retry_after_seconds: int):
        super().__init__("Too many incorrect PINs. Try again later.")
        self.retry_after_seconds = retry_after_seconds


class ProfileFields(BaseModel):
    """Optional, user-entered context the Portfolio Analyzer may use to tailor answers."""

    model_config = ConfigDict(extra="forbid")

    riskAppetite: Literal["low", "moderate", "high"] | None = None
    investmentHorizonYears: int | None = Field(default=None, ge=0, le=60)
    primaryGoal: str | None = Field(default=None, max_length=200)
    monthlyInvestmentInr: int | None = Field(default=None, ge=0, le=1_000_000_000)
    ageBand: Literal["under-25", "25-34", "35-44", "45-54", "55+"] | None = None


def now_ms() -> int:
    return int(time.time() * 1000)


def random_token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


# ── PIN hashing ────────────────────────────────────────────────────────────────

def hash_pin(pin: str) -> str:
    """Return a self-describing scrypt hash: ``scrypt$N$r$p$salt$digest``."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=_SCRYPT_MAXMEM)
    encode = lambda value: base64.b64encode(value).decode()  # noqa: E731 - local one-liner
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${encode(salt)}${encode(digest)}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            pin.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
            maxmem=_SCRYPT_MAXMEM, dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def validate_new_pin(pin: str) -> None:
    if not PIN_MIN_LENGTH <= len(pin) <= PIN_MAX_LENGTH:
        raise ProfileError(f"Choose a PIN of {PIN_MIN_LENGTH} to {PIN_MAX_LENGTH} characters.")


def clean_display_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not 1 <= len(cleaned) <= 40:
        raise ProfileError("Choose a profile name of 1 to 40 characters.")
    return cleaned


# ── Users ──────────────────────────────────────────────────────────────────────

def _user_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "displayName": row["display_name"],
        "pinHash": row["pin_hash"],
        "profile": json.loads(row["profile"] or "{}"),
        "indmoney": json.loads(row["indmoney"] or "{}"),
        "mcpTools": json.loads(row["mcp_tools"]) if row["mcp_tools"] else None,
        "mcpToolsFetchedAt": row["mcp_tools_fetched_at"],
        "snapshotAttemptedAt": row["snapshot_attempted_at"],
        "storageMode": row["storage_mode"],
        "snapshotRetentionDays": row["snapshot_retention_days"],
        "failedLogins": row["failed_logins"],
        "lockedUntil": row["locked_until"],
    }


def create_user(display_name: str, pin_hash: str) -> dict[str, Any]:
    user_id, now = random_token(), now_ms()
    try:
        with connect_db() as connection:
            connection.execute(
                "INSERT INTO users(id, display_name, pin_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, display_name, pin_hash, now, now),
            )
    except sqlite3.IntegrityError as exc:
        raise DuplicateProfileError("A profile with this name already exists.") from exc
    user = get_user(user_id)
    assert user is not None  # Inserted above in the same process.
    return user


def get_user(user_id: str) -> dict[str, Any] | None:
    with connect_db() as connection:
        row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _user_from_row(row) if row else None


def list_profiles() -> list[dict[str, str]]:
    with connect_db() as connection:
        rows = connection.execute("SELECT id, display_name FROM users ORDER BY display_name COLLATE NOCASE").fetchall()
    return [{"id": row["id"], "displayName": row["display_name"]} for row in rows]


def _raise_if_locked(locked_until: int, now: int) -> None:
    if locked_until > now:
        raise ProfileLockedError(max(1, (locked_until - now) // 1000))


def check_pin(user: dict[str, Any], pin: str) -> bool:
    """Verify a PIN with an account-bound lockout. Raises ``ProfileLockedError`` while locked.

    The lockout state is always read from the database, never from ``user`` (which may be
    stale), and the outcome is recorded in a ``BEGIN IMMEDIATE`` transaction that re-checks
    the lock and increments the counter atomically. Concurrent wrong guesses therefore each
    count, and a guess whose (slow) hash finishes after the account locked is refused even
    if it is correct.
    """
    with connect_db() as connection:
        row = connection.execute("SELECT pin_hash, locked_until FROM users WHERE id = ?", (user["id"],)).fetchone()
    if row is None:
        return False
    _raise_if_locked(row["locked_until"], now_ms())
    valid = verify_pin(pin, row["pin_hash"])
    with connect_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT failed_logins, locked_until FROM users WHERE id = ?", (user["id"],),
        ).fetchone()
        if row is None:
            return False
        now = now_ms()
        _raise_if_locked(row["locked_until"], now)
        if valid:
            connection.execute("UPDATE users SET failed_logins = 0, locked_until = 0 WHERE id = ?", (user["id"],))
            return True
        failures = row["failed_logins"] + 1
        locked_until = now + LOCKOUT_SECONDS * 1000 if failures >= MAX_FAILED_LOGINS else 0
        connection.execute(
            "UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
            (0 if locked_until else failures, locked_until, user["id"]),
        )
    logger.warning("profile_sign_in_failed", extra={"user_id": user["id"], "locked": bool(locked_until)})
    if locked_until:
        raise ProfileLockedError(LOCKOUT_SECONDS)
    return False


def update_profile(user_id: str, fields: ProfileFields) -> dict[str, Any]:
    profile = fields.model_dump(exclude_none=True)
    with connect_db() as connection:
        connection.execute(
            "UPDATE users SET profile = ?, updated_at = ? WHERE id = ?", (json.dumps(profile), now_ms(), user_id),
        )
    return profile


def delete_user(user_id: str) -> None:
    with connect_db() as connection:
        connection.execute("DELETE FROM users WHERE id = ?", (user_id,))


# ── Browser sessions ───────────────────────────────────────────────────────────

def get_session(sid: str) -> dict[str, Any]:
    """Return a session's data, expiring sessions idle for longer than the max age."""
    with connect_db() as connection:
        row = connection.execute("SELECT data, updated_at FROM oauth_sessions WHERE id = ?", (sid,)).fetchone()
        if row is None:
            return {}
        if now_ms() - row["updated_at"] > SESSION_MAX_AGE_SECONDS * 1000:
            connection.execute("DELETE FROM oauth_sessions WHERE id = ?", (sid,))
            return {}
        return json.loads(row["data"])


def save_session(sid: str, values: dict[str, Any]) -> None:
    now = now_ms()
    stored = {**values, "updatedAt": now, "sessionExpiresAt": now + SESSION_MAX_AGE_SECONDS * 1000}
    with connect_db() as connection:
        connection.execute(
            "INSERT INTO oauth_sessions(id, data, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (sid, json.dumps(stored), now),
        )


def touch_session(sid: str) -> None:
    """Slide the session expiry forward, creating an empty session row if it is missing."""
    with connect_db() as connection:
        connection.execute(
            "INSERT INTO oauth_sessions(id, data, updated_at) VALUES (?, '{}', ?) "
            "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at",
            (sid, now_ms()),
        )


def session_user_id(sid: str | None) -> str | None:
    if not sid:
        return None
    with connect_db() as connection:
        row = connection.execute(
            "SELECT user_id, updated_at FROM oauth_sessions WHERE id = ?", (sid,),
        ).fetchone()
    if row is None or now_ms() - row["updated_at"] > SESSION_MAX_AGE_SECONDS * 1000:
        return None
    return row["user_id"]


def sign_in(old_sid: str | None, user_id: str) -> str:
    """Bind a fresh session id to ``user_id`` (rotating the id), adopting the old session's data."""
    if old_sid:
        adopt_session_data(old_sid, user_id)
    new_sid = random_token()
    save_session(new_sid, {})
    with connect_db() as connection:
        connection.execute("UPDATE oauth_sessions SET user_id = ? WHERE id = ?", (user_id, new_sid))
        if old_sid:
            connection.execute("DELETE FROM oauth_sessions WHERE id = ?", (old_sid,))
    return new_sid


def sign_out(sid: str) -> None:
    with connect_db() as connection:
        connection.execute("UPDATE oauth_sessions SET user_id = NULL, data = '{}' WHERE id = ?", (sid,))


def adopt_session_data(sid: str, user_id: str) -> None:
    """Move pre-profile chats and INDmoney tokens stored on a browser session into a profile."""
    session = get_session(sid)
    token_keys = ("accessToken", "refreshToken", "expiresAt", "clientId", "clientSecret")
    with connect_db() as connection:
        has_legacy = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chats_legacy'",
        ).fetchone()
        if has_legacy:
            connection.execute(
                "INSERT OR IGNORE INTO chats(id, user_id, title, messages, updated_at, expires_at, agent_type) "
                "SELECT id, ?, title, messages, updated_at, expires_at, agent_type FROM chats_legacy WHERE session_id = ?",
                (user_id, sid),
            )
            connection.execute("DELETE FROM chats_legacy WHERE session_id = ?", (sid,))
    if session.get("accessToken") and not get_indmoney(user_id).get("accessToken"):
        save_indmoney(user_id, {key: session[key] for key in token_keys if key in session})


# ── INDmoney connection per user ───────────────────────────────────────────────

def get_indmoney(user_id: str) -> dict[str, Any]:
    user = get_user(user_id)
    return user["indmoney"] if user else {}


def save_indmoney(user_id: str, values: dict[str, Any]) -> None:
    with connect_db() as connection:
        connection.execute(
            "UPDATE users SET indmoney = ?, updated_at = ? WHERE id = ?", (json.dumps(values), now_ms(), user_id),
        )


def clear_indmoney(user_id: str) -> None:
    """Forget the INDmoney connection and everything derived from it (cached tools, snapshots)."""
    with connect_db() as connection:
        connection.execute(
            "UPDATE users SET indmoney = '{}', mcp_tools = NULL, mcp_tools_fetched_at = NULL, "
            "snapshot_attempted_at = 0, updated_at = ? WHERE id = ?",
            (now_ms(), user_id),
        )
        connection.execute("DELETE FROM portfolio_snapshots WHERE user_id = ?", (user_id,))


def users_with_indmoney() -> list[str]:
    with connect_db() as connection:
        rows = connection.execute("SELECT id FROM users WHERE json_extract(indmoney, '$.refreshToken') IS NOT NULL "
                                  "OR json_extract(indmoney, '$.accessToken') IS NOT NULL").fetchall()
    return [row["id"] for row in rows]


async def user_access_token(user_id: str, *, force_refresh: bool = False) -> str | None:
    """Return a usable INDmoney access token for ``user_id``, refreshing it when needed."""
    stored = get_indmoney(user_id)
    token = stored.get("accessToken")
    if not force_refresh and token and stored.get("expiresAt", 0) > now_ms() + 30_000:
        return token
    if not stored.get("refreshToken") or not stored.get("clientId") or not stored.get("clientSecret"):
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(INDMONEY_TOKEN_URL, data={
                "grant_type": "refresh_token", "refresh_token": stored["refreshToken"],
                "client_id": stored["clientId"], "client_secret": stored["clientSecret"], "resource": MCP_RESOURCE,
            })
    except httpx.HTTPError as exc:
        logger.warning("INDmoney token refresh failed: %s", type(exc).__name__)
        return None
    if not response.is_success:
        logger.warning("INDmoney token refresh failed: HTTP %s", response.status_code)
        return None
    try:
        result = response.json()
    except ValueError:
        result = None
    if not isinstance(result, dict) or not result.get("access_token"):
        logger.warning("INDmoney token refresh returned no access token")
        return None
    stored["accessToken"] = result["access_token"]
    stored["refreshToken"] = result.get("refresh_token", stored["refreshToken"])
    stored["expiresAt"] = now_ms() + int(result.get("expires_in", 3600)) * 1000
    save_indmoney(user_id, stored)
    return stored["accessToken"]


def cache_mcp_tools(user_id: str, tools: list[dict[str, Any]]) -> None:
    with connect_db() as connection:
        connection.execute(
            "UPDATE users SET mcp_tools = ?, mcp_tools_fetched_at = ? WHERE id = ?",
            (json.dumps(tools), now_ms(), user_id),
        )

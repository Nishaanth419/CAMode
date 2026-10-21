"""Account sign-in: Google accounts linked one-to-one to profiles, or the shared-computer picker.

``AUTH_MODE`` decides how a browser reaches a profile:

- ``local`` (default): the shared-computer picker. ``GET /api/session`` lists every profile, and anyone at
  the computer picks one and types its PIN.
- ``google``: a person signs in with Google (OpenID Connect, authorization code flow with PKCE, state and
  nonce). Their Google account id (``sub``) maps to exactly one profile in ``accounts``, and the profile list
  is never sent. The PIN still unlocks the profile's sealed history (``unlock.py``): signing in says who the
  person is, the PIN opens their data.

A Google account without a profile is held on its browser session (``PENDING_KEY``) until the person either
creates a profile (name and PIN) or claims one made in local mode by typing that profile's name and PIN
(``ALLOW_PROFILE_CLAIMS``, on by default; claims share the profile's PIN lockout).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Final, Literal
from urllib.parse import urlencode

import httpx

from . import users, vault
from .db import connect_db

logger = logging.getLogger("ca_mode.accounts")

AuthMode = Literal["local", "google"]
AUTH_MODES: Final = ("local", "google")
PROVIDER_GOOGLE: Final = "google"

GOOGLE_AUTHORIZE_URL: Final = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL: Final = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS: Final = frozenset({"https://accounts.google.com", "accounts.google.com"})
CALLBACK_PATH: Final = "/auth/google/callback"

# Session keys: the sign-in in progress, and a Google account that has no profile yet.
SIGN_IN_KEY: Final = "googleSignIn"
PENDING_KEY: Final = "pendingAccount"
# How long Google's consent screen may stay open, and how long a signed-in Google account may
# take to create or claim its profile before it has to sign in again.
SIGN_IN_SECONDS: Final = 10 * 60
PENDING_SECONDS: Final = 30 * 60
# Allowance for clock differences when checking the ID token's times.
CLOCK_SKEW_SECONDS: Final = 120

_dummy_pin_hash: str | None = None


class AccountConfigError(RuntimeError):
    """``AUTH_MODE`` or the Google client settings are missing or invalid."""


class SignInError(Exception):
    """Google sign-in did not complete. ``code`` is shown to the app as ``?signInError=<code>``."""

    def __init__(self, code: Literal["cancelled", "expired", "failed"], detail: str):
        super().__init__(detail)
        self.code = code


class AccountLinkError(Exception):
    """The Google account or the profile is already linked."""


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    email: str
    # Only used to suggest a profile name; never stored outside the pending session.
    name: str = ""


# ── Configuration ──────────────────────────────────────────────────────────────

def auth_mode() -> AuthMode:
    return "google" if os.getenv("AUTH_MODE", "local").strip().lower() == "google" else "local"


def uses_google() -> bool:
    return auth_mode() == "google"


def claims_allowed() -> bool:
    return os.getenv("ALLOW_PROFILE_CLAIMS", "true").strip().lower() not in {"0", "false", "no", "off"}


def google_client() -> tuple[str, str]:
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise AccountConfigError("AUTH_MODE=google needs GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.")
    return client_id, client_secret


def check_config() -> None:
    """Fail at startup on an unknown ``AUTH_MODE`` or Google mode without client credentials."""
    configured = os.getenv("AUTH_MODE", "local").strip().lower()
    if configured not in AUTH_MODES:
        raise AccountConfigError(f"AUTH_MODE must be one of {', '.join(AUTH_MODES)}, not {configured!r}.")
    if configured == "google":
        google_client()


# ── Google OpenID Connect ──────────────────────────────────────────────────────

def _code_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def start_sign_in(origin: str) -> tuple[str, dict[str, Any]]:
    """Return Google's authorization URL and the values to keep on the browser session until the callback."""
    client_id, _ = google_client()
    state, nonce, verifier = users.random_token(), users.random_token(), users.random_token()
    redirect_uri = origin + CALLBACK_PATH
    url = GOOGLE_AUTHORIZE_URL + "?" + urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "scope": "openid email profile", "state": state, "nonce": nonce,
        "code_challenge": _code_challenge(verifier), "code_challenge_method": "S256",
        "prompt": "select_account",
    })
    stored = {"state": state, "nonce": nonce, "verifier": verifier, "redirectUri": redirect_uri, "startedAt": users.now_ms()}
    return url, {SIGN_IN_KEY: stored}


def id_token_claims(id_token: str) -> dict[str, Any]:
    """Decode an ID token's payload without checking its signature (see ``finish_sign_in``)."""
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as exc:
        raise SignInError("failed", "Google returned an unreadable ID token.") from exc
    if not isinstance(claims, dict):
        raise SignInError("failed", "Google returned an unreadable ID token.")
    return claims


def check_claims(claims: dict[str, Any], *, client_id: str, nonce: str, now_seconds: int) -> GoogleIdentity:
    """Validate an ID token's claims (OpenID Connect Core 3.1.3.7) and return who signed in."""
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise SignInError("failed", "The ID token was not issued by Google.")
    if client_id not in audiences or (len(audiences) > 1 and claims.get("azp") != client_id):
        raise SignInError("failed", "The ID token was issued for another app.")
    try:
        expires, issued = int(claims["exp"]), int(claims.get("iat", 0))
    except (KeyError, TypeError, ValueError) as exc:
        raise SignInError("failed", "The ID token has no expiry.") from exc
    if expires < now_seconds - CLOCK_SKEW_SECONDS or issued > now_seconds + CLOCK_SKEW_SECONDS:
        raise SignInError("expired", "The ID token has expired.")
    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise SignInError("failed", "The ID token does not match this sign-in.")
    subject, email = claims.get("sub"), claims.get("email")
    if not isinstance(subject, str) or not subject or not isinstance(email, str) or not email:
        raise SignInError("failed", "Google did not return an account id and email address.")
    if claims.get("email_verified") not in (True, "true"):
        raise SignInError("failed", "This Google account's email address is not verified.")
    name = claims.get("given_name") or claims.get("name") or ""
    return GoogleIdentity(subject=subject, email=email, name=name if isinstance(name, str) else "")


async def finish_sign_in(stored: dict[str, Any] | None, params: dict[str, str]) -> GoogleIdentity:
    """Check the callback against the sign-in started on this session and exchange the code for an identity.

    The ID token comes straight from Google's token endpoint over TLS, in exchange for the code and this
    app's client secret, so its signature is not checked (OpenID Connect Core 3.1.3.7, step 6); its issuer,
    audience, times and nonce are. Raises ``SignInError``.
    """
    if params.get("error"):
        raise SignInError("cancelled", f"Google sign-in was not completed: {params['error'][:100]}")
    if not isinstance(stored, dict) or not stored.get("state"):
        raise SignInError("expired", "No Google sign-in was started in this browser.")
    if users.now_ms() - int(stored.get("startedAt", 0)) > SIGN_IN_SECONDS * 1000:
        raise SignInError("expired", "The Google sign-in took too long.")
    if not hmac.compare_digest(params.get("state", ""), stored["state"]) or not params.get("code"):
        raise SignInError("failed", "The Google sign-in could not be verified.")
    client_id, client_secret = google_client()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(GOOGLE_TOKEN_URL, data={
                "grant_type": "authorization_code", "code": params["code"], "redirect_uri": stored["redirectUri"],
                "client_id": client_id, "client_secret": client_secret, "code_verifier": stored["verifier"],
            })
    except httpx.HTTPError as exc:
        raise SignInError("failed", f"Could not reach Google: {type(exc).__name__}") from exc
    if not response.is_success:
        raise SignInError("failed", f"Google token exchange failed: HTTP {response.status_code}")
    try:
        token = response.json()
    except ValueError:
        token = None
    if not isinstance(token, dict) or not isinstance(token.get("id_token"), str):
        raise SignInError("failed", "Google returned no ID token.")
    return check_claims(
        id_token_claims(token["id_token"]), client_id=client_id, nonce=stored["nonce"], now_seconds=users.now_ms() // 1000,
    )


# ── Google accounts without a profile yet ─────────────────────────────────────

def pending_values(identity: GoogleIdentity) -> dict[str, Any]:
    """Session values for a signed-in Google account that has no profile yet."""
    return {PENDING_KEY: {"subject": identity.subject, "email": identity.email, "name": identity.name, "at": users.now_ms()}}


def pending_identity(session: dict[str, Any]) -> GoogleIdentity | None:
    """The Google account waiting on this session to create or claim a profile, unless it waited too long."""
    pending = session.get(PENDING_KEY)
    if not isinstance(pending, dict) or users.now_ms() - int(pending.get("at", 0)) > PENDING_SECONDS * 1000:
        return None
    return GoogleIdentity(subject=pending["subject"], email=pending["email"], name=pending.get("name", ""))


# ── Linked accounts ────────────────────────────────────────────────────────────

def user_for_identity(identity: GoogleIdentity) -> str | None:
    """The profile linked to this Google account, recording the sign-in."""
    with connect_db() as connection:
        row = connection.execute(
            "SELECT user_id FROM accounts WHERE provider = ? AND subject = ?", (PROVIDER_GOOGLE, identity.subject),
        ).fetchone()
        if row is None:
            return None
        # Google accounts can change their address; keep the latest one.
        connection.execute(
            "UPDATE accounts SET email = ?, last_sign_in_at = ? WHERE provider = ? AND subject = ?",
            (vault.encrypt(identity.email), users.now_ms(), PROVIDER_GOOGLE, identity.subject),
        )
    return row["user_id"]


def is_linked(user_id: str) -> bool:
    with connect_db() as connection:
        return connection.execute("SELECT 1 FROM accounts WHERE user_id = ?", (user_id,)).fetchone() is not None


def link(user_id: str, identity: GoogleIdentity) -> None:
    """Link a Google account to a profile. Raises ``AccountLinkError`` if either is already linked."""
    now = users.now_ms()
    try:
        with connect_db() as connection:
            connection.execute(
                "INSERT INTO accounts(provider, subject, user_id, email, created_at, last_sign_in_at) VALUES (?, ?, ?, ?, ?, ?)",
                (PROVIDER_GOOGLE, identity.subject, user_id, vault.encrypt(identity.email), now, now),
            )
    except sqlite3.IntegrityError as exc:
        raise AccountLinkError("This Google account or profile is already linked.") from exc


def account_for_user(user_id: str) -> dict[str, Any] | None:
    """The account a profile signs in with: provider, email address and times, or None (local profiles)."""
    with connect_db() as connection:
        row = connection.execute(
            "SELECT provider, email, created_at, last_sign_in_at FROM accounts WHERE user_id = ?", (user_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        email = vault.decrypt(row["email"])
    except vault.VaultError:
        email = ""
    return {"provider": row["provider"], "email": email, "linkedAt": row["created_at"], "lastSignInAt": row["last_sign_in_at"]}


def unclaimed_profile_named(display_name: str) -> dict[str, Any] | None:
    """The profile with this name that no account has claimed, if exactly one exists."""
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT users.id FROM users LEFT JOIN accounts ON accounts.user_id = users.id "
            "WHERE accounts.user_id IS NULL AND users.display_name = ? COLLATE NOCASE",
            (display_name,),
        ).fetchall()
    return users.get_user(rows[0]["id"]) if len(rows) == 1 else None


def spend_pin_check(pin: str) -> None:
    """Take as long as checking a real PIN, so a claim for an unknown name answers no faster."""
    global _dummy_pin_hash
    if _dummy_pin_hash is None:
        _dummy_pin_hash = users.hash_pin(users.random_token())
    users.verify_pin(pin, _dummy_pin_hash)

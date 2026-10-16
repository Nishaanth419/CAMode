"""Unlocking a profile's history: key setup, PIN unlock, recovery codes, and the unlocked set.

After the PIN (or the recovery code) is entered, a profile's private key is held in
this process's memory for that browser session only. A server restart therefore
locks every profile again: the session cookie stays valid, and the app asks for
the PIN before showing chats, snapshots or profile fields.

Profiles created before encryption get their keys, and a recovery code, the next
time they enter their PIN; their plaintext rows are sealed at that point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nacl.public import PrivateKey

from . import chats, memory, sealing, snapshots, users
from .db import connect_db
from .sealing import ProfileKeys, WrongSecretError

# Browser session id -> (user id, unlocked private key).
_unlocked: dict[str, tuple[str, PrivateKey]] = {}


class NoRecoveryCodeError(users.ProfileError):
    """The profile has never had keys, so it has no recovery code to use."""


@dataclass(frozen=True)
class Unlocked:
    """The result of entering a PIN or recovery code."""

    private_key: PrivateKey = field(repr=False)
    # Set only when a new recovery code was just created; show it to the user once.
    recovery_code: str | None = None


def keys_for_session(user: dict[str, Any], sid: str | None) -> ProfileKeys:
    """Return the profile's keys, unlocked if this browser session entered the PIN."""
    entry = _unlocked.get(sid) if sid else None
    private_key = entry[1] if entry and entry[0] == user["id"] else None
    return ProfileKeys(public_key=user["publicKey"], private_key=private_key)


def activate(sid: str, user_id: str, private_key: PrivateKey) -> None:
    """Remember the unlocked key for ``sid``, seal the profile's rows still in plaintext, and move old goals.

    Goals saved inside the profile JSON (before ``financial_goals`` existed) move to
    their own table here, the first time the PIN is entered after the upgrade.
    """
    _unlocked[sid] = (user_id, private_key)
    keys = ProfileKeys(public_key=private_key.public_key, private_key=private_key)
    chats.seal_plaintext_chats(user_id, keys)
    snapshots.seal_plaintext_snapshots(user_id, keys)
    with connect_db() as connection:
        row = connection.execute("SELECT profile FROM users WHERE id = ?", (user_id,)).fetchone()
        if row and row["profile"] != "{}" and not sealing.is_sealed(row["profile"]):
            connection.execute("UPDATE users SET profile = ? WHERE id = ?", (keys.seal(row["profile"]), user_id))
    memory.migrate_profile_goals(user_id, keys)


def forget_session(sid: str) -> None:
    _unlocked.pop(sid, None)


def forget_user(user_id: str) -> None:
    for sid in [sid for sid, (owner, _) in _unlocked.items() if owner == user_id]:
        del _unlocked[sid]


def _wrap(private_key: PrivateKey, secret: str) -> str:
    return sealing.wrap_private_key(private_key, secret, n=users.SCRYPT_N, r=users.SCRYPT_R, p=users.SCRYPT_P)


def create_keys(user_id: str, pin: str) -> Unlocked:
    """Give a profile its keypair and first recovery code. The PIN must already be verified.

    If another request created keys first, unlock those instead of replacing them.
    """
    private_key = PrivateKey.generate()
    recovery_code = sealing.new_recovery_code()
    with connect_db() as connection:
        created = connection.execute(
            "UPDATE users SET history_public_key = ?, history_key_pin = ?, history_key_recovery = ? "
            "WHERE id = ? AND history_key_pin IS NULL",
            (ProfileKeys(public_key=private_key.public_key).public_key_text, _wrap(private_key, pin),
             _wrap(private_key, recovery_code), user_id),
        ).rowcount
    if created:
        return Unlocked(private_key, recovery_code)
    user = users.get_user(user_id)
    assert user is not None and user["keyWrappedWithPin"]  # The UPDATE matched no row only because keys exist.
    return Unlocked(sealing.unwrap_private_key(user["keyWrappedWithPin"], pin))


def unlock_with_pin(user: dict[str, Any], pin: str) -> Unlocked:
    """Unwrap the private key with an already verified PIN, creating keys for profiles that have none."""
    if not user["keyWrappedWithPin"]:
        return create_keys(user["id"], pin)
    return Unlocked(sealing.unwrap_private_key(user["keyWrappedWithPin"], pin))


def keys_from_pin(user: dict[str, Any], pin: str) -> ProfileKeys:
    """Unlocked keys from an already verified PIN, without creating any (plaintext profiles read as is)."""
    if not user["keyWrappedWithPin"]:
        return ProfileKeys()
    private_key = sealing.unwrap_private_key(user["keyWrappedWithPin"], pin)
    return ProfileKeys(public_key=private_key.public_key, private_key=private_key)


def recover(user_id: str, recovery_code: str, new_pin: str) -> Unlocked:
    """Reset the PIN using the recovery code, and replace the recovery code.

    Wrong codes count towards the same lockout as wrong PINs. Raises
    ``NoRecoveryCodeError``, ``WrongSecretError``, ``users.ProfileLockedError``, or
    ``users.ProfileError`` for an unacceptable new PIN.
    """
    users.validate_new_pin(new_pin)
    user = users.get_user(user_id)
    if user is None or not user["keyWrappedWithRecovery"]:
        raise NoRecoveryCodeError("This profile has no recovery code. It can only be unlocked with its PIN.")
    code = sealing.normalize_recovery_code(recovery_code)
    found: dict[str, PrivateKey] = {}

    def verify(row: Any) -> bool:
        try:
            found["key"] = sealing.unwrap_private_key(row["history_key_recovery"], code)
        except WrongSecretError:
            return False
        return True

    if not users.check_with_lockout(user_id, verify):
        raise WrongSecretError("That recovery code is not correct.")
    private_key = found["key"]
    new_code = sealing.new_recovery_code()
    with connect_db() as connection:
        connection.execute(
            "UPDATE users SET pin_hash = ?, history_key_pin = ?, history_key_recovery = ?, updated_at = ? WHERE id = ?",
            (users.hash_pin(new_pin), _wrap(private_key, new_pin), _wrap(private_key, new_code), users.now_ms(), user_id),
        )
    return Unlocked(private_key, new_code)


def replace_recovery_code(user: dict[str, Any], pin: str) -> str:
    """Issue a new recovery code (the old one stops working). The PIN must already be verified."""
    unlocked = unlock_with_pin(user, pin)
    if unlocked.recovery_code:
        return unlocked.recovery_code  # Keys were just created with a fresh code.
    code = sealing.new_recovery_code()
    with connect_db() as connection:
        connection.execute(
            "UPDATE users SET history_key_recovery = ?, updated_at = ? WHERE id = ?",
            (_wrap(unlocked.private_key, code), users.now_ms(), user["id"]),
        )
    return code

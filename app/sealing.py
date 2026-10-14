"""Per-profile encryption keys for chats, snapshots and profile fields.

Each profile has an X25519 keypair. The public key is stored as is, so the
snapshot scheduler can encrypt ("seal") new data without the PIN. The private key
is stored twice, each copy encrypted ("wrapped") with a key derived by scrypt from
a secret the user knows: their PIN, and a one-time recovery code. Without one of
those secrets, sealed data cannot be read, even with the database and the token
master key.

Sealed values are ``sealed:v1:<base64 of a libsodium sealed box>``. Values without
that prefix are plaintext from before a profile had keys, and pass through
``ProfileKeys.open`` unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Final

from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.secret import SecretBox

SEALED_PREFIX: Final = "sealed:v1:"
# Crockford base32 without the ambiguous I, L, O and U: 20 characters carry 100 bits.
RECOVERY_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
RECOVERY_CODE_LENGTH: Final = 20
_SCRYPT_MAXMEM = 256 * 1024 * 1024


class WrongSecretError(Exception):
    """The PIN or recovery code does not unwrap this profile's private key."""


class LockedError(Exception):
    """Sealed data was read without the profile's private key."""


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode()


def _derive(secret: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(secret.encode(), salt=salt, n=n, r=r, p=p, maxmem=_SCRYPT_MAXMEM, dklen=SecretBox.KEY_SIZE)


def wrap_private_key(private_key: PrivateKey, secret: str, *, n: int, r: int = 8, p: int = 1) -> str:
    """Encrypt the private key under ``secret``: ``scrypt$N$r$p$salt$box`` (parameters travel with it)."""
    salt = secrets.token_bytes(16)
    box = SecretBox(_derive(secret, salt, n, r, p)).encrypt(bytes(private_key))
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(bytes(box))}"


def unwrap_private_key(wrapped: str, secret: str) -> PrivateKey:
    """Recover the private key. Raises ``WrongSecretError`` if ``secret`` is not the one it was wrapped with."""
    try:
        scheme, n, r, p, salt, box = wrapped.split("$")
        if scheme != "scrypt":
            raise WrongSecretError("Unknown key wrapping scheme.")
        key = _derive(secret, base64.b64decode(salt), int(n), int(r), int(p))
        return PrivateKey(SecretBox(key).decrypt(base64.b64decode(box)))
    except (ValueError, TypeError, CryptoError) as exc:
        raise WrongSecretError("The PIN or recovery code is not correct.") from exc


def _group(raw: str) -> str:
    return "-".join(raw[index:index + 4] for index in range(0, len(raw), 4))


def new_recovery_code() -> str:
    """Return a random code formatted for writing down, like ``7KQ2-...`` (five groups of four)."""
    return _group("".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH)))


def normalize_recovery_code(code: str) -> str:
    """Canonicalise user input: case, spaces and dashes don't matter, and O/I/L read as 0/1/1."""
    return _group("".join(code.upper().split()).replace("-", "").translate(str.maketrans("OIL", "011")))


def is_sealed(stored: str | None) -> bool:
    return bool(stored) and stored.startswith(SEALED_PREFIX)


@dataclass(frozen=True)
class ProfileKeys:
    """A profile's keys as available to one request.

    ``public_key`` is None for a profile created before encryption that has not
    unlocked since; its data is still written in plaintext until it does.
    ``private_key`` is None when the profile is locked (no PIN entered since restart).
    """

    public_key: PublicKey | None = None
    private_key: PrivateKey | None = field(default=None, repr=False)

    @classmethod
    def from_stored_public_key(cls, stored: str | None) -> ProfileKeys:
        """Keys that can seal but not open, from a ``users.history_public_key`` value."""
        return cls(public_key=PublicKey(base64.b64decode(stored)) if stored else None)

    @property
    def public_key_text(self) -> str | None:
        """The public key as stored in ``users.history_public_key``."""
        return _b64(bytes(self.public_key)) if self.public_key else None

    @property
    def is_unlocked(self) -> bool:
        return self.private_key is not None

    def seal(self, plaintext: str) -> str:
        if self.public_key is None:
            return plaintext
        return SEALED_PREFIX + _b64(SealedBox(self.public_key).encrypt(plaintext.encode()))

    def open(self, stored: str) -> str:
        """Return the plaintext of a sealed value; plaintext values pass through.

        Raises ``LockedError`` for a sealed value when the private key is not available.
        """
        if not is_sealed(stored):
            return stored
        if self.private_key is None:
            raise LockedError("Unlock this profile with its PIN to read its history.")
        return SealedBox(self.private_key).decrypt(base64.b64decode(stored[len(SEALED_PREFIX):])).decode()

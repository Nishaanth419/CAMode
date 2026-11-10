"""Encrypt secrets stored in SQLite with a master key kept outside the database.

INDmoney OAuth tokens and in-flight OAuth session data are stored as
``enc:v1:<Fernet token>`` (AES-128-CBC with HMAC-SHA256), so a copy of the database
file alone does not reveal them. The key is resolved in this order:

1. ``APP_MASTER_KEY``: a Fernet key (32 url-safe base64-encoded bytes).
2. ``APP_MASTER_KEY_FILE``: a file holding that key, created on first use.
3. ``~/.config/ca-mode/master.key``, created on first use.

A key file is only created while the database holds no encrypted value. If the file is missing
but ``enc:v1:`` values exist (a lost or unmounted key), startup fails naming the file, instead of
silently making a new key that cannot read any of them.

The default file lives outside the project and ``.data/``, so database copies and
backups do not carry it. Losing the key makes stored tokens unreadable; profiles
then show as disconnected and must reconnect INDmoney.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from functools import cache
from pathlib import Path
from typing import Final

from cryptography.fernet import Fernet, InvalidToken

from .db import connect_db

logger = logging.getLogger("ca_mode.vault")

PREFIX: Final = "enc:v1:"
# Columns that hold ``enc:v1:`` values; checked before a new key file is created.
ENCRYPTED_COLUMNS: Final = (("users", "indmoney"), ("broker_connections", "session"), ("oauth_sessions", "data"))
DEFAULT_KEY_FILE: Final = Path.home() / ".config" / "ca-mode" / "master.key"


class VaultError(Exception):
    """The master key is invalid, or a stored value cannot be decrypted with it."""


def _parse_key(raw: bytes, source: str) -> Fernet:
    try:
        return Fernet(raw.strip())
    except ValueError as exc:
        raise VaultError(f"{source} is not a valid Fernet key (32 url-safe base64-encoded bytes).") from exc


def _create_key_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return  # Another process created it first; use theirs.
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(Fernet.generate_key())
    logger.info("master_key_created", extra={"path": str(path)})


def encrypted_values_stored() -> bool:
    """Whether the database already holds a value encrypted with some master key (a missing table counts as none)."""
    with connect_db() as connection:
        for table, column in ENCRYPTED_COLUMNS:
            try:
                found = connection.execute(
                    f"SELECT 1 FROM {table} WHERE {column} LIKE ? LIMIT 1", (PREFIX + "%",),  # noqa: S608 (fixed names)
                ).fetchone()
            except sqlite3.OperationalError:
                continue  # table not created yet
            if found:
                return True
    return False


@cache
def _fernet() -> Fernet:
    literal = os.getenv("APP_MASTER_KEY", "").strip()
    if literal:
        return _parse_key(literal.encode(), "APP_MASTER_KEY")
    path = Path(os.getenv("APP_MASTER_KEY_FILE", "").strip() or DEFAULT_KEY_FILE).expanduser()
    if not path.exists():
        if encrypted_values_stored():
            raise VaultError(
                f"The master key file {path} is missing, but the database holds secrets encrypted with a master key. "
                "Restore that file (or set APP_MASTER_KEY / APP_MASTER_KEY_FILE to the key in use). "
                "A new key is not created, because it could not read any of them."
            )
        _create_key_file(path)
    return _parse_key(path.read_bytes(), f"The master key file {path}")


def check_key() -> None:
    """Load the master key now so a bad key fails at startup, not on first use."""
    _fernet()


def is_encrypted(stored: str | None) -> bool:
    return bool(stored) and stored.startswith(PREFIX)


def encrypt(plaintext: str) -> str:
    return PREFIX + _fernet().encrypt(plaintext.encode()).decode()


def decrypt(stored: str) -> str:
    """Decrypt a value written by ``encrypt``. Raises ``VaultError`` if the key does not match."""
    if not is_encrypted(stored):
        raise VaultError("Value is not encrypted.")
    try:
        return _fernet().decrypt(stored[len(PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise VaultError("Stored secret cannot be decrypted with the current master key.") from exc

"""Data-storage consent, per-profile storage mode, export, and history clearing.

A profile must accept the current ``POLICY_VERSION`` notice before it can chat,
connect INDmoney, or have snapshots captured. Every acceptance, including a later
change of storage mode or retention, appends a row to ``consents``, so the table
is a record of what each profile agreed to and when. Bump ``POLICY_VERSION``
whenever ``NOTICE`` changes what is stored or shared; every profile is then asked
again, and the scheduler pauses their snapshots until they accept.
"""

from __future__ import annotations

import json
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict

from . import chats, users
from .db import connect_db

POLICY_VERSION: Final = "2026-10-14"

StorageMode = Literal["device", "private"]
RetentionDays = Literal[30, 90, 180, 365]

NOTICE: Final = (
    {
        "title": "What is saved on this computer",
        "body": (
            "Your profile name, a scrypt hash of your PIN, the optional profile details you enter, and your INDmoney "
            "connection tokens. If you choose to save history: your chats (each kept 30 days after its last message, "
            "at most 50) and a daily snapshot of your INDmoney net worth, allocation and holdings for the retention "
            "period you pick. Everything is in a SQLite file readable only by this computer's user account. It is not "
            "encrypted, so anyone who can sign in to this computer as you can read it."
        ),
    },
    {
        "title": "What leaves this computer",
        "body": (
            "To answer you, your message, recent chat history and profile details are sent to the AI provider you "
            "select (OpenAI or Groq), along with any INDmoney or snapshot data Portfolio Analyzer reads for that "
            "answer. That data is handled under the provider's own data policy. Portfolio Analyzer reads from INDmoney "
            "with read-only access and cannot trade or move money."
        ),
    },
    {
        "title": "Private mode",
        "body": (
            "Chats are kept in memory only and are gone when you sign out or the app restarts. No portfolio snapshots "
            "are taken, and switching to private mode deletes the chats and snapshots already saved for this profile. "
            "Your INDmoney connection tokens are still saved so you stay connected; disconnect INDmoney to remove them."
        ),
    },
    {
        "title": "Your controls",
        "body": (
            "From your profile you can change these choices at any time, export your data, clear your saved history, "
            "disconnect INDmoney, or delete the profile and everything stored for it."
        ),
    },
)


class StaleNoticeError(Exception):
    """The client accepted a notice version that is no longer current."""


class ConsentRequest(BaseModel):
    """The notice version the user read and the storage choices they made."""

    model_config = ConfigDict(extra="forbid")

    policyVersion: str
    storageMode: StorageMode
    snapshotRetentionDays: RetentionDays = 365


def latest_consent(user_id: str) -> dict[str, Any] | None:
    with connect_db() as connection:
        row = connection.execute(
            "SELECT policy_version, storage_mode, snapshot_retention_days, accepted_at FROM consents "
            "WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "policyVersion": row["policy_version"], "storageMode": row["storage_mode"],
        "snapshotRetentionDays": row["snapshot_retention_days"], "acceptedAt": row["accepted_at"],
    }


def has_current_consent(user_id: str) -> bool:
    consent = latest_consent(user_id)
    return consent is not None and consent["policyVersion"] == POLICY_VERSION


def privacy_state(user: dict[str, Any]) -> dict[str, Any]:
    """Return the notice, whether it still needs accepting, and the profile's current choices."""
    consent = latest_consent(user["id"])
    return {
        "policyVersion": POLICY_VERSION,
        "notice": list(NOTICE),
        "consentRequired": consent is None or consent["policyVersion"] != POLICY_VERSION,
        "acceptedAt": consent["acceptedAt"] if consent else None,
        "storageMode": user["storageMode"],
        "snapshotRetentionDays": user["snapshotRetentionDays"],
    }


def _delete_saved_history(connection: Any, user_id: str) -> None:
    connection.execute("DELETE FROM chats WHERE user_id = ?", (user_id,))
    connection.execute("DELETE FROM portfolio_snapshots WHERE user_id = ?", (user_id,))
    connection.execute("UPDATE users SET snapshot_attempted_at = 0 WHERE id = ?", (user_id,))


def record_consent(user: dict[str, Any], choice: ConsentRequest) -> None:
    """Record acceptance of the current notice and apply the chosen storage settings.

    Choosing private mode deletes the profile's saved chats and snapshots. Changing
    mode in either direction drops in-memory private chats. Raises
    ``StaleNoticeError`` if ``choice`` names an older notice than the current one.
    """
    if choice.policyVersion != POLICY_VERSION:
        raise StaleNoticeError("The privacy notice has changed. Review it again.")
    with connect_db() as connection:
        connection.execute(
            "INSERT INTO consents(user_id, policy_version, storage_mode, snapshot_retention_days, accepted_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user["id"], POLICY_VERSION, choice.storageMode, choice.snapshotRetentionDays, users.now_ms()),
        )
        connection.execute(
            "UPDATE users SET storage_mode = ?, snapshot_retention_days = ?, updated_at = ? WHERE id = ?",
            (choice.storageMode, choice.snapshotRetentionDays, users.now_ms(), user["id"]),
        )
        if choice.storageMode == "private":
            _delete_saved_history(connection, user["id"])
    if choice.storageMode != user["storageMode"]:
        chats.forget_private_chats(user["id"])


def clear_history(user_id: str) -> None:
    """Delete the profile's chats (saved and in-memory) and snapshots, keeping the profile and connection."""
    with connect_db() as connection:
        _delete_saved_history(connection, user_id)
    chats.forget_private_chats(user_id)


def export_data(user: dict[str, Any]) -> dict[str, Any]:
    """Return everything stored for the profile except its PIN hash and INDmoney tokens."""
    with connect_db() as connection:
        consent_rows = connection.execute(
            "SELECT policy_version, storage_mode, snapshot_retention_days, accepted_at FROM consents "
            "WHERE user_id = ? ORDER BY id", (user["id"],),
        ).fetchall()
        snapshot_rows = connection.execute(
            "SELECT as_of_date, captured_at, networth_inr, data FROM portfolio_snapshots "
            "WHERE user_id = ? ORDER BY as_of_date", (user["id"],),
        ).fetchall()
    return {
        "exportedAt": users.now_ms(),
        "profile": {"displayName": user["displayName"], "fields": user["profile"]},
        "settings": {"storageMode": user["storageMode"], "snapshotRetentionDays": user["snapshotRetentionDays"]},
        "indmoneyConnected": bool(user["indmoney"].get("accessToken") or user["indmoney"].get("refreshToken")),
        "consents": [
            {"policyVersion": row["policy_version"], "storageMode": row["storage_mode"],
             "snapshotRetentionDays": row["snapshot_retention_days"], "acceptedAt": row["accepted_at"]}
            for row in consent_rows
        ],
        "chats": chats.export_chats(user),
        "snapshots": [
            {"asOfDate": row["as_of_date"], "capturedAt": row["captured_at"], "networthInr": row["networth_inr"],
             **json.loads(row["data"])}
            for row in snapshot_rows
        ],
    }

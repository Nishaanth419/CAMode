"""Data-storage consent, per-profile storage mode, export, and history clearing.

A profile must accept the current ``POLICY_VERSION`` notice before it can chat,
connect INDmoney, or have snapshots captured. Every acceptance, including a later
change of storage mode or retention, appends a row to ``consents``, so the table
is a record of what each profile agreed to and when. Bump ``POLICY_VERSION``
whenever ``NOTICE`` changes what is stored or shared; every profile is then asked
again, and the scheduler pauses their snapshots until they accept. Wording fixes
that only make the notice more accurate (such as describing added encryption) do
not need a bump.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict

from . import chats, users
from .db import connect_db

# 2026-10-17.1: anonymous daily counts of which app pages are opened, kept on this computer.
# 2026-10-17.2: a record of the steps behind each chat answer (agent_runs), for the Activity page.
POLICY_VERSION: Final = "2026-10-17.2"

StorageMode = Literal["device", "private"]
RetentionDays = Literal[30, 90, 180, 365]

NOTICE: Final = (
    {
        "title": "What is saved on this computer",
        "body": (
            "Your profile name, a scrypt hash of your PIN, the optional profile details, savings goals and decisions you "
            "enter yourself, and your INDmoney connection tokens. CA Mode never saves details the assistant infers "
            "from your chats. If you choose to save history: your chats (each kept 30 days after its last message, "
            "at most 50) and a daily snapshot of your INDmoney net worth, allocation and holdings for the retention "
            "period you pick, the holdings from any CAS statement you upload (the last 12; never the PDF, its "
            "password, or your name, PAN or contact details), and the portfolio change investigations you run (the "
            "last 50: the snapshots compared, the figures computed from them, the checks and the result), and a record of the steps behind each chat answer (the last 200, none older than 30 days: the start of your question, which data was read and a short preview of it, the checks on the answer and how long each step took; never the answer itself or your PIN, tokens or PAN). Everything is in a SQLite file readable only by this computer's user account. INDmoney "
            "tokens are encrypted with a key kept outside that file."
        ),
    },
    {
        "title": "Locked with your PIN",
        "body": (
            "Your saved chats, answer records, portfolio snapshots, investigations, CAS holdings, profile details, goals and decisions are encrypted with a key that only your PIN or "
            "your recovery code can unlock. Nobody else using this computer, and nobody with a copy of the database, "
            "can read them. After the app restarts you enter your PIN again. If you forget your PIN, your recovery "
            "code lets you set a new one. If you lose both, your saved history cannot be recovered by anyone."
        ),
    },
    {
        "title": "What leaves this computer",
        "body": (
            "To answer you, your message, recent chat history, and the profile details, goals and decisions that bear on "
            "your question (with figures calculated from them) go to the AI model you select, "
            "along with any INDmoney or snapshot data Portfolio Analyzer reads for that answer. With Ollama (on this "
            "device) the model runs on this computer and none of it leaves. With OpenAI or Groq it is sent to that "
            "company and handled under its own data policy. Portfolio Analyzer reads from INDmoney with read-only "
            "access and cannot trade or move money."
        ),
    },
    {
        "title": "Private mode",
        "body": (
            "Chats are kept in memory only and are gone when you sign out or the app restarts. No portfolio snapshots "
            "are taken, CAS statements cannot be uploaded, investigations and answer records are not saved, and switching "
            "to private mode deletes the chats, answer records, snapshots, investigations and CAS holdings already "
            "saved for this profile. "
            "Your INDmoney connection tokens, profile details, goals and decisions are still saved; disconnect INDmoney "
            "or remove your saved goals and decisions on the Goals page to delete them."
        ),
    },
    {
        "title": "In your browser",
        "body": (
            "CA Mode sets one cookie, which keeps this browser signed in to your profile, and remembers your theme, "
            "agent and model choice in this browser. It has no advertising and no third-party analytics or tracking. "
            "Pages load fonts from Google Fonts and the chart library from cdn.plot.ly, so those services see this "
            "computer's IP address when the app opens."
        ),
    },
    {
        "title": "Page-view counts",
        "body": (
            "If you save history on this computer, CA Mode adds one to a daily count each time you open one of its "
            "pages, such as Goals or Portfolio Overview. Only the date, the page name and the number are stored: not "
            "your profile, what you typed, which chat or investigation you opened, your IP address or your browser. "
            "The counts stay in the same SQLite file for 90 days, are never sent anywhere, and can be read by whoever "
            "runs CA Mode on this computer. Because they are not linked to you, they are not part of your export and "
            "are not removed when you clear history or delete your profile. Private mode is never counted."
        ),
    },
    {
        "title": "Your controls",
        "body": (
            "From your profile you can change these choices at any time, export your data, clear your saved history, "
            "disconnect INDmoney, or delete the profile and everything stored for it. The Goals page shows the goals, "
            "decisions and profile details the assistant can use, and lets you edit or remove them."
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
    connection.execute("DELETE FROM cas_statements WHERE user_id = ?", (user_id,))
    connection.execute("DELETE FROM investigations WHERE user_id = ?", (user_id,))
    connection.execute("DELETE FROM agent_runs WHERE user_id = ?", (user_id,))
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


def export_data(
    user: dict[str, Any],
    snapshots: list[dict[str, Any]],
    cas_statements: list[dict[str, Any]] | None = None,
    investigations: list[dict[str, Any]] | None = None,
    saved_context: dict[str, list[dict[str, Any]]] | None = None,
    agent_runs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return everything stored for the profile except its PIN hash, keys and INDmoney tokens.

    ``user["keys"]`` must be unlocked; ``snapshots`` are the profile's decrypted snapshots.
    """
    with connect_db() as connection:
        consent_rows = connection.execute(
            "SELECT policy_version, storage_mode, snapshot_retention_days, accepted_at FROM consents "
            "WHERE user_id = ? ORDER BY id", (user["id"],),
        ).fetchall()
    return {
        "exportedAt": users.now_ms(),
        "profile": {"displayName": user["displayName"], "fields": users.profile_fields(user, user["keys"])},
        "settings": {"storageMode": user["storageMode"], "snapshotRetentionDays": user["snapshotRetentionDays"]},
        "indmoneyConnected": users.has_indmoney_tokens(user["indmoney"]),
        "consents": [
            {"policyVersion": row["policy_version"], "storageMode": row["storage_mode"],
             "snapshotRetentionDays": row["snapshot_retention_days"], "acceptedAt": row["accepted_at"]}
            for row in consent_rows
        ],
        "chats": chats.export_chats(user),
        "snapshots": snapshots,
        "casStatements": cas_statements or [],
        "investigations": investigations or [],
        "agentRuns": agent_runs or [],
        "goals": (saved_context or {}).get("goals", []),
        "decisions": (saved_context or {}).get("decisions", []),
        "goalAllocations": (saved_context or {}).get("goalAllocations", []),
    }

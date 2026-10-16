"""Financial context the user confirmed: goals and recorded decisions, sealed per profile.

Only explicit user actions (the Goals page, the goal planner, the API) write here,
and every record says so (``source: "user_entered"``) with the times it was created
and last changed. Observed data stays where it already lives (portfolio snapshots
and CAS statements, each with its capture time), and anything the assistant infers
in a chat is never stored. ``app.context`` reads from all of these and labels each.

Goals and decisions are stored like investigations: plain id and time columns, and
the record sealed under the profile's public key, so reading needs the PIN.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import date, timedelta
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import users
from .db import connect_db
from .sealing import ProfileKeys

logger = logging.getLogger("ca_mode.memory")

MAX_GOALS: Final = 10
MAX_DECISIONS: Final = 50
ID_PATTERN: Final = r"^[A-Za-z0-9_-]{1,32}$"
SOURCE_USER: Final = "user_entered"
EARLIEST_DATE: Final = date(2000, 1, 1)
LATEST_DATE: Final = date(2100, 12, 31)

Currency = Literal["INR", "USD", "EUR", "GBP"]
GoalKind = Literal["home", "education", "retirement", "car", "travel", "wedding", "emergency", "other"]
Table = Literal["financial_goals", "financial_decisions"]


class SavedContextError(Exception):
    """A goal or decision could not be saved for a reason the user can act on."""


class LimitReachedError(SavedContextError):
    """The profile already keeps the most goals or decisions allowed."""


class UnknownGoalError(SavedContextError):
    """A decision names a goal this profile does not have."""


def _blank_to_none(value: str | None) -> str | None:
    return value if value else None


class GoalInput(BaseModel):
    """A savings goal as the user confirms it. Amounts are whole units of ``currency``."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80, examples=["Home down payment"])
    kind: GoalKind | None = None
    # Required: the app never assumes a currency for a new goal.
    currency: Currency
    targetAmount: int = Field(ge=1, le=100_000_000_000, examples=[2_500_000])
    targetDate: date = Field(examples=["2032-03-31"])
    savedAmount: int = Field(default=0, ge=0, le=100_000_000_000)
    monthlyContribution: int = Field(default=0, ge=0, le=1_000_000_000)
    # The user's own assumption; None means none was given, and plans show only no growth.
    assumedReturnPct: float | None = Field(default=None, ge=0, le=30)
    notes: str | None = Field(default=None, max_length=500)

    _notes = field_validator("notes")(_blank_to_none)

    @field_validator("targetDate")
    @classmethod
    def _date_in_range(cls, value: date) -> date:
        if not EARLIEST_DATE <= value <= LATEST_DATE:
            raise ValueError("Choose a target date between 2000 and 2100.")
        return value


class DecisionInput(BaseModel):
    """A financial decision the user chose to record, in their words."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=500, examples=["Keep six months of expenses in a liquid fund"])
    decidedOn: date | None = None
    goalId: str | None = Field(default=None, pattern=ID_PATTERN)

    @field_validator("decidedOn")
    @classmethod
    def _not_in_future(cls, value: date | None) -> date | None:
        # A day of slack: the browser's "today" may be ahead of the server's.
        if value is not None and not EARLIEST_DATE <= value <= date.today() + timedelta(days=1):
            raise ValueError("A decision date cannot be in the future.")
        return value


def new_id() -> str:
    return secrets.token_urlsafe(9)


# ── Sealed rows ────────────────────────────────────────────────────────────────

def _from_row(row: Any, keys: ProfileKeys) -> dict[str, Any]:
    return {**json.loads(keys.open(row["data"])), "id": row["id"], "createdAt": row["created_at"], "updatedAt": row["updated_at"]}


def _list(table: Table, user_id: str, keys: ProfileKeys) -> list[dict[str, Any]]:
    with connect_db() as connection:
        rows = connection.execute(
            f"SELECT id, created_at, updated_at, data FROM {table} WHERE user_id = ? ORDER BY created_at, id",
            (user_id,),
        ).fetchall()
    return [_from_row(row, keys) for row in rows]


def _get(table: Table, user_id: str, record_id: str, keys: ProfileKeys) -> dict[str, Any] | None:
    with connect_db() as connection:
        row = connection.execute(
            f"SELECT id, created_at, updated_at, data FROM {table} WHERE user_id = ? AND id = ?", (user_id, record_id),
        ).fetchone()
    return _from_row(row, keys) if row else None


def _insert(table: Table, user_id: str, keys: ProfileKeys, data: dict[str, Any], *, limit: int) -> dict[str, Any]:
    record_id, now = new_id(), users.now_ms()
    with connect_db() as connection:
        # Count and insert in one write transaction, so concurrent creates cannot pass the limit.
        connection.execute("BEGIN IMMEDIATE")
        count = connection.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (user_id,)).fetchone()[0]
        if count >= limit:
            raise LimitReachedError(f"You can keep up to {limit}. Delete one to add another.")
        connection.execute(
            f"INSERT INTO {table}(id, user_id, created_at, updated_at, data) VALUES (?, ?, ?, ?, ?)",
            (record_id, user_id, now, now, keys.seal(json.dumps(data, ensure_ascii=False))),
        )
    return {**data, "id": record_id, "createdAt": now, "updatedAt": now}


def _replace(table: Table, user_id: str, record_id: str, keys: ProfileKeys, data: dict[str, Any]) -> bool:
    with connect_db() as connection:
        cursor = connection.execute(
            f"UPDATE {table} SET data = ?, updated_at = ? WHERE user_id = ? AND id = ?",
            (keys.seal(json.dumps(data, ensure_ascii=False)), users.now_ms(), user_id, record_id),
        )
    return cursor.rowcount > 0


def _delete(table: Table, user_id: str, record_id: str) -> bool:
    with connect_db() as connection:
        cursor = connection.execute(f"DELETE FROM {table} WHERE user_id = ? AND id = ?", (user_id, record_id))
    return cursor.rowcount > 0


# ── Goals ──────────────────────────────────────────────────────────────────────

def _goal_data(body: GoalInput, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    data = {**body.model_dump(mode="json"), "source": SOURCE_USER}
    if previous and previous.get("migratedFrom"):
        data["migratedFrom"] = previous["migratedFrom"]
    return data


def list_goals(user_id: str, keys: ProfileKeys) -> list[dict[str, Any]]:
    return _list("financial_goals", user_id, keys)


def get_goal(user_id: str, goal_id: str, keys: ProfileKeys) -> dict[str, Any] | None:
    return _get("financial_goals", user_id, goal_id, keys)


def create_goal(user_id: str, body: GoalInput, keys: ProfileKeys) -> dict[str, Any]:
    """Save a goal the user confirmed. Raises ``LimitReachedError`` at ``MAX_GOALS``."""
    try:
        return _insert("financial_goals", user_id, keys, _goal_data(body), limit=MAX_GOALS)
    except LimitReachedError as exc:
        raise LimitReachedError(f"You can keep up to {MAX_GOALS} goals. Delete one to add another.") from exc


def update_goal(user_id: str, goal_id: str, body: GoalInput, keys: ProfileKeys) -> dict[str, Any] | None:
    """Replace a goal with the user's edited version; None if the profile has no such goal."""
    previous = get_goal(user_id, goal_id, keys)
    if previous is None or not _replace("financial_goals", user_id, goal_id, keys, _goal_data(body, previous)):
        return None
    return get_goal(user_id, goal_id, keys)


def delete_goal(user_id: str, goal_id: str) -> bool:
    """Delete a goal. Decisions linked to it keep their text and show the goal as deleted."""
    return _delete("financial_goals", user_id, goal_id)


# ── Decisions ──────────────────────────────────────────────────────────────────

def _decision_data(user_id: str, body: DecisionInput, keys: ProfileKeys) -> dict[str, Any]:
    if body.goalId and get_goal(user_id, body.goalId, keys) is None:
        raise UnknownGoalError("That goal no longer exists. Choose another goal or none.")
    return {**body.model_dump(mode="json"), "source": SOURCE_USER}


def list_decisions(user_id: str, keys: ProfileKeys) -> list[dict[str, Any]]:
    return _list("financial_decisions", user_id, keys)


def create_decision(user_id: str, body: DecisionInput, keys: ProfileKeys) -> dict[str, Any]:
    """Record a decision the user entered. Raises ``LimitReachedError`` or ``UnknownGoalError``."""
    data = _decision_data(user_id, body, keys)
    try:
        return _insert("financial_decisions", user_id, keys, data, limit=MAX_DECISIONS)
    except LimitReachedError as exc:
        raise LimitReachedError(f"You can keep up to {MAX_DECISIONS} decisions. Delete one to add another.") from exc


def update_decision(user_id: str, decision_id: str, body: DecisionInput, keys: ProfileKeys) -> dict[str, Any] | None:
    """Replace a decision's text, date or goal; None if the profile has no such decision."""
    if not _replace("financial_decisions", user_id, decision_id, keys, _decision_data(user_id, body, keys)):
        return None
    return _get("financial_decisions", user_id, decision_id, keys)


def delete_decision(user_id: str, decision_id: str) -> bool:
    return _delete("financial_decisions", user_id, decision_id)


# ── Whole-profile operations ───────────────────────────────────────────────────

def _legacy_goal(goal: dict[str, Any], migrated_at: int) -> tuple[str, dict[str, Any]] | None:
    """Convert a goal from the old profile list (whole rupees, a target year); None if it is unreadable."""
    try:
        body = GoalInput(
            name=goal["name"], kind=goal.get("kind"), currency="INR", targetAmount=goal["targetInr"],
            targetDate=date(int(goal["targetYear"]), 12, 31), savedAmount=goal.get("savedInr", 0),
            monthlyContribution=goal.get("monthlyInr", 0), assumedReturnPct=goal.get("expectedReturnPct"),
        )
    except (KeyError, TypeError, ValueError):
        return None
    goal_id = str(goal.get("id") or "")
    if not re.fullmatch(ID_PATTERN, goal_id):
        goal_id = new_id()
    data = _goal_data(body)
    data["migratedFrom"] = {
        "at": migrated_at,
        "note": "Moved from your profile's goal list; the target date is 31 December of the target year you entered.",
    }
    return goal_id, data


def migrate_profile_goals(user_id: str, keys: ProfileKeys) -> int:
    """Move goals saved inside the profile JSON (before this table existed) into ``financial_goals``.

    Runs when the profile is unlocked (``unlock.activate``); ``keys`` must be unlocked.
    Returns how many goals moved. Goals that no longer validate are logged and dropped
    from the profile rather than stored half-filled.
    """
    user = users.get_user(user_id)
    if user is None:
        return 0
    stored = users.stored_profile(user, keys)
    legacy = stored.pop("goals", None)
    if legacy is None:
        return 0
    now = users.now_ms()
    converted = [item for item in (_legacy_goal(goal, now) for goal in legacy[:MAX_GOALS] if isinstance(goal, dict)) if item]
    with connect_db() as connection:
        for goal_id, data in converted:
            connection.execute(
                "INSERT OR IGNORE INTO financial_goals(id, user_id, created_at, updated_at, data) VALUES (?, ?, ?, ?, ?)",
                (goal_id, user_id, now, now, keys.seal(json.dumps(data, ensure_ascii=False))),
            )
        connection.execute(
            "UPDATE users SET profile = ? WHERE id = ?", (keys.seal(json.dumps(stored)) if stored else "{}", user_id),
        )
    if len(converted) != len(legacy):
        logger.warning("profile_goals_dropped", extra={"user_id": user_id, "count": len(legacy) - len(converted)})
    return len(converted)


def export_context(user_id: str, keys: ProfileKeys) -> dict[str, list[dict[str, Any]]]:
    return {"goals": list_goals(user_id, keys), "decisions": list_decisions(user_id, keys)}


def clear_saved_context(user_id: str) -> None:
    """Delete the profile's goals, decisions and profile fields (its preferences)."""
    with connect_db() as connection:
        connection.execute("DELETE FROM financial_goals WHERE user_id = ?", (user_id,))
        connection.execute("DELETE FROM financial_decisions WHERE user_id = ?", (user_id,))
        connection.execute("UPDATE users SET profile = '{}', updated_at = ? WHERE id = ?", (users.now_ms(), user_id))

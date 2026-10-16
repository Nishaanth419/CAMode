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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import goal_planning, users
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
# "custom" is accepted as input and stored as "other", so one meaning has one value.
GoalKind = Literal[
    "home", "education", "retirement", "financial_independence", "car", "travel", "wedding", "emergency", "other",
]
GoalStatus = Literal["draft", "active"]
Flexibility = Literal["fixed", "some", "flexible"]
Level = Literal["low", "medium", "high"]
MAX_HORIZON_YEARS: Final = 60
Table = Literal["financial_goals", "financial_decisions"]


class SavedContextError(Exception):
    """A goal or decision could not be saved for a reason the user can act on."""


class LimitReachedError(SavedContextError):
    """The profile already keeps the most goals or decisions allowed."""


class UnknownGoalError(SavedContextError):
    """A decision or an allocation names a goal this profile does not have."""


class InvalidGoalError(SavedContextError):
    """A goal's values cannot be saved as given (for example a horizon that ends after 2100)."""


def _blank_to_none(value: str | None) -> str | None:
    return value if value else None


class GoalInput(BaseModel):
    """A savings goal as the user confirms it. Amounts are whole units of ``currency``.

    A draft needs only a name, so the user can start a goal and finish it later. An
    active goal needs a currency, a target amount and a target date (or a horizon in
    years, which is converted to a date when the goal is saved).
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80, examples=["Home down payment"])
    kind: GoalKind | None = None
    status: GoalStatus = "active"
    # Never assumed: an active goal must name its currency.
    currency: Currency | None = None
    targetAmount: int | None = Field(default=None, ge=1, le=100_000_000_000, examples=[2_500_000])
    targetDate: date | None = Field(default=None, examples=["2032-03-31"])
    # An alternative way to give the date: "in N years" from the day the goal is saved.
    horizonYears: int | None = Field(default=None, ge=1, le=MAX_HORIZON_YEARS)
    savedAmount: int = Field(default=0, ge=0, le=100_000_000_000)
    monthlyContribution: int = Field(default=0, ge=0, le=1_000_000_000)
    # The user's own assumption; None means none was given, and plans show only no growth.
    assumedReturnPct: float | None = Field(default=None, ge=0, le=30)
    # Yearly inflation applied to the target, which is then read as today's money; None means not applied.
    inflationPct: float | None = Field(default=None, ge=0, le=15)
    # Suitability inputs; None means the user did not say, and the rules then assume the cautious answer.
    flexibility: Flexibility | None = None
    liquidityNeed: Level | None = None
    riskTolerance: Level | None = None
    lossCapacity: Level | None = None
    notes: str | None = Field(default=None, max_length=500)

    _notes = field_validator("notes")(_blank_to_none)

    @field_validator("kind", mode="before")
    @classmethod
    def _custom_is_other(cls, value: Any) -> Any:
        return "other" if value == "custom" else value

    @field_validator("targetDate")
    @classmethod
    def _date_in_range(cls, value: date | None) -> date | None:
        if value is not None and not EARLIEST_DATE <= value <= LATEST_DATE:
            raise ValueError("Choose a target date between 2000 and 2100.")
        return value

    @model_validator(mode="after")
    def _complete_when_active(self) -> GoalInput:
        if self.targetDate is not None and self.horizonYears is not None:
            raise ValueError("Give a target date or a horizon, not both.")
        if self.status == "active" and (
            self.currency is None or self.targetAmount is None or (self.targetDate is None and self.horizonYears is None)
        ):
            raise ValueError(
                "Add a currency, a target amount and a target date (or horizon), or save the goal as a draft."
            )
        return self


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

def _goal_data(body: GoalInput, previous: dict[str, Any] | None = None, today: date | None = None) -> dict[str, Any]:
    data = {**body.model_dump(mode="json"), "source": SOURCE_USER}
    if body.horizonYears is not None:
        # Stored as a date (what every plan reads) with the horizon kept for display.
        target = goal_planning.date_after_years(today or date.today(), body.horizonYears)
        if target > LATEST_DATE:
            raise InvalidGoalError("That horizon ends after 2100. Choose a shorter one.")
        data["targetDate"] = target.isoformat()
        data["targetDateFrom"] = "horizon"
    if previous and previous.get("migratedFrom"):
        data["migratedFrom"] = previous["migratedFrom"]
    return data


def list_goals(user_id: str, keys: ProfileKeys) -> list[dict[str, Any]]:
    return _list("financial_goals", user_id, keys)


def count_goals(user_id: str) -> int:
    with connect_db() as connection:
        return connection.execute("SELECT COUNT(*) FROM financial_goals WHERE user_id = ?", (user_id,)).fetchone()[0]


def get_goal(user_id: str, goal_id: str, keys: ProfileKeys) -> dict[str, Any] | None:
    return _get("financial_goals", user_id, goal_id, keys)


def create_goal(user_id: str, body: GoalInput, keys: ProfileKeys, *, today: date | None = None) -> dict[str, Any]:
    """Save a goal the user confirmed. Raises ``LimitReachedError`` at ``MAX_GOALS``, ``InvalidGoalError``."""
    data = _goal_data(body, today=today)
    try:
        return _insert("financial_goals", user_id, keys, data, limit=MAX_GOALS)
    except LimitReachedError as exc:
        raise LimitReachedError(f"You can keep up to {MAX_GOALS} goals. Delete one to add another.") from exc


def update_goal(
    user_id: str, goal_id: str, body: GoalInput, keys: ProfileKeys, *, today: date | None = None,
) -> dict[str, Any] | None:
    """Replace a goal with the user's edited version; None if the profile has no such goal."""
    previous = get_goal(user_id, goal_id, keys)
    if previous is None or not _replace("financial_goals", user_id, goal_id, keys, _goal_data(body, previous, today)):
        return None
    return get_goal(user_id, goal_id, keys)


def delete_goal(user_id: str, goal_id: str, keys: ProfileKeys | None = None) -> bool:
    """Delete a goal. Decisions linked to it keep their text and show the goal as deleted.

    With ``keys``, holdings assigned to the goal are released in the same transaction.
    """
    with connect_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute("DELETE FROM financial_goals WHERE user_id = ? AND id = ?", (user_id, goal_id))
        if cursor.rowcount and keys is not None:
            holdings = _read_allocations(connection, user_id, keys)
            for key, entry in list(holdings.items()):
                if entry["goals"].pop(goal_id, None) is not None and not entry["goals"] and not entry["generalWealthPct"]:
                    del holdings[key]
            _write_allocations(connection, user_id, keys, holdings)
    return cursor.rowcount > 0


# ── Holding allocations ────────────────────────────────────────────────────────

MAX_ALLOCATED_HOLDINGS: Final = 500


class GoalShare(BaseModel):
    """A whole-number share of one holding assigned to one goal."""

    model_config = ConfigDict(extra="forbid")

    goalId: str = Field(pattern=ID_PATTERN)
    pct: int = Field(ge=1, le=100)


class AllocationInput(BaseModel):
    """How one holding's value is split across goals and general wealth. One PUT replaces the whole split."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    holdingKey: str = Field(min_length=1, max_length=200)
    goals: list[GoalShare] = Field(default_factory=list, max_length=MAX_GOALS)
    generalWealthPct: int = Field(default=0, ge=0, le=100)

    @model_validator(mode="after")
    def _within_the_whole(self) -> AllocationInput:
        if len({share.goalId for share in self.goals}) != len(self.goals):
            raise ValueError("Each goal can appear once.")
        if sum(share.pct for share in self.goals) + self.generalWealthPct > 100:
            raise ValueError("A holding can be assigned at most 100% in total.")
        return self


def _read_allocations(connection: Any, user_id: str, keys: ProfileKeys) -> dict[str, dict[str, Any]]:
    row = connection.execute("SELECT data FROM goal_allocations WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        return {}
    return json.loads(keys.open(row["data"])).get("holdings", {})


def _write_allocations(connection: Any, user_id: str, keys: ProfileKeys, holdings: dict[str, dict[str, Any]]) -> None:
    if not holdings:
        connection.execute("DELETE FROM goal_allocations WHERE user_id = ?", (user_id,))
        return
    data = keys.seal(json.dumps({"source": SOURCE_USER, "holdings": holdings}, ensure_ascii=False))
    connection.execute(
        "INSERT INTO goal_allocations(user_id, updated_at, data) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET updated_at = excluded.updated_at, data = excluded.data",
        (user_id, users.now_ms(), data),
    )


def get_allocations(user_id: str, keys: ProfileKeys) -> dict[str, dict[str, Any]]:
    """Every holding's split, keyed by holding key: ``{name, assetType, broker, goals: {goalId: pct}, generalWealthPct}``."""
    with connect_db() as connection:
        return _read_allocations(connection, user_id, keys)


def set_allocation(
    user_id: str, keys: ProfileKeys, holding_key: str, entry: dict[str, Any] | None, *,
    max_holdings: int = MAX_ALLOCATED_HOLDINGS,
) -> dict[str, Any] | None:
    """Replace one holding's split, or remove it when ``entry`` is empty. Returns the stored entry (None if removed).

    The read-modify-write runs in one write transaction, so concurrent updates to
    different holdings both survive. Raises ``UnknownGoalError`` for a goal this
    profile does not have and ``LimitReachedError`` past ``max_holdings`` holdings.
    """
    shares = (entry or {}).get("goals", {})
    empty = not entry or not (shares or entry.get("generalWealthPct"))
    with connect_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if shares:
            marks = ",".join("?" * len(shares))
            known = {
                row["id"] for row in connection.execute(
                    f"SELECT id FROM financial_goals WHERE user_id = ? AND id IN ({marks})", (user_id, *shares),
                )
            }
            if known != set(shares):
                raise UnknownGoalError("One of those goals no longer exists. Reload the page and try again.")
        holdings = _read_allocations(connection, user_id, keys)
        if empty:
            holdings.pop(holding_key, None)
            stored = None
        else:
            if holding_key not in holdings and len(holdings) >= max_holdings:
                raise LimitReachedError(f"You can assign up to {max_holdings} holdings.")
            stored = {**entry, "updatedAt": users.now_ms()}
            holdings[holding_key] = stored
        _write_allocations(connection, user_id, keys, holdings)
    return stored


def clear_allocations(user_id: str) -> None:
    with connect_db() as connection:
        connection.execute("DELETE FROM goal_allocations WHERE user_id = ?", (user_id,))


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
    return {
        "goals": list_goals(user_id, keys), "decisions": list_decisions(user_id, keys),
        "goalAllocations": [{"holdingKey": key, **entry} for key, entry in get_allocations(user_id, keys).items()],
    }


def clear_saved_context(user_id: str) -> None:
    """Delete the profile's goals, decisions, holding allocations and profile fields (its preferences)."""
    with connect_db() as connection:
        connection.execute("DELETE FROM goal_allocations WHERE user_id = ?", (user_id,))
        connection.execute("DELETE FROM financial_goals WHERE user_id = ?", (user_id,))
        connection.execute("DELETE FROM financial_decisions WHERE user_id = ?", (user_id,))
        connection.execute("UPDATE users SET profile = '{}', updated_at = ? WHERE id = ?", (users.now_ms(), user_id))

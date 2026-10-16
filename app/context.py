"""Choose the saved context that bears on one chat request and render it for the agent prompt.

Selection uses fixed keyword rules over the current message and the user's two
previous turns, not a model or a vector store: the data is a handful of
structured records, and the rules are predictable and testable.

- Goals: the goals the message names (by name or type, such as "house" for a home
  goal); otherwise every goal when the message is about goals or saving for
  something; otherwise none.
- Preferences (profile fields): when goals are included, or the message is about
  risk, allocation, horizon or how to invest.
- Decisions: those linked to an included goal, those sharing a distinctive word
  with the message, or all of them when the message asks about decisions.

The rendered text keeps what the user confirmed apart from what code calculated
from it, with dates for both, and states that anything not listed is unknown.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, Final

from . import cas, goal_planning, memory, snapshots, users
from .agent import SavedContext
from .sealing import ProfileKeys

MAX_CONTEXT_CHARS: Final = 6000
MAX_DECISIONS_SHOWN: Final = 10
RECENT_USER_TURNS: Final = 2

PREFERENCE_LABELS: Final = {
    "riskAppetite": "risk appetite",
    "investmentHorizonYears": "investment horizon (years)",
    "primaryGoal": "primary goal",
    "monthlyInvestmentInr": "monthly investment budget (INR)",
    "ageBand": "age band",
}

_GOAL_TERMS: Final = re.compile(
    r"\b(goals?|targets?|on track|afford\w*|sav(?:e|es|ed|ing|ings)\s+(?:up\s+)?(?:for|towards?)|corpus|retir\w*"
    r"|plan(?:s|ning)?\s+(?:for|to)|milestones?|deadlines?|down ?payment|earmark\w*|shortfall)\b",
    re.IGNORECASE,
)
_PREFERENCE_TERMS: Final = re.compile(
    r"\b(risk\w*|allocat\w*|rebalanc\w*|horizon|suitab\w*|aggressive|conservative|diversif\w*|asset mix"
    r"|portfolio review|review my portfolio|how (?:should|do) i invest|where (?:should|do) i invest)\b",
    re.IGNORECASE,
)
_FOLLOW_UP: Final = re.compile(
    r"^\s*(?:and|also|what about|how about|what if|then|so|but|instead)\b|\b(?:it|that|this|those|them|instead)\b",
    re.IGNORECASE,
)
_DECISION_TERMS: Final = re.compile(r"\b(decid\w*|decisions?|remember|agreed|committed|what did i)\b", re.IGNORECASE)
# Mirrors GOAL_KINDS in frontend/js/finance.js, so "my house" finds a goal the app shows with the home icon.
_KIND_TERMS: Final = {
    "home": r"home|house|flat|apartment|property|down ?payment",
    "education": r"education|college|school|university|studies|child|kid|son|daughter",
    "retirement": r"retire\w*|pension",
    "car": r"car|bike|vehicle|scooter",
    "travel": r"travel|trip|vacation|holiday|tour",
    "wedding": r"wedding|marriage",
    "emergency": r"emergency|safety|rainy",
}
_STOPWORDS: Final = frozenset({
    "goal", "goals", "fund", "funds", "save", "saving", "savings", "plan", "planning", "about", "after", "before",
    "every", "month", "monthly", "money", "should", "would", "could", "there", "their", "which", "where", "invest",
    "investing", "investment", "amount", "target", "years", "today", "with", "from", "that", "this", "what", "much",
    "keep", "make", "will", "have", "into", "more", "less",
})


def _words(text: str, *, min_length: int = 4) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", text.lower()) if len(word) >= min_length and word not in _STOPWORDS}


def _goal_kind(goal: dict[str, Any]) -> str | None:
    if goal.get("kind") and goal["kind"] != "other":
        return goal["kind"]
    name = str(goal.get("name") or "")
    return next((kind for kind, terms in _KIND_TERMS.items() if re.search(rf"\b({terms})\b", name, re.IGNORECASE)), None)


def _mentions_goal(goal: dict[str, Any], text: str) -> bool:
    if _words(str(goal.get("name") or "")) & _words(text):
        return True
    kind = _goal_kind(goal)
    return bool(kind and re.search(rf"\b({_KIND_TERMS[kind]})\b", text, re.IGNORECASE))


def select_goals(goals: Sequence[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """The goals ``text`` names; else all of them when it is about goals; else none."""
    named = [goal for goal in goals if _mentions_goal(goal, text)]
    if named:
        return named
    return list(goals) if _GOAL_TERMS.search(text) else []


def select_decisions(
    decisions: Sequence[dict[str, Any]], text: str, goal_ids: set[str],
) -> list[dict[str, Any]]:
    """Decisions linked to ``goal_ids`` or sharing a distinctive word with ``text``; all when asked about decisions."""
    if _DECISION_TERMS.search(text):
        chosen = list(decisions)
    else:
        words = _words(text, min_length=5)
        chosen = [
            item for item in decisions
            if item.get("goalId") in goal_ids or _words(str(item.get("text") or ""), min_length=5) & words
        ]
    return sorted(chosen, key=lambda item: item.get("updatedAt") or 0, reverse=True)[:MAX_DECISIONS_SHOWN]


def wants_preferences(text: str, *, has_goals: bool) -> bool:
    return has_goals or bool(_PREFERENCE_TERMS.search(text))


def request_text(message: str, history: Sequence[dict[str, str]]) -> str:
    """The message plus the user's previous turns, so follow-ups such as "and the car one?" still match."""
    earlier = [item.get("content", "") for item in history if item.get("role") == "user"][-RECENT_USER_TURNS:]
    return "\n".join([*earlier, message])


def is_follow_up(message: str) -> bool:
    """Whether ``message`` leans on the previous turns ("and if I add 5,000 more?", "is that enough?")."""
    return bool(_FOLLOW_UP.search(message))


# ── Rendering ──────────────────────────────────────────────────────────────────

def _day(timestamp_ms: int | None) -> str:
    if not timestamp_ms:
        return "date not recorded"
    return datetime.fromtimestamp(timestamp_ms / 1000, snapshots.SNAPSHOT_TIMEZONE).date().isoformat()


def _preference_line(fields: dict[str, Any], updated_at: int | None) -> str | None:
    parts = [f"{label}: {fields[key]}" for key, label in PREFERENCE_LABELS.items() if fields.get(key) not in (None, "")]
    return f"- Profile, saved {_day(updated_at)}: " + "; ".join(parts) if parts else None


def _goal_line(goal: dict[str, Any]) -> str:
    currency = goal.get("currency") or "currency not given"
    assumption = (
        f"return assumption {goal['assumedReturnPct']:g}% a year" if goal.get("assumedReturnPct") is not None
        else "no return assumption given"
    )
    line = (
        f'- Goal "{goal.get("name")}": target {currency} {goal.get("targetAmount")} by {goal.get("targetDate")}; '
        f"earmarked savings {currency} {goal.get('savedAmount', 0)}; planned monthly contribution "
        f"{currency} {goal.get('monthlyContribution', 0)}; {assumption}; saved {_day(goal.get('updatedAt'))}"
    )
    if goal.get("notes"):
        line += f'; notes: "{goal["notes"]}"'
    return line


def _decision_line(decision: dict[str, Any], goal_names: dict[str, str]) -> str:
    when = decision.get("decidedOn") or _day(decision.get("createdAt"))
    linked = ""
    if decision.get("goalId"):
        linked = f' (goal "{goal_names[decision["goalId"]]}")' if decision["goalId"] in goal_names else " (a deleted goal)"
    return f'- Decision recorded {when}{linked}: "{decision.get("text")}"'


def _plan_lines(goal: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    name, currency = goal.get("name"), plan.get("currency")
    if plan["status"] == "insufficient_data":
        return [f'- "{name}": cannot be calculated; missing {", ".join(plan["missing"])}.']
    lines = [
        f'- "{name}": status {plan["status"].replace("_", " ")}; {plan["monthsRemaining"]} months left; '
        f'{currency} {plan["remainingGap"]} still to save today ({plan["progressPct"]:g}% saved).'
    ]
    for scenario in plan["scenarios"]:
        difference = scenario["difference"]
        outcome = f"ahead by {currency} {difference}" if difference >= 0 else f"short by {currency} {-difference}"
        if scenario["requiredMonthly"] is None:
            needed = "no months left to contribute"
        else:
            needed = f"needs {currency} {scenario['requiredMonthly']} a month"
            if scenario["additionalMonthly"]:
                needed += f" ({currency} {scenario['additionalMonthly']} more than planned)"
        lines.append(f"  - {scenario['label']}: projected {currency} {scenario['projectedAmount']}, {outcome}; {needed}.")
    lines.extend(f"  - Note: {warning}" for warning in plan["warnings"])
    return lines


def render(
    *, preferences: str | None, goals: Sequence[dict[str, Any]], decisions: Sequence[dict[str, Any]],
    goal_names: dict[str, str], today: date,
) -> str:
    """The prompt text: confirmed entries first, then the figures calculated from them."""
    confirmed = [line for line in [preferences] if line]
    confirmed += [_goal_line(goal) for goal in goals]
    confirmed += [_decision_line(item, goal_names) for item in decisions]
    sections = ["Confirmed by the user (entered in CA Mode; not checked against any account):", *confirmed]
    if goals:
        sections.append(
            f"Calculated in code on {today.isoformat()} from the goals above "
            "(hypothetical illustrations, not forecasts or promised returns):"
        )
        for goal in goals:
            sections.extend(_plan_lines(goal, goal["plan"]))
        sections.append("Assumptions behind these figures: " + " ".join(goal_planning.ASSUMPTIONS))
    sections.append(
        "Not saved: anything not listed here (for example income, expenses, age or dependants) is unknown; "
        "ask the user rather than assume it."
    )
    text = "\n".join(sections)
    if len(text) <= MAX_CONTEXT_CHARS:
        return text
    return text[: MAX_CONTEXT_CHARS - 40].rsplit("\n", 1)[0] + "\n(more saved context not shown)"


def with_plan(goal: dict[str, Any], today: date) -> dict[str, Any]:
    """The goal with its default scenarios; a goal whose stored values no longer validate gets a failed plan."""
    try:
        plan = goal_planning.plan_goal(goal, today)
    except goal_planning.PlanningError as exc:
        plan = {"status": "insufficient_data", "missing": [str(exc)], "scenarios": [], "warnings": [str(exc)]}
    return {**goal, "plan": plan}


def _select(
    user: dict[str, Any], text: str, all_goals: list[dict[str, Any]], decisions: list[dict[str, Any]], today: date,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    keys: ProfileKeys = user["keys"]
    goals = [with_plan(goal, today) for goal in select_goals(all_goals, text)]
    chosen = select_decisions(decisions, text, {goal["id"] for goal in goals})
    preferences = None
    if wants_preferences(text, has_goals=bool(goals)):
        preferences = _preference_line(users.profile_fields(user, keys), users.profile_updated_at(user, keys))
    return goals, chosen, preferences


def retrieve_context(
    user: dict[str, Any], message: str, history: Sequence[dict[str, str]], *, today: date | None = None,
) -> SavedContext | None:
    """Return the saved context relevant to ``message``, or None when nothing saved bears on it.

    The message is matched on its own first; the user's previous turns are added only
    when it matches nothing and reads as a follow-up, so an unrelated new question
    does not carry the last topic's goals along. ``user["keys"]`` must be unlocked
    (``main.history_gate`` checks this before chat).
    """
    keys: ProfileKeys = user["keys"]
    today = today or snapshots.local_now().date()
    all_goals = memory.list_goals(user["id"], keys)
    all_decisions = memory.list_decisions(user["id"], keys)
    if not (all_goals or all_decisions or users.profile_fields(user, keys)):
        return None
    goals, decisions, preferences = _select(user, message, all_goals, all_decisions, today)
    if not (goals or decisions or preferences) and history and is_follow_up(message):
        goals, decisions, preferences = _select(user, request_text(message, history), all_goals, all_decisions, today)
    if not (goals or decisions or preferences):
        return None
    items = [*(["Profile preferences"] if preferences else [])]
    items += [f"Goal: {goal['name']}" for goal in goals]
    items += [f"Decision: {str(item['text'])[:60]}" for item in decisions]
    body = render(
        preferences=preferences, goals=goals, decisions=decisions,
        goal_names={goal["id"]: goal["name"] for goal in all_goals}, today=today,
    )
    return SavedContext(text=body, items=tuple(items))


def observed_sources(user: dict[str, Any]) -> dict[str, Any]:
    """Where observed (not user-entered) portfolio data comes from, with its capture times, for ``GET /api/context``."""
    keys: ProfileKeys = user["keys"]
    snapshot = snapshots.latest_snapshot(user["id"], keys=keys)
    statements = cas.list_statements(user["id"], keys)
    newest = statements[0] if statements else None
    return {
        "latestSnapshot": (
            {"source": "INDmoney", "asOfDate": snapshot["asOfDate"], "capturedAt": snapshot["capturedAt"]}
            if snapshot else None
        ),
        "casStatements": {
            "count": len(statements),
            "latest": (
                {"source": newest["source"], "periodTo": newest["periodTo"], "uploadedAt": newest["uploadedAt"]}
                if newest else None
            ),
        },
    }

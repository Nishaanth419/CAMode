"""Check that the numbers in a portfolio answer come from the data the tools returned.

Every amount and percentage in the answer is matched against the numbers in this
run's tool results (and the user's own message), allowing for the rounding and the
lakh/crore units the answer uses. Numbers that match nothing are reported so the
answer can carry a visible warning instead of presenting an invented figure as fact.

This is a heuristic guard, not proof: a figure can match a different field by
coincidence, and plain counts, years and dates are not checked.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Final

_UNITS: Final = {
    "crore": Decimal(10_000_000), "crores": Decimal(10_000_000), "cr": Decimal(10_000_000),
    "lakh": Decimal(100_000), "lakhs": Decimal(100_000), "lac": Decimal(100_000), "lacs": Decimal(100_000),
    "l": Decimal(100_000), "k": Decimal(1_000), "thousand": Decimal(1_000),
    "million": Decimal(1_000_000), "mn": Decimal(1_000_000), "m": Decimal(1_000_000),
    "billion": Decimal(1_000_000_000), "bn": Decimal(1_000_000_000),
}
_NUMBER_RE: Final = re.compile(
    r"(?P<currency>₹|rs\.?\s?|inr\s?|\$|usd\s?)?"
    r"(?P<sign>[-−+])?"
    r"(?P<number>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s?(?P<unit>crores?|cr|lakhs?|lacs?|l|k|thousand|million|mn|m|billion|bn)\b)?"
    r"\s?(?P<percent>%|percent\b)?",
    re.IGNORECASE,
)
_YEAR_RE: Final = re.compile(r"^(?:19|20)\d\d$")
# Relative slack for figures the answer rounded without saying so ("about ₹12.5 lakh").
_RELATIVE_TOLERANCE: Final = Decimal("0.005")


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def _rounding_slack(number_text: str, multiplier: Decimal) -> Decimal:
    """Half a unit of the last digit the answer shows, in rupees (₹12.5 lakh -> ₹5,000)."""
    decimals = len(number_text.split(".")[1]) if "." in number_text else 0
    return Decimal(1).scaleb(-decimals) * multiplier / 2


def _known_numbers(texts: list[str]) -> set[Decimal]:
    """Every number in the tool results: JSON values, numeric strings, list lengths, and numbers in plain text."""
    found: set[Decimal] = set()

    def walk(node: Any) -> None:
        if isinstance(node, bool) or node is None:
            return
        if isinstance(node, (int, float)):
            found.add(Decimal(str(node)))
        elif isinstance(node, str):
            for match in _NUMBER_RE.finditer(node):
                value = _decimal(match.group("number"))
                if value is not None:
                    found.add(value * _UNITS.get((match.group("unit") or "").lower(), Decimal(1)))
                    found.add(value)
        elif isinstance(node, dict):
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            found.add(Decimal(len(node)))
            for item in node:
                walk(item)

    for text in texts:
        try:
            walk(json.loads(text))
        except (ValueError, TypeError):
            walk(text)
    return found


def _strip_unchecked(answer: str) -> str:
    """Remove text whose numbers are not portfolio figures: links and list numbering."""
    answer = re.sub(r"\]\([^)]*\)", "]", answer)
    return re.sub(r"(?m)^\s*\d+[.)]\s", "", answer)


def unverified_numbers(answer: str, sources: list[str]) -> list[str]:
    """Return the amounts and percentages in ``answer`` that match no number in ``sources``."""
    known = _known_numbers(sources)
    unverified: list[str] = []
    for match in _NUMBER_RE.finditer(_strip_unchecked(answer)):
        raw = match.group("number")
        value = _decimal(raw)
        if value is None:
            continue
        unit = (match.group("unit") or "").lower()
        is_money = bool(match.group("currency") or unit in _UNITS)
        is_percent = bool(match.group("percent"))
        if not (is_money or is_percent):
            # Plain numbers ("18 holdings", "top 5") are counts or labels, not figures to check.
            continue
        if not unit and not is_percent and _YEAR_RE.match(raw):
            continue
        multiplier = _UNITS.get(unit, Decimal(1))
        if match.group("sign") in ("-", "−"):
            value = -value
        target = value * multiplier
        slack = max(_rounding_slack(raw, multiplier), abs(target) * _RELATIVE_TOLERANCE)
        if not any(abs(abs(target) - abs(candidate)) <= slack for candidate in known):
            unverified.append(match.group(0).strip())
    return list(dict.fromkeys(unverified))


def warning_note(numbers: list[str]) -> str:
    shown = ", ".join(numbers[:6]) + (" and others" if len(numbers) > 6 else "")
    return (
        f"\n\n> ⚠️ I couldn't match these figures to your INDmoney data: {shown}. "
        "Treat them with caution and check them in INDmoney."
    )

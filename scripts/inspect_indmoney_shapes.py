"""Print the structure of INDmoney's portfolio responses with every value removed.

Calls networth_snapshot and networth_holdings for a few asset types using a
profile's saved INDmoney connection, then prints only field names and value types
(``number``, ``string``, ``null``...), so the layout can be shared without sharing
any holdings, amounts or names:

    set -a; source .env; set +a
    python -m scripts.inspect_indmoney_shapes

``--funds`` inspects the mutual fund tools instead: ``get_mf_by_category`` for two
categories, then ``get_mf_funds_details`` (baseline and ``fund_performance``) for the
first scheme listed. Date-like strings show their pattern (digits as ``9``), so the
as-of date format is visible without the date. ``--labels`` also prints the distinct
values of public scheme labels (category, risk, rating, benchmark, as-of keys), which
describe funds, not the user:

    python -m scripts.inspect_indmoney_shapes --funds --labels

``--brokers`` also prints, for each holdings response, how many rows each list
holds and the distinct ``broker`` and ``asset_type`` values with row counts (no
amounts or names), and the asset types ``networth_snapshot`` lists. ``--profile
<id>`` picks the profile whose connection is used (default: the first connected):

    python -m scripts.inspect_indmoney_shapes --brokers --profile <id>
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import Any

from app import users
from app.mcp_client import call_mcp_tool, fetch_mcp_session

MCP_URL = users.MCP_RESOURCE
CALLS = [
    ("networth_snapshot", {}),
    ("networth_holdings", {"asset_type": "IND_STOCK"}),
    ("networth_holdings", {"asset_type": "MF"}),
    ("networth_holdings", {"asset_type": "US_STOCK"}),
]
FUND_CATEGORY_CALL = ("get_mf_by_category", {"categories": ["flexi-cap", "liquid"], "size": 3})
FUND_DETAIL_INCLUDES = (None, ["fund_performance"])
# Keys whose values name a public property of a scheme; printed only with --labels.
LABEL_KEY = re.compile(r"categor|risk|rating|benchmark|as_?of|date|updated|source|period|plan|option", re.I)
ID_KEYS = ("fund_id", "scheme_id", "fundId", "schemeId", "ind_key", "id", "isin")
DATE_LIKE = re.compile(r"\d{1,4}[-/ ](?:\d{1,2}|[A-Za-z]{3,9})[-/ ]\d{1,4}(?:[T ][\d:.]+Z?)?")


def shape(value: Any, depth: int = 0) -> Any:
    """Replace every value with its type; lists show their length and the shape of their first item."""
    if depth > 8:
        return "…"
    if isinstance(value, dict):
        return {key: shape(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return {"list_of": len(value), "item": shape(value[0], depth + 1) if value else None}
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return "null"
    if isinstance(value, str) and re.fullmatch(r"-?[\d,]+(?:\.\d+)?%?", value.strip()):
        return "numeric string"
    if isinstance(value, str) and DATE_LIKE.search(value) and len(value) <= 40:
        return "date string " + re.sub(r"\d", "9", value)
    return "string"


def labels(value: Any, path: str = "", found: dict[str, set[str]] | None = None) -> dict[str, set[str]]:
    """Distinct short string values under keys that name a public scheme property (see ``LABEL_KEY``)."""
    found = {} if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            if LABEL_KEY.search(key) and isinstance(item, str | int | float) and len(str(item)) <= 80:
                found.setdefault(f"{path}.{key}", set()).add(str(item))
            labels(item, f"{path}.{key}", found)
    elif isinstance(value, list):
        for item in value:
            labels(item, f"{path}[]", found)
    return found


def broker_counts(value: Any) -> None:
    """Row counts per list, and per ``broker`` / ``asset_type`` label, in a holdings or snapshot response."""
    body = value.get("result") if isinstance(value, dict) and isinstance(value.get("result"), dict) else value
    if not isinstance(body, dict):
        return
    print("--- lists")
    for key, item in body.items():
        if isinstance(item, list):
            print(f"{key}: {len(item)} rows")
    for key in ("holdings", "investments"):
        rows = [row for row in body.get(key) or [] if isinstance(row, dict)]
        for label in ("broker", "asset_type"):
            counts: dict[str, int] = {}
            for row in rows:
                if label in row:
                    counts[str(row[label])] = counts.get(str(row[label]), 0) + 1
            if counts:
                print(f"{key}.{label}: {counts}")


def first_fund_id(value: Any) -> str | None:
    """The scheme id of the first row in a get_mf_by_category response, whatever the id key is called."""
    if isinstance(value, dict):
        for key in ID_KEYS:
            if isinstance(value.get(key), str | int) and str(value[key]):
                return str(value[key])
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return None
    for child in children:
        found = first_fund_id(child)
        if found:
            return found
    return None


def parsed(result: Any) -> Any:
    """A tool result as JSON, unwrapping a JSON string under ``result``; None when it is not JSON."""
    text = result if isinstance(result, str) else json.dumps(result)
    try:
        value = json.loads(text)
    except ValueError:
        return None
    if isinstance(value, dict) and isinstance(value.get("result"), str):
        try:
            return {**value, "result": json.loads(value["result"])}
        except ValueError:
            return value
    return value


def text_outline(text: str) -> str:
    """For a non-JSON response: the first lines with letters and digits masked."""
    masked = re.sub(r"\d", "9", re.sub(r"[A-Za-z]", "a", text))
    return "\n".join(masked.splitlines()[:15])


async def run(name: str, arguments: dict[str, Any], session: dict[str, Any], token: str, show_labels: bool) -> Any:
    """Call one tool and print its shape (and labels when asked); returns the parsed result."""
    print(f"\n=== {name} {json.dumps(arguments)}")
    try:
        result = await call_mcp_tool(
            MCP_URL, token, session["protocolVersion"], session["sessionId"], name, arguments,
        )
    except RuntimeError as exc:
        print("error:", str(exc)[:200])
        return None
    value = parsed(result)
    if value is None:
        text = result if isinstance(result, str) else json.dumps(result)
        print("(not JSON) outline:\n" + text_outline(text))
        return None
    print(json.dumps(shape(value), indent=2))
    if "--brokers" in sys.argv:
        broker_counts(value)
    if show_labels:
        print("--- labels")
        for path, values in sorted(labels(value).items()):
            print(f"{path}: {sorted(values)[:12]}")
    return value


async def main() -> int:
    profiles = users.users_with_indmoney()
    if "--profile" in sys.argv:
        wanted = sys.argv[sys.argv.index("--profile") + 1]
        profiles = [profile for profile in profiles if profile == wanted]
    if not profiles:
        print("No matching profile has a usable INDmoney connection.")
        return 1
    token = await users.user_access_token(profiles[0])
    if not token:
        print("The saved INDmoney session has expired; reconnect INDmoney in the app first.")
        return 1
    session = await fetch_mcp_session(MCP_URL, token)
    show_labels = "--labels" in sys.argv
    if "--funds" not in sys.argv:
        for name, arguments in CALLS:
            await run(name, arguments, session, token, show_labels)
        return 0
    listing = await run(*FUND_CATEGORY_CALL, session, token, show_labels)
    fund_id = first_fund_id(listing)
    if not fund_id:
        print("\nNo scheme id found in the category response; cannot call get_mf_funds_details.")
        return 1
    for includes in FUND_DETAIL_INCLUDES:
        arguments: dict[str, Any] = {"fund_ids": fund_id}
        if includes:
            arguments["includes"] = includes
        await run("get_mf_funds_details", arguments, session, token, show_labels)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

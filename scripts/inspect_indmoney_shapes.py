"""Print the structure of INDmoney's portfolio responses with every value removed.

Calls networth_snapshot and networth_holdings for a few asset types using a
profile's saved INDmoney connection, then prints only field names and value types
(``number``, ``string``, ``null``...), so the layout can be shared without sharing
any holdings, amounts or names:

    set -a; source .env; set +a
    python -m scripts.inspect_indmoney_shapes
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
    return "string"


def text_outline(text: str) -> str:
    """For a non-JSON response: the first lines with letters and digits masked."""
    masked = re.sub(r"\d", "9", re.sub(r"[A-Za-z]", "a", text))
    return "\n".join(masked.splitlines()[:15])


async def main() -> int:
    profiles = users.users_with_indmoney()
    if not profiles:
        print("No profile has a usable INDmoney connection.")
        return 1
    token = await users.user_access_token(profiles[0])
    if not token:
        print("The saved INDmoney session has expired; reconnect INDmoney in the app first.")
        return 1
    session = await fetch_mcp_session(MCP_URL, token)
    for name, arguments in CALLS:
        print(f"\n=== {name} {json.dumps(arguments)}")
        try:
            result = await call_mcp_tool(
                MCP_URL, token, session["protocolVersion"], session["sessionId"], name, arguments,
            )
        except RuntimeError as exc:
            print("error:", str(exc)[:200])
            continue
        text = result if isinstance(result, str) else json.dumps(result)
        try:
            print(json.dumps(shape(json.loads(text)), indent=2))
        except ValueError:
            print("(not JSON) outline:\n" + text_outline(text))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

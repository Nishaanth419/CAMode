"""Print the structure of a connected broker's holdings responses with every value removed.

Uses a profile's stored broker connection (the same token and session helpers the app uses) and
calls only that broker's holdings tools and its status tool. For each response it prints key names,
value types, list lengths and nesting; date-like strings show their pattern (digits as ``9``).
Names, codes, amounts, tokens and session ids are never printed, and neither is an error's text.

    set -a; source .env; set +a
    python -m scripts.inspect_broker_shapes [--profile <id>] [--broker <id>]

Default: the first profile with a broker connection, every broker it has connected. A login that
has ended prints "reconnect <broker> first" and the script exits with status 1.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from typing import Any

from app import brokers, mcp_client
from app.db import connect_db
KEY_RE = re.compile(r"^[a-z][A-Za-z0-9_]{0,40}$")
# A field name's parts (split on "_" and camelCase) carry at most two digits ("return_1yr", "t1_quantity");
# a key like "zd1234" is a code, not a field name, so it is masked.
_KEY_PART_RE = re.compile(r"[A-Z]?[a-z0-9]+")
_MONTH = (r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|july"
          r"|august|september|october|november|december)")
DATE_LIKE = re.compile(
    rf"\d{{1,4}}[-/ ](?:\d{{1,2}}|{_MONTH})[-/ ]\d{{1,4}}(?:[T ][\d:.]+(?:Z|[+-]\d\d:?\d\d)?)?", re.IGNORECASE,
)
MAX_KEYS = 40
MAX_DEPTH = 8


def is_field_name(key: Any) -> bool:
    text = str(key)
    if not KEY_RE.match(text):
        return False
    return all(sum(char.isdigit() for char in part) <= 2 for part in _KEY_PART_RE.findall(text))


def _keyed_by_data(value: dict[Any, Any]) -> bool:
    """A map from codes or symbols to records: three or more containers that all share one layout."""
    items = list(value.values())
    if len(items) < 3 or not all(isinstance(item, (dict, list)) for item in items):
        return False
    first = json.dumps(layout(items[0], MAX_DEPTH - 2), sort_keys=True)
    return all(json.dumps(layout(item, MAX_DEPTH - 2), sort_keys=True) == first for item in items[1:])


def layout(value: Any, depth: int = 0) -> Any:
    """The shape of a value: every value replaced by its type; keys that look like data are masked."""
    if depth > MAX_DEPTH:
        return "..."
    if isinstance(value, dict):
        if len(value) > MAX_KEYS or _keyed_by_data(value):
            return {"<keys>": {"count": len(value), "value": layout(next(iter(value.values())), depth + 1)}}
        masked = 0
        shaped: dict[str, Any] = {}
        for key, item in value.items():
            if is_field_name(key):
                shaped[str(key)] = layout(item, depth + 1)
            else:
                masked += 1
                shaped[f"<key {masked}>"] = layout(item, depth + 1)
        return shaped
    if isinstance(value, list):
        return {"list_of": len(value), "item": layout(value[0], depth + 1) if value else None}
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return "null"
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"-?[\d,]+(?:\.\d+)?%?", text):
            return "numeric string"
        if DATE_LIKE.fullmatch(text) and len(text) <= 40:
            return "date string " + re.sub(r"\d", "9", text)
        try:
            inner = json.loads(text)
        except ValueError:
            return "string"
        if isinstance(inner, (dict, list)):
            return {"json_string": layout(inner, depth + 1)}
    return "string"


def outline(text: str) -> str:
    """A non-JSON response: only its line count and the length of each of the first lines."""
    lines = text.splitlines()
    return f"(not JSON) {len(lines)} lines; lengths of the first 10: {[len(line) for line in lines[:10]]}"


def describe(result: Any) -> str:
    text = result if isinstance(result, str) else json.dumps(result)
    try:
        value = json.loads(text)
    except ValueError:
        return outline(text)
    return json.dumps(layout(value), indent=2)


def connected_brokers(user_id: str, wanted: str | None) -> list[str]:
    found = [item["broker"] for item in brokers.connections(user_id)]
    return [broker_id for broker_id in found if wanted in (None, broker_id)]


def first_profile() -> str | None:
    with connect_db() as connection:
        row = connection.execute("SELECT user_id FROM broker_connections ORDER BY user_id LIMIT 1").fetchone()
    return row["user_id"] if row else None


async def inspect_broker(user_id: str, broker: brokers.Broker) -> bool:
    """Print one broker's response layouts; False when its login has ended."""
    connection = brokers.get_connection(user_id, broker.id)
    print(f"\n##### {broker.name}")
    if not brokers.has_credentials(connection, broker) or connection["status"] != "connected":
        print(f"reconnect {broker.name} first")
        return False
    try:
        opened = await brokers._open(user_id, broker, connection)  # type: ignore[arg-type]
        targets = brokers.holdings_targets(broker, opened)
        calls = ([(broker.status_tool, {})] if broker.status_tool else []) + [(tool, {}) for tool in targets]
        for tool, arguments in calls:
            print(f"\n=== {tool}")
            result = await brokers._invoke(opened, broker, connection, tool, arguments)  # type: ignore[arg-type]
            print(describe(result))
    except Exception as exc:
        if isinstance(exc, (mcp_client.McpAuthError, mcp_client.McpToolError, mcp_client.McpHttpError)) and (
            brokers._needs_login(broker, exc) or getattr(exc, "status_code", None) in (400, 404, 410)
        ):
            print(f"reconnect {broker.name} first")
            return False
        print(f"error: {type(exc).__name__}")
        return False
    return True


async def run(profile: str | None, broker_id: str | None) -> int:
    user_id = profile or first_profile()
    if not user_id:
        print("No profile has a broker connection.")
        return 1
    if broker_id is not None and broker_id not in brokers.BROKERS:
        print("Unknown broker.")
        return 1
    ids = connected_brokers(user_id, broker_id)
    if not ids:
        print("No matching broker connection.")
        return 1
    ok = True
    for found in ids:
        ok = await inspect_broker(user_id, brokers.BROKERS[found]) and ok
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--profile")
    parser.add_argument("--broker")
    args = parser.parse_args(argv)
    return asyncio.run(run(args.profile, args.broker))


if __name__ == "__main__":
    sys.exit(main())

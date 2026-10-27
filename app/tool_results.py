"""One shape for every tool failure the agent reports back to the model and records in its events.

A tool call ends in one of these kinds:

- ``invalid_arguments``: the call was refused before running (arguments that are not a JSON
  object, a missing required argument, a value outside the tool's enum);
- ``rejected``: the tool ran and refused the request (INDmoney ``isError``, or a local tool that
  returned ``{"error": ...}``), so corrected arguments may work;
- ``unavailable``: INDmoney could not be reached or failed; calling again will not help now;
- ``auth``: INDmoney rejected the token; the request ends so the token can be refreshed;
- ``failed``: a local tool raised an exception.

The model sees ``{"error", "errorKind", "next_step"}``; the events keep the kind, so a trace,
the task contract and the evals can tell a correctable call from an outage.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final, Literal

ToolErrorKind = Literal["invalid_arguments", "rejected", "unavailable", "auth", "failed"]

MAX_ERROR_CHARS: Final = 600

NEXT_STEPS: Final[dict[str, str]] = {
    "invalid_arguments": "Call it again with arguments that match its input schema, or tell the user this data is unavailable.",
    "rejected": "Correct the arguments using the tool's input schema and call it again, or tell the user this data is unavailable.",
    "unavailable": (
        "Do not call it again for this answer. Tell the user which data is unavailable right now and answer from "
        "what the other tools returned."
    ),
    "failed": "Do not call it again for this answer; tell the user this data is unavailable right now.",
}


def error_payload(kind: ToolErrorKind, message: str, next_step: str | None = None) -> str:
    """The tool result the model reads for a failed call."""
    return json.dumps({
        "error": message[:MAX_ERROR_CHARS], "errorKind": kind, "next_step": next_step or NEXT_STEPS.get(kind, ""),
    })


def reported_error(result: str) -> str | None:
    """The message of a result that is a top-level ``{"error": ...}`` object (a local tool refusing the call)."""
    if not result.lstrip().startswith("{") or '"error"' not in result:
        return None
    try:
        parsed = json.loads(result)
    except ValueError:
        return None
    error = parsed.get("error") if isinstance(parsed, dict) else None
    return str(error) if error else None


def argument_problems(schema: Mapping[str, Any] | None, arguments: Mapping[str, Any]) -> list[str]:
    """Required arguments that are missing and values outside an ``enum``, per the tool's input schema.

    Deliberately narrow: types are not checked, because a server may accept "5" for an integer and
    refusing what it would accept would make answers worse. Only what the tool would reject anyway.
    """
    if not isinstance(schema, Mapping):
        return []
    problems = [f"{name} is required" for name in schema.get("required") or () if arguments.get(name) is None]
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        for name, value in arguments.items():
            spec = properties.get(name)
            choices = spec.get("enum") if isinstance(spec, Mapping) else None
            if isinstance(choices, list) and choices and value is not None and value not in choices:
                shown = ", ".join(str(choice) for choice in choices[:12])
                problems.append(f"{name} must be one of {shown} (got {str(value)[:40]!r})")
    return problems


# Fields of an error object a tool returns as an ordinary result, e.g. INDmoney's chain for an expiry it does not list:
# {"error": "validation_error", "message": "... Available: 20261027, ...", "tool": "..."}.
_ERROR_FIELDS: Final = frozenset({"error", "message", "tool", "error_type", "status_code", "errorKind", "next_step", "detail"})


def returned_error(result: str) -> str | None:
    """The message of a result made only of error fields, else ``None``.

    A response with data beside an empty or non-empty ``error`` (``get_mf_by_category`` carries one) is data.
    """
    if not result.lstrip().startswith("{") or '"error"' not in result:
        return None
    try:
        parsed = json.loads(result)
    except ValueError:
        return None
    if isinstance(parsed, dict) and parsed.get("error") and set(parsed) <= _ERROR_FIELDS:
        return str(parsed.get("message") or parsed["error"])[:MAX_ERROR_CHARS]
    return None

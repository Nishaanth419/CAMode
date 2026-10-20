"""Code mode: the model writes one JavaScript snippet per step, a Cloudflare Worker runs it, and each data call comes back here.

With ``TOOL_MODE=code`` the model is offered a single ``run_code`` tool instead of one function per tool.
Its snippet calls ``api.<tool>(args)``; the Worker (``worker/``) runs it in a fresh Dynamic Worker isolate
with no network access and sends every ``api`` call back over the run's WebSocket. ``execute()`` hands each
call to the agent's own tool dispatch (the same argument checks, error kinds and events as a direct call)
and returns the result to the snippet.

The connection is opened from this process, so the INDmoney token, the MCP session and the local data
never leave it: the Worker sees the code, the arguments it passes and the results it reads.

Protocol (JSON text frames):
  → {"type": "run", "code", "api": [names], "timeoutMs", "maxCalls"}
  ← {"type": "call", "id", "name", "args"}        → {"type": "result", "id", "ok", "value" | "error"}
  ← {"type": "done", "result", "error", "logs"}   → {"type": "abort", "reason"} when the answer must stop
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException

from . import tool_results
from .tool_results import ToolOutcome

logger = logging.getLogger("ca_mode.code_runner")

RUN_CODE_TOOL: Final = "run_code"
# Yielded by ``execute()`` (and by the agent's dispatch) as the last item; never forwarded as an agent event.
OUTCOME_EVENT: Final = "_outcome"

MAX_CODE_CHARS: Final = 8000
MAX_CALLS_PER_RUN: Final = 12
# Groq caps a tool result at about 6,000 characters.
MAX_RESULT_CHARS: Final = 5500
MAX_LOG_LINES: Final = 20
MAX_FRAME_BYTES: Final = 4 * 1024 * 1024

CodeStatus = Literal["completed", "failed", "unavailable", "stopped"]
Event = tuple[str, dict[str, Any]]
Dispatch = Callable[[str, Any], AsyncIterator[Event]]


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    url: str
    secret: str
    timeout_seconds: int


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def runner_config() -> RunnerConfig | None:
    """The Worker to run code on, or ``None`` when code mode is off or not configured."""
    if os.getenv("TOOL_MODE", "direct").strip().lower() != "code":
        return None
    url = os.getenv("CODE_RUNNER_URL", "").strip()
    secret = os.getenv("CODE_RUNNER_SECRET", "").strip()
    if not url or not secret:
        logger.warning("code_mode_not_configured: set CODE_RUNNER_URL and CODE_RUNNER_SECRET")
        return None
    if url.startswith("https://"):
        url = "wss://" + url.removeprefix("https://")
    elif url.startswith("http://"):
        url = "ws://" + url.removeprefix("http://")
    return RunnerConfig(url=url, secret=secret, timeout_seconds=_positive_int("CODE_RUNNER_TIMEOUT_SECONDS", 60))


# ── What the model sees ─────────────────────────────────────────────────────────

def _ts_type(schema: Any, depth: int = 0) -> str:
    """A JSON Schema as a compact TypeScript type (enough for the model to write a call, not a validator)."""
    if not isinstance(schema, Mapping) or depth > 3:
        return "any"
    choices = schema.get("enum")
    if isinstance(choices, list) and choices:
        return " | ".join(json.dumps(choice) for choice in choices[:16])
    kind = schema.get("type")
    if isinstance(kind, list):
        kinds = [item for item in kind if item != "null"]
        kind = kinds[0] if len(kinds) == 1 else None
    if kind == "object" or (kind is None and isinstance(schema.get("properties"), Mapping)):
        properties = schema.get("properties")
        if not isinstance(properties, Mapping) or not properties:
            return "Record<string, any>"
        required = set(schema.get("required") or ())
        fields = "; ".join(
            f"{name}{'' if name in required else '?'}: {_ts_type(spec, depth + 1)}" for name, spec in properties.items()
        )
        return "{ " + fields + " }"
    if kind == "array":
        item = _ts_type(schema.get("items"), depth + 1)
        return f"({item})[]" if "|" in item else f"{item}[]"
    return {"string": "string", "integer": "number", "number": "number", "boolean": "boolean"}.get(str(kind), "any")


def _summary(description: Any, limit: int = 500) -> str:
    """One-line text for a doc comment; 500 characters, like the direct tools on Groq."""
    text = " ".join(str(description or "").split()).replace("*/", "* /")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _arguments_type(schema: Any) -> str:
    """The top-level arguments object, one property per line with its description, which the model needs for
    formats and units (direct tool calls see the same descriptions in the JSON schema)."""
    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    if not isinstance(properties, Mapping) or not properties:
        return _ts_type(schema)
    required = set(schema.get("required") or ())
    lines = ["{"]
    for name, spec in properties.items():
        description = _summary(spec.get("description"), 200) if isinstance(spec, Mapping) else ""
        if description:
            lines.append(f"    /** {description} */")
        lines.append(f"    {name}{'' if name in required else '?'}: {_ts_type(spec, 1)};")
    lines.append("  }")
    return "\n".join(lines)


def api_declarations(tools: Sequence[Mapping[str, Any]]) -> str:
    """``api.<tool>(args)`` signatures for the system prompt, from MCP-style ``{name, description, inputSchema}``."""
    lines = ["declare const api: {"]
    for tool in tools:
        description = _summary(tool.get("description"))
        if description:
            lines.append(f"  /** {description} */")
        schema = tool.get("inputSchema")
        has_required = isinstance(schema, Mapping) and bool(schema.get("required"))
        lines.append(f"  {tool['name']}(args{'' if has_required else '?'}: {_arguments_type(schema)}): Promise<any>;")
    lines.append("};")
    return "\n".join(lines)


CODE_MODE_RULES: Final = (
    "You read data by calling the run_code tool with a short JavaScript snippet (an async function body; use await, "
    "and `return` the value you want to see). It runs in an isolated sandbox with no network access: the only way "
    "to reach data is the `api` object declared below, and each `api` call takes one arguments object. Where these "
    "instructions say to call a tool, call it as api.<tool>(args) inside run_code. Prefer one snippet that makes "
    "every call the question needs and returns only the fields you will use (filter, sort and pick in code), "
    "because large results are cut off. A failed call throws an Error whose message says what went wrong and what "
    "to do next; catch it when the other calls can still answer the question. This code is for reading data only: "
    "never show it, mention it or describe it in your answer, and the rule against writing code for the user still "
    "applies. Quote figures exactly as the snippet returned them."
)


def system_prompt_section(tools: Sequence[Mapping[str, Any]]) -> str:
    return CODE_MODE_RULES + "\n\n```typescript\n" + api_declarations(tools) + "\n```"


def model_tool() -> dict[str, Any]:
    """The one function tool offered in code mode."""
    return {"type": "function", "function": {
        "name": RUN_CODE_TOOL,
        "description": (
            "Run JavaScript in an isolated sandbox to read the user's data through the `api` object described in "
            "the system prompt. Returns {result, logs} or {error, errorKind, next_step}."
        ),
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "Async function body; `return` the result."}},
            "required": ["code"],
        },
    }}


def code_argument(raw: Any) -> str | None:
    """The ``code`` argument of a run_code call, or ``None`` when it is missing or not text."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return None
    code = raw.get("code") if isinstance(raw, dict) else None
    return code if isinstance(code, str) and code.strip() else None


# ── Running a snippet ──────────────────────────────────────────────────────────

@dataclass(slots=True)
class CodeOutcome:
    """How one snippet ended. ``content`` is the tool result the model reads."""

    status: CodeStatus
    content: str
    result_preview: str = ""
    error: str | None = None
    logs: list[str] = field(default_factory=list)
    calls: int = 0
    # Set when no connection to the Worker could be made, so no call ran and the answer may fall back to direct tools.
    unreachable: bool = False
    fatal: dict[str, Any] | None = None


def _sandbox_value(content: str) -> Any:
    """A tool result as the snippet receives it: parsed when it is JSON, otherwise the text itself."""
    stripped = content.strip()
    if stripped[:1] in ("{", "["):
        try:
            return json.loads(stripped)
        except ValueError:
            pass
    return content


def _call_error(outcome: ToolOutcome) -> str:
    """The message a failed call throws inside the snippet, with what to do next."""
    next_step = tool_results.NEXT_STEPS.get(outcome.error_kind or "", "")
    message = f"{outcome.error_kind}: {outcome.error or 'the call failed'}"
    return f"{message}. {next_step}" if next_step else message


def _bounded_json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= MAX_RESULT_CHARS:
        return text
    note = " …[cut off: return only the fields you need]"
    return text[: MAX_RESULT_CHARS - len(note)] + note


def _logs(raw: Any) -> list[str]:
    return [str(line)[:300] for line in raw[:MAX_LOG_LINES]] if isinstance(raw, list) else []


def _failed(status: CodeStatus, kind: tool_results.ToolErrorKind, message: str, *, calls: int = 0,
            logs: list[str] | None = None, next_step: str | None = None, unreachable: bool = False,
            fatal: dict[str, Any] | None = None) -> CodeOutcome:
    payload = json.loads(tool_results.error_payload(kind, message, next_step))
    if logs:
        payload["logs"] = logs
    return CodeOutcome(status, json.dumps(payload, ensure_ascii=False), error=message, logs=logs or [], calls=calls,
                       unreachable=unreachable, fatal=fatal)


UNREACHABLE_NEXT_STEP: Final = (
    "Do not call run_code again for this answer; tell the user their data could not be read right now."
)


async def execute(code: str, api: Sequence[str], dispatch: Dispatch, config: RunnerConfig) -> AsyncIterator[Event]:
    """Run ``code`` on the Worker, dispatching each ``api`` call it makes.

    Yields the dispatch's own events (``tool_call`` / ``tool_result``) as the calls happen, then
    ``(OUTCOME_EVENT, {"outcome": CodeOutcome})``. Never raises for a Worker or network failure.
    """
    allowed = set(api)
    calls = 0
    connected = False
    fatal: dict[str, Any] | None = None
    deadline = config.timeout_seconds + 15
    try:
        async with asyncio.timeout(deadline):
            async with connect(
                config.url, additional_headers={"Authorization": f"Bearer {config.secret}"},
                open_timeout=15, max_size=MAX_FRAME_BYTES, ping_interval=20,
            ) as socket:
                connected = True
                await socket.send(json.dumps({
                    "type": "run", "code": code, "api": sorted(allowed),
                    "timeoutMs": config.timeout_seconds * 1000, "maxCalls": MAX_CALLS_PER_RUN,
                }))
                async for frame in socket:
                    message = json.loads(frame)
                    kind = message.get("type") if isinstance(message, dict) else None
                    if kind == "call":
                        call_id, name = message.get("id"), str(message.get("name", ""))
                        calls += 1
                        outcome: ToolOutcome | None = None
                        if name not in allowed:
                            outcome = tool_results.failure("rejected", f"api.{name} does not exist.",
                                                           "Use only the api functions declared in the system prompt.")
                        elif calls > MAX_CALLS_PER_RUN:
                            outcome = tool_results.failure("rejected", f"More than {MAX_CALLS_PER_RUN} api calls in one snippet.")
                        else:
                            async for event in dispatch(name, message.get("args")):
                                if event[0] == OUTCOME_EVENT:
                                    outcome = event[1]["outcome"]
                                else:
                                    yield event
                        assert outcome is not None
                        if outcome.fatal:
                            fatal = outcome.fatal
                            await socket.send(json.dumps({"type": "abort", "reason": "stopped"}))
                            break
                        reply: dict[str, Any] = {"type": "result", "id": call_id}
                        if outcome.error_kind:
                            reply.update(ok=False, error=_call_error(outcome))
                        else:
                            reply.update(ok=True, value=_sandbox_value(outcome.content))
                        await socket.send(json.dumps(reply, ensure_ascii=False, default=str))
                    elif kind == "done":
                        logs = _logs(message.get("logs"))
                        error = message.get("error")
                        if error:
                            result = _failed("failed", "rejected", f"The code failed: {str(error)[:500]}", calls=calls,
                                             logs=logs, next_step="Fix the code and call run_code again, or answer from what you have.")
                        else:
                            content = _bounded_json({"result": message.get("result"), **({"logs": logs} if logs else {})})
                            result = CodeOutcome("completed", content, result_preview=content[:1000], logs=logs, calls=calls)
                        yield OUTCOME_EVENT, {"outcome": result}
                        return
                    elif kind == "error":
                        yield OUTCOME_EVENT, {"outcome": _failed(
                            "failed", "rejected", f"The code runner refused the snippet: {str(message.get('error'))[:300]}",
                            calls=calls)}
                        return
    except InvalidStatus as exc:
        status = exc.response.status_code
        logger.warning("code_runner_refused", extra={"status": status})
        yield OUTCOME_EVENT, {"outcome": _failed(
            "unavailable", "unavailable", f"The code runner refused the connection (HTTP {status}).",
            next_step=UNREACHABLE_NEXT_STEP, unreachable=True)}
        return
    except TimeoutError:
        yield OUTCOME_EVENT, {"outcome": _failed(
            "failed", "unavailable", f"The code did not finish within {config.timeout_seconds} seconds.", calls=calls,
            next_step="Call run_code again with fewer api calls, or answer from what you have.", unreachable=not connected)}
        return
    except (OSError, WebSocketException, ValueError) as exc:
        logger.warning("code_runner_unavailable: %s", type(exc).__name__)
        yield OUTCOME_EVENT, {"outcome": _failed(
            "unavailable", "unavailable", "The code runner could not be reached.", calls=calls,
            next_step=UNREACHABLE_NEXT_STEP, unreachable=not connected)}
        return
    if fatal:
        yield OUTCOME_EVENT, {"outcome": _failed("stopped", "auth", "INDmoney rejected the session.", calls=calls, fatal=fatal)}
        return
    yield OUTCOME_EVENT, {"outcome": _failed(
        "failed", "unavailable", "The code runner closed the connection before the code finished.", calls=calls,
        next_step=UNREACHABLE_NEXT_STEP)}

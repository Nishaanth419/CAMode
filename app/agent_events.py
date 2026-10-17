"""The recorded events of one agent run, and the redaction applied to anything shown or saved.

``agent.stream_agent`` yields ``(event, payload)`` tuples; ``RunRecorder`` turns them into the
run's event log: numbered, timed from the start of the run, with tool arguments, results and
errors redacted and size-capped, and the answer itself left out (the chat already holds it).

Event kinds (camelCase keys, ``seq`` and ``atMs`` on every event):

- ``route``: ``category``, ``source``, ``model``, ``durationMs``
- ``context``: ``items`` (saved context added to the prompt)
- ``contract``: the task contract (``tasks.TaskContract.describe()``)
- ``model_call`` / ``model_result``: ``attempt``; the result adds ``status``, ``httpStatus``,
  ``finishReason``, ``toolCalls``, ``promptTokens``, ``completionTokens``, ``durationMs``
- ``tool_call`` / ``tool_result``: ``name``, ``source``, ``arguments``; the result adds ``errorKind``
  (``tool_results.ToolErrorKind``) and ``error``, or ``resultPreview``, plus ``durationMs``
- ``retry``: ``reason`` (``no_account_tool``, ``goal_calculator``, ``invalid_tool_call``,
  ``rerun_with_live_tools``) and ``detail``
- ``validation``: ``check`` (``code_output``, ``numbers``, ``length``, ``task_contract``), ``outcome``
  and ``detail``
- ``answer``: ``chars``
- ``error``: ``message``, ``code``
- ``termination``: ``reason`` (``answered``, ``refused``, ``failed``, ``auth_failed``) and ``code``
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from typing import Any, Final

# Trace text is shown to the user and saved, so secrets and identifiers a tool argument or result
# might carry are masked first. Keys are matched on their last word(s).
_SENSITIVE_KEY_WORDS: tuple[str, ...] = (
    "token", "secret", "password", "passwd", "pin", "pan", "otp", "cookie", "authorization",
    "api_key", "apikey", "private_key", "secret_key", "session_id", "client_secret",
)
_JSON_PAIR = re.compile(r'"([A-Za-z0-9_\-]{1,60})"(\s*:\s*)("(?:[^"\\]|\\.)*(?:"|$)|[^,}\]\s]+)')
_FREE_TEXT_SECRETS = (
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=\-]+", re.IGNORECASE), "Bearer [redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), "[redacted]"),
    (re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"), "[redacted PAN]"),
)
REDACTED = "[redacted]"

MAX_ARGUMENT_CHARS: Final = 600
MAX_PREVIEW_CHARS: Final = 1000
MAX_ERROR_CHARS: Final = 300


def _is_sensitive_key(key: str) -> bool:
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).lower().replace("-", "_")
    return any(words == word or words.endswith("_" + word) for word in _SENSITIVE_KEY_WORDS)


def redact_trace_text(text: str) -> str:
    """Mask credentials and identifiers in trace text (JSON or a JSON prefix cut at any point)."""
    def mask(match: re.Match[str]) -> str:
        if not _is_sensitive_key(match.group(1)):
            return match.group(0)
        return f'"{match.group(1)}"{match.group(2)}"{REDACTED}"'

    text = _JSON_PAIR.sub(mask, text)
    for pattern, replacement in _FREE_TEXT_SECRETS:
        text = pattern.sub(replacement, text)
    return text


def arguments_text(arguments: Any) -> str:
    """Tool arguments as redacted text; a string is the model's own text when it was not a JSON object."""
    text = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    return redact_trace_text(text[:MAX_ARGUMENT_CHARS])


def _clean(text: Any, limit: int) -> str:
    return redact_trace_text(str(text)[:limit])


class RunRecorder:
    """Builds a run's event log from ``stream_agent`` events."""

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock
        self._started = clock()
        self.events: list[dict[str, Any]] = []
        self._open_calls: dict[str, list[dict[str, Any]]] = {}
        self._model_started: dict[int, int] = {}

    def __repr__(self) -> str:
        return f"RunRecorder(events={len(self.events)})"

    def _now_ms(self) -> int:
        return round((self._clock() - self._started) * 1000)

    def _append(self, kind: str, **data: Any) -> dict[str, Any]:
        event = {"seq": len(self.events) + 1, "atMs": self._now_ms(), "kind": kind,
                 **{key: value for key, value in data.items() if value is not None}}
        self.events.append(event)
        return event

    def add(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Record one stream event; returns the recorded event (``None`` for ``done``)."""
        handler = getattr(self, f"_on_{event_type}", None)
        return handler(payload) if handler else None

    def _on_route(self, p: dict[str, Any]) -> dict[str, Any]:
        return self._append("route", category=p.get("category"), source=p.get("source"), model=p.get("model"),
                            durationMs=p.get("duration_ms"))

    def _on_context(self, p: dict[str, Any]) -> dict[str, Any]:
        return self._append("context", items=[_clean(item, 120) for item in p.get("items", [])[:12]])

    def _on_contract(self, p: dict[str, Any]) -> dict[str, Any]:
        contract = p["contract"]
        return self._append("contract", **contract.describe())

    def _on_model_call(self, p: dict[str, Any]) -> dict[str, Any]:
        attempt = int(p.get("attempt", 1))
        self._model_started[attempt] = self._now_ms()
        return self._append("model_call", provider=p.get("provider"), model=p.get("model"), attempt=attempt)

    def _on_model_result(self, p: dict[str, Any]) -> dict[str, Any]:
        attempt = int(p.get("attempt", 1))
        started = self._model_started.pop(attempt, None)
        return self._append(
            "model_result", attempt=attempt, status=p.get("status"), httpStatus=p.get("http_status"),
            finishReason=p.get("finish_reason"), toolCalls=p.get("tool_calls"), promptTokens=p.get("prompt_tokens"),
            completionTokens=p.get("completion_tokens"),
            durationMs=self._now_ms() - started if started is not None else None,
        )

    def _on_tool_call(self, p: dict[str, Any]) -> dict[str, Any]:
        event = self._append("tool_call", name=str(p.get("name", ""))[:100], source=p.get("source"),
                             arguments=arguments_text(p.get("arguments", {})))
        self._open_calls.setdefault(event["name"], []).append(event)
        return event

    def _on_tool_result(self, p: dict[str, Any]) -> dict[str, Any]:
        name = str(p.get("name", ""))[:100]
        opened = self._open_calls.get(name) or []
        call = opened.pop(0) if opened else None
        failed = bool(p.get("error"))
        return self._append(
            "tool_result", name=name, source=call.get("source") if call else None,
            errorKind=p.get("error_kind") or ("failed" if failed else None),
            error=_clean(p["error"], MAX_ERROR_CHARS) if failed else None,
            resultPreview=_clean(p["result"], MAX_PREVIEW_CHARS) if p.get("result") else None,
            investigationId=p.get("investigation_id"),
            durationMs=self._now_ms() - call["atMs"] if call else None,
        )

    def _on_retry(self, p: dict[str, Any]) -> dict[str, Any]:
        return self._append("retry", reason=p.get("reason"), detail=_clean(p.get("detail", ""), 200) or None)

    def _on_validation(self, p: dict[str, Any]) -> dict[str, Any]:
        return self._append("validation", check=p.get("check"), outcome=p.get("outcome"),
                            detail=_clean(p.get("detail", ""), 300) or None)

    def _on_content(self, p: dict[str, Any]) -> dict[str, Any]:
        return self._append("answer", chars=len(p.get("text", "")))

    def _on_error(self, p: dict[str, Any]) -> dict[str, Any]:
        return self._append("error", message=_clean(p.get("message") or "", MAX_ERROR_CHARS), code=p.get("code"))

    def terminate(self, reason: str, code: str | None = None) -> dict[str, Any]:
        """Record how the run ended."""
        return self._append("termination", reason=reason, code=code)


def summarize(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Counts and token totals for a run's events (for lists and the Activity page)."""
    results = [event for event in events if event.get("kind") == "model_result"]
    tools = [event for event in events if event.get("kind") == "tool_result"]
    termination = next((event for event in reversed(events) if event.get("kind") == "termination"), {})

    def total(key: str) -> int | None:
        values = [event[key] for event in results if isinstance(event.get(key), int)]
        return sum(values) if values else None

    return {
        "modelCalls": sum(1 for event in events if event.get("kind") == "model_call"),
        "toolCalls": len(tools),
        "failedToolCalls": sum(1 for event in tools if event.get("errorKind")),
        "retries": sum(1 for event in events if event.get("kind") == "retry"),
        "promptTokens": total("promptTokens"), "completionTokens": total("completionTokens"),
        "durationMs": events[-1]["atMs"] if events else 0,
        "termination": termination.get("reason"),
    }

"""Progress events for a streamed chat answer: map agent events to safe dicts and frame server-sent events."""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import Any, Final

from fastapi import Response

from . import analysis_tools, brokers

KEEPALIVE_SECONDS: Final = 15
KEEPALIVE: Final = b": keepalive\n\n"

_IDENTIFIER: Final = re.compile(r"^[a-z0-9_]{1,40}$")
_BROKER_TOOLS: Final = frozenset({"get_broker_holdings", "call_broker_tool"})


def sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode("utf-8")


def _identifier(value: Any) -> str | None:
    """A short fixed identifier (never free text), else None."""
    return value if isinstance(value, str) and _IDENTIFIER.match(value) else None


def _broker_id(arguments: Any) -> str | None:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return None
    broker = arguments.get("broker") if isinstance(arguments, dict) else None
    return broker if isinstance(broker, str) and broker in brokers.BROKERS else None


class ProgressMapper:
    """Turns agent events into progress dicts holding only seq, kind, name, source and status."""

    def __init__(self) -> None:
        self._seq = 0
        self._allowed: set[str] = set()
        self._last_call: dict[str, Any] = {}
        self._calls: dict[str, dict[str, Any]] = {}

    def _next(self, kind: str, **fields: Any) -> dict[str, Any]:
        item = {"seq": self._seq, "kind": kind, **{k: v for k, v in fields.items() if v is not None}}
        self._seq += 1
        return item

    def start(self) -> dict[str, Any]:
        return self._next("start")

    def waiting(self) -> dict[str, Any]:
        return self._next("waiting")

    def _remember(self, contract: Any) -> None:
        self._allowed.update(getattr(contract, "allowed_tools", ()) or ())
        for part in getattr(contract, "parts", ()) or ():
            self._remember(part)

    def _source(self, name: str, data: dict[str, Any]) -> str:
        if name in _BROKER_TOOLS:
            broker = _broker_id(data.get("arguments"))
            return f"broker:{broker}" if broker else "broker"
        if data.get("source") == "indmoney":
            return "indmoney"
        if name in analysis_tools.ANALYSIS_TOOL_NAMES:
            return "analysis"
        return "saved"

    def map(self, event: str, data: dict[str, Any]) -> dict[str, Any] | None:
        if event == "contract":
            self._remember(data.get("contract"))
            return None
        if event == "route":
            return self._next("route", name=_identifier(data.get("category")))
        if event in ("context", "model_call"):
            return self._next(event)
        if event == "retry":
            return self._next("retry", name=_identifier(data.get("reason")))
        if event == "validation":
            return self._next("validation", name=_identifier(data.get("check")))
        if event == "plan":
            return self._next("plan", name=_identifier(data.get("stage")))
        if event == "tool_call":
            name = str(data.get("name", ""))
            source = self._source(name, data)
            self._last_call = {"name": name, "source": source}
            # Results can arrive in another order than calls, so remember each by its step (planned runs) or name.
            key = data.get("step") if isinstance(data.get("step"), str) else name
            self._calls[key] = self._last_call
            return self._next("tool_call", name=name if name in self._allowed else None, source=source)
        if event == "tool_result":
            key = data.get("step") if isinstance(data.get("step"), str) else data.get("name")
            call = self._calls.get(key) if isinstance(key, str) else None
            if call is None and not isinstance(key, str):
                call = self._last_call
            call = call or {}
            name = call.get("name") or (data.get("name") if isinstance(data.get("name"), str) else "")
            source = call.get("source") or self._source(name, {})
            failed = any(data.get(key) for key in ("error", "error_kind", "errorKind"))
            return self._next("tool_result", name=name if name in self._allowed else None, source=source,
                              status="failed" if failed else "ok")
        return None


def parse_trace_header(value: str | None) -> list[dict[str, Any]]:
    """Inverse of ``main.trace_header()``: the compact trace, or [] when absent or unreadable."""
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


def _int_header(response: Response, name: str) -> int | None:
    try:
        return int(response.headers[name])
    except (KeyError, ValueError):
        return None


def response_event(response: Response) -> tuple[str, dict[str, Any]]:
    """The one final event for the chat route's finished response."""
    body = bytes(response.body).decode("utf-8", errors="replace")
    trace = parse_trace_header(response.headers.get("X-Agent-Trace"))
    if response.status_code == 200 and "json" not in response.headers.get("content-type", ""):
        data: dict[str, Any] = {"text": body, "trace": trace}
        if run_id := response.headers.get("X-Agent-Run"):
            data["runId"] = run_id
        for key, header in (("questionsRemaining", "X-Questions-Remaining"), ("creditsRemaining", "X-Credits-Remaining"),
                            ("creditsCharged", "X-Credits-Charged")):
            if (number := _int_header(response, header)) is not None:
                data[key] = number
        return "answer", data
    try:
        details = json.loads(body)
    except ValueError:
        details = {}
    return "error", {"status": response.status_code, "trace": trace, **(details if isinstance(details, dict) else {})}


async def events(
    task: asyncio.Task[Response], queue: asyncio.Queue[dict[str, Any]], first: dict[str, Any], failure: dict[str, Any],
) -> AsyncIterator[bytes]:
    """The stream for one answer: ``first``, queued progress, keepalives, then one ``answer`` or ``error`` event.

    Closing or cancelling this generator (the browser went away) never cancels ``task``: it finishes and saves.
    """
    yield sse("progress", first)
    while True:
        while not queue.empty():
            yield sse("progress", queue.get_nowait())
        if task.done():
            break
        getter = asyncio.ensure_future(queue.get())
        try:
            # asyncio.wait never cancels what it waits on, so a cancelled stream leaves the task running.
            done, _ = await asyncio.wait({getter, task}, timeout=KEEPALIVE_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not getter.done():
                getter.cancel()
        if getter.done() and not getter.cancelled():
            yield sse("progress", getter.result())
        elif not done:
            yield KEEPALIVE
    if task.cancelled() or task.exception() is not None:
        yield sse("error", failure)
    else:
        yield sse(*response_event(task.result()))

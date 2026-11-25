"""Server-sent events for ``POST /api/chat`` when the browser asks for ``text/event-stream``.

The answer text streams as the model writes it (``delta``), then the checked, saved answer replaces the draft
(``answer``). A draft can be void (a retry, tool calls after preface text): ``reset`` clears it. A failure after
the stream started is an ``error`` event carrying the status and body the plain route would have returned.

Draft text is answer text the person sees anyway, but it is never logged, and no tool arguments or results
are ever put in an event. The plain route is unchanged when the header is absent.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Final

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

logger = logging.getLogger("ca_mode.chat_stream")

KEEPALIVE_SECONDS: float = 15
HEADERS: Final = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
GENERIC_ERROR: Final = "Something went wrong while answering. Try again."


def wants_stream(request: Request) -> bool:
    return "text/event-stream" in request.headers.get("accept", "").lower()


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=True, separators=(',', ':'))}\n\n"


def _int_header(response: Response, name: str) -> int | None:
    try:
        return int(response.headers[name])
    except (KeyError, ValueError):
        return None


def final_event(outcome: Response | BaseException | None) -> tuple[str, dict[str, Any]]:
    """The ``answer`` or ``error`` event for what the chat run ended with."""
    if isinstance(outcome, PlainTextResponse):
        data: dict[str, Any] = {"text": bytes(outcome.body).decode("utf-8", errors="replace"),
                                "trace": outcome.headers.get("X-Agent-Trace", "[]")}
        if run_id := outcome.headers.get("X-Agent-Run"):
            data["runId"] = run_id
        for key, header in (("questionsRemaining", "X-Questions-Remaining"), ("creditsRemaining", "X-Credits-Remaining"),
                            ("creditsCharged", "X-Credits-Charged")):
            if (value := _int_header(outcome, header)) is not None:
                data[key] = value
        return "answer", data
    if isinstance(outcome, JSONResponse):
        try:
            body = json.loads(bytes(outcome.body))
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        return "error", {"status": outcome.status_code, **body, "trace": outcome.headers.get("X-Agent-Trace", "")}
    if isinstance(outcome, BaseException):
        logger.error("chat_stream_failed", exc_info=(type(outcome), outcome, outcome.__traceback__))
    return "error", {"status": 500, "error": GENERIC_ERROR, "trace": ""}


class AnswerStream:
    """The queue between the answer's background task and the response that sends its events."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self.drafting = False  # text has been sent since the last reset

    def on_draft(self, kind: str, text: str) -> None:
        """The agent's ``DraftSink``: text becomes ``delta``; a discard becomes ``reset`` only if text was sent."""
        if kind == "text":
            self.drafting = True
            self.queue.put_nowait(("event", sse("delta", {"text": text})))
        elif kind == "discard" and self.drafting:
            self.drafting = False
            self.queue.put_nowait(("event", sse("reset", {})))

    def finish(self, outcome: Response | BaseException | None) -> None:
        self.queue.put_nowait(("final", outcome))

    async def events(self, task: asyncio.Task[Any] | None = None) -> AsyncIterator[str]:
        """SSE text until the final event. Closing this generator (the browser left) never stops the task."""
        yield ": open\n\n"
        while True:
            try:
                kind, item = await asyncio.wait_for(self.queue.get(), KEEPALIVE_SECONDS)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if kind == "final":
                yield sse(*final_event(item))
                return
            yield item

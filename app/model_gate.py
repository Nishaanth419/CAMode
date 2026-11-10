"""Admission control and time budgets for model calls.

One Ollama on the operator's Mac answers every profile. It runs ``OLLAMA_NUM_PARALLEL`` requests at once
and queues the rest, so without a limit here fifty people asking together all wait on the Mac until the
model or proxy timeout. Instead:

- **A process-wide queue.** At most ``MODEL_CONCURRENCY`` Ollama calls (``OLLAMA_NUM_PARALLEL``, else 1)
  are in flight. A chat waits at most ``MODEL_QUEUE_SECONDS`` (20) for its first slot; when none frees up it
  fails fast with ``ModelBusy`` (HTTP 503 with ``Retry-After``) instead of hanging. Once a chat has had a
  slot, later calls of the same answer wait as long as its budget allows, so an answer is not abandoned
  halfway. Cloud providers are not queued here.
- **A budget per chat.** ``chat_budget()`` (``main.chat``) gives the whole answer ``CHAT_DEADLINE_SECONDS``
  (240), below the proxy's timeout; every model call is cut off when it runs out (``DeadlineReached``).
  ``call_deadline()`` narrows it further for a stretch of the answer (the general agent's own deadline).
- **The last good call.** ``status()`` reports when a model call last succeeded, for the admin page.

The queue and the budgets live in this process's memory: CA Mode runs one worker (``db.claim_single_process``).
"""

from __future__ import annotations

import asyncio
import contextvars
import math
import os
import time
import weakref
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final, TypeVar

T = TypeVar("T")

# Queued: the one local model. Cloud providers scale on their side.
GATED_PROVIDERS: Final = frozenset({"ollama"})


def _env_number(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default
    return value if value > 0 else default


def concurrency() -> int:
    """Model calls Ollama runs at once: ``MODEL_CONCURRENCY``, else ``OLLAMA_NUM_PARALLEL``, else 1."""
    raw = os.getenv("MODEL_CONCURRENCY", "").strip() or os.getenv("OLLAMA_NUM_PARALLEL", "").strip()
    try:
        return max(1, int(raw)) if raw else 1
    except ValueError:
        return 1


def queue_seconds() -> float:
    return _env_number("MODEL_QUEUE_SECONDS", 20)


def chat_deadline_seconds() -> float:
    return _env_number("CHAT_DEADLINE_SECONDS", 240)


class ModelBusy(Exception):
    """Every model slot stayed taken for the whole wait: ask again shortly."""

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            "The assistant is busy answering other people right now. Try again in about "
            f"{retry_after_seconds} seconds."
        )


class DeadlineReached(Exception):
    """The answer's time budget ran out before this model call could finish."""


@dataclass(slots=True)
class _Budget:
    deadline: float  # time.monotonic()
    admitted: bool = False


_budget: contextvars.ContextVar[_Budget | None] = contextvars.ContextVar("model_budget", default=None)
_call_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("model_call_deadline", default=None)
# asyncio primitives belong to one event loop; the tests start several.
_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, tuple[int, asyncio.Semaphore]] = weakref.WeakKeyDictionary()
_last_success: dict[str, float] = {}
_waiting = 0


@contextmanager
def chat_budget(seconds: float | None = None) -> Iterator[None]:
    """Give the model calls made inside this block (one chat answer) ``seconds`` in all."""
    token = _budget.set(_Budget(time.monotonic() + (seconds if seconds is not None else chat_deadline_seconds())))
    try:
        yield
    finally:
        _budget.reset(token)


@contextmanager
def call_deadline(deadline: float | None) -> Iterator[None]:
    """Cut off model calls inside this block at ``deadline`` (``time.monotonic()``); None changes nothing."""
    token = _call_deadline.set(deadline)
    try:
        yield
    finally:
        _call_deadline.reset(token)


def remaining() -> float | None:
    """Seconds left for model calls here (the chat budget or a narrower deadline), or None when unbounded."""
    budget = _budget.get()
    ends = [end for end in (budget.deadline if budget else None, _call_deadline.get()) if end is not None]
    return min(ends) - time.monotonic() if ends else None


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    size = concurrency()
    current = _slots.get(loop)
    if current is None or current[0] != size:
        current = (size, asyncio.Semaphore(size))
        _slots[loop] = current
    return current[1]


async def call(provider: str, request: Callable[[], Awaitable[T]]) -> T:
    """Run one model HTTP call under the queue and the time budget.

    Raises ``ModelBusy`` when no slot frees up within the wait, ``DeadlineReached`` when the budget is spent
    before or during the call. A response that is not an error marks the provider as last answering now.
    """
    global _waiting
    budget = _budget.get()
    left = remaining()
    if left is not None and left <= 0:
        raise DeadlineReached
    gated = provider in GATED_PROVIDERS
    semaphore = _semaphore() if gated else None
    if semaphore is not None:
        # A chat already under way waits as long as its budget allows; a new one only for the queue wait.
        if budget is not None and budget.admitted:
            wait, busy = left, False
        else:
            wait, busy = queue_seconds(), True
            if left is not None and left < wait:
                wait, busy = left, False
        _waiting += 1
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=wait)
        except TimeoutError:
            if busy:
                raise ModelBusy(retry_after_seconds=max(5, math.ceil(queue_seconds()))) from None
            raise DeadlineReached from None
        finally:
            _waiting -= 1
        if budget is not None:
            budget.admitted = True
    try:
        left = remaining()
        if left is None:
            response = await request()
        else:
            if left <= 0:
                raise DeadlineReached
            try:
                response = await asyncio.wait_for(request(), timeout=left)
            except TimeoutError:
                raise DeadlineReached from None
    finally:
        if semaphore is not None:
            semaphore.release()
    if getattr(response, "is_success", True):
        _last_success[provider] = time.time()
    return response


def status() -> dict[str, Any]:
    """For the admin page: the queue's size, how many calls wait now, and each provider's last good answer."""
    return {
        "concurrency": concurrency(),
        "waiting": _waiting,
        "lastSuccessAt": {provider: round(at * 1000) for provider, at in _last_success.items()},
    }


def reset() -> None:
    """Forget the last successful calls (tests)."""
    _last_success.clear()

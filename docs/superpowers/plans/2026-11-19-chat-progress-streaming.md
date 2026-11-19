# Chat Progress Streaming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** While an answer is being made, the chat shows plain progress lines from the first second and the connection never goes quiet; the checked answer arrives as today.

**Architecture:** `POST /api/chat` with `Accept: text/event-stream` runs today's pre-start checks (same status codes), then answers `200 text/event-stream`. The answer itself runs in its own `asyncio` task (so a disconnect never cancels it); `run_agent(on_event=)` feeds a queue, `app/chat_stream.py` turns agent events into privacy-safe `progress` events, adds a 15 s keepalive, and converts the task's final `Response` into one `answer` or `error` event. Without the header nothing changes.

**Tech Stack:** FastAPI/Starlette `StreamingResponse`, asyncio, vanilla JS (`fetch` + `ReadableStream`), pytest, `node --test`.

**Spec:** `docs/superpowers/specs/2026-11-19-chat-progress-streaming-design.md` (read it first; this plan argues from it).

## Global Constraints

- Streaming only when the request has `Accept: text/event-stream`; otherwise `POST /api/chat` is byte-for-byte today's behaviour. No existing test may change.
- Pre-start failures keep their status codes and JSON bodies in both modes (validation, `history_gate`, rate limit, `429 chatInFlight`, chat not found / wrong agent, INDmoney not connected/unavailable, daily limit `429`, credits `402`).
- Stream headers: `Content-Type: text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`.
- Event names: `progress`, `answer`, `error`; keepalive is the SSE comment line `: keepalive`. First event is `progress {kind: "start"}`.
- `progress` data keys only `seq`, `kind`, `name`, `source`, `status`. `kind` ∈ `start, waiting, route, context, model_call, tool_call, tool_result, retry, validation, plan`. Never arguments, results or figures.
- `source` ∈ `saved`, `indmoney`, `broker:<id>` (id in `brokers.BROKERS`), `broker`, `analysis`.
- Keepalive after `KEEPALIVE_SECONDS = 15` with no other event.
- `answer.trace` is the compact `trace_header()` form, not the full trace.
- `error` data = `{status, trace, ...today's JSON body}`.
- Frontend never shows a raw tool name; unknown steps read "Working on it…".
- `waitForSavedAnswer()` window: 270 s (was 150 s).
- Hosted copy: `CHAT_DEADLINE_SECONDS` back to 240 behind Cloudflare (docs + upgrade note).
- No Claude co-author trailer or "Generated with Claude Code" line in commits or the PR (user's global rule).

## Review Focus

1. **Browser closes the tab mid-answer** → the answer is still saved to the chat, the daily slot confirmed, `_chats_in_flight` released (test in Task 5).
2. **Old app (no `Accept` header) during rollout** → plain text answer exactly as today (test in Task 5).
3. **INDmoney session expires during a streamed answer** → refresh-and-retry still happens; a failed retry is an `error` event with `status: 401` and `loginUrl` (test in Task 5).
4. **Stream cut by a proxy with no `answer`/`error`** → the page falls back to `waitForSavedAnswer()` instead of showing a blank answer (parser test for a trailing partial event in Task 6; manual check in Task 7).
5. **Caddy compression holding back chunks** → progress lines still arrive live behind Caddy (manual check in Task 8).

---

### Task 0: Branch and carry the `$$$` indicator

The `$$$` typing indicator exists only as an uncommitted change in the main checkout (`/Users/nishaanth/Desktop/CAMode/CAMode`, branch `fix/kite-mode-tools`). Start from `main`.

**Files:**
- Modify: `frontend/js/app.js` (`addTypingIndicator()`), `frontend/css/app.css` (`.typing-indicator` block), `frontend/index.html`, `frontend/sw.js`
- Copy in: `docs/superpowers/specs/2026-11-19-chat-progress-streaming-design.md`, `docs/superpowers/plans/2026-11-19-chat-progress-streaming.md` (from the main checkout)

- [ ] **Step 1:** Create branch `feat/chat-progress-streaming` from `main`.
- [ ] **Step 2:** In `addTypingIndicator()` each of the three `<span></span>` becomes `<span aria-hidden="true">$</span>`. In `app.css` `.typing-indicator` gets `gap: 4px; padding: 2px 0`, and `.typing-indicator span` drops `width`, `height`, `border-radius`, `background` and gets `font-size: 16px; font-weight: 700; line-height: 1; color: var(--accent-3)` (animation unchanged).
- [ ] **Step 3:** Run `node --check frontend/js/app.js`. Expected: no output.
- [ ] **Step 4:** Commit spec, plan and indicator: `feat(chat): show $ signs while an answer is made`. Cache-busters are bumped once, in Task 7.

---

### Task 1: Tell listeners when a model call waits for a slot

**Files:**
- Modify: `app/model_gate.py`
- Test: `tests/test_model_gate.py`

**Interfaces:**
- Produces: `model_gate.wait_listener(callback: Callable[[], None] | None) -> ContextManager[None]`. Inside the block, `call()` invokes `callback()` once per call when a gated provider's semaphore is not free at once (`semaphore.locked()` before acquiring). A raising callback is logged (`model_wait_listener_failed`) and ignored.

A context variable keeps the four `model_gate.call()` sites (`agent.py` ×3, `router.py`) unchanged. That differs from the spec's "agent passes `on_wait` through"; the intent (a `waiting` event, nothing else changes in the agent) is the same.

- [ ] **Step 1: Write the failing tests**

```python
async def test_wait_listener_is_told_when_the_slot_is_taken(monkeypatch):
    monkeypatch.setenv("MODEL_CONCURRENCY", "1")
    model_gate.reset()
    told = []
    release = asyncio.Event()

    async def slow():
        await release.wait()
        return SimpleNamespace(is_success=True)

    first = asyncio.create_task(model_gate.call("ollama", slow))
    await asyncio.sleep(0)
    with model_gate.chat_budget(5), model_gate.wait_listener(lambda: told.append(1)):
        second = asyncio.create_task(model_gate.call("ollama", lambda: asyncio.sleep(0, SimpleNamespace(is_success=True))))
        await asyncio.sleep(0.01)
        release.set()
        await asyncio.gather(first, second)
    assert told == [1]

async def test_wait_listener_is_quiet_when_a_slot_is_free(): ...        # told == []
async def test_a_failing_wait_listener_never_fails_the_call(): ...       # callback raises; call still returns
async def test_cloud_providers_never_call_the_wait_listener(): ...       # provider "openai"; told == []
```

(Use the file's existing async test style; set the listener in the task that makes the waiting call, since context variables are copied at task creation.)

- [ ] **Step 2:** Run `python -m pytest tests/test_model_gate.py -q`. Expected: the four new tests FAIL (`wait_listener` missing).
- [ ] **Step 3:** Implement `wait_listener()` (a `contextvars.ContextVar[Callable[[], None] | None]`, same pattern as `call_deadline`) and the check in `call()` before `semaphore.acquire()`.
- [ ] **Step 4:** Run `python -m pytest tests/test_model_gate.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit `feat(model_gate): tell a listener when a model call waits for a slot`.

---

### Task 2: `run_agent(on_event=)`

**Files:**
- Modify: `app/agent.py` (`run_agent`, around line 1760)
- Test: `tests/test_agent.py`

**Interfaces:**
- Produces: `run_agent(..., plan_parts=(), on_event: Callable[[str, dict[str, Any]], None] | None = None) -> AgentResult`. Called with every `(event_type, payload)` from `stream_agent()` in order, then with `("validation", <the task_contract validation payload>)` where `run_agent` records it itself. A raising callback is logged once (`agent_event_listener_failed`) and never changes the answer.

- [ ] **Step 1: Write the failing tests** (monkeypatch `agent.stream_agent` with an async generator yielding `route`, `tool_call`, `tool_result`, `content`, `done`):

```python
async def test_run_agent_passes_every_event_to_on_event(monkeypatch):
    seen = []
    result = await agent.run_agent("q", "url", None, [], on_event=lambda kind, data: seen.append(kind))
    assert seen[:5] == ["route", "tool_call", "tool_result", "content", "done"]
    assert str(result) == "answer text"

async def test_run_agent_reports_the_task_contract_check(monkeypatch): ...  # a "contract" event in the stream → ("validation", {"check": "task_contract", ...}) reaches on_event
async def test_a_failing_on_event_never_fails_the_answer(monkeypatch): ...  # callback raises; same AgentResult
async def test_run_agent_without_on_event_is_unchanged(monkeypatch): ...     # no kwarg; same result as before
```

- [ ] **Step 2:** Run `python -m pytest tests/test_agent.py -q -k on_event`. Expected: FAIL (unexpected keyword `on_event`).
- [ ] **Step 3:** Add the parameter; call it after `recorder.add(...)` in the loop, and where `run_agent` adds the `task_contract` validation (`_contract_verdict`, `agent.py:1751`) with the same payload it records. Wrap calls in one helper that catches `Exception`.
- [ ] **Step 4:** Run `python -m pytest tests/test_agent.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit `feat(agent): let run_agent report its events as they happen`.

---

### Task 3: `app/chat_stream.py` — progress mapping and SSE framing

**Files:**
- Create: `app/chat_stream.py`
- Test: `tests/test_chat_stream.py`

**Interfaces:**
- Produces:
  - `KEEPALIVE_SECONDS: Final = 15`; `KEEPALIVE: Final = b": keepalive\n\n"`
  - `def sse(event: str, data: dict[str, Any]) -> bytes` → `f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"` encoded UTF-8.
  - `class ProgressMapper` with `start() -> dict` (returns `{"seq": 0, "kind": "start"}`), `waiting() -> dict` (`kind: "waiting"`), and `map(event: str, data: dict) -> dict | None`.
    - `contract` → remembers `contract.allowed_tools` (union over contract events, including `parts`) and returns None.
    - `route` → `{kind, name: data["category"]}`; `context` → `{kind}`; `model_call` → `{kind}`; `retry` → `{kind, name: data["reason"]}`; `validation` → `{kind, name: data["check"]}`; `plan` → `{kind, name: data["stage"]}`.
    - `tool_call` → `{kind, name?, source}`: `name` only if in the remembered allowed tools; `source` = `indmoney` when `data["source"] == "indmoney"`; for `get_broker_holdings` / `call_broker_tool` `broker:<id>` where `<id>` is the `broker` key of the arguments (dict, or JSON text parsed safely) and is in `brokers.BROKERS`, else `broker`; `analysis` when the name is in `analysis_tools.ANALYSIS_TOOL_NAMES`; else `saved`. Remembers name/source for the matching result.
    - `tool_result` → `{kind, name?, source, status}`: name from `data["name"]` or the last `tool_call` (planned runs send `step`); `status` = `failed` when `data` has `error` or `error_kind`, else `ok`.
    - `content`, `model_result`, `plan_stop`, `done`, `error`, anything else → None.
    - Every returned dict gets the next `seq` (0, 1, 2…).
  - `def response_event(response: Response) -> tuple[str, dict[str, Any]]`:
    - `200` plain text → `("answer", {"text": body, "trace": <parsed X-Agent-Trace>, "runId"?: X-Agent-Run, "questionsRemaining"?: int, "creditsRemaining"?: int, "creditsCharged"?: int})`.
    - JSON response → `("error", {"status": response.status_code, "trace": <parsed X-Agent-Trace or []>, **json_body})`.

- [ ] **Step 1: Write the failing tests**

```python
def test_sse_frames_one_event(): assert sse("progress", {"seq": 1}) == b'event: progress\ndata: {"seq":1}\n\n'
def test_start_is_seq_zero(): assert ProgressMapper().start() == {"seq": 0, "kind": "start"}
def test_tool_name_is_kept_only_when_offered():
    m = ProgressMapper(); m.start()
    m.map("contract", {"contract": tasks.TaskContract("account_data", allowed_tools=frozenset({"get_portfolio_snapshot"}))})
    assert m.map("tool_call", {"name": "get_portfolio_snapshot", "arguments": {}, "source": "local"}) == {"seq": 1, "kind": "tool_call", "name": "get_portfolio_snapshot", "source": "saved"}
    assert "name" not in m.map("tool_call", {"name": "made_up_tool", "arguments": {}, "source": "local"})
def test_sources_per_tool_kind(): ...        # indmoney; broker:zerodha from {"broker": "zerodha"} and from '{"broker":"zerodha"}'; broker for "nope"; analysis for "calculate"; saved
def test_progress_never_carries_arguments_results_or_figures():
    # a tool_call with arguments {"amount": 125000} and a tool_result {"result": "₹1,25,000"}: the json of every mapped dict has no "125000"/"1,25,000" and only allowed keys
def test_tool_result_status_and_planned_step_name(): ...   # error_kind → failed; {"step": "s1"} takes the last call's name
def test_unsent_events_map_to_none(): ...                  # content, model_result, plan_stop, done, error
def test_response_event_for_an_answer(): ...               # PlainTextResponse with X-Agent-Trace/X-Agent-Run/X-Questions-Remaining/X-Credits-* → answer dict with ints
def test_response_event_for_a_failure(): ...               # JSONResponse 503 {"error", "modelBusy", "retryAfterSeconds"} → ("error", {"status": 503, "trace": [], ...})
```

- [ ] **Step 2:** Run `python -m pytest tests/test_chat_stream.py -q`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement `app/chat_stream.py` per the interfaces. Parse `X-Agent-Trace` with the inverse of `main.trace_header()` (read it there; it is URL-safe JSON) — move the encode/decode pair into `chat_stream` if that avoids importing `main`.
- [ ] **Step 4:** Run `python -m pytest tests/test_chat_stream.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit `feat(chat): map agent events to safe progress events`.

---

### Task 4: Split `_answer_chat()` into prepare and run

Pure refactor; behaviour unchanged.

**Files:**
- Modify: `app/main.py` (`chat()` ~1990, `_answer_chat()` ~2030)

**Interfaces:**
- Produces:
  - `@dataclass class PreparedChat: body: ChatRequest; request: Request; user: dict; message: str; provider: str; data: dict; history: list; saved_context: SavedContext | None; context: PortfolioContext | None; token: str | None; reservation: limits.Reservation | None; monthly_credits: int | None`
  - `async def _prepare_chat(body, request, user, message, provider) -> Response | PreparedChat` — everything in `_answer_chat()` up to and including `limits.reserve_question` and `credits.ensure_available` (releasing the reservation on `CreditsExhausted`, as today).
  - `async def _run_chat(prepared: PreparedChat, on_event: Callable[[str, dict], None] | None = None) -> Response` — the rest (agent run with `on_event` passed to every `_run_agent_within_budget(...)`, the INDmoney auth retry, error responses, saving, charging, the confirm/release `finally`).
  - `_answer_chat()` becomes `prepared = await _prepare_chat(...)`; return it if it is a `Response`, else `await _run_chat(prepared)`.

- [ ] **Step 1:** Run `python -m pytest -q tests/test_api.py tests/test_limits.py tests/test_plans.py tests/test_agent_runs.py tests/test_privacy.py tests/test_unlock.py`. Expected: PASS (baseline).
- [ ] **Step 2:** Do the split. Fake `run_agent`s in tests take `**kwargs`, so passing `on_event=None` is safe — pass it only when not None anyway, to keep fakes with fixed signatures working.
- [ ] **Step 3:** Re-run the Step 1 command, then `python -m pytest -q`. Expected: PASS, no test changed.
- [ ] **Step 4:** Commit `refactor(chat): split preparing an answer from running it`.

---

### Task 5: The streaming route

**Files:**
- Modify: `app/main.py` (`chat()`), `app/chat_stream.py`
- Test: `tests/test_chat_stream.py`

**Interfaces:**
- Consumes: `PreparedChat`, `_prepare_chat`, `_run_chat` (Task 4); `ProgressMapper`, `sse`, `KEEPALIVE*`, `response_event` (Task 3); `model_gate.wait_listener` (Task 1).
- Produces:
  - `def wants_stream(request: Request) -> bool` — `"text/event-stream"` in the `Accept` header.
  - `async def chat_stream.events(task: asyncio.Task[Response], queue: asyncio.Queue[dict], first: dict, failure: dict[str, Any]) -> AsyncIterator[bytes]` — yields `sse("progress", first)`, then each queued progress dict, `KEEPALIVE` whenever `KEEPALIVE_SECONDS` pass with nothing queued, and when the task is done (drain the queue first) one `sse(*response_event(task.result()))`. If the task raised, yield `sse("error", failure)`; `main` passes `{"status": 502, "trace": [], "error": chat_failure_message(None, "")}` (`chat_stream` never imports `main`). Cancelling the generator must not cancel the task.
  - In `chat()`: after validation, `history_gate`, rate limit and the in-flight check, when `wants_stream(request)`: add the profile to `_chats_in_flight`, run `_prepare_chat()` inside `model_gate.chat_budget()`; a `Response` result → discard from `_chats_in_flight` and return it (status codes kept). Otherwise create the task running `_run_chat(prepared, on_event)` inside a fresh `model_gate.chat_budget()` and `model_gate.wait_listener(...)`, discarding from `_chats_in_flight` in its own `finally`; keep a strong reference in a module-level `_stream_tasks: set[asyncio.Task]` (removed by `add_done_callback`). `on_event` maps through a `ProgressMapper` and `queue.put_nowait`s non-None results; the wait listener queues `mapper.waiting()`. Return `StreamingResponse(chat_stream.events(...), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})`.

- [ ] **Step 1: Write the failing tests** (helper `stream_ask(client, chat_id, message=...)` posts with `headers={"Accept": "text/event-stream"}` via `client.stream(...)` and parses events into `[(name, data)]`; fakes patch `main.run_agent` and call `kwargs["on_event"]`):

```python
def test_streamed_answer_sends_start_progress_then_one_answer(client, signed_in, monkeypatch):
    # fake run_agent calls on_event("route", {"category": "general_finance"}) then returns "Diversification spreads risk."
    events = stream_ask(client, new_chat(client))
    assert [name for name, _ in events] == ["progress", "progress", "answer"]
    assert events[0][1] == {"seq": 0, "kind": "start"}
    assert events[-1][1]["text"] == "Diversification spreads risk."

def test_streamed_answer_matches_the_plain_answer(client, signed_in, monkeypatch): ...      # same text, trace, runId, questionsRemaining as headers of a plain call (DAILY_QUESTION_LIMIT set)
def test_without_the_accept_header_the_answer_is_plain_text(client, signed_in, monkeypatch): ...   # text/plain, X-Agent-Trace present
def test_pre_start_failures_keep_their_status_codes(client, signed_in, monkeypatch): ...  # in-flight 429, daily limit 429, credits 402, wrong agent 409 — each with the Accept header, JSON not a stream
def test_a_busy_model_after_start_is_an_error_event(client, signed_in, monkeypatch): ...  # AgentRunError(code="model_busy", retry_after=20) → ("error", {"status": 503, "modelBusy": True, "retryAfterSeconds": 20, ...}); question released
def test_a_deadline_after_start_is_an_error_event(client, signed_in, monkeypatch): ...    # code="chat_deadline" → status 504, chatDeadline
def test_indmoney_auth_failure_retries_then_asks_to_reconnect(client, signed_in, monkeypatch): ...  # portfolio agent, auth error twice → ("error", {"status": 401, "loginUrl": ...}); progress from both runs streamed
def test_keepalive_when_nothing_happens(client, signed_in, monkeypatch): ...  # monkeypatch chat_stream.KEEPALIVE_SECONDS = 0.05; fake sleeps 0.2 s; raw body contains b": keepalive"
def test_a_waiting_model_slot_is_a_progress_event(client, signed_in, monkeypatch): ...  # fake triggers the current wait listener → ("progress", {"kind": "waiting", ...})
def test_progress_events_have_only_allowed_keys(client, signed_in, monkeypatch): ...     # every progress dict's keys ⊆ {seq, kind, name, source, status}
def test_a_disconnect_still_saves_the_answer(client, signed_in, monkeypatch): ...
    # fake run_agent waits on a threading.Event, then returns an answer; read the first event from client.stream(...)
    # and close the response (the client disconnects); set the Event; wait (poll ≤ 2 s) until main._stream_tasks is empty;
    # assert GET /api/chats/{id} has the answer and main._chats_in_flight == set()
    # (if TestClient cannot surface the disconnect, test chat_stream.events directly: aclose() the generator, then await the task)
def test_in_flight_is_released_after_a_streamed_failure(client, signed_in, monkeypatch): ...
```

- [ ] **Step 2:** Run `python -m pytest tests/test_chat_stream.py -q`. Expected: the new route tests FAIL (plain response returned).
- [ ] **Step 3:** Implement `wants_stream`, `chat_stream.events`, and the streaming branch of `chat()` per the interfaces.
- [ ] **Step 4:** Run `python -m pytest tests/test_chat_stream.py -q`, then `python -m pytest -q`. Expected: PASS.
- [ ] **Step 5:** Commit `feat(chat): stream progress and the checked answer as server-sent events`.

---

### Task 6: SSE parser and progress labels (browser helpers)

**Files:**
- Modify: `frontend/js/ui.js`, `frontend/js/trace.js`
- Test: `tests/js/ui.test.js`, `tests/js/trace.test.js`

**Interfaces:**
- Produces:
  - `ui.js`: `createSseParser(onEvent: (name: string, data: object) => void) -> { push(text: string): void, end(): boolean }` — buffers chunks, splits on blank lines (`\n\n`, also `\r\n\r\n`), joins multiple `data:` lines with `\n`, ignores lines starting with `:`, skips events whose data is not JSON; `end()` returns `true` if an incomplete event was left in the buffer (and drops it).
  - `trace.js`: `progressLabel(event, brokerNames = {}) -> string`:
    - `start` → "Understanding your question"; `waiting` → "Waiting for the assistant — someone else's question is being answered"; `route` → "Understanding your question"; `context` → "Reading your saved details"; `model_call` → "Thinking"; `plan` → "Planning the steps"; `retry` → "Taking another look"; `validation` with `name` in `CHECKS` → that check's label (e.g. "Check the figures" → "Checking the figures": use an `-ing` form table for the five checks).
    - `tool_call` / `tool_result`: `source: "indmoney"` → "Asking INDmoney"; `source: "broker:<id>"` → `Asking ${brokerNames[id] || 'your broker'}`; `source: "broker"` → "Asking your broker"; a `name` in `LOCAL_TOOLS` → its label (add `get_mf_tax: ['Read mutual fund tax', 'Capital gains worked out from your detailed CAS']`); otherwise "Working on it".
    - Anything else → "Working on it". Labels end with "…" when shown (caller adds it). Never returns a raw tool name.

- [ ] **Step 1: Write the failing Node tests**

```js
test('parser joins chunks split mid-event', () => { /* push('event: progress\nda'), push('ta: {"seq":0,"kind":"start"}\n\n') → one ('progress', {seq:0, kind:'start'}) */ });
test('parser ignores keepalive comments and bad JSON', () => { /* ': keepalive\n\n' and 'event: x\ndata: {\n\n' → no events */ });
test('parser joins multi-line data', () => {});
test('parser end() reports a trailing partial event', () => { /* push('event: answer\ndata: {"te') ; end() === true */ });
test('progress labels never show a raw tool name', () => {
  assert.equal(progressLabel({ kind: 'tool_call', name: 'get_holdings_v2', source: 'saved' }), 'Working on it');
  assert.equal(progressLabel({ kind: 'tool_call', source: 'broker:zerodha' }, { zerodha: 'Zerodha' }), 'Asking Zerodha');
  assert.equal(progressLabel({ kind: 'tool_call', name: 'get_portfolio_snapshot', source: 'saved' }), 'Read your saved snapshot');
  assert.equal(progressLabel({ kind: 'tool_call', source: 'indmoney' }), 'Asking INDmoney');
  assert.equal(progressLabel({ kind: 'validation', name: 'numbers' }), 'Checking the figures');
  assert.equal(progressLabel({ kind: 'mystery' }), 'Working on it');
});
```

- [ ] **Step 2:** Run `node --test tests/js/*.test.js`. Expected: new tests FAIL.
- [ ] **Step 3:** Implement and export both helpers (follow each file's existing export pattern for Node).
- [ ] **Step 4:** Run `node --test tests/js/*.test.js` and `for f in frontend/js/*.js; do node --check "$f"; done`. Expected: PASS, no output from `--check`.
- [ ] **Step 5:** Commit `feat(chat): parse streamed events and label progress steps`.

---

### Task 7: Stream in the chat page

**Files:**
- Modify: `frontend/js/app.js` (`sendMessage()` ~1841, `addTypingIndicator()` ~1818, `waitForSavedAnswer()` ~2001), `frontend/css/app.css`, `frontend/index.html`, `frontend/sw.js`

**Interfaces:**
- Consumes: `createSseParser` (Task 6), `progressLabel` (Task 6), the event shapes (Tasks 3, 5).
- Produces: `handleChatFailure(status: number, body: object, trace: array, createdChatId: string|null) -> Promise<boolean>` — today's non-OK branches of `sendMessage()` moved verbatim (limits, busy/deadline/in-flight, credits, `loginUrl`, session prompts, generic `plainError`); returns `true` when it rendered a message, otherwise throws as today. Both the plain path (`res.json()` body, `res.status`) and the `error` event (`data.status`, `data`) call it.

- [ ] **Step 1:** `addTypingIndicator()` adds `<p class="typing-progress" aria-live="polite"></p>` after the `$$$` indicator; add `setTypingProgress(text)` that fills it (and calls `setStatus('loading', text)`). CSS: `.typing-progress { margin: 6px 0 0; font-size: 13px; color: var(--text-2); }` (use the muted text token the file already uses for `.message-meta`).
- [ ] **Step 2:** `sendMessage()` sends `Accept: 'text/event-stream'`. If `res.ok` and `res.headers.get('Content-Type')` starts with `text/event-stream`: read `res.body` through `createSseParser`; `progress` → `setTypingProgress(progressLabel(data, brokerNamesById) + '…')` (`brokerNamesById` from the brokers list `app.js` already loads; `{}` if none); `answer` → today's success rendering using `data.text`, `data.trace`, `data.runId`, `data.questionsRemaining`, `data.creditsRemaining`; `error` → `handleChatFailure(data.status, data, data.trace || [], createdChatId)`. If the stream ends with neither `answer` nor `error` (or `parser.end()` reports a partial event, or the read throws), go to the existing `waitForSavedAnswer()` recovery branch. Non-stream responses take today's path unchanged.
- [ ] **Step 3:** `waitForSavedAnswer()` window 150 s → 270 s (keep the 10 s interval).
- [ ] **Step 4:** Bump cache-busters from `main`'s values once: `app.css`, `app.js`, `ui.js`, `trace.js` `?v=` +1 each, `CACHE_NAME` +1 in `sw.js`.
- [ ] **Step 5:** Run `for f in frontend/js/*.js; do node --check "$f"; done && node --test tests/js/*.test.js`. Expected: PASS.
- [ ] **Step 6: Verify in the browser** (preview `ca-mode` from `.claude/launch.json`, signed-in local profile): send a Finance Agent question; a progress line appears within about 2 s and changes as steps run; the answer, trace and run link render as before. Stop Ollama and ask again: today's error wording appears. In devtools, block the request mid-stream (or stop the server after the first event, restart it): the page shows "Checking for the answer…" instead of a blank answer. Screenshot the progress line for the PR.
- [ ] **Step 7:** Commit `feat(chat): show live progress while an answer is made`.

---

### Task 8: Docs, hosted limit and proxy check

**Files:**
- Modify: `docs/hosting.md` (line ~257 and "Upgrading an existing server"), `docs/api.md` (`POST /api/chat`), `docs/future-enhancements.md`, `.claude/CLAUDE.md`
- Possibly modify: `deploy/Caddyfile`

- [ ] **Step 1:** `docs/hosting.md`: replace "so set `CHAT_DEADLINE_SECONDS=90` behind the tunnel" with: answers stream progress and send a keepalive every 15 s, so the tunnel's 100-second limit on a silent request no longer applies; keep the default 240. Add an "Upgrading an existing server" entry for this release: remove `CHAT_DEADLINE_SECONDS=90` (and any lowered `AGENT_DEADLINE_SECONDS` / `PLAN_DEADLINE_SECONDS`) from `deploy/app.env`, then `docker compose up -d`.
- [ ] **Step 2:** Caddy check: run the app behind `deploy/docker-compose.caddy.yml` locally (or `caddy run` with `deploy/Caddyfile` pointed at `127.0.0.1:8003`) and `curl -N -H 'Accept: text/event-stream' ...` a chat (cookie from a signed-in browser session). Expected: the `start` event prints within about 2 s, before the answer. If it only appears with the answer, change `encode zstd gzip` to skip event streams (`encode { zstd gzip; match { not header Content-Type text/event-stream* } }`) and add that `deploy/` change to the upgrade entry from Step 1.
- [ ] **Step 3:** `docs/api.md`: document the streaming mode (the `Accept` header, which failures keep status codes, the three events and their fields, keepalive). `docs/future-enhancements.md`: replace "Stream answers and the trace" with "Stream the answer text" (word by word, after the checks are reworked). CLAUDE.md: update the `app/main.py` line ("returns the full answer as plain text … no streaming to the browser"), the "Answer trace and Activity" line "No streaming: …", the `model_gate` entry (`wait_listener`), add an `app/chat_stream.py` entry, the `ui.js` / `trace.js` helpers, `waitForSavedAnswer` 270 s, and `tests/test_chat_stream.py` under Testing.
- [ ] **Step 4:** Run the full checks: `python -m compileall -q app tests && python -m pytest -q && for f in frontend/js/*.js; do node --check "$f"; done && node --test tests/js/*.test.js && python -m scripts.eval_tasks && python -m scripts.eval_workflows`. Expected: all pass.
- [ ] **Step 5:** Commit `docs: chat progress streaming and the hosted time limit`, push, open the PR (no Claude attribution lines).

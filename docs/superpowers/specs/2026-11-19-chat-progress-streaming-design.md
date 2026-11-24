# Chat progress streaming — design

Date: 2026-11-19. Status: ready for approval.

## Why

Answers take 9–21 s at p50 on the local model and can take minutes. During that time the chat shows only a
bouncing `$$$` indicator, so people think the app is stuck. The goal is that the screen always shows what
CA Mode is doing, from the first second until the answer arrives.

## Goals

- A first progress line within about 2 s of pressing Send, in plain words ("Understanding your question").
- A new line whenever the agent starts a new step ("Reading your saved snapshot", "Asking INDmoney for your
  holdings", "Checking the figures").
- No gap of more than 15 s without something reaching the browser, so neither the person nor a proxy
  (Cloudflare Tunnel, Caddy) treats the request as dead.
- The finished answer, its trace, run id, credits and questions left arrive exactly as they do today.

## Non-goals

- Showing the answer word by word (approach B, below). The answer still appears in one piece after its checks.
- Merging the three agent paths (previous loop, `submit_plan` planner, `agent_graph`). Streaming works on all
  three as they are; the clean-up is a separate, later project.
- Changing the agent's behaviour, prompts, budgets or checks.

## Anti-goals (a failure even if it works)

- A figure, tool argument, tool result or any account data in a progress event. Progress events carry step
  kinds and tool names only.
- An answer lost because the browser disconnected mid-stream. Today the server finishes and saves the answer
  and `waitForSavedAnswer()` recovers it; that must keep working.
- Breaking a browser that still runs the previous `app.js` (an installed PWA before its service worker
  updates), the tests, or the eval scripts that call `POST /api/chat` and expect plain text.

## Approaches considered

- **A. Stream progress, then the checked answer (chosen).** `POST /api/chat` answers with server-sent events
  when the browser asks for them: progress lines and heartbeats while the agent works, then one `answer` event.
  Keeps every answer check in front of the person; the answer text itself still appears at the end.
- **B. A plus the answer word by word.** First words sooner, but text would appear before the number check,
  chart grounding and the code-output filter run, and `agent_graph` can send a finished answer back for
  untraced figures, so a figure could appear and then be flagged or replaced. Needs streaming model calls on
  three providers. Left for later if A still feels slow.
- **C. Background job plus polling.** Works through any proxy, but adds a job store and a second endpoint, and
  the app already recovers dropped connections from the saved chat.

## Design

### Wire format (content negotiation)

- The browser sends `Accept: text/event-stream` on `POST /api/chat`. Without it the route behaves exactly as
  today (plain text, headers, status codes), so old clients, the test suite and the eval scripts are unchanged.
- **Checks before any work starts keep their status codes** and JSON bodies in both modes: message
  validation, profile/unlock/consent (`history_gate`), rate limit, one answer in flight (`429 chatInFlight`),
  chat not found / wrong agent, INDmoney not connected or unavailable, daily limit (`429`), credits (`402`).
- After those pass, the response is `200 text/event-stream` with `Cache-Control: no-cache` (kept by the
  `no_store_api` middleware, which leaves a route's own header alone) and `X-Accel-Buffering: no`.

Events (each `event: <name>` plus one `data: <json>` line):

| Event | Data | When |
| --- | --- | --- |
| `progress` | `{seq, kind, name?, source?, status?}` | Each agent step starts or ends. `kind` is one of `start`, `waiting`, `route`, `context`, `model_call`, `tool_call`, `tool_result`, `retry`, `validation`, `plan`; `name` is the tool name (only when it is one of the tools offered for this answer, else left out), route category, retry reason or check name; `source` (tool events only) is worked out by the server: `saved` (snapshot, CAS, goal and other saved-data tools), `indmoney` (live INDmoney tools), `broker:<id>` (`get_broker_holdings` / `call_broker_tool`, the id checked against `brokers.BROKERS`, else plain `broker`) or `analysis` (`analysis_tools`); `status` is `ok` / `failed` for `tool_result`. Nothing else: never arguments, results or figures. |
| `answer` | `{text, trace, runId?, questionsRemaining?, creditsRemaining?, creditsCharged?}` | The answer was produced and saved. Same values as today's body and headers; `trace` is the same compact form `trace_header()` puts in `X-Agent-Trace` (not the full trace). |
| `error` | `{status, ...body}` | A failure after the stream started: `status` is the status code today's route would have used (`401` reconnect, `502`, `503 modelBusy`, `504 chatDeadline`, `503 indmoneyUnavailable`) and the rest is today's JSON body unchanged (`error`, `code`, `runId`, `loginUrl`, `retryAfterSeconds`...), plus `trace`. |
| comment `: keepalive` | — | Every 15 s with no other event. |

The first event is `progress {kind: "start"}`, sent as soon as the stream opens.

### Server

- **Split `_answer_chat()`** into `_prepare_chat()` (everything up to and including the daily reservation and
  the credits check; returns either a ready-made error `Response` or a `PreparedChat`) and `_run_chat()`
  (runs the agent, the INDmoney auth retry, saving, charging, and returns the final `Response`, exactly
  today's logic). The plain path calls one then the other, so its behaviour does not change.
- **`run_agent(..., on_event=None)`**: an optional callback called with every `(event, data)` it receives from
  `stream_agent()`, and with the `task_contract` validation event `run_agent` adds itself. All three agent
  paths emit these events today. A callback failure is logged and never fails the answer.
- **Waiting for a model slot** *(designer's call: it serves the goal directly)*: when a model call has to wait in
  `model_gate`'s queue (`MODEL_CONCURRENCY=1` on the shared copy), a `waiting` progress event is sent
  ("Waiting for the assistant — someone else's question is being answered"). This is the one change to
  `model_gate` / `agent.py` beyond `on_event`: `model_gate.call()` takes an optional `on_wait` callback, called
  once when the slot is not free at once, and `agent.py` passes it through from `on_event`. The 2 s target
  counts from when the stream opens; `_prepare_chat()` (which may refresh the INDmoney token) runs before
  that so its failures keep their status codes.
- **Progress mapping** (`main` or a small `app/chat_stream.py`): turns agent events into `progress` data with
  only the allowed fields; `content`, `contract`, `model_result`, `plan_stop` and `done` are not sent.
- **The answer runs in its own task, not inside the response generator.** On a streaming request, `chat()`
  starts `_run_chat()` with `asyncio.create_task()`; the task owns `_chats_in_flight`, the
  `model_gate.chat_budget()` context, and the reservation confirm/release (today's `finally` blocks). Its
  `on_event` puts progress on an `asyncio.Queue`; the generator reads the queue with a 15 s timeout (timeout →
  keepalive), and when the task finishes turns its `Response` into an `answer` or `error` event. If the browser
  disconnects, Starlette cancels only the generator; the task finishes and saves the answer as today, and
  `waitForSavedAnswer()` finds it. The task keeps a strong reference (a module-level set) until done.
- **The INDmoney auth retry** stays inside `_run_chat()`; its second run's events stream too.

### Frontend (`app.js`, `trace.js`, `app.css`)

- `sendMessage()` sends `Accept: text/event-stream`. If the response is `text/event-stream`, it reads
  `res.body` with a small SSE parser (`ui.js`, exported for Node tests: splits on blank lines, joins
  `data:` lines, ignores comments). Otherwise it takes today's path unchanged.
- **The typing indicator shows the latest progress line** next to the `$$$` animation, and the status pill
  shows the same words. Labels come from `trace.js` (`progressLabel(event)`), reusing `LOCAL_TOOLS`,
  `ROUTE_LABELS`, `RETRY_REASONS` and `CHECKS` (add `get_mf_tax`); `source: indmoney` reads "Asking INDmoney…",
  `broker:<id>` "Asking <broker name>…" (name from the brokers list already loaded), `analysis` tools use
  their `LOCAL_TOOLS` label; anything unknown reads "Working on it…". Never a raw tool name. The checks run
  in a fraction of a second at the end, so "Checking the figures" may flash; that is fine.
- `answer` → the same rendering as today's success path (message, trace, run link, questions/credits left).
- `error` → the same handling as today's non-OK branches. Those branches are refactored into one
  `handleChatFailure(status, body)` that both paths call, so the wording stays identical.
- A stream that ends without `answer` or `error` (dropped connection, proxy cut) goes to today's
  `waitForSavedAnswer()` recovery.
- Bump the `?v=` cache-busters and `CACHE_NAME`. The service worker already skips `/api/`.

### Proxies

- `deploy/Caddyfile` has `encode zstd gzip`; compression can hold back small event chunks. Verify on the Caddy
  setup, and if chunks are delayed, exclude `text/event-stream` from `encode` (a `deploy/` change: list it under
  docs/hosting.md "Upgrading an existing server").

### Hosted time limit *(decided by the owner, 2026-11-19)*

- Behind Cloudflare the hosted copy sets `CHAT_DEADLINE_SECONDS=90` because silent requests were cut off at
  about 100 s. With the stream never silent for more than 15 s, raise it to the default 240 s.
- Update docs/hosting.md (the `CHAT_DEADLINE_SECONDS=90` advice) and `deploy/app.env.example`, and add the
  `.env` edit to docs/hosting.md "Upgrading an existing server" (the image does not ship `deploy/` changes).
  `AGENT_DEADLINE_SECONDS` (180) and `PLAN_DEADLINE_SECONDS` (240) stay at their defaults, below or at the chat
  budget; if the hosted `.env` lowered them, the upgrade note says to remove those lines too.
- `waitForSavedAnswer()` looks for the saved answer for up to 270 s (was 150 s), so a stream that drops early
  on an answer that uses the whole budget (240 s plus the 20 s backstop grace) is still found.

### Branch *(designer's call)*

- The `$$$` typing indicator is an uncommitted change on `fix/kite-mode-tools`. The streaming work starts from
  `main` and includes that change (same files, `app.js` / `app.css` / cache-busters), so it ships in one PR.

### Tests

- Existing `/api/chat` tests stay as they are (plain mode).
- New Python tests (streaming mode, scripted provider): event order (`start` first, progress, one `answer`),
  `answer` fields equal the plain mode's body and headers, pre-start failures keep their status codes, mid-run
  failures become `error` events with today's bodies (`modelBusy`, `chatDeadline`, auth retry then reconnect),
  keepalive after 15 s idle (patched shorter), progress events never contain arguments, results or figures,
  a tool name the model made up is left out, `source` per tool kind, a `waiting` event when the model slot is
  taken, a failing `on_event` callback not failing the answer,
  disconnect mid-stream still saves the answer and releases `_chats_in_flight`, the plain mode is unchanged.
- Node tests: the SSE parser (split chunks, multi-line data, comments, a trailing partial event) and
  `progressLabel()` for every known kind and an unknown one.

### Verification

- Manual on the local server through the browser: the first line appears within about 2 s, lines change as
  tools run, the answer and trace look as before, an error (stop Ollama) shows today's message.
- Behind Cloudflare on the hosted copy: an answer longer than 100 s completes instead of being cut off.

## Left to the builder

- Whether the progress mapping lives in `main.py` or a new `app/chat_stream.py` (prefer the new module, given
  `main.py`'s size).
- The exact wording of each progress line, within the rules above.
- How `_prepare_chat()` / `_run_chat()` pass state (a small dataclass is fine).
- Documentation updates: `docs/api.md` (`POST /api/chat` streaming mode), CLAUDE.md (the route line and the
  "No streaming" notes in `app/main.py` and "Answer trace and Activity"), and removing "Stream answers and the
  trace" from `docs/future-enhancements.md` in favour of "Stream the answer text".

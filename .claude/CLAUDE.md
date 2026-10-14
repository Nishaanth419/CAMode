# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**CA Mode** is a local finance assistant with two specialized agents:
- **Finance Agent**: Answers general finance questions (budgeting, saving, concepts) — no account access
- **Portfolio Analyzer**: Answers questions about the user's INDmoney portfolio from a saved daily snapshot plus read-only MCP tools for live data

Several people can share one machine: each has a PIN-protected local profile that owns its chats, INDmoney connection, profile fields and portfolio snapshots. The app supports OpenAI and Groq, stores everything in local SQLite, and renders Plotly charts from portfolio data. It runs as a FastAPI backend with a vanilla JS PWA frontend.

## Architecture

### Backend modules
- **`app/main.py`**: FastAPI routes, INDmoney OAuth, and the lifespan task that runs the snapshot scheduler. `POST /api/chat` returns the full answer as plain text with the call trace in an `X-Agent-Trace` header (no streaming to the browser).
- **`app/db.py`**: `connect_db()` context manager (commits/rolls back, always closes), schema, and migrations. Tables: `users`, `oauth_sessions`, `chats`, `portfolio_snapshots`, `consents` (WAL mode). Columns added to `users` after release go in `_ADDED_USER_COLUMNS`. A pre-profile `chats` table is renamed `chats_legacy` and adopted on first sign-in.
- **`app/users.py`**: Profiles (scrypt PIN hashes, account-bound lockout), browser sessions bound to profiles, profile fields (`ProfileFields`), and per-profile INDmoney tokens (`user_access_token` refreshes them).
- **`app/privacy.py`**: The privacy notice (`NOTICE`, `POLICY_VERSION`), consent records, per-profile storage mode and snapshot retention, data export, and history clearing.
- **`app/chats.py`**: Chat storage behind one API: SQLite rows in `device` mode, process memory in `private` mode.
- **`app/snapshots.py`**: Daily capture of net worth/allocation/holdings, history queries, the polling scheduler, profile prompt text, and the agent's local snapshot tools.
- **`app/agent.py`**: Provider selection (OpenAI/Groq), request filtering, and the tool-calling loop (`stream_agent` yields `(event, data)` tuples; `run_agent` collects them into an answer plus trace).
- **`app/mcp_client.py`**: INDmoney MCP session setup, tool calls, and the read-only tool allowlist.

### Key Design Patterns

**Profiles and sessions** (`users.py`):
- The `indmoney_session` HttpOnly cookie maps to an `oauth_sessions` row whose `user_id` names the signed-in profile; the id rotates on sign-in
- Routes other than `/healthz`, `/api/providers`, `/api/session` and the sign-up/sign-in/sign-out routes return `401 {"profileRequired": true}` without a profile
- `check_pin()` reads the lock state from the DB and records the outcome in a `BEGIN IMMEDIATE` transaction, so concurrent wrong PINs cannot bypass the 5-attempt / 5-minute lock
- PIN hashing (scrypt N=2^17) runs via `asyncio.to_thread`; tests lower `users.SCRYPT_N`
- The PIN is a local lock, not network-grade auth; data is not encrypted at rest

**Consent and storage modes** (`privacy.py`, `chats.py`):
- A profile must accept the current `POLICY_VERSION` before `POST /api/chats`, `POST /api/chat`, `GET /api/tools`, `POST /api/snapshots/refresh` or `/auth/indmoney/connect`; otherwise `403 {"consentRequired": true}` (connect redirects home). Reading, deleting and exporting existing data stays allowed
- Every acceptance or settings change appends a `consents` row (version, mode, retention, time); the latest row decides `has_current_consent()`
- Bump `POLICY_VERSION` whenever `NOTICE` changes what is stored or shared: every profile is asked again and its scheduled snapshots pause until it accepts
- `storage_mode` is `device` or `private`. Private keeps chats in `chats._private_chats` (lost on sign-out, mode switch or restart), takes no snapshots, and switching to it deletes saved chats and snapshots. INDmoney tokens are still stored in both modes
- `snapshot_retention_days` (30/90/180/365) is capped by `SNAPSHOT_RETENTION_DAYS`; pruning runs on each capture and when the choice changes
- Export (`POST /api/users/me/export`) needs the PIN and omits the PIN hash and tokens

**Daily snapshots** (`snapshots.py`):
- `run_scheduler()` polls the wall clock every 5 minutes (macOS monotonic time pauses during sleep) and refreshes consenting `device`-mode profiles whose last capture predates the latest `SNAPSHOT_TIME` in `SNAPSHOT_TIMEZONE`; failures retry at most hourly (`users.snapshot_attempted_at`); a per-profile asyncio lock prevents overlapping runs
- `capture_snapshot()` calls `networth_snapshot`, `networth_allocation_breakdown`, `networth_holdings` on one shared client, keeps partial results, caches the filtered MCP tool list on the profile, and upserts one row per profile per local date
- `extract_networth_inr()` is a best-effort parse (INDmoney's format is undocumented); `networth_inr` is NULL when unclear
- Snapshots are SQLite rows, deliberately not a vector store (exact values, date ranges, sorting)

**Request filtering** (`agent.py`):
- `_blocked_request_reason()` refuses code requests (finance identifiers like "IFSC code" are exempt) and off-topic messages before any model call
- Follow-ups ("why?", "and also…") stay in scope only when the user's own recent messages were about finance
- `_requires_indmoney_data()` decides whether Portfolio Analyzer gets tools at all
- `_contains_code_output()` replaces code-shaped model output (except `chart` blocks) with the refusal notice

**Portfolio tools** (`agent.py`, `snapshots.py`):
- `PortfolioContext` carries the local `LocalTool`s (`get_portfolio_snapshot`, `get_networth_history`), the profile text, cached MCP tools, and an `on_mcp_tools` cache callback
- With cached MCP tools (< 24h), the MCP session opens lazily on the first MCP tool call; snapshot-only answers make no MCP calls
- A failing local tool returns an error tool result so the model can fall back to live tools; MCP failures abort the request
- Profile fields go into a labelled `<user_profile>` block in the portfolio system prompt as context, not instructions

**Tool allowlist** (`mcp_client.py`):
- `IMPORTANT_PORTFOLIO_TOOL_NAMES`: 15 documented read-only INDmoney tools
- `filter_portfolio_tools()` drops anything else the MCP server exposes, including write tools

**MCP session management** (`mcp_client.py`):
- `fetch_mcp_session()`: initialize → notifications/initialized → tools/list
- `_rpc_body()` parses JSON or `text/event-stream` bodies, skipping notifications and joining multi-line `data:` fields
- `call_mcp_tool()` accepts an optional shared `httpx.AsyncClient`

**Chat persistence** (`main.py`):
- Lives in `chats.py`; routes in `main.py` only call it
- Max 20 messages per conversation, max 12,000 chars per stored message, max 50 chats per profile (oldest evicted), in both storage modes
- Chats expire 30 days after their last message and are hidden/purged once expired
- Sessions and the cookie slide forward on each `/api/session` visit
- Disconnect revokes the INDmoney token and deletes snapshots and cached tools but keeps chats; deleting a profile cascades to everything, including consent records

### Frontend
- Static HTML/CSS/JS in `frontend/` (installable PWA)
- `index.html`: app shell, profile picker (`#profile-gate`, including the `#consent-form` privacy step), profile dialog (`<dialog id="profile-dialog">`, including the `#data-zone` privacy settings)
- `app.js`: profile sign-in/creation, privacy notice and storage choices, export and history clearing, profile dialog, chat UI, agent selection, INDmoney connect/disconnect, snapshot card with sparkline, Markdown rendering (links limited to http(s)/mailto), Plotly charts, theme toggle
- `sw.js`: service worker that serves the cached app shell offline (API data is always fetched live)
- Plotly is loaded from the CDN by a `<script>` tag in `index.html`
- Bump the `?v=` cache-busters in `index.html` and `CACHE_NAME` in `sw.js` when changing frontend assets

## Common Commands

### Development Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env   # add OPENAI_API_KEY or GROQ_API_KEY
```

### Run the App
```bash
# With Python (use the venv's uvicorn; restart after code changes)
uvicorn --env-file .env app.main:app --host 127.0.0.1 --port 8003

# With Docker
docker compose up --build -d
docker compose logs -f
docker compose down
```

### Testing & Code Checks
```bash
python -m pip install -r requirements-dev.txt

python -m pytest -q
python -m pytest tests/test_users.py -q
python -m pytest tests/test_agent.py::test_finance_agent_blocks_net_worth_code_requests_before_provider_call -q

python -m compileall -q app tests
node --check frontend/js/app.js

# GitHub Actions runs these two
python -m compileall -q app tests && python -m pytest -q
```

## Key Files & Patterns

### `app/main.py`
- OAuth routes: `GET /auth/indmoney/connect` (requires a profile), `GET /auth/indmoney/callback` (saves tokens on the profile and queues a first snapshot), `POST /auth/indmoney/disconnect`
- Privacy routes: `GET /api/privacy`, `POST /api/privacy/consent`, `POST /api/users/me/export`, `DELETE /api/users/me/history`
- Helpers `consent_required()` and `pin_error()` build the 403 and PIN-failure responses
- Profile routes: `GET /api/session` (current user, `consentRequired`, `storageMode`, profile list), `POST /api/users` (create + sign in), `POST /api/session/login`, `POST /api/session/logout`, `GET`/`PUT /api/profile`, `DELETE /api/users/me`
- Chat routes: `POST /api/chats`, `GET /api/chats`, `GET`/`DELETE /api/chats/{id}`, `POST /api/chat`
- Snapshot routes: `GET /api/snapshots?days=`, `POST /api/snapshots/refresh`
- Other: `GET /api/providers`, `GET /api/tools`, `GET /healthz`
- Helpers: `current_user(request)`, `profile_required()`, `auth_error()`, `lifespan()` (scheduler)
- Re-exports `connect_db`, `initialize_db`, `get_session`, `save_session`, `touch_session` for tests

### `app/users.py`
- `hash_pin()` / `verify_pin()` / `check_pin()`: scrypt hashes and the atomic lockout
- `create_user()`, `get_user()`, `list_profiles()`, `update_profile()`, `delete_user()`
- `get_session()`, `save_session()`, `touch_session()`, `session_user_id()`, `sign_in()` (rotates the id and adopts legacy data), `sign_out()`
- `get_indmoney()`, `save_indmoney()`, `clear_indmoney()`, `user_access_token()`, `cache_mcp_tools()`

### `app/privacy.py`
- `POLICY_VERSION`, `NOTICE`, `ConsentRequest`, `StaleNoticeError`
- `latest_consent()`, `has_current_consent()`, `privacy_state()`, `record_consent()`, `clear_history()`, `export_data()`

### `app/chats.py`
- `list_chats()`, `create_chat()`, `read_chat()`, `append_turn()`, `delete_chat()`, `purge_expired()`, `forget_private_chats()`, `export_chats()`

### `app/snapshots.py`
- `snapshots_allowed()`, `prune_snapshots()`
- `capture_snapshot()`, `refresh_user()`, `refresh_due_users()`, `due_user_ids()`, `run_scheduler()`
- `store_snapshot()`, `latest_snapshot()`, `networth_history()`, `extract_networth_inr()`, `latest_scheduled_time()`
- `snapshot_tools()`, `profile_context()`, `portfolio_context()` (builds the agent's `PortfolioContext`)

### `app/agent.py`
- `_blocked_request_reason()`, `_requires_indmoney_data()`, `_contains_code_output()`: request/answer filtering
- `LocalTool`, `PortfolioContext`: local tools and per-profile context for Portfolio Analyzer
- `stream_agent()` / `run_agent()`: OpenAI/Groq loop; retries once if the portfolio model skips tools; marks answers cut off by `finish_reason == "length"`
- `_fit_groq_context()`: trims history and tool results to `GROQ_INPUT_TOKEN_BUDGET`, always keeping the current question

### `app/mcp_client.py`
- `fetch_mcp_session()`, `call_mcp_tool()`, `filter_portfolio_tools()`

### Environment Variables
- `OPENAI_API_KEY` / `GROQ_API_KEY`: enable providers; `OPENAI_MODEL` / `GROQ_MODEL`, `OPENAI_BASE_URL` / `GROQ_BASE_URL`: overrides
- `AI_PROVIDER`: default provider (`openai` or `groq`); users can switch in the app
- `GROQ_INPUT_TOKEN_BUDGET`, `GROQ_COMPLETION_TOKEN_LIMIT`: Groq limits
- `MCP_SERVER_URL`: INDmoney MCP endpoint (default `https://mcp.indmoney.com/mcp`)
- `APP_ORIGIN`: public origin for the OAuth callback
- `SNAPSHOT_TIME`, `SNAPSHOT_TIMEZONE`, `SNAPSHOT_RETENTION_DAYS`, `SNAPSHOT_SCHEDULER_ENABLED`: snapshot job (defaults `09:00`, `Asia/Kolkata`, `365`, `true`; falls back to a fixed +05:30 offset when tz data is missing)
- `APP_DATA_DIR`: SQLite directory (default `.data/`); `LOG_LEVEL` (default `INFO`); `PORT` (Docker, default `8080`)

## Testing Strategy

Tests use mocked AI, MCP and OAuth responses (no live API calls). `conftest.py` points `app.db` at a temporary database per test, lowers the scrypt cost, disables the scheduler, and provides `client`, `signed_in` (a signed-in profile that accepted the notice in `device` mode) and `sign_up()` (pass `storage_mode=None` to skip consent, or `"private"`).

Test coverage:
- `test_api.py`: routes, OAuth flow, chats, profile-scoped access, snapshot endpoints
- `test_agent.py`: request filtering, Finance Agent and Portfolio Analyzer loops, snapshot tools, lazy MCP, Groq context fitting, error handling
- `test_users.py`: PIN hashing, sign-in, lockout (including concurrent guesses), session rotation, legacy adoption, profile isolation
- `test_snapshots.py`: net-worth parsing, IST schedule, capture, due-profile selection and backoff, scheduler, local tools, retention
- `test_privacy.py`: consent gate, stale notices, storage modes, private in-memory chats, export, history clearing, retention, scheduler exclusion, column migration
- `test_mcp_client.py`: MCP session init, event-stream parsing, tool calls, tool filtering

## Performance Notes

- SQLite in WAL mode handles concurrent requests; connections always close
- Snapshot-backed answers skip the MCP initialize, initialized and tools/list round trips
- Chat history trimmed to 20 messages to limit token usage
- Groq uses conservative context/completion limits (adjustable in `.env`)
- One pooled httpx client per agent run for MCP tool calls

## Debugging Tips

- Logs go to stdout at `LOG_LEVEL`; snapshot failures log `snapshot_refresh_failed`, `initial_snapshot_failed` or `manual_snapshot_failed`
- SQLite at `.data/codemode.sqlite3` holds live user data and tokens; inspect read-only and never use it as test scratch space (set `APP_DATA_DIR` to a temp folder)
- "Could not sign in (HTTP 404/405)" means the running server predates the frontend: restart it
- `ModuleNotFoundError: httpx` on startup means a global `uvicorn` was used instead of `.venv/bin/uvicorn`
- MCP errors (401, 500) include hints in the exception message; a 401 triggers one token refresh and retry

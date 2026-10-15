# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**CA Mode** is a local finance assistant with two specialized agents:
- **Finance Agent**: Answers general finance questions (budgeting, saving, concepts) — no account access
- **Portfolio Analyzer**: Answers questions about the user's INDmoney portfolio from a saved daily snapshot plus read-only MCP tools for live data

Several people can share one machine: each has a PIN-protected local profile that owns its chats, INDmoney connection, profile fields and portfolio snapshots. The app runs its model locally through Ollama by default (OpenAI and Groq are optional), stores everything in local SQLite, and renders Plotly charts from portfolio data. It runs as a FastAPI backend with a vanilla JS PWA frontend.

## Architecture

### Backend modules
- **`app/main.py`**: FastAPI routes, INDmoney OAuth, and the lifespan task that runs the snapshot scheduler. `POST /api/chat` returns the full answer as plain text with the call trace in an `X-Agent-Trace` header (no streaming to the browser).
- **`app/db.py`**: `connect_db()` context manager (commits/rolls back, always closes), schema, and migrations. Tables: `users`, `oauth_sessions`, `chats`, `portfolio_snapshots`, `consents`, `cas_statements`, `investigations` (WAL mode). Columns added to `users` after release go in `_ADDED_USER_COLUMNS`. A pre-profile `chats` table is renamed `chats_legacy` and adopted on first sign-in.
- **`app/users.py`**: Profiles (scrypt PIN hashes, account-bound lockout), browser sessions bound to profiles, profile fields (`ProfileFields`), and per-profile INDmoney tokens (`user_access_token` refreshes them).
- **`app/privacy.py`**: The privacy notice (`NOTICE`, `POLICY_VERSION`), consent records, per-profile storage mode and snapshot retention, data export, and history clearing.
- **`app/chats.py`**: Chat storage behind one API: SQLite rows in `device` mode, process memory in `private` mode.
- **`app/snapshots.py`**: Daily capture of net worth/allocation/holdings, history queries, the polling scheduler, profile prompt text, and the agent's local snapshot tools.
- **`app/agent.py`**: Provider selection (Ollama/OpenAI/Groq), request filtering, and the tool-calling loop (`stream_agent` yields `(event, data)` tuples; `run_agent` collects them into an answer plus trace).
- **`app/sealing.py`**: Per-profile X25519 keypairs, wrapping the private key with a PIN or recovery code (scrypt + SecretBox), recovery codes, and `ProfileKeys` (`seal()` / `open()`).
- **`app/unlock.py`**: Key creation, `unlock_with_pin()`, `recover()`, `replace_recovery_code()`, and the in-memory map of unlocked browser sessions (`activate()`, `keys_for_session()`).
- **`app/cas.py`**: CAS uploads. `parse_statement()` runs `casparser.read_cas_pdf` in memory (offline) on NSDL/CDSL (demat accounts by broker, MF folios, NPS) or CAMS/KFintech (MF folios) statements and keeps only holdings rows in INDmoney's `networth_holdings` layout, so `statement_metrics()` reuses `portfolio_math.compute_metrics()`. Never stored: the PDF, password, investor name, PAN, address, contact details, DP/client IDs. Statements are sealed in `cas_statements` (newest 12 kept)
- **`app/backup.py`**: Daily database backups (`backup_if_due()` from the scheduler loop, `create_backup()` via SQLite's online backup API with `journal_mode = DELETE` so each backup is one file, verified before it is kept), pruning to `BACKUP_KEEP`, `verify_backup()`, and `restore_backup()` (moves the current database aside). Never copies the master key. CLI: `python -m scripts.backup now|list|verify|restore`
- **`app/vault.py`**: Master key resolution (`APP_MASTER_KEY`, `APP_MASTER_KEY_FILE`, default `~/.config/ca-mode/master.key`) and Fernet `encrypt()` / `decrypt()` for secrets stored in SQLite.
- **`app/mcp_client.py`**: INDmoney MCP session setup, tool calls, and the read-only tool allowlist.
- **`app/investigation_math.py`**: Pure `Decimal` layer: `normalize_snapshot()` (`PortfolioSnapshot` / `HoldingSnapshot` with scope, capture time, currency, holdings coverage), `calculate_change()`, `calculate_portfolio_change()` (price and unit effects per holding), `calculate_allocation()`.
- **`app/investigation_checks.py`**: `run_checks()` returns `CheckResult`s (`passed`/`warning`/`failed`/`not_checked`, evidence, next step); tolerance from `INVESTIGATION_TOLERANCE_PCT` / `_INR`, staleness from `INVESTIGATION_STALE_HOURS`.
- **`app/investigations.py`**: The bounded portfolio-change workflow (`investigate_portfolio_change()`), sealed `investigations` rows (newest 50), `agent_payload()` and the `investigate_portfolio_change` `LocalTool`.

### Key Design Patterns

**Local model** (`agent.py`, `ollama/Modelfile`):
- `PROVIDERS["ollama"]` has no `key_env`: it is configured when `OLLAMA_MODEL` is set, is listed first and is the default `AI_PROVIDER`; the request goes to Ollama's OpenAI-compatible `/v1` API with a dummy key
- `provider_config()` returns `timeout` / `router_timeout` (`OLLAMA_TIMEOUT_SECONDS`, default 300, for Ollama; 120 s and `router.ROUTER_TIMEOUT_SECONDS` for cloud)
- Ollama's `/v1` API ignores per-request context options and silently truncates long prompts (4,096-token default on smaller Macs). `ollama/Modelfile` builds `ca-mode` (gpt-oss:20b, `num_ctx 32768`); after the first successful response of each answer, `_ollama_context_problem()` reads `/api/ps` and errors below `OLLAMA_MIN_CONTEXT` (16,384)
- Model choice and scores are in the README ("Use the local model"); re-run both evals with `--provider ollama` before changing it

**Profiles and sessions** (`users.py`):
- The `indmoney_session` HttpOnly cookie maps to an `oauth_sessions` row whose `user_id` names the signed-in profile; the id rotates on sign-in
- Routes other than `/healthz`, `/api/providers`, `/api/session` and the sign-up/sign-in/sign-out routes return `401 {"profileRequired": true}` without a profile
- `check_pin()` reads the lock state from the DB and records the outcome in a `BEGIN IMMEDIATE` transaction, so concurrent wrong PINs cannot bypass the 5-attempt / 5-minute lock
- PIN hashing (scrypt N=2^17) runs via `asyncio.to_thread`; tests lower `users.SCRYPT_N`
- The PIN is a local lock, not network-grade auth; chats, snapshots and profile fields are not encrypted at rest

**PIN-locked history** (`sealing.py`, `unlock.py`):
- Saved chat titles and messages, snapshot `data` (with the net worth inside; `networth_inr` is NULL) and `users.profile` are `sealed:v1:<sealed box>` under the profile's public key; plaintext rows (profiles that never unlocked since this feature) pass through `ProfileKeys.open()` and are sealed by `unlock.activate()` at the next PIN entry
- `users.history_key_pin` / `history_key_recovery` hold the private key wrapped as `scrypt$N$r$p$salt$box`; `history_public_key` is plain so the scheduler can seal without a PIN
- `main.current_user()` attaches `user["keys"]`; `history_gate()` returns `403 {"unlockRequired": true}` when this session has not entered the PIN (unlocked keys are per session id, memory only, so a restart locks everyone)
- Sign-up, sign-in and `/api/session/unlock` call `unlock.activate()`; responses carry `recoveryCode` only when one was just created. Recovery codes and PINs share the lockout via `users.check_with_lockout()`
- Export unwraps keys from the PIN, so it works while locked. Losing both the PIN and the recovery code loses the history; there is no admin reset

**Encrypted secrets** (`vault.py`, `users.py`):
- `users.indmoney` and `oauth_sessions.data` are stored as `enc:v1:<Fernet token>` via `_encode_secret()` / `_decode_secret()`; an empty dict stays a plain `{}`, so SQL can test "connected" with `indmoney <> '{}'` (never `json_extract` on these columns)
- `main.py` runs `vault.check_key()` and `users.encrypt_stored_secrets()` at startup: plaintext rows are encrypted, rows under another key are counted, logged as `stored_secrets_unreadable` and left untouched (they read as `{}`, so the profile shows as disconnected)
- The key lives outside `.data/`; Docker mounts `~/.config/ca-mode` so both run modes share it. Tests set a generated `APP_MASTER_KEY` in `conftest.py` and must never touch the real key file

**Consent and storage modes** (`privacy.py`, `chats.py`):
- A profile must accept the current `POLICY_VERSION` before `POST /api/chats`, `POST /api/chat`, `GET /api/tools`, `POST /api/snapshots/refresh` or `/auth/indmoney/connect`; otherwise `403 {"consentRequired": true}` (connect redirects home). Reading, deleting and exporting existing data stays allowed
- Every acceptance or settings change appends a `consents` row (version, mode, retention, time); the latest row decides `has_current_consent()`
- Bump `POLICY_VERSION` whenever `NOTICE` changes what is stored or shared: every profile is asked again and its scheduled snapshots pause until it accepts
- `storage_mode` is `device` or `private`. Private keeps chats in `chats._private_chats` (lost on sign-out, mode switch or restart), takes no snapshots, and switching to it deletes saved chats and snapshots. INDmoney tokens are still stored in both modes
- `snapshot_retention_days` (30/90/180/365) is capped by `SNAPSHOT_RETENTION_DAYS`; pruning runs on each capture and when the choice changes
- Export (`POST /api/users/me/export`) needs the PIN and omits the PIN hash and tokens

**Daily snapshots** (`snapshots.py`):
- `run_scheduler()` polls the wall clock every 5 minutes (macOS monotonic time pauses during sleep) and refreshes consenting `device`-mode profiles whose last capture predates the latest `SNAPSHOT_TIME` in `SNAPSHOT_TIMEZONE`; failures retry at most hourly (`users.snapshot_attempted_at`); a per-profile asyncio lock prevents overlapping runs
- `capture_snapshot()` calls `networth_snapshot`, then `networth_holdings` (which requires `asset_type`) for each held type from `holdings_types_to_fetch()` (every served type if the names don't match), up to 4 in parallel on one shared client. Results are keyed `networth_snapshot` and `networth_holdings:<TYPE>`; partial results are kept, the filtered MCP tool list is cached, and one row is upserted per profile per local date
- `extract_networth_inr()` is a best-effort parse (INDmoney's format is undocumented); `networth_inr` is NULL when unclear
- Snapshots are SQLite rows, deliberately not a vector store (exact values, date ranges, sorting)

**Portfolio numbers** (`portfolio_math.py`, `number_check.py`):
- `compute_metrics()` turns stored results into `figures` (totals, P&L over holdings with a cost basis, splits by asset type/class/market cap/broker, concentration, top holdings, USD kept separate, `data_notes`), all in `Decimal`. `get_portfolio_snapshot` returns these instead of raw text (raw only for old, unparseable snapshots); `include_all_holdings` returns every row in compact form to fit Groq's 6,000-char tool-result limit
- INDmoney traps: linked brokers report `invested_amount` 0 (excluded from P&L, noted); `holding_percent` is per asset type; US rows carry INR and USD
- The portfolio prompt says to quote `figures`, never compute. `agent._number_warning()` runs `number_check.unverified_numbers()` on every portfolio answer that used tools and appends `warning_note()` for amounts/percentages matching no tool number (counts, years, list numbering and links are skipped)
- `tests/fixtures/indmoney_sample.py` is synthetic data in the real response layout; `scripts/inspect_indmoney_shapes.py` prints a live response's layout with values removed; `scripts/eval_numbers.py` scores answers on the fixture

**Request routing** (`router.py`, `agent.py`):
- `_route_request()` labels every message `off_topic`, `code_request`, `general_finance` or `account_data` with one classifier call (`router.classify()`: forced `classify_request` tool call, enum validated in code, user text sent as a JSON object of untrusted data, last 3 user turns as context). `off_topic` and `code_request` are refused without calling the answer model
- The classifier uses `OPENAI_ROUTER_MODEL` / `GROQ_ROUTER_MODEL`, else the provider's chat model. Any failure (HTTP error, 429, timeout, no or unknown category) falls back to the keyword rules (`_keyword_category()` over `_blocked_request_reason()` and `_requires_indmoney_data()`); `REQUEST_ROUTER=keywords` skips the classifier entirely (tests set this in `conftest.py`)
- The route is the first trace step (`kind: "route"`, `source: "model" | "keywords"`)
- Portfolio Analyzer gets `get_cas_holdings` (`snapshots.cas_tool()`) only when the profile has uploaded a CAS; the prompt says to state the statement date and never add CAS figures to INDmoney's
- Portfolio Analyzer always gets its local snapshot tools; live INDmoney tools (and the must-call-a-tool retry) only for `account_data`
- Changing `ROUTER_PROMPT` means bumping `ROUTER_PROMPT_VERSION` and re-running `python -m scripts.eval_router --provider <p>` on `evals/request_routing.jsonl` (keyword baseline: 65.5%)
- `_contains_code_output()` replaces code-shaped model output (except `chart` blocks) with the refusal notice
- If Portfolio Analyzer was not given the live tools and the model writes a tool call as JSON text (`_looks_like_tool_call()`), the request is rerun once with `force_account_data=True` instead of showing the JSON

**Portfolio tools** (`agent.py`, `snapshots.py`):
- `PortfolioContext` carries the local `LocalTool`s (`get_portfolio_snapshot`, `get_networth_history`), the profile text, cached MCP tools, and an `on_mcp_tools` cache callback
- With cached MCP tools (< 24h), the MCP session opens lazily on the first MCP tool call; snapshot-only answers make no MCP calls
- A failing local tool returns an error tool result so the model can fall back to live tools; MCP failures abort the request
- Profile fields go into a labelled `<user_profile>` block in the portfolio system prompt as context, not instructions

**Portfolio change investigations** (`investigations.py`, `investigation_math.py`, `investigation_checks.py`):
- A coded workflow, not a model loop: latest snapshot → `snapshots.previous_snapshot()` (before it, on or before `baseline_date`) → normalise → changes → allocation → checks → `build_result()`. The model only narrates `agent_payload()`
- Missing is never zero: no earlier snapshot is `insufficient_data` (a failed `history_available` check), an asset type fetched in only one snapshot is `not_comparable`, a new holding has previous value 0 only because its type was fetched
- Holding identity is `asset_type|code|broker`; same-identity rows merge (reported as duplicates); a changed code is matched on asset type, normalised name and broker (`identifier_changed`)
- A finding is `verified` only when every check in its `dependsOn` passed; estimates (market effect) are `kind: "estimate"`. Shares of the change (`shareOfChangePct`) are computed so the model never derives them
- Bounded by `STEP_TIMEOUT_SECONDS` and `DEADLINE_SECONDS`; only snapshot reads retry, once, on `database is locked`; errors become safe messages (`_safe_error`), never raw exception text
- Status/times are plain columns, the record is sealed; runs still `running` after `INTERRUPTED_AFTER_MS` read as interrupted. Private mode does not persist; a save failure before start is `InvestigationStorageError` (503), after the run the result is returned with `saved: false` (200). The tool memoises results per request and keeps its payload under `MAX_AGENT_PAYLOAD_CHARS` (Groq)
- `main.chat` passes `investigation_tool(user, provider=...)` via `portfolio_context(extra_tools=...)` (investigations imports snapshots, so not the reverse). The tool result's `investigationId` becomes the trace step's `investigation_id`; `app.js` `attachInvestigationCard()` fetches the run by id to render the evidence card
- Before changing the portfolio prompt or `agent_payload()`, re-run `python -m scripts.eval_investigations --provider ollama --runs 3` (`evals/portfolio_change.jsonl`, graded in code; baseline in the README). It runs on a temp `APP_DATA_DIR`, injects scope/timeout faults, and stubs live INDmoney tools; `--regrade` re-scores an `--out` file without the model
- Deleted with history clearing, private mode, INDmoney disconnect and profile deletion; included in export
- Records carry `updatedAt` (the plain column) so clients can order copies; `list_investigations()` items add `durationMs` (unset while running or interrupted), `trigger`, `baselineDate`, `chatId` (`investigation_tool(user, chat_id=...)`, passed by `main.chat`), `period`, `error`, `findingCount`, `warningCount`

**Answer trace and Activity** (`agent.py`, `frontend/js/trace.js`, `pages.js`):
- `agent.redact_trace_text()` masks tokens, secrets, PINs, PAN and bearer/JWT strings in trace `arguments`, `result_preview` and `error` before the trace is sent or saved; `trace.js` `redactText()` repeats it for older saved chats
- `trace.js` (loaded after `finance.js`, exported for Node tests) renders the answer trace (`renderChatTrace()`), the evidence card (`renderInvestigationCard(record, {chart})`) and Activity rows; it formats figures but never computes them. A step still `running` in a finished trace or run shows as *Did not finish*
- `#/activity` lists `GET /api/investigations?limit=50` with client-side filters (All/Running/Completed/Failed; no approval state exists); `#/activity/<id>` shows one run. Lists are reconciled with `reconcileInvestigations()` (newer `updatedAt` wins, a finished run is never replaced by an older running copy) and out-of-order responses dropped with `createSequencer()`; polling (`pollDelay()`) runs only while a run is active and the page is visible
- No streaming: the trace arrives with the finished answer. "Run again" (failed/cancelled only) re-POSTs the same `baselineDate` on a click, never automatically

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
- `app.js`: profile sign-in/creation, privacy notice and storage choices, export and history clearing, profile dialog (including INDmoney connect/disconnect), chat UI, agent switch above the input, sidebar recent chats (newest 6), `refreshSnapshot()`, Markdown rendering (links limited to http(s)/mailto), Plotly charts, theme toggle. Fires `ca:session`, `ca:chats` and `ca:snapshot` document events for the pages
- `trace.js` (loaded after `finance.js`): answer trace timeline, investigation evidence card and Activity helpers (see "Answer trace and Activity")
- `finance.js` (loaded first): pure helpers shared by the pages and the rail and exported for Node tests: Indian money formatting, SIP/lumpsum/goal math, `goalProjection()`, `goalKind()`, `historyChange()`, `portfolioInsights()` (observations about the user's own holdings, never market news) and `incomeTax()` with `TAX_RULES` (FY 2026-27 slabs, 87A rebate and marginal relief, surcharge with marginal relief, 4% cess). Update `TAX_RULES`, its label and `tests/js/finance.test.js` after each budget
- `rail.js` (loaded last): the right rail's Financial Portfolio (net worth, change over the saved history, sparkline, Total Invested / Total Returns / Return % over holdings with a cost), Financial Goals (the 3 nearest target years; a row opens the goal editor, "+ Add a goal" below), Market Insights and Quick Tools cards; renders on `ca:session`, `ca:snapshot` and `ca:goals`, and clears on `ca:session-ended`. Hidden below 1280px like before
- Session events: `loadChats()` sets `isSessionReady = false` and fires `ca:session-ended` (pages and rail drop the previous profile's figures), then `ca:session` once the profile is signed in, unlocked and consenting. Data loaders in `pages.js` (`getPortfolio()`, `getHistory(days)`, `getGoals()`) share one cached request; a 401/403 from them triggers a single `reloadSession()`
- `pages.js` (loaded after `app.js`, uses its globals): hash routes `#/` (Home), `#/chat` (Ask AI), `#/activity` and `#/activity/<id>` (saved investigations), `#/portfolio`, `#/investments` (holdings table, CSV, CAS upload), `#/planning/<sip|lumpsum|goal|emi|tax>` (calculators run in the browser), `#/markets` (`#/markets/funds` focuses the fund form), `#/goals`, `#/reports`, `#/chats`; `navigate(route)` switches between the chat view and `#page-view`. Prompt buttons anywhere use `data-prompt` / `data-agent` (one delegated listener in `app.js`)
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
for file in frontend/js/*.js; do node --check "$file"; done
node --test tests/js/*.test.js

# GitHub Actions runs all of these
python -m compileall -q app tests && python -m pytest -q
```

## Key Files & Patterns

### `app/main.py`
- OAuth routes: `GET /auth/indmoney/connect` (requires a profile), `GET /auth/indmoney/callback` (saves tokens on the profile and queues a first snapshot), `POST /auth/indmoney/disconnect`
- Unlock routes: `POST /api/session/unlock`, `POST /api/session/recover`, `POST /api/users/me/recovery-code`; helper `history_gate()`
- Privacy routes: `GET /api/privacy`, `POST /api/privacy/consent`, `POST /api/users/me/export`, `DELETE /api/users/me/history`
- Helpers `consent_required()` and `pin_error()` build the 403 and PIN-failure responses
- Profile routes: `GET /api/session` (current user, `consentRequired`, `storageMode`, profile list), `POST /api/users` (create + sign in), `POST /api/session/login`, `POST /api/session/logout`, `GET`/`PUT /api/profile`, `DELETE /api/users/me`
- Chat routes: `POST /api/chats`, `GET /api/chats`, `GET`/`DELETE /api/chats/{id}`, `POST /api/chat`
- Snapshot routes: `GET /api/snapshots?days=`, `POST /api/snapshots/refresh`
- Investigation routes: `POST /api/investigations` (consent + unlocked; `201` + `Location` when saved, `200` when not), `GET /api/investigations?limit=`, `GET`/`DELETE /api/investigations/{id}`
- Backup routes: `GET /api/backups` (status), `POST /api/backups` (back up now)
- CAS routes: `GET /api/cas`, `POST /api/cas` (JSON `{password, pdfBase64}`; needs an unlocked session, consent, and `device` mode), `DELETE /api/cas/{id}`
- Page routes: `GET /api/portfolio` (latest snapshot's `figures` plus every holding via `portfolio_math.all_holdings()`, or `{available: false}`), `GET`/`PUT /api/goals` (replaces the goals list, max 10; each goal has an optional `kind` for its icon, inferred from the name when missing). Goals are added, edited and deleted in one `#goal-dialog` opened from the Goals page, the rail and the goal planner (`openGoalDialog()` in `pages.js`)
- Other: `GET /api/providers`, `GET /api/tools`, `GET /healthz`
- Helpers: `current_user(request)`, `profile_required()`, `auth_error()`, `lifespan()` (scheduler)
- Re-exports `connect_db`, `initialize_db`, `get_session`, `save_session`, `touch_session` for tests

### `app/users.py`
- `hash_pin()` / `verify_pin()` / `check_pin()` / `check_with_lockout()`: scrypt hashes and the atomic lockout shared by PINs and recovery codes
- `profile_fields(user, keys)`, `update_profile(user_id, fields, keys)`: profile fields are sealed; `ProfileFields.goals` (`Goal` list) is kept when an update leaves it unset, and goals reach the portfolio prompt through `snapshots.profile_context()`
- `create_user()`, `get_user()`, `list_profiles()`, `update_profile()`, `delete_user()`
- `get_session()`, `save_session()`, `touch_session()`, `session_user_id()`, `sign_in()` (rotates the id and adopts legacy data), `sign_out()`
- `get_indmoney()`, `save_indmoney()`, `clear_indmoney()`, `user_access_token()`, `cache_mcp_tools()`, `users_with_indmoney()`, `has_indmoney_tokens()`
- `encrypt_stored_secrets()`: startup migration of plaintext tokens and session data

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

### `app/vault.py`
- `check_key()`, `encrypt()`, `decrypt()`, `is_encrypted()`, `VaultError`

### `app/mcp_client.py`
- `fetch_mcp_session()`, `call_mcp_tool()`, `filter_portfolio_tools()`

### Environment Variables
- `OLLAMA_MODEL` (enables the local provider; `ca-mode`), `OLLAMA_BASE_URL` (default `http://localhost:11434/v1`; Compose sets `host.docker.internal`), `OLLAMA_TIMEOUT_SECONDS`, `OLLAMA_ROUTER_MODEL`
- `OPENAI_API_KEY` / `GROQ_API_KEY`: enable providers; `OPENAI_MODEL` / `GROQ_MODEL`, `OPENAI_BASE_URL` / `GROQ_BASE_URL`: overrides
- `AI_PROVIDER`: default provider (`ollama` by default, `openai` or `groq`); users can switch in the app
- `GROQ_INPUT_TOKEN_BUDGET`, `GROQ_COMPLETION_TOKEN_LIMIT`: Groq limits
- `REQUEST_ROUTER` (`model` default, or `keywords`), `OPENAI_ROUTER_MODEL` / `GROQ_ROUTER_MODEL`: request classifier
- `MCP_SERVER_URL`: INDmoney MCP endpoint (default `https://mcp.indmoney.com/mcp`)
- `APP_ORIGIN`: public origin for the OAuth callback
- `INVESTIGATION_TOLERANCE_PCT` (0.5), `INVESTIGATION_TOLERANCE_INR` (1), `INVESTIGATION_STALE_HOURS` (36): investigation checks
- `SNAPSHOT_TIME`, `SNAPSHOT_TIMEZONE`, `SNAPSHOT_RETENTION_DAYS`, `SNAPSHOT_SCHEDULER_ENABLED`: snapshot job (defaults `09:00`, `Asia/Kolkata`, `365`, `true`; falls back to a fixed +05:30 offset when tz data is missing)
- `APP_MASTER_KEY_FILE` / `APP_MASTER_KEY`: token encryption key (default file `~/.config/ca-mode/master.key`, created on first start)
- `BACKUP_DIR` (default `~/ca-mode-backups`), `BACKUP_KEEP` (14), `BACKUP_ENABLED` (true): daily backups; `conftest.py` points `BACKUP_DIR` at a temp folder
- `APP_DATA_DIR`: SQLite directory (default `.data/`); `LOG_LEVEL` (default `INFO`); `PORT` (Docker, default `8080`)

## Testing Strategy

Tests use mocked AI, MCP and OAuth responses (no live API calls). `conftest.py` points `app.db` at a temporary database per test, lowers the scrypt cost, disables the scheduler, and provides `client`, `signed_in` (a signed-in, unlocked profile that accepted the notice in `device` mode) and `sign_up()` (pass `storage_mode=None` to skip consent, or `"private"`).

Test coverage:
- `test_api.py`: routes, OAuth flow, chats, profile-scoped access, snapshot endpoints
- `test_agent.py`: request filtering, Finance Agent and Portfolio Analyzer loops, snapshot tools, lazy MCP, Groq context fitting, error handling
- `test_users.py`: PIN hashing, sign-in, lockout (including concurrent guesses), session rotation, legacy adoption, profile isolation
- `test_snapshots.py`: net-worth parsing, IST schedule, capture, due-profile selection and backoff, scheduler, local tools, retention
- `test_privacy.py`: consent gate, stale notices, storage modes, private in-memory chats, export, history clearing, retention, scheduler exclusion, column migration
- `test_unlock.py`: sealed storage, restart locking, per-session unlock, recovery and its lockout, new recovery codes, upgrading profiles from before encryption, export while locked
- `test_cas.py`: parsing NSDL/CDSL and CAMS statements built from `casparser`'s own models (personal details dropped), error messages, upload/list/delete API and gates, retention, the agent tool, clearing and export
- `test_investigation_math.py`, `test_investigation_checks.py`: change maths (zero/missing bases, duplicates, changed codes, coverage, allocation bases) and every check, including the asset-type `holding_percent` denominator trap; `tests/fixtures/snapshot_pairs.py` builds self-consistent snapshot pairs
- `test_investigations.py`: API, sealed persistence, gates, profile isolation, private mode, privacy deletion paths, deadline, lock retry, safe errors, storage failures, interruption, cancellation, the agent tool and its trace link
- `test_eval_investigations.py`: every eval fixture yields its expected investigation status and flagged checks; the grader's arithmetic, unsupported-claim, honesty and refusal rules on canned answers
- `test_backup.py`: backup file permissions and consistency, pruning, daily schedule, verify, restore (and refusing corrupt backups), API, CLI
- `test_vault.py`: master key loading and creation, token and session encryption, startup migration, wrong-key handling
- `test_portfolio_math.py`: computed figures on the fixture, P&L exclusions, broker split, USD separation, notes, Indian number formatting, holdings types to fetch
- `test_number_check.py`: number matching (rounding, lakh/crore, negatives, skipped counts/years), the warning on portfolio answers
- `test_router.py`: classifier routing in model mode, forced tool call and request shape, router model override, fallbacks to keywords
- `test_mcp_client.py`: MCP session init, event-stream parsing, tool calls, tool filtering
- `tests/js/trace.test.js` (`node --test`): status labels and tones, step ordering and investigation links, details disclosure, missing fields and unknown kinds, evidence card sections, Activity filters, retry rules, reconciliation of duplicate/out-of-order/refreshed lists, redaction and escaping
- `tests/js/finance.test.js` (`node --test`): formatting, growth and goal math, goal icons, history change, snapshot insights, income tax for both regimes including rebates, surcharge and marginal relief

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

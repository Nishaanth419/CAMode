# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**CA Mode** is a local finance assistant with two specialized agents:
- **Finance Agent**: Answers general finance questions (budgeting, saving, concepts) — no account access
- **Portfolio Analyzer**: Answers questions about the user's INDmoney portfolio from a saved daily snapshot plus read-only MCP tools for live data

Several people can share one machine: each has a PIN-protected local profile that owns its chats, INDmoney connection, profile fields and portfolio snapshots. With `AUTH_MODE=google` each person instead signs in with their own Google account (one account per profile, no profile list) and the PIN only unlocks their sealed history. The app runs its model locally through Ollama by default (OpenAI and Groq are optional), stores everything in local SQLite, and renders Plotly charts from portfolio data. It runs as a FastAPI backend with a vanilla JS PWA frontend.

## Architecture

### Backend modules
- **`app/main.py`**: FastAPI routes, INDmoney OAuth, and the lifespan task that runs the snapshot scheduler. `POST /api/chat` returns the full answer as plain text with the call trace in an `X-Agent-Trace` header (no streaming to the browser).
- **`app/db.py`**: `connect_db()` context manager (commits/rolls back, always closes), schema, and migrations. Tables: `users`, `accounts`, `oauth_sessions`, `chats`, `portfolio_snapshots`, `consents`, `cas_statements`, `investigations`, `agent_runs`, `financial_goals`, `financial_decisions`, `goal_allocations`, `page_views`, `question_counts` (WAL mode). Columns added to `users` after release go in `_ADDED_USER_COLUMNS`; the `users` columns live in `_USERS_COLUMNS` (shared with `_drop_unique_display_name()`, which rebuilds an older table whose `display_name` was UNIQUE, foreign keys off, in one transaction). A pre-profile `chats` table is renamed `chats_legacy` and adopted on first sign-in.
- **`app/users.py`**: Profiles (scrypt PIN hashes, account-bound lockout), browser sessions bound to profiles, profile fields (`ProfileFields`), and per-profile INDmoney tokens (`user_access_token` refreshes them).
- **`app/accounts.py`**: `AUTH_MODE` (`local` default, `google`), `check_config()` (startup), Google OpenID Connect (`start_sign_in()`, `finish_sign_in()`, `id_token_claims()`, `check_claims()`), the pending Google account on a session (`pending_values()`, `pending_identity()`), and the `accounts` table (`user_for_identity()`, `link()`, `is_linked()`, `account_for_user()`, `unclaimed_profile_named()`).
- **`app/admin.py`**: The operator's admin page (`ADMIN_EMAILS`; every admin path 404s without it): admin Google sign-in on its own `ca_admin` cookie and unbound session row (`is_signing_in()` picks the shared `/auth/google/callback` by `state`, `finish_sign_in()`, `admin_for_session()` re-checks the address and `SESSION_SECONDS` on every request), `overview()` / `profiles()` (only unsealed columns: name, Google email, plan, today's questions, consent, INDmoney connected, snapshot failure, last seen, lockout), `clear_lockout()`, `sign_out_everywhere()`. `check_config()` needs a Google client in either `AUTH_MODE`
- **`app/privacy.py`**: The privacy notice (`NOTICE`, `POLICY_VERSION`), consent records, per-profile storage mode and snapshot retention, data export, and history clearing.
- **`app/chats.py`**: Chat storage behind one API: SQLite rows in `device` mode, process memory in `private` mode.
- **`app/snapshots.py`**: Daily capture of net worth/allocation/holdings, history queries, the polling scheduler, profile prompt text, and the agent's local snapshot tools.
- **`app/agent.py`**: Provider selection (Ollama/OpenAI/Groq), request filtering, and the tool-calling loop (`stream_agent` yields `(event, data)` tuples; `run_agent` collects them into an answer, its trace, its recorded events and its task-contract verdict).
- **`app/agent_events.py`**: `RunRecorder` (turns stream events into the run's event log: `seq`, `atMs`, camelCase keys, tool arguments/results/errors redacted and capped, the answer reduced to its length), `summarize()` (call counts, failed tool calls, retries, token totals, termination) and `redact_trace_text()` (re-exported by `agent`).
- **`app/tool_results.py`**: Tool failure kinds (`invalid_arguments`, `rejected`, `unavailable`, `auth`, `failed`), `error_payload()` (`{error, errorKind, next_step}` for the model), `reported_error()` (a local tool's top-level `{"error"}`) and `argument_problems()` (missing required arguments and values outside an `enum`; types are deliberately not checked).
- **`app/agent_runs.py`**: Saved answer runs: `save_run()` (one sealed row per chat answer or failed attempt in `device` mode: question start, contract verdict, events, summary; never fails the answer), `list_runs()`, `get_run()`, `all_runs()` (export), `delete_run()`, `delete_chat_runs()`. Newest `KEEP_RUNS` (200), none older than the chat lifetime
- **`app/tasks.py`**: Task contracts: `contract_for(category, agent_type, offered_tools)` (`refusal`, `general_finance`, `goal_calculation`, `account_data`: required and allowed tools, whether a clarifying question counts, observed call budgets), `plan_contract()` (`multi_step`: one part per kind of task, each part's required tools checked on its own), `evaluate()`, `facts_from_events()`, `is_account_tool()`.
- **`app/planner.py`**: Planned answers for messages with several kinds of task: budgets (`MAX_PLAN_STEPS` 4, `MAX_ADDED_STEPS` 2, `MAX_REPLANS` 2, `MAX_TOOL_CALLS` 8, `MAX_MODEL_CALLS` 6, `STEP_TIMEOUT_SECONDS`, `DEADLINE_SECONDS`), the `submit_plan` / `add_plan_steps` tools and prompts, `check_plan()` / `check_steps()` (offered tool, `tool_results.argument_problems()`, `after` naming only earlier steps, no repeated call, fix a rejected call once, never retry an unavailable one, at least one account step), `repaired_call()` (an Ollama "error parsing tool call" 500 whose quoted text needs only stray trailing characters removed or brackets closed), `PlanState` (`ready()`, `missing()`), `parts_covered()`. Pure; `agent` runs it.
- **`app/sealing.py`**: Per-profile X25519 keypairs, wrapping the private key with a PIN or recovery code (scrypt + SecretBox), recovery codes, and `ProfileKeys` (`seal()` / `open()`).
- **`app/unlock.py`**: Key creation, `unlock_with_pin()`, `recover()`, `replace_recovery_code()`, and the in-memory map of unlocked browser sessions (`activate()`, `keys_for_session()`).
- **`app/cas.py`**: CAS uploads. `parse_statement()` runs `casparser.read_cas_pdf` in memory (offline) on NSDL/CDSL (demat accounts by broker, MF folios, NPS) or CAMS/KFintech (MF folios) statements and keeps only holdings rows in INDmoney's `networth_holdings` layout, so `statement_metrics()` reuses `portfolio_math.compute_metrics()`. Never stored: the PDF, password, investor name, PAN, address, contact details, DP/client IDs. Statements are sealed in `cas_statements` (newest 12 kept)
- **`app/backup.py`**: Daily database backups (`backup_if_due()` from the scheduler loop, `create_backup()` via SQLite's online backup API with `journal_mode = DELETE` so each backup is one file, verified before it is kept), pruning to `BACKUP_KEEP`, `verify_backup()`, and `restore_backup()` (moves the current database aside). Never copies the master key. CLI: `python -m scripts.backup now|list|verify|restore`
- **`app/vault.py`**: Master key resolution (`APP_MASTER_KEY`, `APP_MASTER_KEY_FILE`, default `~/.config/ca-mode/master.key`) and Fernet `encrypt()` / `decrypt()` for secrets stored in SQLite.
- **`app/charts.py`**: Candlestick charts in answers filled from the OHLC tool result, and chart data kept out of the model's history.
- **`app/mcp_client.py`**: INDmoney MCP session setup, tool calls, and the read-only tool allowlist.
- **`app/investigation_math.py`**: Pure `Decimal` layer: `normalize_snapshot()` (`PortfolioSnapshot` / `HoldingSnapshot` with scope, capture time, currency, holdings coverage), `calculate_change()`, `calculate_portfolio_change()` (price and unit effects per holding), `calculate_allocation()`.
- **`app/investigation_checks.py`**: `run_checks()` returns `CheckResult`s (`passed`/`warning`/`failed`/`not_checked`, evidence, next step); tolerance from `INVESTIGATION_TOLERANCE_PCT` / `_INR`, staleness from `INVESTIGATION_STALE_HOURS`.
- **`app/memory.py`**: User-confirmed context: `GoalInput` / `DecisionInput`, sealed CRUD on `financial_goals` / `financial_decisions` (max 10 / 50, counted in a `BEGIN IMMEDIATE` insert), `migrate_profile_goals()` (called by `unlock.activate()`), `export_context()`, `clear_saved_context()`.
- **`app/goal_planning.py`**: Pure `Decimal` goal maths: `months_until()`, `future_value()`, `required_monthly()`, `plan_goal()` (status, gap, progress, scenarios with a `basis`, inflation, `feasibility`, `alternatives`, assumptions, warnings; `PlanningError` for invalid inputs), `date_after_years()`, `add_months()`, `follow_ups()`.
- **`app/goal_suitability.py`**: Pure rules as data: `CATEGORIES`, `evaluate()` (verdict per investment category from horizon, risk, loss capacity, liquidity, flexibility; cautious values for missing inputs), `classify_holding()` / `review_holding()`, and `FUND_DATA` (describes the fund figures in `fund_data.py`; still no `shortlist`).
- **`app/fund_data.py`**: INDmoney mutual fund figures for a goal's suitable categories: `INDMONEY_CATEGORIES` (slug → `CATEGORIES` id), `parse_category()` / `parse_scheme()` / `parse_benchmarks()` (`Decimal`, each metric `{value, asOf, status, source}`, `missing` with reasons), `fetch_categories()` (live MCP, in-memory cache), `build_comparison()`, `goal_comparison()`, `tool_payload()` and `fund_comparison_tool()` (`get_goal_fund_comparison`).
- **`app/goal_portfolio.py`**: Goals joined with snapshot holdings: `holding_key()`, `load_portfolio()`, `goal_view()`, `goals_overview()`, `allocations_view()`, `assign()` and `goal_tools()` (`get_goal_plans`, `plan_goal_what_if`).
- **`app/context.py`**: `retrieve_context()` picks the saved goals, decisions and preferences relevant to one chat request (keyword rules) and renders the prompt text; `with_plan()`, `observed_sources()`, `task_parts()` (the kinds of tool-backed task in a message, by clause: `account_data` and `goal_calculation`, or `()` when there is only one).
- **`app/public_pages.py`**: Pages served without a profile: `app_shell(origin)` (index.html with `{{origin}}` replaced, escaped, for absolute `og:*` URLs; file read once), `privacy_page()` (renders `privacy.NOTICE`, dated from `POLICY_VERSION`), `pricing_page()` (renders `plans.PLANS`; buttons are `mailto:` the contact, else a note), `not_found_page()`, `admin_page()` (noindex, filled by `js/admin.js` + `css/admin.css`), `robots_txt()` (disallows `/api/`, `/auth/`, `/admin`), `sitemap_xml()` (only `INDEXABLE_PATHS` = `/privacy`, `/pricing`; the app shell is `noindex` because the sign-in screen lists profile names). `main.http_error()` serves the HTML 404 for unknown GET/HEAD pages and leaves `/api/` and `/auth/` on FastAPI's JSON errors
- **`app/page_views.py`**: Anonymous page-view counts: `page_views(day, route, views)` rows only (no profile, session, IP or browser), `Route` literal matching `pages.js` `ROUTE_META`, `record()` (upsert + prune past `RETENTION_DAYS` 90), `counts()`, `totals()`, `is_enabled()` (`PAGE_VIEWS_ENABLED`). Counted only when `snapshots.snapshots_allowed(user)`; not in export, history clearing or profile deletion (not linked to a profile). CLI: `python -m scripts.page_views [--days N] [--daily]`
- **`app/plans.py`**: Plans as data (`PLANS`: `free`, `individual`, `team`; prices shown on `/pricing`, `PRICES_UPDATED`), `plan_of()` (unknown values count as Free), `has_daily_cap()` (Free only), `usage()` (`plan`, `planName`, `limit`, `used`, `remaining`, `resetsAt` for `/api/session`), `set_plan()`, `free_daily_questions()` (this copy's cap, else `FREE_DAILY_QUESTIONS` 5), `contact_email()` (`PRICING_CONTACT_EMAIL`). No checkout: `python -m scripts.plans list|set <name, Google email or id> <plan>`
- **`app/limits.py`**: Usage limits for a shared copy, each off unless configured: `reserve_question()` / `release_question()` (`DAILY_QUESTION_LIMIT` per Free-plan profile per local day; `capped=False` for paid plans in `SNAPSHOT_TIMEZONE`, `question_counts` rows, `BEGIN IMMEDIATE`, today and yesterday kept, `DailyLimitReached`), `check_rate()` (`RATE_LIMITS_ENABLED`; in-memory `RateLimiter` sliding windows per IP and per profile from `RATE_RULES`: `chat`, `sign_up`, `sign_in` (login, unlock, recover), `investigation`; `RateLimited`), `client_ip()` / `client_address()` (`CF-Connecting-IP` only with `TRUSTED_PROXY=cloudflare`; IPv6 grouped by /64), `turnstile_site_key()` / `verify_turnstile()` (both `TURNSTILE_*` keys set; fails closed with `TurnstileUnavailable`)
- **`app/investigations.py`**: The bounded portfolio-change workflow (`investigate_portfolio_change()`), sealed `investigations` rows (newest 50), `agent_payload()` and the `investigate_portfolio_change` `LocalTool`.

### Key Design Patterns

**Local model** (`agent.py`, `ollama/Modelfile`):
- `PROVIDERS["ollama"]` has no `key_env`: it is configured when `OLLAMA_MODEL` is set, is listed first and is the default `AI_PROVIDER`; the request goes to Ollama's OpenAI-compatible `/v1` API with a dummy key
- `provider_config()` returns `timeout` / `router_timeout` (`OLLAMA_TIMEOUT_SECONDS`, default 300, for Ollama; 120 s and `router.ROUTER_TIMEOUT_SECONDS` for cloud)
- Ollama's `/v1` API ignores per-request context options and silently truncates long prompts (4,096-token default on smaller Macs). `ollama/Modelfile` builds `ca-mode` (gpt-oss:20b, `num_ctx 32768`); after the first successful response of each answer, `_ollama_context_problem()` reads `/api/ps` and errors below `OLLAMA_MIN_CONTEXT` (16,384)
- Model choice and scores are in the README ("Use the local model"); re-run both evals with `--provider ollama` before changing it
- Hosted setup (`deploy/`, README "Host on a server"): the app and cloudflared run on a server behind Cloudflare Access; Ollama stays on the Mac, shared with `tailscale serve --tcp 11434` and reached as `ollama.internal` (`extra_hosts`), because Ollama bound to loopback returns 403 for a Tailscale name or IP in the `Host` header. Model and classifier calls use a `router.CONNECT_TIMEOUT_SECONDS` (10 s) connect limit, so a sleeping Mac fails fast with a plain message

**Profiles and sessions** (`users.py`):
- The `indmoney_session` HttpOnly cookie maps to an `oauth_sessions` row whose `user_id` names the signed-in profile; the id rotates on sign-in
- Routes other than `/healthz`, `/api/providers`, `/api/session`, the sign-up/sign-in/claim/sign-out routes, `/auth/google/*` and the admin routes return `401 {"profileRequired": true}` without a profile; admin routes need an admin session instead (`401 {"adminRequired": true}`)
- `check_pin()` reads the lock state from the DB and records the outcome in a `BEGIN IMMEDIATE` transaction, so concurrent wrong PINs cannot bypass the 5-attempt / 5-minute lock
- PIN hashing (scrypt N=2^17) runs via `asyncio.to_thread`; tests lower `users.SCRYPT_N`
- The PIN is a local lock, not network-grade auth; anywhere other people can reach the app, use `AUTH_MODE=google` (below)
- `users.create_user(unique_name=)`: the picker refuses a name in use (checked in `BEGIN IMMEDIATE`); Google mode allows repeats, since refusing would reveal that the name exists

**Account sign-in** (`accounts.py`, `AUTH_MODE=google`):
- Needs `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` (`accounts.check_config()` fails startup otherwise) and `APP_ORIGIN`; redirect URI is `APP_ORIGIN + /auth/google/callback`
- `GET /auth/google/start` stores `state`, `nonce`, PKCE verifier and start time under `accounts.SIGN_IN_KEY` on the session (a fresh session id when not signed in) and redirects to Google with `prompt=select_account`. The callback exchanges the code with the client secret; the ID token comes from the token endpoint over TLS, so its claims (issuer, audience/azp, exp/iat with `CLOCK_SKEW_SECONDS`, nonce, verified email) are checked, not its signature. Failures redirect to `/?signInError=cancelled|expired|failed` (`app.js` `SIGN_IN_ERRORS`)
- A linked account signs in with `users.sign_in()` (rotated id, history still locked until `/api/session/unlock`). An unlinked one gets a fresh unbound session holding `accounts.PENDING_KEY` (`PENDING_SECONDS`, 30 min) until `POST /api/users` (create + link) or `POST /api/account/claim` (name + PIN of a profile no account has claimed; one generic `CLAIM_FAILED` answer, `accounts.spend_pin_check()` for unknown names, wrong PINs count towards that profile's lockout; `ALLOW_PROFILE_CLAIMS=false` returns 403)
- `main.signed_in_user_id()` (used by `current_user()`, `/api/session`, logout and the INDmoney routes) treats a session bound to a profile without an `accounts` row as signed out in Google mode. `/api/session` returns `profiles: []`, `authMode` and `account`; `POST /api/session/login` is 404; `/api/session/recover` resets only the signed-in profile
- `accounts.email` is vault-encrypted; `accounts.subject` (Google `sub`) is plain for lookup and never exported. The link cascades with the profile; export has `account` (`provider`, `email`, times). `POLICY_VERSION` 2026-10-21.1 added the notice section on Google sign-in; 2026-10-22.1 added the stored plan; 2026-10-22.2 added what the admin page shows

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
- A profile must accept the current `POLICY_VERSION` before `POST /api/chats`, `POST /api/chat`, `POST`/`PUT` on goals and decisions, `GET /api/tools`, `POST /api/snapshots/refresh` or `/auth/indmoney/connect`; otherwise `403 {"consentRequired": true}` (connect redirects home). Reading, deleting and exporting existing data stays allowed
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
- The portfolio prompt says to quote `figures`, never compute. `agent._number_warning()` runs `number_check.unverified_numbers()` on every answer that used tools (either agent) or was goal arithmetic, and appends `warning_note()` for amounts/percentages matching no tool number, the message or the saved context (counts, years, list numbering and links are skipped); goal answers get the `GOAL_SOURCE` wording
- `tests/fixtures/indmoney_sample.py` is synthetic data in the real response layout; `scripts/inspect_indmoney_shapes.py` prints a live response's layout with values removed; `scripts/eval_numbers.py` scores answers on the fixture

**Request routing** (`router.py`, `agent.py`):
- `_route_request()` labels every message `off_topic`, `code_request`, `general_finance` or `account_data` with one classifier call (`router.classify()`: forced `classify_request` tool call, enum validated in code, user text sent as a JSON object of untrusted data, last 3 user turns as context). `off_topic` and `code_request` are refused without calling the answer model
- The classifier uses `OPENAI_ROUTER_MODEL` / `GROQ_ROUTER_MODEL`, else the provider's chat model. Any failure (HTTP error, 429, timeout, no or unknown category) falls back to the keyword rules (`_keyword_category()` over `_blocked_request_reason()` and `_requires_indmoney_data()`); `REQUEST_ROUTER=keywords` skips the classifier entirely (tests set this in `conftest.py`)
- The route is the first trace step (`kind: "route"`, `source: "model" | "keywords"`)
- Portfolio Analyzer gets `get_cas_holdings` (`snapshots.cas_tool()`) only when the profile has uploaded a CAS; the prompt says to state the statement date and never add CAS figures to INDmoney's
- Portfolio Analyzer always gets its local snapshot tools; live INDmoney tools (and the must-call-a-tool retry) only for `account_data`
- Changing `ROUTER_PROMPT` means bumping `ROUTER_PROMPT_VERSION` and re-running `python -m scripts.eval_router --provider <p>` on `evals/request_routing.jsonl` (64 cases; keyword baseline 41/64, 64.1%; `ca-mode` 60/64 on every run)
- The Ollama classifier call sets `temperature: 0`; at the model's default of 1 it occasionally labelled a price-chart request `general_finance`, so Portfolio Analyzer got no live tools. Cloud providers get no temperature because some reasoning models reject one
- `_contains_code_output()` replaces code-shaped model output (except `chart` blocks) with the refusal notice
- If Portfolio Analyzer was not given the live tools and the model writes a tool call as JSON text (`_looks_like_tool_call()`), the request is rerun once with `force_account_data=True` instead of showing the JSON

**Portfolio tools** (`agent.py`, `snapshots.py`):
- `PortfolioContext` carries the local `LocalTool`s (`get_portfolio_snapshot`, `get_networth_history`), the profile text, cached MCP tools, and an `on_mcp_tools` cache callback
- With cached MCP tools (< 24h), the MCP session opens lazily on the first MCP tool call; snapshot-only answers make no MCP calls
- A failing local tool returns an error tool result pointing to the live tools only when they are offered. An MCP result flagged `isError` (e.g. an argument outside the tool's enum) raises `mcp_client.McpToolError` and goes back to the model as an error tool result with a `next_step`, so it can correct the call; it still counts as a tool call. Other INDmoney failures (transport, HTTP 5xx, JSON-RPC errors, a session that cannot be opened) also go back as an error tool result telling the model not to call again (a failed session is tried once per answer). Only `mcp_client.McpAuthError` (HTTP 401, or error text that reads as an auth failure) ends the request; `run_agent` raises `AgentRunError(auth_failed=True)` and `main.is_indmoney_auth_error()` checks that flag, never the message text, before refreshing the token
- Tool arguments that are not a JSON object are refused with an error tool result (the tool is not run on `{}`); the trace keeps the model's raw text. Provider timeouts and errors with empty messages (`httpx.ReadTimeout`) become a readable error and are logged (`provider_timeout`, `agent_run_failed`); `run_agent` never returns an empty answer
- Profile fields go into a labelled `<user_profile>` block in the portfolio system prompt as context, not instructions

**Goal planning, suitability and allocations** (`goal_planning.py`, `goal_suitability.py`, `goal_portfolio.py`, `memory.py`):
- Goals can be `status: "draft"` (only a name required); an active goal needs currency, target amount and a date or `horizonYears` (converted to `targetDate` on save, stored with `targetDateFrom: "horizon"`). `custom` is an input alias of `other`; `financial_independence` is a kind. Suitability inputs (`flexibility`, `liquidityNeed`, `riskTolerance`, `lossCapacity`) and `inflationPct` are optional and stay `None` when not given
- `plan_goal(goal, today, mapped_amount=)` keeps every older key and adds `effectiveTarget`/`inflation` (the entered target is today's money), `startingAmount` (saved + mapped), `totalFutureContributions`, `feasibility` and `alternatives` (kind × basis: `zero_return` always, `assumed_return` only with a saved assumption). `follow_ups()` lists unanswered questions; nothing is guessed. An assumption above `HIGH_RETURN_PCT` (12) adds a warning, and `goal_suitability.assumption_warning()` (added by `goal_view()` and `/api/goals/plan`) questions any assumed return when every equity category is unsuitable
- Suitability is fixed rules, never the model: equity is unsuitable under 36 months, a fixed goal with low loss capacity rules out equity at any horizon, missing inputs take the cautious value and are listed in `assumed`. Suitability stays category level; scheme figures come only from `fund_data.py` (below), and there is no `shortlist` anywhere
- `goal_allocations` holds one sealed row per profile: `{holdingKey: {name, assetType, broker, goals: {goalId: pct}, generalWealthPct}}`, holding key `asset_type|code|broker` (same as `investigation_math` holding ids). `memory.set_allocation()` runs in `BEGIN IMMEDIATE`; `AllocationInput` caps the total at 100. Shares are valued from the latest snapshot rounded down, so a rupee counts toward at most one goal; a holding missing from the snapshot is `missing` (value unknown, not counted); only INR holdings count toward INR goals. Deleting a goal, `DELETE /api/context` and INDmoney disconnect release allocations; export has `goalAllocations`
- `get_goal_plans` and `plan_goal_what_if` (read-only, nothing saved, payload under 5,500 chars) go to both agents, only for profiles with a goal. `calculate_goal_plan` (`goal_portfolio.goal_calculator_tool()` / `calculate_goal()`: a goal described in chat, validated by `GoalInput`, reads and saves nothing; blank arguments count as not given, currency symbols and case map to codes, and a suitability input that is not exactly one of its choices is left out (cautious value) and listed in `ignoredInputs` instead of failing the call; `main.chat` passes the user's own words (`context.request_text()`) as `user_text`, and a call without a currency is planned in INR, with `currencyNote` saying so, when they use ₹, Rs, lakh or crore and name no other currency) goes to both agents when `context.wants_goal_calculator()` matches (goal words, or an amount with a time span). Whenever a goal tool is offered the prompt gets `agent.GOAL_TOOL_RULES`; an answer with its own figures and no tool call is retried once with `GOAL_CALCULATOR_NUDGE`, then flagged. The Finance Agent gets them through `agent.FINANCE_LOCAL_TOOL_NAMES` and never any snapshot, profile or MCP tool; it sees only a count of assigned holdings, Portfolio Analyzer also their values (`retrieve_context(include_holdings=)`)
- Routes: `GET`/`PUT /api/goals/allocations` (declared before `/api/goals/{goal_id}`); `GET /api/goals` also returns `fundData` and `portfolio`; `POST /api/goals/plan` also returns `suitability` and `followUps`, and with `goalId` counts that goal's assigned holdings (`goal_portfolio.counted_value()`) so the editor preview matches the saved card. `finance.js` `goalStatus()` shows `on_track` with `feasibility.status == "depends_on_returns"` as "On track if returns hold"

**Fund figures** (`fund_data.py`, layout in `tests/fixtures/indmoney_funds.py`, recorded with `scripts/inspect_indmoney_shapes.py --funds --labels`):
- `get_mf_by_category` rows: `id`, `name`, `category` ("flexi cap", the slug with spaces), `risk` ("Very High Risk"), `expense_ratio`, `return_1yr/3yr/5yr`, `nav_date` ("15 Oct 2026"), `launch_date` (often ""), plus INDmoney's `rating` / `ind_ranking` (always dropped). `get_mf_funds_details` (`data[].fund_id`, `data[].data.fund_detail`) has `benchmark_name` and no date anywhere, so only the benchmark is taken from it
- Returns are dated by `nav_date`; status `current` only when dated and within `FUND_STALE_DAYS` (5), else `stale`; no readable or a future date is `undated`. Expense ratio, risk and benchmark are always `undated` with `retrievedAt`. The frontend (`finance.js` `fundMetricNote()`) never shows an undated figure as current
- Missing is never zero: a return of exactly 0 (INDmoney's "no figure"), a period longer than the scheme has existed (`launch_date`), a return outside (-100, 1000], an expense ratio outside 0.01–3 are `missing` with the reason
- Only slugs in `INDMONEY_CATEGORIES` are fetched; a row is kept only when its own `category` maps to the category asked for, else it is `excluded` with the reason. Listing call: `sort_key: "aum"`, descending, `FUNDS_PER_CATEGORY` (5); results sorted by name. Unsuitable categories are never fetched (`unsuitableCategories` lists them)
- Nothing is stored (no POLICY_VERSION change): `_cache` keyed by (profile, category) for `CACHE_SECONDS` (6 h), cleared on INDmoney disconnect; failed listings are not cached
- `GET /api/goals/{id}/funds?category=` (consent + unlocked; 422 for an unknown or unsuitable category). `get_goal_fund_comparison` goes to Portfolio Analyzer only, for profiles with a goal; the prompt then gets `agent.FUND_COMPARISON_RULES` instead of `NO_FUND_DATA_RULE` (Finance Agent always gets the latter). Quoted figures pass `_number_warning` because they are in the tool result
- Goals page: "Show fund figures" in each goal card (`fundBlock()` / `fundComparisonHtml()` in `pages.js`), every figure with its date and source beside it

**Goals and saved context** (`memory.py`, `goal_planning.py`, `context.py`):
- Confirmed context (goals, decisions, profile fields) is written only by explicit user actions and carries `source: "user_entered"` plus created/updated times; observed data stays in snapshots and CAS rows; nothing the model infers is stored (`tests/test_memory.py::test_chats_never_write_saved_context`)
- Goals: `name`, `kind`, required `currency` (INR/USD/EUR/GBP, never converted or summed across currencies), `targetAmount`, `targetDate`, `savedAmount`, `monthlyContribution`, optional `assumedReturnPct` (0–30; none means only the no-growth scenario), optional `notes`. Plans are computed on every read (`context.with_plan()`), never stored
- `plan_goal()` mirrors `finance.js` (start-of-month contributions, monthly compounding); scenarios are no growth plus the assumption ±2 points; `requiredMonthly` rounds up, `additionalMonthly` is the extra over the planned contribution; missing target/date/currency is `insufficient_data`, not a guess
- `retrieve_context()` matches the message alone first and adds the last 2 user turns only for follow-ups (`is_follow_up()`); goals named (or by kind) else all goals for goal-ish questions; decisions linked to chosen goals, sharing a word, or all for "what did I decide"; preferences with goals or risk/allocation questions. Capped at `MAX_CONTEXT_CHARS`
- `main.chat` passes it as `run_agent(..., saved_context=)` to both agents: `<saved_context>` block plus `SAVED_CONTEXT_RULES` (quote calculated figures, never recompute, projections are illustrations, unknown means unknown); a `context` trace step lists the items; `_number_warning` accepts figures from it. Without saved context the prompts are unchanged. `PortfolioContext.profile_context` now holds only `snapshots.snapshot_context()`
- Old goals inside `users.profile` (`targetInr`, `targetYear`) move to `financial_goals` on unlock with `targetDate` = 31 Dec and a `migratedFrom` note; `ProfileFields` has no `goals`, and the stored profile carries `updatedAt`
- Goal and decision create/update need consent; reads and deletes do not. `DELETE /api/context` removes goals, decisions and profile fields (not chats/snapshots). Export includes goals and decisions; profile deletion cascades

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
- `#/activity` also lists saved answer runs (`GET /api/runs?limit=50`, loaded beside investigations; a failure there never hides them) with `trace.js` `renderRunList()`; `#/activity/answer-<id>` shows one with `renderRunCard()` (the question, the contract's checks, counts and tokens, and every recorded step via `runSteps()`). An answer's trace links there when the chat message has a `runId` (`X-Agent-Run` for a new answer)
- `#/activity` lists `GET /api/investigations?limit=50` with client-side filters (All/Running/Completed/Failed; no approval state exists); `#/activity/<id>` shows one run. Lists are reconciled with `reconcileInvestigations()` (newer `updatedAt` wins, a finished run is never replaced by an older running copy) and out-of-order responses dropped with `createSequencer()`; polling (`pollDelay()`) runs only while a run is active and the page is visible
- `agent.bounded_trace()` caps a trace at `MAX_TRACE_STEPS` (12): the first 3 steps, an `{"kind": "omitted", "count": n}` step, and the last 8, so the final answer or failure always shows; `trace.js` renders it as "n steps not shown" and counts it in the total
- Recorded events (`agent_events`): `route`, `context`, `contract`, `model_call`/`model_result` (status, finish reason, tool names, prompt/completion tokens), `tool_call`/`tool_result` (`errorKind`), `retry` (`no_account_tool`, `goal_calculator`, `invalid_tool_call`, `rerun_with_live_tools`), `validation` (`code_output`, `numbers`, `length`, `task_contract`), `plan` (`stage`, `outcome`, `steps` or `reason`) and `plan_stop` (`reason`, `missing`) in planned runs, whose `tool_call`/`tool_result` also carry the plan `step`, `answer`, `error` (with a `code` such as `provider_timeout`, `unknown_tool`, `tool_call_limit`) and `termination` (`answered`, `refused`, `failed`, `auth_failed`). The trace adds `retry` and `check` steps and model `tokens`; a refusal gets no contract step
- Task contracts observe and never change an answer, with one rule enforced: an account-data question is satisfied only by an account tool (`tasks.is_account_tool`: snapshot, history, CAS, investigation, fund comparison or any INDmoney tool), so the goal calculator alone triggers the `no_account_tool` retry. INDmoney calls with a missing required argument or a value outside the schema's `enum` are refused before the call (`invalid_arguments`); a local tool returning `{"error"}` is recorded as `rejected`
- `scripts/eval_tasks.py` grades task handling from the recorded events on `evals/agent_tasks.jsonl` (21 cases; `--dataset evals/multi_step.jsonl` for planned answers, also graded on plan events, `plan_stop`, tool order, repeated calls and budgets). Scripted mode (default, offline, also run by `tests/test_eval_tasks.py`) replays each case's model replies and checks the contract, verdict, termination and code, retries, tool error kinds and number check; `--provider ollama --runs 3` runs the 8 `live` cases on the real model with the real calculator, the synthetic snapshot and stubbed INDmoney behaviours, graded on contract, traced figures, expected figures, clarifying questions, invented amounts, rejected calls and model calls. Re-run it before changing the agent loop, the goal tools or the contracts; a new case needs `expect` (and optionally `live.grade`)
- No streaming: the trace arrives with the finished answer. "Run again" (failed/cancelled only) re-POSTs the same `baselineDate` on a click, never automatically

**Planned answers** (`planner.py`, `agent._planned_answer()`):
- Only for Portfolio Analyzer, an `account_data` route, a goal tool offered and `context.task_parts(message)` naming both kinds (e.g. "why did my portfolio drop and is my house goal still on track"); `main.chat` passes it as `run_agent(..., plan_parts=)`. Everything else, including the `force_account_data` rerun, stays on the ordinary loop
- One forced `submit_plan` call (only that tool offered; the offered tools listed as data in the system prompt via `planner.tool_catalogue()`); a plan failing `check_plan()` or a failed planner call is a `plan` event with `outcome: "rejected"` and the reason, then the ordinary loop answers under the ordinary contract (a second `contract` event)
- The local model's habits are accepted, not rejected: `name`/`tool_name` for `tool`, `args`/`parameters` for `arguments`, `after` by position ("step1"); an unrecognised `after` ("0", a tool name) waits for every earlier step (arguments are fixed at planning, so `after` only orders). A reference to the step itself or a later one is still refused
- Steps whose `after` steps have finished run together (`asyncio.gather`, each under `STEP_TIMEOUT_SECONDS`; a timeout is `unavailable`); `_ToolRunner` serves both paths and opens the INDmoney session once under a lock. An auth failure ends the answer as before (`plan_stop` `error`, then `indmoney_auth`)
- Then one call offering only `add_plan_steps` (auto): a plain answer ends the run; added steps (at most 2, renamed `r1-…`) go through `check_steps()`; after `MAX_REPLANS`, a rejected addition or another tool, a last call with no tools answers with `planner.final_prompt()` naming what is missing. Past `DEADLINE_SECONDS` or `MAX_TOOL_CALLS` no new step starts
- `plan_stop` reasons: `complete` (every part has a succeeded tool), `clarify` (a part is not covered and the answer asks a question), `partial`, `budget`, `deadline`, `error`. The answer goes through `_guarded_answer()` (code output, charts, number check with `goal_maths`, length) like any other
- Re-run `python -m scripts.eval_tasks --dataset evals/multi_step.jsonl` (15 scripted cases; also in the test suite) and `--provider ollama --runs 3` (4 live cases) before changing the planner, its prompts or budgets

**Charts from tool data** (`charts.py`):
- `ground_candlestick_charts()` (run on every answer in `stream_agent`) replaces each candlestick block with the latest `get_indian_stocks_ohlc` result as compact `ohlc` rows `[label, open, high, low, close]`, titled with the tool's instrument name. Candles the model wrote (or elided) are always replaced; with no OHLC candles the block becomes `CANDLES_UNAVAILABLE_NOTE`. The prompt still asks the model for full candles: telling it to write a title-only block instead raised unsupported claims in the investigation eval (9/60 vs 4/60 answers), so that change was dropped
- `ohlc_candles()` searches the parsed result (including a JSON string under `result`) for rows with open/high/low/close and a date, dropping impossible rows
- `strip_chart_blocks()` replaces chart blocks in assistant history before it reaches the model; `app.js` `chartCandles()` reads both `candles` and `ohlc`, and an undrawable chart block shows a note instead of a blank space

**Tool allowlist** (`mcp_client.py`):
- `IMPORTANT_PORTFOLIO_TOOL_NAMES`: 15 documented read-only INDmoney tools
- `filter_portfolio_tools()` drops anything else the MCP server exposes, including write tools

**MCP session management** (`mcp_client.py`):
- `fetch_mcp_session()`: initialize → notifications/initialized → tools/list
- `_rpc_body()` parses JSON or `text/event-stream` bodies, skipping notifications and joining multi-line `data:` fields
- `call_mcp_tool()` accepts an optional shared `httpx.AsyncClient`

**Chat persistence** (`main.py`):
- Lives in `chats.py`; routes in `main.py` only call it
- Max 20 messages per conversation, max 12,000 chars per stored question and 24,000 per stored answer (`chats.MAX_ANSWER_CHARS`, room for a year of candles), max 50 chats per profile (oldest evicted), in both storage modes
- Chats expire 30 days after their last message and are hidden/purged once expired
- Sessions and the cookie slide forward on each `/api/session` visit
- Disconnect revokes the INDmoney token and deletes snapshots and cached tools but keeps chats; deleting a profile cascades to everything, including consent records

### Frontend
- Static HTML/CSS/JS in `frontend/` (installable PWA)
- `index.html`: app shell, profile picker (`#profile-gate`: the picker, `#google-sign-in`, create and `#profile-claim-form`, unlock, recovery, and the `#consent-form` privacy step), profile dialog (`<dialog id="profile-dialog">`, including the `#data-zone` privacy settings)
- `app.js`: profile sign-in/creation (`authMode` from `/api/session`: the picker, or `showAccountGate()` for Google sign-in, create and claim), privacy notice and storage choices, export and history clearing, profile dialog (including INDmoney connect/disconnect), chat UI, agent switch above the input, sidebar recent chats (newest 6), `refreshSnapshot()`, Markdown rendering (links limited to http(s)/mailto), Plotly charts, theme toggle. Fires `ca:session`, `ca:chats` and `ca:snapshot` document events for the pages
- `trace.js` (loaded after `finance.js`): answer trace timeline, investigation evidence card and Activity helpers (see "Answer trace and Activity")
- `finance.js` (loaded first): pure helpers shared by the pages and the rail and exported for Node tests: Indian money formatting, SIP/lumpsum/goal math, `goalKind()`, `historyChange()`, `portfolioInsights()` (observations about the user's own holdings, never market news) and goal formatting (`amount()`, `amountShort()`, `totalsByCurrency()`, `goalStatus()`, `baseScenario()`; plans come from the server) and `incomeTax()` with `TAX_RULES` (FY 2026-27 slabs, 87A rebate and marginal relief, surcharge with marginal relief, 4% cess). Update `TAX_RULES`, its label and `tests/js/finance.test.js` after each budget
- `rail.js` (loaded last): the right rail's Financial Portfolio (net worth, change over the saved history, sparkline, Total Invested / Total Returns / Return % over holdings with a cost), Financial Goals (the 3 nearest target years; a row opens the goal editor, "+ Add a goal" below), Market Insights and Quick Tools cards; renders on `ca:session`, `ca:snapshot` and `ca:goals`, and clears on `ca:session-ended`. Hidden below 1280px like before
- Session events: `loadChats()` sets `isSessionReady = false` and fires `ca:session-ended` (pages and rail drop the previous profile's figures), then `ca:session` once the profile is signed in, unlocked and consenting. Data loaders in `pages.js` (`getPortfolio()`, `getHistory(days)`, `getGoals()`) share one cached request; a 401/403 from them triggers a single `reloadSession()`
- `pages.js` (loaded after `app.js`, uses its globals): hash routes `#/` (Home), `#/chat` (Ask AI), `#/activity` and `#/activity/<id>` (saved investigations), `#/portfolio`, `#/investments` (holdings table, CSV, CAS upload), `#/planning/<sip|lumpsum|goal|emi|tax>` (calculators run in the browser), `#/markets` (`#/markets/funds` focuses the fund form), `#/goals`, `#/reports`, `#/chats`; `navigate(route)` switches between the chat view and `#page-view`. Prompt buttons anywhere use `data-prompt` / `data-agent` (one delegated listener in `app.js`)
- `sw.js`: service worker that serves the cached app shell offline (API data is always fetched live)
- `pages.js` `countPageView()` POSTs `/api/page-views` once per route change after sign-in (skipped in private mode, never retried); `ROUTE_META` sets `document.title` and the meta description per hash route; an unknown hash shows the `not-found` page section instead of falling back to Home
- Icons: `icon.svg` (source), `favicon.ico`, `apple-touch-icon.png`, `icon-192/512.png`, `icon-maskable-512.png` and `og-image.jpg` are generated by `python -m scripts.build_icons` (macOS `sips`) from `frontend/icon.svg` and `assets/brand/*.svg`; commit the outputs
- `css/public.css` styles `/privacy` and the 404 page (system light/dark, no app shell)
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
python -m scripts.eval_tasks   # task contracts on scripted replies, offline

# GitHub Actions runs all of these
python -m compileall -q app tests && python -m pytest -q
```

## Key Files & Patterns

### `app/main.py`
- OAuth routes: `GET /auth/indmoney/connect` (requires a profile), `GET /auth/indmoney/callback` (saves tokens on the profile and queues a first snapshot), `POST /auth/indmoney/disconnect`
- Unlock routes: `POST /api/session/unlock`, `POST /api/session/recover`, `POST /api/users/me/recovery-code`; helper `history_gate()`
- Privacy routes: `GET /api/privacy`, `POST /api/privacy/consent`, `POST /api/users/me/export`, `DELETE /api/users/me/history`
- Helpers `consent_required()` and `pin_error()` build the 403 and PIN-failure responses
- Profile routes: `GET /api/session` (current user, `consentRequired`, `storageMode`, `usage`, profile list), `POST /api/users` (create + sign in), `POST /api/session/login`, `POST /api/session/logout`, `GET`/`PUT /api/profile`, `DELETE /api/users/me`
- Chat routes: `POST /api/chats`, `GET /api/chats`, `GET`/`DELETE /api/chats/{id}`, `POST /api/chat`
- Snapshot routes: `GET /api/snapshots?days=`, `POST /api/snapshots/refresh`
- Answer run routes: `GET /api/runs?limit=` (summaries), `GET`/`DELETE /api/runs/{id}` (unlocked). `POST /api/chat` saves the run and returns its id in `X-Agent-Run` (and `runId` in a 502 body); the chat's assistant message carries `runId`. Runs go with their chat, history clearing, private mode, INDmoney disconnect and profile deletion, and are in the export as `agentRuns`
- Investigation routes: `POST /api/investigations` (consent + unlocked; `201` + `Location` when saved, `200` when not), `GET /api/investigations?limit=`, `GET`/`DELETE /api/investigations/{id}`
- Backup routes: `GET /api/backups` (status), `POST /api/backups` (back up now)
- CAS routes: `GET /api/cas`, `POST /api/cas` (JSON `{password, pdfBase64}`; needs an unlocked session, consent, and `device` mode), `DELETE /api/cas/{id}`
- Page routes: `GET /api/portfolio` (latest snapshot's `figures` plus every holding via `portfolio_math.all_holdings()`, or `{available: false}`)
- Goal routes: `GET`/`POST /api/goals`, `GET`/`PUT`/`DELETE /api/goals/{id}` (each with `plan`), `POST /api/goals/plan` (stateless preview, optional `annualReturnPcts`), `GET /api/goals/{id}/funds` (INDmoney fund figures, `?category=`), `GET`/`POST /api/decisions`, `PUT`/`DELETE /api/decisions/{id}`, `GET`/`DELETE /api/context`. Goals are added, edited and deleted in one `#goal-dialog` opened from the Goals page, the rail and the goal planner (`openGoalDialog()` in `pages.js`); its preview calls `/api/goals/plan`. The Goals page's "What the assistant can use" panel lists and deletes saved context
- Other: `GET /api/providers`, `GET /api/tools`, `GET /healthz`
- Admin routes (`main.current_admin()` raises 404 when admin is off, `admin_required()` 401): `GET /admin`, `GET /auth/admin/start` (the shared Google callback hands an admin sign-in to `finish_admin_sign_in()` and redirects to `/admin` or `/admin?error=`), `GET /api/admin/session`, `POST /api/admin/sign-out`, `GET /api/admin/overview`, `PUT /api/admin/profiles/{id}/plan`, `POST /api/admin/profiles/{id}/clear-lockout`, `POST /api/admin/profiles/{id}/sign-out`, `DELETE /api/admin/profiles/{id}` (`{confirmName}`; shares `remove_profile()` with `DELETE /api/users/me`), `POST /api/admin/backups`. Each action logs `admin_*` with the admin's email
- Usage limits: `main.rate_limited(request, group, profile_id)` (429 `{error, rateLimited, retryAfterSeconds}` + `Retry-After`) on `POST /api/chat`, `POST /api/users`, `/api/session/login`, `/unlock`, `/recover` and `POST /api/investigations`; `POST /api/chat` reserves a daily slot before `run_agent` (Free plan only) and releases it unless an answer is returned (429 `{error, dailyLimitReached, limit, resetsAt, retryAfterSeconds, pricingUrl}`, shown by `app.js` as a plain assistant message with a link to the plans; a counted answer carries `X-Questions-Remaining`, shown in the profile dialog's plan row); `POST /api/users` checks Turnstile after the name and PIN pass (`400`/`503` `{turnstileFailed: true}`); `GET /api/session` adds `turnstileSiteKey` only when Turnstile is on, and `app.js` then renders the widget in `#create-turnstile`
- Page views: `POST /api/page-views` (`{route}`; `200 {counted}`, `false` without consent, in private mode or when disabled; `422` for an unknown page)
- Public pages: `GET /` and `/index.html` (app shell via `public_pages.app_shell()`), `GET /privacy`, `GET /pricing`, `GET /robots.txt`, `GET /sitemap.xml`
- Helpers: `current_user(request)`, `profile_required()`, `auth_error()`, `lifespan()` (scheduler)
- Re-exports `connect_db`, `initialize_db`, `get_session`, `save_session`, `touch_session` for tests

### `app/users.py`
- `hash_pin()` / `verify_pin()` / `check_pin()` / `check_with_lockout()`: scrypt hashes and the atomic lockout shared by PINs and recovery codes
- `profile_fields(user, keys)` (without `PROFILE_META_KEYS`), `stored_profile()`, `profile_updated_at()`, `update_profile(user_id, fields, keys)`: profile fields are sealed and stamped with `updatedAt`; they reach the prompts through `context.retrieve_context()`
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
- `snapshot_tools()`, `snapshot_context()`, `portfolio_context()` (builds the agent's `PortfolioContext`)

### `app/agent.py`
- `_blocked_request_reason()`, `_requires_indmoney_data()`, `_contains_code_output()`: request/answer filtering
- `LocalTool`, `PortfolioContext`: local tools and per-profile context for Portfolio Analyzer
- `stream_agent()` / `run_agent()`: OpenAI/Groq loop; retries once if the portfolio model skips tools; marks answers cut off by `finish_reason == "length"`
- `_fit_groq_context()`: trims history and tool results to `GROQ_INPUT_TOKEN_BUDGET`, always keeping the current question

### `app/vault.py`
- `check_key()`, `encrypt()`, `decrypt()`, `is_encrypted()`, `VaultError`

### `app/mcp_client.py`
- `fetch_mcp_session()`, `call_mcp_tool()`, `filter_portfolio_tools()`
- `McpToolError` (the tool reported an error the model can fix) and `McpAuthError` (HTTP 401 or auth-failure text: refresh the token)

### Environment Variables
- `OLLAMA_MODEL` (enables the local provider; `ca-mode`), `OLLAMA_BASE_URL` (default `http://localhost:11434/v1`; Compose sets `host.docker.internal`), `OLLAMA_TIMEOUT_SECONDS`, `OLLAMA_ROUTER_MODEL`
- `OPENAI_API_KEY` / `GROQ_API_KEY`: enable providers; `OPENAI_MODEL` / `GROQ_MODEL`, `OPENAI_BASE_URL` / `GROQ_BASE_URL`: overrides
- `AI_PROVIDER`: default provider (`ollama` by default, `openai` or `groq`); users can switch in the app
- `GROQ_INPUT_TOKEN_BUDGET`, `GROQ_COMPLETION_TOKEN_LIMIT`: Groq limits
- `REQUEST_ROUTER` (`model` default, or `keywords`), `OPENAI_ROUTER_MODEL` / `GROQ_ROUTER_MODEL`: request classifier
- `MCP_SERVER_URL`: INDmoney MCP endpoint (default `https://mcp.indmoney.com/mcp`)
- `APP_ORIGIN`: public origin for the OAuth callbacks (INDmoney and Google)
- `AUTH_MODE` (`local` default, or `google`), `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `ALLOW_PROFILE_CLAIMS` (true): sign-in
- `ADMIN_EMAILS`: comma-separated Google accounts allowed into `/admin` (unset: admin off)
- `FUND_STALE_DAYS` (5): days after a NAV date before fund returns are marked old
- `PLAN_STEP_TIMEOUT_SECONDS` (60), `PLAN_DEADLINE_SECONDS` (240): planned answers
- `INVESTIGATION_TOLERANCE_PCT` (0.5), `INVESTIGATION_TOLERANCE_INR` (1), `INVESTIGATION_STALE_HOURS` (36): investigation checks
- `SNAPSHOT_TIME`, `SNAPSHOT_TIMEZONE`, `SNAPSHOT_RETENTION_DAYS`, `SNAPSHOT_SCHEDULER_ENABLED`: snapshot job (defaults `09:00`, `Asia/Kolkata`, `365`, `true`; falls back to a fixed +05:30 offset when tz data is missing)
- `APP_MASTER_KEY_FILE` / `APP_MASTER_KEY`: token encryption key (default file `~/.config/ca-mode/master.key`, created on first start)
- `BACKUP_DIR` (default `~/ca-mode-backups`), `BACKUP_KEEP` (14), `BACKUP_ENABLED` (true): daily backups; `conftest.py` points `BACKUP_DIR` at a temp folder
- `PAGE_VIEWS_ENABLED` (true): anonymous local page-view counts
- `PRICING_CONTACT_EMAIL`: address behind the `/pricing` buttons (unset or invalid: no buttons)
- `DAILY_QUESTION_LIMIT` (unset: unlimited; Free plan only, 5 on the hosted copy), `RATE_LIMITS_ENABLED` (false), `TRUSTED_PROXY` (`cloudflare` behind a Tunnel only), `TURNSTILE_SITE_KEY` + `TURNSTILE_SECRET_KEY`: usage limits for a shared copy (`limits.py`); `conftest.py` clears them and the in-memory limiter for every test
- `APP_DATA_DIR`: SQLite directory (default `.data/`); `LOG_LEVEL` (default `INFO`); `PORT` (Docker, default `8080`)

## Testing Strategy

Tests use mocked AI, MCP and OAuth responses (no live API calls). `conftest.py` points `app.db` at a temporary database per test, lowers the scrypt cost, disables the scheduler, and provides `client`, `signed_in` (a signed-in, unlocked profile that accepted the notice in `device` mode) and `sign_up()` (pass `storage_mode=None` to skip consent, or `"private"`).

Test coverage:
- `test_api.py`: routes, OAuth flow, chats, profile-scoped access, snapshot endpoints; Google sign-in (`google_mode` fixture, `google_sign_in()` with a fake token endpoint): no profile list, PKCE/state/nonce, create, locked return, repeated names, claims and their lockout, pending expiry, callback failures, deletion, export, recovery, local mode unchanged
- `test_agent.py`: request filtering, Finance Agent and Portfolio Analyzer loops, snapshot tools, lazy MCP, Groq context fitting, error handling
- `test_users.py`: PIN hashing, sign-in, lockout (including concurrent guesses), session rotation, legacy adoption, profile isolation, Google ID token claim checks, `AUTH_MODE` config, the unique-name table rebuild
- `test_snapshots.py`: net-worth parsing, IST schedule, capture, due-profile selection and backoff, scheduler, local tools, retention
- `test_privacy.py`: consent gate, stale notices, storage modes, private in-memory chats, export, history clearing, retention, scheduler exclusion, column migration
- `test_unlock.py`: sealed storage, restart locking, per-session unlock, recovery and its lockout, new recovery codes, upgrading profiles from before encryption, export while locked
- `test_cas.py`: parsing NSDL/CDSL and CAMS statements built from `casparser`'s own models (personal details dropped), error messages, upload/list/delete API and gates, retention, the agent tool, clearing and export
- `test_investigation_math.py`, `test_investigation_checks.py`: change maths (zero/missing bases, duplicates, changed codes, coverage, allocation bases) and every check, including the asset-type `holding_percent` denominator trap; `tests/fixtures/snapshot_pairs.py` builds self-consistent snapshot pairs
- `test_agent_runs.py`: runs saved from real agent runs (scripted provider), sealing, failed runs, private mode, unlock and profile isolation, deletion with the chat and every history path, export, pruning by count and age, redaction, a database error never failing the answer
- `test_eval_tasks.py`: every scripted task case behaves as `evals/agent_tasks.jsonl` expects, unique ids, scripted replies, the live grader
- `test_planner.py`: which messages are planned, plan and added-step checks (unknown tools, enums, order, repeats, fix once, unavailable), `multi_step` contracts, parallel steps, deadline, tool budget, step timeouts, the number check on planned answers, plan events in the trace and recorder
- `test_tasks.py`: contracts per route/agent/tools, account tools, evaluation (required tools, clarification, numbers, budgets, allowed tools), tool argument checks and error payloads, the event recorder (numbering, timing, redaction, no answer text) and summaries
- `test_investigations.py`: API, sealed persistence, gates, profile isolation, private mode, privacy deletion paths, deadline, lock retry, safe errors, storage failures, interruption, cancellation, the agent tool and its trace link
- `test_eval_investigations.py`: every eval fixture yields its expected investigation status and flagged checks; the grader's arithmetic, unsupported-claim, honesty and refusal rules on canned answers
- `test_goal_planning.py`: months left, future value and required contribution (matching `finance.js`), scenarios, statuses, rounding, missing inputs, invalid assumptions, currencies, inflation, feasibility, alternatives, follow-up questions
- `test_goal_suitability.py`: the suitability rules, cautious defaults, table integrity, holding classification and review, `FUND_DATA`
- `test_fund_data.py`: parsing in `Decimal`, rankings dropped, zero/young/implausible values missing, stale/undated/future dates, date formats, category mapping against INDmoney's slug enum, excluded rows, fetching and caching, failed listings and details, the API route and its gates, the agent tool with the number check, chat wiring and prompt rules
- `test_goal_portfolio.py`: allocations and their gates, valuation and rounding, missing and stale holdings, cleanup paths, concurrent assignments, the agent tools and chat wiring
- `test_memory.py`: goal/decision CRUD, sealing, validation, limits, access rules (no profile, locked, no consent, other profiles), context inspection and clearing, export, cascade, the old-goal move, chats never writing context
- `test_context.py`: goal/decision/preference selection, follow-ups, rendered sections, currencies, size cap, Finance Agent receiving context
- `test_limits.py`: limits off by default, the daily cap (429 body, per profile, failed answers released, local-midnight reset, pruning, deletion with the profile), the sliding-window limiter (sweeps, key cap), per-IP and per-profile limits on chat, sign-up, sign-in/unlock/recover and investigations, `CF-Connecting-IP` only behind a trusted proxy, IPv6 grouping, Turnstile (site key exposure, missing/rejected tokens, failing closed, checked after name and PIN), notice wording
- `test_admin.py`: admin off by default (404s), startup config, its own sign-in (refused addresses, cancel, expiry, address removed, sign-out, cookies never crossing with profile sessions, Google profile sign-in unaffected), the overview without sealed data, plan/lockout/sign-out/delete actions and backups
- `test_plans.py`: Free capped and paid plans not, usage in the session and `X-Questions-Remaining`, `pricingUrl` on the 429, unknown plans, export, the pricing page (prices, free allowance, contact buttons and escaping), the plans CLI (name, id, email, ambiguous names)
- `test_page_views.py`: counting, retention, table columns, route gates (no profile, private, no or stale consent, disabled), rejected routes, counts outliving profile deletion, notice wording
- `test_public_pages.py`: absolute Open Graph URLs and escaping, noindex, privacy page content, robots and sitemap, HTML 404 for pages vs JSON for API paths, icon sizes, manifest icons
- `test_backup.py`: backup file permissions and consistency, pruning, daily schedule, verify, restore (and refusing corrupt backups), API, CLI
- `test_vault.py`: master key loading and creation, token and session encryption, startup migration, wrong-key handling
- `test_portfolio_math.py`: computed figures on the fixture, P&L exclusions, broker split, USD separation, notes, Indian number formatting, holdings types to fetch
- `test_number_check.py`: number matching (rounding, lakh/crore, negatives, skipped counts/years), the warning on portfolio answers
- `test_charts.py`: reading OHLC results in any nesting, replacing model-written and elided candles with tool candles, the no-data note, other charts untouched, a year of candles fitting a saved answer, chart data left out of model history
- `test_router.py`: classifier routing in model mode, forced tool call and request shape, router model override, fallbacks to keywords
- `test_mcp_client.py`: MCP session init, event-stream parsing, tool calls, tool filtering, retryable vs auth tool errors
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

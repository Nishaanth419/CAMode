# Snapshots, portfolio figures, goals and investigations

Moved out of the always-loaded CLAUDE.md. Read this when changing snapshots, `portfolio_math`, the number check, goals and saved context, goal suitability, allocations, investigations, chat storage or housekeeping.

## Modules

- **`app/chats.py`**: Chat storage behind one API: SQLite rows in `device` mode, process memory in `private` mode.
- **`app/snapshots.py`**: Daily capture of net worth/allocation/holdings, history queries, the polling scheduler, profile prompt text, and the agent's local snapshot tools.

- **`app/housekeeping.py`**: Scheduled clean-up, `run_due(now)` from `snapshots.run_scheduler` on every tick: `sweep_sessions()` (rows idle past `SESSION_MAX_AGE_SECONDS`, unbound rows idle past `UNBOUND_SESSION_SECONDS` = max(`admin.SESSION_SECONDS`, `accounts.PENDING_SECONDS`), then `unlock.sweep()`) and, once per local day, `sweep_expired_data()` (chats past `expires_at`, `agent_runs` past `MAX_AGE_MS`, `snapshots.prune()` per profile for INDmoney and broker snapshots). Plain columns only; logs counts.

- **`app/investigation_math.py`**: Pure `Decimal` layer: `normalize_snapshot()` (`PortfolioSnapshot` / `HoldingSnapshot` with scope, capture time, currency, holdings coverage), `calculate_change()`, `calculate_portfolio_change()` (price and unit effects per holding), `calculate_allocation()`.
- **`app/investigation_checks.py`**: `run_checks()` returns `CheckResult`s (`passed`/`warning`/`failed`/`not_checked`, evidence, next step); tolerance from `INVESTIGATION_TOLERANCE_PCT` / `_INR`, staleness from `INVESTIGATION_STALE_HOURS`.
- **`app/memory.py`**: User-confirmed context: `GoalInput` / `DecisionInput`, sealed CRUD on `financial_goals` / `financial_decisions` (max 10 / 50, counted in a `BEGIN IMMEDIATE` insert), `migrate_profile_goals()` (called by `unlock.activate()`), `export_context()`, `clear_saved_context()`.
- **`app/goal_planning.py`**: Pure `Decimal` goal maths: `months_until()`, `future_value()`, `required_monthly()`, `plan_goal()` (status, gap, progress, scenarios with a `basis`, inflation, `feasibility`, `alternatives`, assumptions, warnings; `PlanningError` for invalid inputs), `date_after_years()`, `add_months()`, `follow_ups()`.
- **`app/goal_suitability.py`**: Pure rules as data: `CATEGORIES`, `evaluate()` (verdict per investment category from horizon, risk, loss capacity, liquidity, flexibility; cautious values for missing inputs), `classify_holding()` / `review_holding()`, and `FUND_DATA` (describes the fund figures in `fund_data.py`; still no `shortlist`).

- **`app/goal_portfolio.py`**: Goals joined with snapshot holdings: `holding_key()`, `load_portfolio()`, `goal_view()`, `goals_overview()`, `allocations_view()`, `assign()` and `goal_tools()` (`get_goal_plans`, `plan_goal_what_if`).
- **`app/context.py`**: `retrieve_context()` picks the saved goals, decisions and preferences relevant to one chat request (keyword rules) and renders the prompt text; `with_plan()`, `observed_sources()`, `task_parts()` (the kinds of tool-backed task in a message, by clause: `account_data` and `goal_calculation`, or `()` when there is only one).

- **`app/investigations.py`**: The bounded portfolio-change workflow (`investigate_portfolio_change()`), sealed `investigations` rows (newest 50), `agent_payload()` and the `investigate_portfolio_change` `LocalTool`.

## Daily snapshots

**Daily snapshots** (`snapshots.py`):
- `run_scheduler()` polls the wall clock every 5 minutes (macOS monotonic time pauses during sleep) and refreshes consenting `device`-mode profiles whose last capture predates the latest `SNAPSHOT_TIME` in `SNAPSHOT_TIMEZONE`; failures retry at most hourly (`users.snapshot_attempted_at`); a per-profile asyncio lock prevents overlapping runs
- `capture_snapshot()` calls `networth_snapshot`, then `networth_holdings` (which requires `asset_type`) for each held type from `holdings_types_to_fetch()` (every served type if the names don't match), up to 4 in parallel on one shared client. Results are keyed `networth_snapshot` and `networth_holdings:<TYPE>`; partial results are kept, the filtered MCP tool list is cached, and one row is upserted per profile per local date
- `extract_networth_inr()` is a best-effort parse (INDmoney's format is undocumented); `networth_inr` is NULL when unclear
- Snapshots are SQLite rows, deliberately not a vector store (exact values, date ranges, sorting)

## Portfolio numbers

**Portfolio numbers** (`portfolio_math.py`, `number_check.py`):
- `compute_metrics()` turns stored results into `figures` (totals, P&L over holdings with a cost basis, splits by asset type/class/market cap/broker, concentration, top holdings, USD kept separate, `data_notes`), all in `Decimal`. `get_portfolio_snapshot` returns these instead of raw text (raw only for old, unparseable snapshots); `include_all_holdings` returns every row in compact form to fit Groq's 6,000-char tool-result limit
- INDmoney traps: linked brokers report `invested_amount` 0 (excluded from P&L, noted); `holding_percent` is per asset type; US rows carry INR and USD; `networth_snapshot` and the `IND_STOCK` holdings rows label Indian stocks `STOCK`, which `networth_holdings` rejects, so `portfolio_math.canonical_asset_type()` maps it to `IND_STOCK` (fetch list, rows, splits, investigation totals, goal holding keys)
- The portfolio prompt says to quote `figures`, never compute. `agent._number_warning()` runs `number_check.unverified_numbers()` on every answer that used tools (either agent) or was goal arithmetic, and appends `warning_note()` for amounts/percentages matching no tool number, the message or the saved context (counts, years, list numbering and links are skipped); goal answers get the `GOAL_SOURCE` wording
- `tests/fixtures/indmoney_sample.py` is synthetic data in the real response layout; `scripts/inspect_indmoney_shapes.py` prints a live response's layout with values removed (`--codes`: counts of `investment_code` patterns and ISIN-shaped codes per asset type and broker label); `scripts/inspect_broker_shapes.py [--profile <id>] [--broker <id>]` does the same for a connected broker's holdings and status tools (no names, codes, amounts, tokens or session ids; an ended login prints "reconnect <broker> first" and exits 1); `scripts/eval_numbers.py` scores answers on the fixture

## Goal planning, suitability and allocations

**Goal planning, suitability and allocations** (`goal_planning.py`, `goal_suitability.py`, `goal_portfolio.py`, `memory.py`):
- Goals can be `status: "draft"` (only a name required); an active goal needs currency, target amount and a date or `horizonYears` (converted to `targetDate` on save, stored with `targetDateFrom: "horizon"`). `custom` is an input alias of `other`; `financial_independence` is a kind. Suitability inputs (`flexibility`, `liquidityNeed`, `riskTolerance`, `lossCapacity`) and `inflationPct` are optional and stay `None` when not given
- `plan_goal(goal, today, mapped_amount=)` keeps every older key and adds `effectiveTarget`/`inflation` (the entered target is today's money), `startingAmount` (saved + mapped), `totalFutureContributions`, `feasibility` and `alternatives` (kind × basis: `zero_return` always, `assumed_return` only with a saved assumption). `follow_ups()` lists unanswered questions; nothing is guessed. An assumption above `HIGH_RETURN_PCT` (12) adds a warning, and `goal_suitability.assumption_warning()` (added by `goal_view()` and `/api/goals/plan`) questions any assumed return when every equity category is unsuitable
- Suitability is fixed rules, never the model: equity is unsuitable under 36 months, a fixed goal with low loss capacity rules out equity at any horizon, missing inputs take the cautious value and are listed in `assumed`. Suitability stays category level; scheme figures come only from `fund_data.py` (below), and there is no `shortlist` anywhere
- `goal_allocations` holds one sealed row per profile: `{holdingKey: {name, assetType, broker, goals: {goalId: pct}, generalWealthPct}}`, holding key `asset_type|code|broker` (same as `investigation_math` holding ids). `memory.set_allocation()` runs in `BEGIN IMMEDIATE`; `AllocationInput` caps the total at 100. Shares are valued from the latest snapshot rounded down, so a rupee counts toward at most one goal; a holding missing from the snapshot is `missing` (value unknown, not counted); only INR holdings count toward INR goals. Deleting a goal, `DELETE /api/context` and INDmoney disconnect release allocations; export has `goalAllocations`
- `get_goal_plans` and `plan_goal_what_if` (read-only, nothing saved, payload under 5,500 chars) go to both agents, only for profiles with a goal. `calculate_goal_plan` (`goal_portfolio.goal_calculator_tool()` / `calculate_goal()`: a goal described in chat, validated by `GoalInput`, reads and saves nothing; blank arguments count as not given, currency symbols and case map to codes, and a suitability input that is not exactly one of its choices is left out (cautious value) and listed in `ignoredInputs` instead of failing the call; `main.chat` passes the user's own words (`context.request_text()`) as `user_text`, and a call without a currency is planned in INR, with `currencyNote` saying so, when they use ₹, Rs, lakh or crore and name no other currency) goes to both agents when `context.wants_goal_calculator()` matches (goal words, or an amount with a time span). Whenever a goal tool is offered the prompt gets `agent.GOAL_TOOL_RULES`; an answer with its own figures and no tool call is retried once with `GOAL_CALCULATOR_NUDGE`, then flagged. The Finance Agent gets them through `agent.FINANCE_LOCAL_TOOL_NAMES` and never any snapshot, profile or MCP tool; it sees only a count of assigned holdings, Portfolio Analyzer also their values (`retrieve_context(include_holdings=)`)
- Routes: `GET`/`PUT /api/goals/allocations` (declared before `/api/goals/{goal_id}`); `GET /api/goals` also returns `fundData` and `portfolio`; `POST /api/goals/plan` also returns `suitability` and `followUps`, and with `goalId` counts that goal's assigned holdings (`goal_portfolio.counted_value()`) so the editor preview matches the saved card. `finance.js` `goalStatus()` shows `on_track` with `feasibility.status == "depends_on_returns"` as "On track if returns hold"

## Goals and saved context

**Goals and saved context** (`memory.py`, `goal_planning.py`, `context.py`):
- Confirmed context (goals, decisions, profile fields) is written only by explicit user actions and carries `source: "user_entered"` plus created/updated times; observed data stays in snapshots and CAS rows; nothing the model infers is stored (`tests/test_memory.py::test_chats_never_write_saved_context`)
- Goals: `name`, `kind`, required `currency` (INR/USD/EUR/GBP, never converted or summed across currencies), `targetAmount`, `targetDate`, `savedAmount`, `monthlyContribution`, optional `assumedReturnPct` (0–30; none means only the no-growth scenario), optional `notes`. Plans are computed on every read (`context.with_plan()`), never stored
- `plan_goal()` mirrors `finance.js` (start-of-month contributions, monthly compounding); scenarios are no growth plus the assumption ±2 points; `requiredMonthly` rounds up, `additionalMonthly` is the extra over the planned contribution; missing target/date/currency is `insufficient_data`, not a guess
- `retrieve_context()` matches the message alone first and adds the last 2 user turns only for follow-ups (`is_follow_up()`); goals named (or by kind) else all goals for goal-ish questions; decisions linked to chosen goals, sharing a word, or all for "what did I decide"; preferences with goals or risk/allocation questions. Capped at `MAX_CONTEXT_CHARS`
- `main.chat` passes it as `run_agent(..., saved_context=)` to both agents: `<saved_context>` block plus `SAVED_CONTEXT_RULES` (quote calculated figures, never recompute, projections are illustrations, unknown means unknown); a `context` trace step lists the items; `_number_warning` accepts figures from it. Without saved context the prompts are unchanged. `PortfolioContext.profile_context` now holds only `snapshots.snapshot_context()`
- Old goals inside `users.profile` (`targetInr`, `targetYear`) move to `financial_goals` on unlock with `targetDate` = 31 Dec and a `migratedFrom` note; `ProfileFields` has no `goals`, and the stored profile carries `updatedAt`
- Goal and decision create/update need consent; reads and deletes do not. `DELETE /api/context` removes goals, decisions and profile fields (not chats/snapshots). Export includes goals and decisions; profile deletion cascades

## Portfolio change investigations

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

## Chat persistence

**Chat persistence** (`main.py`):
- Lives in `chats.py`; routes in `main.py` only call it
- Max 20 messages per conversation, max 12,000 chars per stored question and 24,000 per stored answer (`chats.MAX_ANSWER_CHARS`, room for a year of candles), max 50 chats per profile (oldest evicted), in both storage modes
- Chats expire 30 days after their last message and are hidden/purged once expired
- Sessions and the cookie slide forward on each `/api/session` visit
- Disconnect revokes the INDmoney token and deletes snapshots and cached tools but keeps chats; deleting a profile cascades to everything, including consent records, and `remove_profile()` deletes its session rows (`unlock.end_all_sessions()`) rather than relying on SET NULL
- Expired chats, old answer runs and snapshots past retention are also deleted daily by `housekeeping` for profiles that never come back

## Routes

- Chat routes: `POST /api/chats`, `GET /api/chats`, `GET`/`DELETE /api/chats/{id}`, `POST /api/chat`
- Snapshot routes: `GET /api/snapshots?days=`, `POST /api/snapshots/refresh`

- Investigation routes: `POST /api/investigations` (consent + unlocked; `201` + `Location` when saved, `200` when not), `GET /api/investigations?limit=`, `GET`/`DELETE /api/investigations/{id}`

- Page routes: `GET /api/portfolio` (latest snapshot's `figures` plus every holding via `portfolio_math.all_holdings()`, or `{available: false}`)
- Goal routes: `GET`/`POST /api/goals`, `GET`/`PUT`/`DELETE /api/goals/{id}` (each with `plan`), `POST /api/goals/plan` (stateless preview, optional `annualReturnPcts`), `GET /api/goals/{id}/funds` (INDmoney fund figures, `?category=`), `GET`/`POST /api/decisions`, `PUT`/`DELETE /api/decisions/{id}`, `GET`/`DELETE /api/context`. Goals are added, edited and deleted in one `#goal-dialog` opened from the Goals page, Home and the goal planner (`openGoalDialog()` in `pages.js`); its preview calls `/api/goals/plan`. The Goals page's "What the assistant can use" panel lists and deletes saved context

- `GET /api/snapshots` reads history, the broker-combined values and the latest snapshot in one `asyncio.to_thread` (`_snapshot_history()`); `/api/portfolio` and `/api/holdings/combined` read the latest snapshot in a thread too

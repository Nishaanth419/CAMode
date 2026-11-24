# CLAUDE.md

Guidance for Claude Code in this repository. Detail lives in `docs/architecture/` and is read on demand (index at the bottom).

## Project overview

**CA Mode** is a local finance assistant with two agents: the **Finance Agent** (general finance questions, no account access) and the **Portfolio Analyzer** (the user's INDmoney and broker holdings, from a daily snapshot plus read-only MCP tools). Each person has a PIN-protected local profile that owns its chats, connections and snapshots (`AUTH_MODE=google` uses one Google account per profile instead). The model runs locally through Ollama by default (OpenAI and Groq optional). Everything is stored in local SQLite; Plotly charts are drawn from portfolio data. FastAPI backend, vanilla JS PWA frontend in `frontend/`.

Module descriptions are in each module's docstring (`app/*.py`); cross-module design notes are in `docs/architecture/`.

## Commands

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
python -m scripts.eval_workflows   # the general agent on scripted replies and fake INDmoney, offline

# GitHub Actions runs all of these
python -m compileall -q app tests && python -m pytest -q
```

## Traps and invariants (not visible from the code)

**Privacy and storage**
- Bump `POLICY_VERSION` whenever `privacy.NOTICE` changes what is stored or shared: every profile is asked again and its scheduled snapshots pause until it accepts.
- Chat titles/messages, snapshot `data`, `users.profile`, goals, runs, CAS statements and broker holdings are sealed under the profile's public key (`sealing.py`). Losing both PIN and recovery code loses the history; there is no admin reset. The scheduler can seal without a PIN (public key is plain) but cannot read.
- Never store or log a CAS PDF, its password, investor name, PAN, address, contact details or DP/client IDs; folio numbers only as an HMAC (`folioKey`). Trace text goes through `redact_trace_text()`.
- Secrets in SQLite are `enc:v1:` Fernet values (`users.indmoney`, `broker_connections.session`, `oauth_sessions.data`); an empty dict stays plain `{}`, so test "connected" with `indmoney <> '{}'`, never `json_extract`. The master key lives outside `.data/` and is never auto-created over a database that already holds encrypted values. Tests use a generated `APP_MASTER_KEY` and must never touch the real key file.
- New `users` columns go in `_ADDED_USER_COLUMNS` (and `_USERS_COLUMNS`). Never use `.data/codemode.sqlite3` as test scratch; set `APP_DATA_DIR` to a temp folder.
- The PIN is a local lock, not network-grade auth; anywhere others can reach the app use `AUTH_MODE=google`. Sessions are per `indmoney_session` cookie; unlocked keys live in memory only (a restart locks everyone).

**Numbers**
- Missing is never zero: an unreadable value, absent NAV, no earlier snapshot or an uncovered fund is `None`/`missing`/`insufficient_data`, never 0 (returns, tax, fund data, investigations, benchmarks, combined history).
- Money maths is `Decimal` in pure modules; the model quotes tool figures and never computes. Goal maths in `goal_planning.py` mirrors `finance.js`.

**INDmoney data**
- Linked brokers report `invested_amount` 0 (excluded from P&L); `holding_percent` is per asset type; US rows carry INR and USD; `networth_snapshot` labels Indian stocks `STOCK` but `networth_holdings` needs `IND_STOCK` (`canonical_asset_type()`); `extract_networth_inr()` is a best-effort parse of an undocumented format.
- Real response layouts are recorded in `tests/fixtures/` (synthetic values); re-record with `scripts/inspect_indmoney_shapes.py` / `inspect_broker_shapes.py`, which print layouts with values removed.
- Brokers expose read tools by explicit allowlist (`read_tools`, `read_modes`); write tools must never overlap it. Broker tool handlers never raise `McpAuthError`, so INDmoney's token refresh is not triggered.

**Agent**
- Only `McpAuthError` ends an answer as an auth failure; check `AgentRunError.auth_failed`, never message text. Task contracts observe and never change an answer.
- Ollama's `/v1` API silently truncates long prompts: the model is built from `ollama/Modelfile` (`ca-mode`, `num_ctx 32768`). The Ollama classifier call must set `temperature: 0`; cloud providers get none.
- `AGENT_GRAPH_ENABLED` is forced false in `conftest.py` and `scripts/eval_tasks.py` (they script the previous loop).

**Hosting and frontend**
- `deploy/` changes are not shipped by the image: list the operator's manual steps in `docs/hosting.md` "Upgrading an existing server". Probes: use `/healthz` for rollbacks, never `/readyz` (a sleeping Mac must not roll back). One worker only (`db.claim_single_process`).
- Bump the `?v=` cache-busters in `frontend/index.html` and `CACHE_NAME` in `frontend/sw.js` with every frontend asset change; the CSP blocks inline handlers. Update `TAX_RULES` (`finance.js`) and its test after each budget.

## Before changing X, re-run Y

- Router prompt: bump `ROUTER_PROMPT_VERSION`, run `python -m scripts.eval_router --provider <p>`.
- Portfolio prompt or `agent_payload()`: `python -m scripts.eval_investigations --provider ollama --runs 3`.
- Agent loop, goal tools or task contracts: `python -m scripts.eval_tasks` (and `--provider ollama --runs 3`).
- Planner, its prompts or budgets: `python -m scripts.eval_tasks --dataset evals/multi_step.jsonl` (and live).
- General agent, its tools or `ANALYSIS_GUIDE`: `python -m scripts.eval_workflows` (and `--provider ollama --runs 3`).
- Local model choice: re-run both live evals with `--provider ollama`; scores are in the README.
- Numbers shown from answers: `python -m scripts.eval_numbers`.

## Debugging

- Logs go to stdout at `LOG_LEVEL`; snapshot failures log `snapshot_refresh_failed`, `initial_snapshot_failed` or `manual_snapshot_failed`
- SQLite at `.data/codemode.sqlite3` holds live user data and tokens; inspect read-only and never use it as test scratch space (set `APP_DATA_DIR` to a temp folder)
- "Could not sign in (HTTP 404/405)" means the running server predates the frontend: restart it
- `ModuleNotFoundError: httpx` on startup means a global `uvicorn` was used instead of `.venv/bin/uvicorn`
- MCP errors (401, 500) include hints in the exception message; a 401 triggers one token refresh and retry

## Where to read more (`docs/architecture/`)

- `agent-and-planner.md`: changing the agent loop, router, planner, task contracts, tools, charts, answer trace or local model.
- `brokers.md`: broker connections and OAuth, broker history, MCP client, INDmoney tool allowlist, logos.
- `mutual-funds-and-tax.md`: CAS, fund ledger, XIRR and benchmarks, capital gains tax, AMFI data, fund figures.
- `privacy-sealing-and-auth.md`: profiles, sessions, Google sign-in, PIN sealing, vault, consent, admin, plans, credits, limits.
- `snapshots-and-goals.md`: snapshots, portfolio figures, number check, goals, saved context, investigations, chat storage.
- `frontend.md`: pages, rail, charts, service worker, icons (`frontend/CLAUDE.md` has the editing rules).
- `deployment.md`: Docker/Compose, auto-update, probes, logs, backups, every environment variable.
- `testing.md`: test strategy, fixtures, evals and when to re-run each (`tests/CLAUDE.md` has the editing rules).
- User-facing docs also in `docs/` (`hosting.md`, `evals.md`, `privacy-and-data.md`, `api.md`, `development.md`).

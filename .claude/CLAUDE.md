# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**CA Mode** is a local finance assistant with two specialized agents:
- **Finance Agent**: Answers general finance questions (budgeting, saving, concepts) — no account access
- **Portfolio Analyzer**: Answers questions about the user's INDmoney portfolio using read-only MCP tools

The app supports OpenAI and Groq providers, stores chat history and OAuth tokens locally in SQLite, and generates charts from portfolio data. It runs as a FastAPI backend with a vanilla JS PWA frontend.

## Architecture

### Backend Stack
- **FastAPI** (`app/main.py`): HTTP routes, OAuth flow, SQLite persistence; `POST /api/chat` returns the full answer as plain text with the call trace in an `X-Agent-Trace` header
- **Agent Loop** (`app/agent.py`): Provider selection (OpenAI/Groq), tool-calling chat loop (`stream_agent` yields `(event, data)` tuples, `run_agent` collects them), request filtering
- **MCP Client** (`app/mcp_client.py`): INDmoney OAuth-protected MCP discovery and tool execution
- **Profiles** (`app/users.py`): PIN-protected local profiles (scrypt hashes, account-bound lockout); each profile owns its chats, INDmoney tokens, profile fields and snapshots
- **Snapshots** (`app/snapshots.py`): daily 09:00 IST capture of net worth/allocation/holdings, history queries, the polling scheduler, and the agent's local read-only tools
- **Database** (`app/db.py`): SQLite (`users`, `oauth_sessions`, `chats`, `portfolio_snapshots`) with WAL mode; a pre-profile `chats` table is migrated to `chats_legacy` and adopted on first sign-in

### Key Design Patterns

**Request Filtering** (in `agent.py`):
- Finance Agent: Rejects code requests and off-topic messages using regex patterns (`_FINANCE_TOPIC_RE`, `_CODE_REQUEST_RE`)
- Detects whether a message needs INDmoney data (`_INDMONEY_DATA_REQUEST_RE`) or is general finance
- Greedy matching allows "and also" follow-ups on finance topics to maintain conversation flow

**Tool Filtering** (in `mcp_client.py`):
- `IMPORTANT_PORTFOLIO_TOOL_NAMES`: Allowlist of 15 documented read-only tools
- `filter_portfolio_tools()` enforces this list, dropping any undocumented or write tools the MCP server exposes
- Prevents accidental use of sensitive operations

**MCP Session Management**:
- `fetch_mcp_session()`: Initialize → tools/list → notifications/initialized flow
- Async HTTP streaming for large responses; event-stream parsing
- Session IDs and protocol versions tracked per user request; one pooled httpx client is reused for all tool calls in an agent run

**Chat Persistence**:
- Max 20 messages per conversation (oldest truncated as new messages arrive)
- Max 12,000 chars per stored message, max 50 chats per session (oldest evicted)
- `session_id` is a random HttpOnly cookie bound to a profile (`oauth_sessions.user_id`), rotated on sign-in; INDmoney tokens live on the profile (`users.indmoney`)
- Sessions and the cookie slide forward on each `/api/session` visit; chats expire 30 days after their last message and are hidden/purged once expired
- `connect_db()` is a context manager that commits/rolls back and always closes the connection

### Frontend
- Static HTML/CSS/JS in `frontend/` (manifests as PWA)
- `app.js`: Chat UI, agent selection, INDmoney login/logout, theme toggle
- `sw.js`: Service worker for offline support
- Plotly injected dynamically for chart rendering

## Common Commands

### Development Setup
```bash
# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
python -m pip install -r requirements.txt

# Copy and configure .env
cp .env.example .env
# Add OPENAI_API_KEY or GROQ_API_KEY
```

### Run the App
```bash
# With Python (dev)
uvicorn --env-file .env app.main:app --host 127.0.0.1 --port 8003

# With Docker
docker compose up --build -d
docker compose logs -f
docker compose down
```

### Testing & Code Checks
```bash
# Install dev dependencies
python -m pip install -r requirements-dev.txt

# Run all tests
python -m pytest -q

# Run a single test file or specific test
python -m pytest tests/test_agent.py -q
python -m pytest tests/test_agent.py::test_agent_blocks_code_requests -q

# Check for syntax errors
python -m compileall -q app tests

# Run GitHub Actions checks locally
python -m compileall -q app tests && python -m pytest -q
```

## Key Files & Patterns

### `app/main.py`
- FastAPI app setup, static file serving, JSON response wrappers
- OAuth routes: `GET /auth/indmoney/connect`, `GET /auth/indmoney/callback`, `POST /auth/indmoney/disconnect`
- Profile routes: `GET /api/session` (current user + profile list), `POST /api/users` (create + sign in), `POST /api/session/login`, `POST /api/session/logout`, `GET`/`PUT /api/profile`, `DELETE /api/users/me`
- Chat routes (profile required): `POST /api/chats` (create), `GET /api/chats` (list), `GET`/`DELETE /api/chats/{id}`, `POST /api/chat` (send a message)
- Snapshot routes: `GET /api/snapshots?days=`, `POST /api/snapshots/refresh`
- Other: `GET /api/providers`, `GET /api/tools`, `GET /healthz`
- Helper: `session_id(request)` reads the session cookie; `touch_session()` creates/slides it
- Database helpers: `connect_db()`, `initialize_db()`

### `app/agent.py`
- `_blocked_request_reason()`: Detects and rejects non-finance requests
- `_requires_indmoney_data()`: Determines if message needs Portfolio Analyzer tools
- `_contains_code_output()`: Rejects model output with code (except `chart` blocks)
- `stream_agent()` / `run_agent()`: OpenAI/Groq chat loop; calls MCP tools if needed; retries once if the portfolio model skips tools; marks answers cut off by `finish_reason == "length"`

### `app/mcp_client.py`
- `fetch_mcp_session()`: Initialize MCP session, list tools, return capabilities
- `call_mcp_tool()`: Execute a single tool with params; handles streaming/JSON responses
- `filter_portfolio_tools()`: Whitelist enforcement

### Environment Variables
- `OPENAI_API_KEY` (or `GROQ_API_KEY`): Enables provider
- `AI_PROVIDER`: Default provider (`openai` or `groq`); user can switch in app
- `MCP_SERVER_URL`: INDmoney MCP endpoint (default: `https://mcp.indmoney.com/mcp`)
- `SNAPSHOT_TIME`, `SNAPSHOT_TIMEZONE`, `SNAPSHOT_RETENTION_DAYS`, `SNAPSHOT_SCHEDULER_ENABLED`: daily snapshot job (defaults `09:00`, `Asia/Kolkata`, `365`, `true`; tests set it to `false`)
- `GROQ_INPUT_TOKEN_BUDGET`, `GROQ_COMPLETION_TOKEN_LIMIT`: Rate limits for Groq
- `APP_DATA_DIR`: SQLite data directory (default: `.data/`)
- `LOG_LEVEL`: Logging verbosity (default: `INFO`)

## Testing Strategy

Tests use mocked AI and MCP responses (no live API calls). `conftest.py` provides an ephemeral SQLite database per test and a `TestClient`; OpenAI/Groq, MCP and OAuth calls are faked per test with `monkeypatch`.

Test coverage:
- `test_api.py`: Routes, OAuth flow, chat creation/listing, message streaming
- `test_agent.py`: Request filtering, Finance Agent logic, Portfolio Analyzer logic, Groq context fitting, error handling
- `test_mcp_client.py`: MCP session init, tool calls, tool filtering, error recovery
- `test_users.py`: PIN hashing, sign-in/lockout, session rotation, legacy adoption, profile isolation
- `test_snapshots.py`: net-worth parsing, IST schedule, capture, due-user selection, scheduler, local tools, retention

## Performance Notes

- SQLite with WAL mode handles concurrent requests
- Chat history trimmed to 20 messages to reduce token usage and API costs
- Groq uses conservative context/completion limits (adjustable in `.env`)
- Async HTTP for MCP calls (streaming supported)
- Service worker serves the cached app shell when offline (API data is always fetched live)

## Debugging Tips

- Logs go to stdout at `LOG_LEVEL` (set to `DEBUG` for verbose traces)
- SQLite at `.data/codemode.sqlite3` (read-only access is safe for inspection)
- Frontend console has chat/agent state and API response bodies
- INDmoney OAuth tokens are revoked on disconnect (which also deletes the session's chats); reconnect to test OAuth flow
- MCP errors (401, 500) include hints in the exception message

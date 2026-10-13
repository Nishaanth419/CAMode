# CA Mode

CA Mode is a local finance assistant with two agents:

- **Finance Agent** answers general finance questions.
- **Portfolio Analyzer** answers questions about the signed-in user's INDmoney account through its MCP server.

The app supports OpenAI and Groq. Several people can share one computer: each person has a PIN-protected profile with their own chats, INDmoney connection, and daily portfolio snapshots, all stored locally.

## Features

- Create a profile per person (name + PIN) and switch between profiles.
- Save optional profile details (risk appetite, horizon, goal, monthly investment, age band) that Portfolio Analyzer uses as context.
- Save a daily INDmoney portfolio snapshot at 09:00 IST and show net worth with a 30-day trend.
- Start new chats and return to earlier conversations.
- Keep each conversation associated with Finance Agent or Portfolio Analyzer.
- Connect or disconnect INDmoney for portfolio questions.
- Generate charts when account tools return comparable numeric data.
- Switch between light and dark themes.
- Focus Portfolio Analyzer on six account tools: net-worth snapshot, asset-class allocation, holdings, Indian stock SIPs, mutual fund SIPs, and US stock SIPs.

## Requirements

- Docker Desktop, or Python 3.12 or newer
- An OpenAI or Groq API key
- An INDmoney account to use Portfolio Analyzer

## Configure providers

Copy the example environment file and add at least one provider key:

```bash
cp .env.example .env
```

- `OPENAI_API_KEY` enables OpenAI. The default model is `gpt-4o`.
- `GROQ_API_KEY` enables Groq. The default model is `openai/gpt-oss-120b`.
- `AI_PROVIDER` chooses the initial provider (`openai` or `groq`).

You can add both keys and switch providers in the app. Groq requests use conservative context and completion limits to help stay within common tokens-per-minute limits; adjust `GROQ_INPUT_TOKEN_BUDGET` and `GROQ_COMPLETION_TOKEN_LIMIT` in `.env` only if your account supports different limits.

Chat messages are sent to the selected AI provider. Portfolio Analyzer also sends the INDmoney tool results needed to answer the question. Provider keys remain in the local server environment and are not sent to the browser.

## Run with Docker

After configuring `.env`, build and start the app:

```bash
docker compose up --build -d
```

Open [http://localhost:8003](http://localhost:8003). Use this same local address for INDmoney sign-in; the OAuth callback is `http://localhost:8003/auth/indmoney/callback`.

To view logs or stop the app:

```bash
docker compose logs -f
docker compose down
```

## Run with Python

Create a virtual environment, install the app dependencies, and start the server:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
uvicorn --env-file .env app.main:app --host 127.0.0.1 --port 8003
```

Open [http://localhost:8003](http://localhost:8003). Keep using this address during INDmoney sign-in.

## Use the agents

Choose **Finance Agent** for general topics such as budgeting, saving, and finance concepts. It does not access your INDmoney account.

Choose **Portfolio Analyzer** for account and market questions, then connect INDmoney when prompted. CA Mode exposes INDmoney's documented read-only finance capabilities when the connected MCP server provides them:

| Tool | What it retrieves |
| --- | --- |
| `networth_snapshot` | Overall portfolio and net-worth summary |
| `networth_allocation_breakdown` | Breakdown for an asset class |
| `networth_holdings` | Holdings for an asset class |
| `indian_stocks_sips` | Indian stock SIPs |
| `lookup_ind_keys` | Resolve stock, index, derivative, and fund names to INDmoney identifiers |
| `get_indian_stocks_ohlc` | Historical OHLC candles for Indian stocks, indices, and derivatives |
| `get_indian_stocks_details` | Indian market prices, analyst data, and news |
| `get_indian_stocks_option_chain` | Option-chain strikes, premiums, open interest, and implied volatility |
| `get_indian_stocks_greeks_history` | Historical option Greeks and volatility measures |
| `user_watchlist` | Connected Indian and US stock watchlists |
| `get_mf_by_category` | Mutual-fund category search and ranking |
| `get_mf_funds_details` | Mutual-fund profiles, returns, benchmarks, and allocations |
| `get_us_stocks_details` | US market prices, analyst data, and news |
| `mf_sips` | Mutual fund SIPs |
| `us_stocks_sips` | US stock SIPs |

### Daily portfolio snapshots

Once a profile connects INDmoney, CA Mode saves a snapshot of `networth_snapshot`, `networth_allocation_breakdown`, and `networth_holdings` right away and then every day at 09:00 IST (before the 09:15 market open, so it captures the previous close). The app must be running to take the snapshot; if it was closed or the laptop was asleep at 09:00, it catches up within five minutes of starting or waking. Use **Refresh snapshot now** in the portfolio card to take one on demand.

Portfolio Analyzer answers net-worth, allocation, holdings, and trend questions from the saved snapshots (`get_portfolio_snapshot`, `get_networth_history`) and says when the snapshot was captured. It still calls INDmoney live for current prices, SIPs, watchlists, options, and anything the snapshot lacks. Snapshots are kept for 365 days.

Snapshots are stored as SQLite rows, not in a vector database: net worth and holdings are exact numbers that need latest-value, date-range, and sorting queries, which similarity search cannot answer reliably.

The Portfolio Analyzer can combine these read-only tools for portfolio breakdowns, fund comparisons, watchlist scans, price and candle charts, and options scenario calculations. The MCP server's current tool list determines which capabilities are available for the connected account. It cannot place trades or change account settings.

## Local data

The SQLite database at `.data/codemode.sqlite3` stores profiles, chats, INDmoney OAuth tokens, and portfolio snapshots on this computer. Each profile only sees its own data. PINs are stored as salted scrypt hashes, and five wrong PINs pause sign-in for five minutes.

- **Disconnect** revokes the INDmoney access token and deletes that profile's snapshots; its chats are kept.
- **Delete this profile** (in the profile dialog) removes the profile with its chats, snapshots, and INDmoney connection.
- Upgrading from a version without profiles: the first profile created or unlocked in a browser adopts that browser's existing chats and INDmoney connection. Back up `.data/` before upgrading.

The PIN is a local lock for people sharing one computer, not internet-grade authentication, and tokens and snapshots are not encrypted beyond the file's `0600` permissions. Add real authentication and encryption at rest before exposing CA Mode to a network.

Keep `.env` and `.data/` private; neither should be committed to Git.

## Development checks

Install development dependencies and run the same checks as GitHub Actions:

```bash
python -m pip install -r requirements-dev.txt
python -m compileall -q app tests
python -m pytest -q
```

The test suite uses mocked AI and INDmoney responses, so it does not make live API calls. GitHub Actions runs these checks on pushes and pull requests.

## Project structure

```text
app/
  main.py          FastAPI routes, OAuth, and the snapshot scheduler lifespan
  db.py            SQLite connection, schema, and migrations
  users.py         Profiles, PIN sign-in, sessions, and per-profile INDmoney tokens
  snapshots.py     Daily portfolio snapshots, history, scheduler, and agent tools
  agent.py         Provider selection and AI tool-call loop
  mcp_client.py    INDmoney MCP discovery and tool calls
frontend/          Local web app and PWA assets
tests/             API, agent, OAuth, and MCP tests
.github/workflows/ GitHub Actions CI workflow
requirements.txt   Runtime dependencies
requirements-dev.txt  Test dependencies
docker-compose.yml Local Docker setup
Dockerfile         Python container image
```

# CA Mode

CA Mode is a local finance assistant with two agents:

- **Finance Agent** answers general finance questions.
- **Portfolio Analyzer** answers questions about the signed-in user's INDmoney account through its MCP server.

The app supports OpenAI and Groq. It stores chats and INDmoney OAuth data locally.

## Features

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

Choose **Portfolio Analyzer** for account questions, then connect INDmoney when prompted. The analyzer offers these core tools to the model:

| Tool | What it retrieves |
| --- | --- |
| `networth_snapshot` | Overall portfolio and net-worth summary |
| `networth_allocation_breakdown` | Breakdown for an asset class |
| `networth_holdings` | Holdings for an asset class |
| `indian_stocks_sips` | Indian stock SIPs |
| `mf_sips` | Mutual fund SIPs |
| `us_stocks_sips` | US stock SIPs |

Use **Review portfolio** in Portfolio Analyzer to create a profile review in a new chat. It charts available allocation, largest holdings, and gains or losses; calls out concentration and laggards when the data supports those comparisons; and uses fund return history or market research data when available. This review is historical and informational: it does not forecast returns or select specific investments as “best.” Any allocation decision depends on goals, time horizon, liquidity needs, and risk tolerance. The review temporarily adds focused fund/stock research and identifier lookup tools to the six core tools; the regular chat and `/api/tools` listing remain limited to the core six.

## Local data

The SQLite database at `.data/codemode.sqlite3` stores chats and INDmoney OAuth tokens on this computer. Chat records are isolated by local session. Selecting **Disconnect** revokes the INDmoney access token and removes that session and its chats.

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
  main.py          FastAPI routes, OAuth, and SQLite persistence
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

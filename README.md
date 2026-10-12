# CA Mode

A local Python finance assistant with two selectable agents: Finance Agent for general finance questions, and Portfolio Analyzer for account-specific questions using INDmoney's MCP server. Sign in to INDmoney only when using Portfolio Analyzer. Chat history, OAuth, and settings run on this computer and are stored in `.data/codemode.sqlite3`.

## Requirements

- Docker Desktop, or Python 3.12+
- An API key for at least one supported AI provider
- An INDmoney account for Portfolio Analyzer

## Run locally with Docker

```bash
cp .env.example .env
```

Add at least one AI provider key to `.env`, then start the app. OpenAI, Kimi, and Groq are supported; configure any combination of them. The provider selector only enables providers with a key present. API keys stay on the local server.

For Kimi, create an API key in the [Kimi API Platform](https://platform.kimi.ai/) and set `KIMI_API_KEY`. For Groq, create a key in the [Groq Console](https://console.groq.com/keys) and set `GROQ_API_KEY`. OpenAI uses `OPENAI_API_KEY`. Chat messages and, when using Portfolio Analyzer, retrieved portfolio information are sent to the provider you select to generate the answer.

Groq requests are kept within a conservative input budget by prioritizing recent chat messages and shortening exceptionally large tool results. Adjust `GROQ_INPUT_TOKEN_BUDGET` and `GROQ_COMPLETION_TOKEN_LIMIT` in `.env` only if your Groq plan supports a different tokens-per-minute limit.

Then start the app:

```bash
docker compose up --build -d
```

Open [http://localhost:8003](http://localhost:8003) and choose **Finance Agent** or **Portfolio Analyzer**. Connect INDmoney to use the portfolio agent. OAuth will return to `http://localhost:8003/auth/indmoney/callback`. Keep using this same local address for the sign-in flow.

To view server logs, run `docker compose logs -f`. To stop it, run `docker compose down`.

The SQLite database is stored in `.data/` and remains on this computer across restarts. Do not commit `.env` or `.data/`.
Use the sun/moon button in the header to switch between light and dark themes; the choice is remembered in this browser.

## Chat formatting

Assistant replies support Markdown headings and pipe tables. Tables render as horizontally scrollable tables, including when a reply omits the Markdown separator row. Table text and headings use theme-aware colors so they remain readable in dark mode. The service worker cache is versioned so browsers can load updated app files after a release; if an old interface remains visible, refresh the page.

## Run with local Python

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Add at least one provider API key to `.env`, then run:

```bash
uvicorn --env-file .env app.main:app --host 127.0.0.1 --port 8003
```

Open [http://localhost:8003](http://localhost:8003).

## Local data

OAuth tokens, chats, and chat history are stored in `.data/codemode.sqlite3`. Selecting **Disconnect** revokes the INDmoney access token and removes that local session and its chats.

## Project structure

```text
app/
  main.py          Local API, INDmoney OAuth, and SQLite persistence
  agent.py         OpenAI tool-call loop and conversation context
  mcp_client.py    INDmoney MCP discovery and tool calls
frontend/          Local website, styles, JavaScript, and PWA assets
  css/app.css      Interface styles
  js/app.js        Chat and visualization behavior
  index.html       App page structure
requirements.txt   Python dependencies
docker-compose.yml Local app container
Dockerfile         Python container image
```

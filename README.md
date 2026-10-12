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

Add an AI provider key to `.env` for OpenAI, Kimi, or Groq; you can also use a local Ollama model without a cloud API key. Cloud provider keys stay on the local server.

For Kimi, create an API key in the [Kimi API Platform](https://platform.kimi.ai/) and set `KIMI_API_KEY`. For Groq, create a key in the [Groq Console](https://console.groq.com/keys) and set `GROQ_API_KEY`. OpenAI uses `OPENAI_API_KEY`. Chat messages and, when using Portfolio Analyzer, retrieved portfolio information are sent to the provider you select to generate the answer.

Groq requests are kept within a conservative input budget by prioritizing recent chat messages and shortening exceptionally large tool results. Adjust `GROQ_INPUT_TOKEN_BUDGET` and `GROQ_COMPLETION_TOKEN_LIMIT` in `.env` only if your Groq plan supports a different tokens-per-minute limit.

### Use a local Ollama model

Install [Ollama](https://ollama.com/download). Since the app runs in Docker, Ollama must accept connections from the Docker container. If you start Ollama from a terminal, use:

```bash
OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

Keep that terminal open, then download a small model in another terminal:

```bash
ollama pull qwen2.5:3b
```

The [Ollama model library](https://ollama.com/library/qwen2.5) lists other model sizes. Binding to `0.0.0.0` lets Docker Desktop reach Ollama, and also makes Ollama reachable on your network; use it on a trusted network and limit access with your firewall. See Ollama's [network configuration notes](https://github.com/ollama/ollama/blob/main/docs/faq.mdx). The example `.env` uses `http://host.docker.internal:11434/v1`. If running the app directly with Python instead of Docker, change `OLLAMA_BASE_URL` to `http://localhost:11434/v1`. Select **Ollama (local)** in the provider menu. The app uses Ollama's [OpenAI-compatible API](https://docs.ollama.com/api/openai-compatibility). For local portfolio analysis, use an installed model that supports tool calling; Finance Agent is suitable for testing basic questions.

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

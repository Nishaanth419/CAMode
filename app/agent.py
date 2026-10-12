"""OpenAI-compatible provider chat loop with INDmoney MCP tools."""

from __future__ import annotations

import json
import math
import os
from typing import Any

import httpx

from .mcp_client import call_mcp_tool, fetch_mcp_session

PORTFOLIO_SYSTEM_PROMPT = """You are Portfolio Analyzer, a helpful assistant that answers questions using available INDmoney MCP tools. Use earlier user and assistant messages as context, especially for follow-up questions.

Call relevant tools directly when the user asks about their account. Use only data returned by tools; never invent holdings, balances, transactions, or performance. If a tool fails, report its exact error and do not claim that data was retrieved or retry speculatively.

When tool results contain comparable numeric data (such as holdings, allocations, balances over time, or category totals), proactively include one chart in a `chart` fenced block using valid JSON, for example:
```chart
{"type":"bar","title":"Current value by holding","unit":"₹","data":[{"label":"Fund A","value":125000}]}
```
Use `bar` to compare categories and `donut` for a part-to-whole split. Include only relevant items (usually 5–10), explain the largest concentration in the surrounding text, and chart only values returned by tools. If a tool fails or provides no numeric data, explain that clearly and do not fabricate a chart."""

FINANCE_SYSTEM_PROMPT = """You are Finance Agent in CA Mode. Answer basic, general finance questions in clear, plain language. Explain concepts, common terms, budgeting, saving, investing basics, and how financial products work. Do not claim to know the user's personal finances or INDmoney portfolio. Do not provide personalized investment, tax, or legal advice, and do not promise returns. When a question depends on the user's account, explain that they can switch to Portfolio Analyzer and connect INDmoney. Use earlier messages as context for follow-up questions."""

PROVIDERS = {
    "openai": {
        "label": "OpenAI",
        "key_env": "OPENAI_API_KEY",
        "base_url_env": "OPENAI_BASE_URL",
        "default_base_url": "https://api.openai.com/v1",
        "model_env": "OPENAI_MODEL",
        "default_model": "gpt-4o",
    },
    "kimi": {
        "label": "Kimi",
        "key_env": "KIMI_API_KEY",
        "alternate_key_env": "MOONSHOT_API_KEY",
        "base_url_env": "KIMI_BASE_URL",
        "default_base_url": "https://api.moonshot.ai/v1",
        "model_env": "KIMI_MODEL",
        "default_model": "kimi-k3",
    },
    "groq": {
        "label": "Groq",
        "key_env": "GROQ_API_KEY",
        "base_url_env": "GROQ_BASE_URL",
        "default_base_url": "https://api.groq.com/openai/v1",
        "model_env": "GROQ_MODEL",
        "default_model": "openai/gpt-oss-120b",
    },
    "ollama": {
        "label": "Ollama (local)",
        "local": True,
        "base_url_env": "OLLAMA_BASE_URL",
        "default_base_url": "http://host.docker.internal:11434/v1",
        "model_env": "OLLAMA_MODEL",
        "default_model": "qwen2.5:3b",
    },
}

# Groq's on-demand tier has a relatively tight tokens-per-minute budget. Keep
# enough headroom for completion tokens as well as the serialized prompt and
# tool definitions. This is a conservative estimate based on request size.
def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


GROQ_INPUT_TOKEN_BUDGET = _positive_env_int("GROQ_INPUT_TOKEN_BUDGET", 5600)
GROQ_COMPLETION_TOKEN_LIMIT = _positive_env_int("GROQ_COMPLETION_TOKEN_LIMIT", 1200)


def _estimated_request_tokens(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
    serialized = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False, separators=(",", ":"))
    # JSON and tool schemas tend to tokenize more densely than ordinary prose.
    return math.ceil(len(serialized) / 1.8)


def _fit_groq_context(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the newest chat context that fits Groq's input budget."""
    if not messages:
        return messages

    # Long provider descriptions cost tokens but don't help the model choose
    # the tool; preserve the actual input schema unchanged.
    compact_tools = []
    for tool in tools:
        item = dict(tool)
        function = dict(item.get("function") or {})
        description = function.get("description")
        if isinstance(description, str) and len(description) > 500:
            function["description"] = description[:497] + "..."
        item["function"] = function
        compact_tools.append(item)

    system = messages[0]
    last_user_index = next((i for i in range(len(messages) - 1, 0, -1) if messages[i].get("role") == "user"), len(messages) - 1)
    history = messages[1:last_user_index]
    current_user = dict(messages[last_user_index])
    suffix = [dict(item) for item in messages[last_user_index + 1:]]

    # Bound current-turn tool data if it is exceptionally large. Keep the start
    # and end of each result so identifiers and summary fields are more likely
    # to survive; mark that the omitted middle must not be treated as complete.
    for item in suffix:
        if item.get("role") == "tool" and isinstance(item.get("content"), str) and len(item["content"]) > 6000:
            content = item["content"]
            item["content"] = content[:4000] + "\n...[tool result shortened for request size]...\n" + content[-1500:]

    if isinstance(current_user.get("content"), str) and len(current_user["content"]) > 8000:
        current_user["content"] = current_user["content"][:8000] + "\n...[message shortened for request size]"

    result = [system, current_user, *suffix]
    if _estimated_request_tokens(result, compact_tools) > GROQ_INPUT_TOKEN_BUDGET:
        # Further reduce oversized tool results before discarding chat history.
        for item in sorted((m for m in suffix if m.get("role") == "tool"), key=lambda m: len(m.get("content", "")), reverse=True):
            content = item.get("content", "")
            if len(content) > 1500:
                item["content"] = content[:1200] + "\n...[tool result shortened]"
                result = [system, current_user, *suffix]
                if _estimated_request_tokens(result, compact_tools) <= GROQ_INPUT_TOKEN_BUDGET:
                    break

    if _estimated_request_tokens(result, compact_tools) > GROQ_INPUT_TOKEN_BUDGET:
        raise RuntimeError(
            "This request is still too large for Groq after shortening chat history and tool results. "
            "Try a shorter question or select OpenAI/Kimi for this request."
        )

    # Add recent history one message at a time, newest first, stopping when the
    # prompt is full. The saved conversation itself remains untouched.
    selected_history: list[dict[str, Any]] = []
    for item in reversed(history):
        candidate_history = [dict(item), *selected_history]
        candidate = [system, *candidate_history, current_user, *suffix]
        if _estimated_request_tokens(candidate, compact_tools) <= GROQ_INPUT_TOKEN_BUDGET:
            selected_history = candidate_history
        else:
            break

    return [system, *selected_history, current_user, *suffix]


def provider_config(provider: str) -> dict[str, str]:
    definition = PROVIDERS.get(provider)
    if not definition:
        raise RuntimeError(f"Unknown AI provider: {provider}")
    if definition.get("local"):
        api_key = os.getenv("OLLAMA_API_KEY") or "ollama"
    else:
        api_key = os.getenv(definition["key_env"], "")
        if not api_key and definition.get("alternate_key_env"):
            api_key = os.getenv(definition["alternate_key_env"], "")
    if not definition.get("local") and (not api_key or api_key == "sk-your-key-here"):
        raise RuntimeError(f"{definition['label']} API key is not configured. Add {definition['key_env']} to .env and restart the app.")
    return {
        "label": definition["label"],
        "api_key": api_key,
        "base_url": os.getenv(definition["base_url_env"], definition["default_base_url"]).rstrip("/"),
        "model": os.getenv(definition["model_env"], definition["default_model"]),
    }


def configured_providers() -> list[dict[str, str]]:
    available = []
    for provider, definition in PROVIDERS.items():
        if definition.get("local"):
            configured = True
        else:
            key = os.getenv(definition["key_env"], "")
            if not key and definition.get("alternate_key_env"):
                key = os.getenv(definition["alternate_key_env"], "")
            configured = bool(key and key != "sk-your-key-here")
        available.append({
            "id": provider,
            "label": definition["label"],
            "model": os.getenv(definition["model_env"], definition["default_model"]),
            "configured": configured,
        })
    return available


def default_provider() -> str:
    configured = [item for item in configured_providers() if item["configured"]]
    requested = os.getenv("AI_PROVIDER", "openai").strip().lower()
    if any(item["id"] == requested for item in configured):
        return requested
    return configured[0]["id"] if configured else "openai"


async def run_agent(
    message: str,
    mcp_url: str,
    token: str | None,
    history: list[dict[str, str]],
    agent_type: str = "finance",
    provider: str = "openai",
) -> str:
    config = provider_config(provider)
    session = None
    tools = []
    if agent_type == "portfolio":
        if not token:
            raise RuntimeError("Connect your INDmoney account to use Portfolio Analyzer.")
        session = await fetch_mcp_session(mcp_url, token)
        tools = session["tools"]
    tool_map = {tool["name"]: tool for tool in tools}
    model_tools = [
        {"type": "function", "function": {
            "name": tool["name"],
            "description": tool.get("description") or f"Call the {tool['name']} tool",
            "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
        }}
        for tool in tools
    ]
    if provider == "groq":
        for tool in model_tools:
            description = tool["function"].get("description", "")
            if len(description) > 500:
                tool["function"]["description"] = description[:497] + "..."
    prompt = PORTFOLIO_SYSTEM_PROMPT if agent_type == "portfolio" else FINANCE_SYSTEM_PROMPT
    messages: list[dict[str, Any]] = [{"role": "system", "content": prompt}]
    messages.extend({"role": item["role"], "content": item["content"]} for item in history)
    messages.append({"role": "user", "content": message})

    async with httpx.AsyncClient(timeout=120) as client:
        for _ in range(5):
            request_messages = _fit_groq_context(messages, model_tools) if provider == "groq" else messages
            payload: dict[str, Any] = {"model": config["model"], "messages": request_messages}
            if provider == "groq":
                payload["max_tokens"] = GROQ_COMPLETION_TOKEN_LIMIT
            if model_tools:
                payload["tools"] = model_tools
                payload["tool_choice"] = "auto"
            try:
                response = await client.post(
                    f"{config['base_url']}/chat/completions",
                    headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"},
                    json=payload,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if provider == "ollama":
                    raise RuntimeError(
                        f"Cannot reach Ollama at {config['base_url']}. Start Ollama on this computer, "
                        f"then run `ollama pull {config['model']}`."
                    ) from exc
                raise
            if not response.is_success:
                raise RuntimeError(f"{config['label']} request failed: HTTP {response.status_code} {response.text[:800]}")
            choice = response.json()["choices"][0]["message"]
            calls = choice.get("tool_calls") or []
            if not calls:
                return choice.get("content") or ""
            messages.append({"role": "assistant", "content": choice.get("content"), "tool_calls": calls})
            for call in calls:
                function = call.get("function", {})
                name = function.get("name", "")
                if name not in tool_map:
                    raise RuntimeError(f"Model requested an unknown INDmoney tool: {name}")
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                    result = await call_mcp_tool(
                        mcp_url, token, session["protocolVersion"], session["sessionId"], name, arguments,
                    )
                except Exception as exc:
                    raise RuntimeError(f"INDmoney data request failed: {exc}") from exc
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": str(result)})
    return "I could not finish processing the INDmoney response within the tool-call limit."

"""OpenAI-compatible provider chat loop with INDmoney MCP tools."""

from __future__ import annotations

import json
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
}


def provider_config(provider: str) -> dict[str, str]:
    definition = PROVIDERS.get(provider)
    if not definition:
        raise RuntimeError(f"Unknown AI provider: {provider}")
    api_key = os.getenv(definition["key_env"], "")
    if not api_key and definition.get("alternate_key_env"):
        api_key = os.getenv(definition["alternate_key_env"], "")
    if not api_key or api_key == "sk-your-key-here":
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
        key = os.getenv(definition["key_env"], "")
        if not key and definition.get("alternate_key_env"):
            key = os.getenv(definition["alternate_key_env"], "")
        available.append({
            "id": provider,
            "label": definition["label"],
            "model": os.getenv(definition["model_env"], definition["default_model"]),
            "configured": bool(key and key != "sk-your-key-here"),
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
    prompt = PORTFOLIO_SYSTEM_PROMPT if agent_type == "portfolio" else FINANCE_SYSTEM_PROMPT
    messages: list[dict[str, Any]] = [{"role": "system", "content": prompt}]
    messages.extend({"role": item["role"], "content": item["content"]} for item in history)
    messages.append({"role": "user", "content": message})

    async with httpx.AsyncClient(timeout=120) as client:
        for _ in range(5):
            payload: dict[str, Any] = {"model": config["model"], "messages": messages}
            if model_tools:
                payload["tools"] = model_tools
                payload["tool_choice"] = "auto"
            response = await client.post(
                f"{config['base_url']}/chat/completions",
                headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"},
                json=payload,
            )
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

"""OpenAI-compatible provider chat loop with INDmoney MCP tools."""

from __future__ import annotations

import json
import logging
import math
import os
from typing import Any, AsyncIterator

import httpx

from .mcp_client import call_mcp_tool, fetch_mcp_session, filter_portfolio_tools

logger = logging.getLogger("ca_mode.agent")

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
    "groq": {
        "label": "Groq",
        "key_env": "GROQ_API_KEY",
        "base_url_env": "GROQ_BASE_URL",
        "default_base_url": "https://api.groq.com/openai/v1",
        "model_env": "GROQ_MODEL",
        "default_model": "openai/gpt-oss-120b",
    },
}

def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


GROQ_INPUT_TOKEN_BUDGET = _positive_env_int("GROQ_INPUT_TOKEN_BUDGET", 5600)
GROQ_COMPLETION_TOKEN_LIMIT = _positive_env_int("GROQ_COMPLETION_TOKEN_LIMIT", 1200)


def _estimated_request_tokens(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
    serialized = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False, separators=(",", ":"))
    return math.ceil(len(serialized) / 1.8)


def _fit_groq_context(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the newest chat context that fits Groq's input budget."""
    if not messages:
        return messages

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

    for item in suffix:
        if item.get("role") == "tool" and isinstance(item.get("content"), str) and len(item["content"]) > 6000:
            content = item["content"]
            item["content"] = content[:4000] + "\n...[tool result shortened for request size]...\n" + content[-1500:]

    if isinstance(current_user.get("content"), str) and len(current_user["content"]) > 8000:
        current_user["content"] = current_user["content"][:8000] + "\n...[message shortened for request size]"

    result = [system, current_user, *suffix]
    if _estimated_request_tokens(result, compact_tools) > GROQ_INPUT_TOKEN_BUDGET:
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
            "Try a shorter question or select OpenAI for this request."
        )

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
    api_key = os.getenv(definition["key_env"], "")
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


# ── SSE event helpers ──────────────────────────────────────────────────────────

def _sse(event: str, data: Any) -> str:
    """Format a single SSE message."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ── Streaming agent ────────────────────────────────────────────────────────────

async def stream_agent(
    message: str,
    mcp_url: str,
    token: str | None,
    history: list[dict[str, str]],
    agent_type: str = "finance",
    provider: str = "openai",
) -> AsyncIterator[str]:
    """
    Async generator that yields SSE-formatted strings.

    Event types:
      • tool_call   – { name, arguments }          tool is about to be called
      • tool_result – { name, result, error? }      tool returned
      • content     – { text }                      streamed answer chunk
      • error       – { message }                   fatal error
      • done        – {}                            stream finished
    """
    config = provider_config(provider)
    session = None
    tools: list[dict[str, Any]] = []

    if agent_type == "portfolio":
        if not token:
            yield _sse("error", {"message": "Connect your INDmoney account to use Portfolio Analyzer."})
            yield _sse("done", {})
            return
        try:
            session = await fetch_mcp_session(mcp_url, token)
        except Exception as exc:
            yield _sse("error", {"message": f"Could not connect to INDmoney: {exc}"})
            yield _sse("done", {})
            return
        tools = filter_portfolio_tools(session["tools"])
        if not tools:
            yield _sse("error", {"message": "INDmoney did not provide any supported portfolio tools. Reconnect INDmoney or check the MCP server."})
            yield _sse("done", {})
            return

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

    retried_portfolio_tool_selection = False
    portfolio_tool_called = False
    full_answer = ""

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            for _ in range(5):
                try:
                    request_messages = _fit_groq_context(messages, model_tools) if provider == "groq" else messages
                except RuntimeError as exc:
                    yield _sse("error", {"message": str(exc)})
                    yield _sse("done", {})
                    return

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
                    yield _sse("error", {"message": f"Could not connect to {config['label']}: {exc}"})
                    yield _sse("done", {})
                    return

                if not response.is_success:
                    yield _sse("error", {"message": f"{config['label']} request failed: HTTP {response.status_code} {response.text[:800]}"})
                    yield _sse("done", {})
                    return

                completion = response.json()
                choice_data = completion["choices"][0]
                choice = choice_data["message"]
                calls = choice.get("tool_calls") or []

                if not calls:
                    content = choice.get("content")
                    if isinstance(content, list):
                        content = "\n".join(
                            part["text"] for part in content
                            if isinstance(part, dict) and isinstance(part.get("text"), str)
                        )

                    if agent_type == "portfolio" and not portfolio_tool_called:
                        if not retried_portfolio_tool_selection:
                            logger.warning("Portfolio model returned no INDmoney tool call; retrying")
                            retried_portfolio_tool_selection = True
                            messages.append({"role": "assistant", "content": content or ""})
                            messages.append({
                                "role": "user",
                                "content": "Do not tell me to run an account check. Use one of the available INDmoney tools now to retrieve the account data needed for my original question. After the tool returns, answer using that data only.",
                            })
                            continue
                        yield _sse("error", {"message": f"{config['label']} did not call an INDmoney account tool. No portfolio summary was generated; try a stronger tool-capable model or another provider."})
                        yield _sse("done", {})
                        return

                    if isinstance(content, str) and content.strip():
                        # Stream the content word by word for live feel
                        words = content.split(" ")
                        chunk_size = 4
                        for i in range(0, len(words), chunk_size):
                            chunk = " ".join(words[i:i + chunk_size])
                            if i + chunk_size < len(words):
                                chunk += " "
                            full_answer += chunk
                            yield _sse("content", {"text": chunk})
                        yield _sse("done", {"answer": full_answer})
                        return

                    yield _sse("error", {"message": f"{config['label']} returned an empty response. Check that the selected model is available and try again."})
                    yield _sse("done", {})
                    return

                # Process tool calls
                messages.append({"role": "assistant", "content": choice.get("content"), "tool_calls": calls})
                for call in calls:
                    function = call.get("function", {})
                    name = function.get("name", "")
                    if name not in tool_map:
                        yield _sse("error", {"message": f"Model requested an unknown INDmoney tool: {name}"})
                        yield _sse("done", {})
                        return

                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        arguments = {}

                    # Emit tool_call event
                    yield _sse("tool_call", {"name": name, "arguments": arguments})

                    try:
                        result = await call_mcp_tool(
                            mcp_url, token, session["protocolVersion"], session["sessionId"], name, arguments,
                        )
                        if agent_type == "portfolio":
                            portfolio_tool_called = True
                        # Emit tool_result event
                        result_str = str(result)
                        yield _sse("tool_result", {
                            "name": name,
                            "result": result_str[:4000] + ("…" if len(result_str) > 4000 else ""),
                        })
                        messages.append({"role": "tool", "tool_call_id": call["id"], "content": result_str})
                    except Exception as exc:
                        yield _sse("tool_result", {"name": name, "error": str(exc)})
                        yield _sse("error", {"message": f"INDmoney data request failed: {exc}"})
                        yield _sse("done", {})
                        return

    except Exception as exc:
        yield _sse("error", {"message": str(exc)})
        yield _sse("done", {})
        return

    yield _sse("error", {"message": "Could not finish processing the INDmoney response within the tool-call limit."})
    yield _sse("done", {})


# ── Legacy non-streaming wrapper (kept for internal use) ───────────────────────

async def run_agent(
    message: str,
    mcp_url: str,
    token: str | None,
    history: list[dict[str, str]],
    agent_type: str = "finance",
    provider: str = "openai",
) -> str:
    """Collect the full answer from stream_agent (no SSE, just the text)."""
    answer_parts: list[str] = []
    last_error: str | None = None
    async for raw in stream_agent(message, mcp_url, token, history, agent_type, provider):
        for line in raw.splitlines():
            if line.startswith("data:"):
                payload = json.loads(line[5:].strip())
                event_type = None
                for el in raw.splitlines():
                    if el.startswith("event:"):
                        event_type = el[6:].strip()
                if event_type == "content":
                    answer_parts.append(payload.get("text", ""))
                elif event_type == "error":
                    last_error = payload.get("message", "Unknown error")
    answer = "".join(answer_parts)
    if not answer and last_error:
        raise RuntimeError(last_error)
    return answer

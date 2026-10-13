"""OpenAI-compatible provider chat loop with INDmoney MCP tools."""

from __future__ import annotations

import json
import logging
import math
import os
import time
from typing import Any, AsyncIterator

import httpx

from .mcp_client import call_mcp_tool, fetch_mcp_session, filter_portfolio_tools

logger = logging.getLogger("ca_mode.agent")

PORTFOLIO_SYSTEM_PROMPT = """You are Portfolio Analyzer, a helpful assistant that answers questions using available INDmoney MCP tools. Use earlier user and assistant messages as context, especially for follow-up questions.

Answer only what the user asked. Keep the response proportional to the question. Do not add unsolicited next steps, action plans, recommendations, or suggestions for what to invest in. In particular, when asked to analyze an equity or holding, provide the requested analysis and stop; do not append future-investment ideas or a generic “what to do next” section. Offer next steps or future-investment ideas only when the user explicitly asks for them.

Call relevant tools directly when the user asks about their account. Use only data returned by tools; never invent holdings, balances, transactions, returns, dates, or benchmark values. If a tool fails, report its exact error and do not claim that data was retrieved or retry speculatively. The connected tools may provide current holdings and SIPs but not historical performance or benchmark series. Compare portfolio returns with a benchmark only when the retrieved data includes comparable return values for both over the same period and on the same basis. State the period and source when available; otherwise say that the comparison needs historical portfolio and benchmark data and do not estimate it.

Use clear headings and explain what the numbers mean. When useful numeric data is available, include a detailed, data-driven visualization in a `chart` fenced block. Supported formats are `bar` or `donut` with `data:[{"label":"...","value":123}]`, and `line` with `series:[{"name":"Portfolio","data":[{"label":"Jan","value":2.3}]}]`. Example for a valid return comparison:
```chart
{"type":"line","title":"Cumulative returns vs benchmark","unit":"%","series":[{"name":"Portfolio","data":[{"label":"Jan","value":2.3},{"label":"Feb","value":4.1}]},{"name":"Benchmark","data":[{"label":"Jan","value":1.8},{"label":"Feb","value":3.2}]}]}
```
Use line charts for dated trends or return comparisons, bars for category/holding comparisons, and donuts only for allocation splits. Include about 5–10 useful items, keep series labels and time periods aligned, and chart only tool-returned values. Never present current balances as returns. If no suitable numeric data is available, explain why and do not fabricate a chart.

If explicitly asked what to invest in next, offer cautious, educational ideas to research based on the information actually available (for example, diversification, costs, risk, time horizon, liquidity, and overlap with existing holdings). Do not tell the user to buy or sell a specific security, promise returns, or claim an investment is suitable for them. Clearly separate observations from possibilities and mention missing information that affects the decision. Whenever discussing future investment ideas, end with: “This is not investment advice. Research each investment, its risks, costs, and fit for your circumstances before investing; consider consulting a qualified financial adviser.”"""

FINANCE_SYSTEM_PROMPT = """You are Finance Agent in CA Mode. Answer only what the user asked, in clear and plain language; keep the answer proportional and do not add unsolicited next steps or investment suggestions. Explain concepts, common terms, budgeting, saving, investing basics, and how financial products work. Do not claim to know the user's personal finances or INDmoney portfolio. Do not provide personalized investment, tax, or legal advice, and do not promise returns. Offer possible investments only if the user explicitly asks for ideas; then frame them as educational topics to research, discuss their risks and costs, and say: “This is not investment advice. Research each investment, its risks, costs, and fit for your circumstances before investing; consider consulting a qualified financial adviser.” When a question depends on the user's account, explain that they can switch to Portfolio Analyzer and connect INDmoney. Use earlier messages as context for follow-up questions."""

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
      • model_call  – { provider, model, attempt }  model endpoint called
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
            yield _sse("error", {"message": "INDmoney did not provide any of CA Mode's supported portfolio tools. Reconnect INDmoney or check the MCP server."})
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

                yield _sse("model_call", {
                    "provider": config["label"], "model": config["model"], "attempt": _ + 1,
                })
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

class AgentResult(str):
    """String answer with compact trace metadata for the chat UI."""

    def __new__(cls, answer: str, trace: list[dict[str, Any]]):
        value = super().__new__(cls, answer)
        value.trace = trace
        return value


class AgentRunError(RuntimeError):
    def __init__(self, message: str, trace: list[dict[str, Any]]):
        super().__init__(message)
        self.trace = trace


async def run_agent(
    message: str,
    mcp_url: str,
    token: str | None,
    history: list[dict[str, str]],
    agent_type: str = "finance",
    provider: str = "openai",
) -> AgentResult:
    """Collect the answer and call trace from the internal SSE event stream."""
    answer_parts: list[str] = []
    trace: list[dict[str, Any]] = []
    last_error: str | None = None
    active_model_step: dict[str, Any] | None = None

    def finish_model_step(status: str) -> None:
        if active_model_step and active_model_step.get("status") == "running":
            active_model_step["status"] = status
            active_model_step["duration_ms"] = round((time.perf_counter() - active_model_step.pop("_started")) * 1000)

    async for raw in stream_agent(message, mcp_url, token, history, agent_type, provider):
        event_type = next((line[6:].strip() for line in raw.splitlines() if line.startswith("event:")), None)
        for line in raw.splitlines():
            if line.startswith("data:"):
                payload = json.loads(line[5:].strip())
                if event_type == "content":
                    finish_model_step("completed")
                    answer_parts.append(payload.get("text", ""))
                elif event_type == "model_call":
                    finish_model_step("completed")
                    active_model_step = {
                        "kind": "model", "provider": payload.get("provider", "AI"),
                        "model": payload.get("model", ""), "attempt": payload.get("attempt", 1),
                        "status": "running", "_started": time.perf_counter(),
                    }
                    trace.append(active_model_step)
                elif event_type == "tool_call":
                    finish_model_step("completed")
                    arguments = payload.get("arguments", {})
                    arguments_text = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
                    trace.append({
                        "kind": "tool", "name": str(payload.get("name", "INDmoney tool"))[:100],
                        "arguments": arguments_text[:600], "status": "running",
                        "_started": time.perf_counter(),
                    })
                elif event_type == "tool_result":
                    name = str(payload.get("name", ""))
                    step = next((item for item in reversed(trace) if item.get("kind") == "tool" and item.get("name") == name and item.get("status") == "running"), None)
                    if step:
                        step["status"] = "failed" if payload.get("error") else "completed"
                        step["duration_ms"] = round((time.perf_counter() - step.pop("_started")) * 1000)
                        if payload.get("result"):
                            step["result_preview"] = str(payload["result"])[:1000]
                        if payload.get("error"):
                            step["error"] = str(payload["error"])[:300]
                elif event_type == "error":
                    finish_model_step("failed")
                    last_error = payload.get("message", "Unknown error")
    finish_model_step("completed")
    answer = "".join(answer_parts)
    if not answer and last_error:
        raise AgentRunError(last_error, trace[:12])
    return AgentResult(answer, trace[:12])

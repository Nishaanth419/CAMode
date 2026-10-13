"""OpenAI-compatible provider chat loop with INDmoney MCP tools."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from typing import Any, AsyncIterator

import httpx

from .mcp_client import call_mcp_tool, fetch_mcp_session, filter_portfolio_tools

logger = logging.getLogger("ca_mode.agent")

FINANCE_ONLY_NOTICE = "I can help with finance questions and portfolio analysis. Ask me about budgeting, saving, banking, investing concepts, financial products, or your connected portfolio."

_CODE_REQUEST_RE = re.compile(
    r"\b(?:code|coding|programming|script|snippet|source\s+file|"
    r"python|javascript|typescript|java|c\+\+|rust|html|css)\b|"
    r"\b(?:write|create|generate|build|make|show|give|debug|fix|implement)\b"
    r".{0,80}\b(?:program|function|algorithm)\b",
    re.IGNORECASE,
)
# Finance identifiers that contain the word "code" (IFSC code, ISIN code, ...) are
# removed before the code-request check so they are not refused as programming.
_FINANCE_CODE_TERM_RE = re.compile(
    r"\b(?:ifsc|swift|isin|bic|micr|hsn|sac|sort|tax|bank|branch|scheme|amfi|nse|bse|"
    r"ticker|stock|security|pin|zip|postal|routing|promo|referral|coupon)\s+codes?\b",
    re.IGNORECASE,
)
_FINANCE_TOPIC_RE = re.compile(
    r"\b(?:financ\w*|money|budget\w*|saving\w*|spend\w*|bank\w*|cash|income|salary|"
    r"expense\w*|debt|loan\w*|credit|mortgage|interest|tax\w*|insurance|retire\w*|"
    r"invest\w*|stock\w*|share\w*|equity|bond\w*|portfolio|holding\w*|account|net\s+worth|"
    r"balance\w*|allocation\w*|transaction\w*|mutual\s+fund\w*|funds?|nav|aum|xirr|p\s*&\s*l|"
    r"etf\w*|sip\w*|dividend\w*|return\w*|benchmark\w*|asset\w*|liabilit\w*|"
    r"inflation|market\w*|crypto\w*|pension|rupee\w*|dollar\w*|price\w*|quote\w*|ticker\w*|"
    r"nifty|sensex|index\w*|option\w*|derivative\w*|f\s*&\s*o|futures?|expiry|strike\w*|"
    r"candle\w*|ohlc|watchlist|analyst\w*|target\s+price|sentiment|news|greeks?|delta|gamma|"
    r"theta|vega|iv|vwap|pcr|max\s+pain|breakout\w*|support|resistance|volatil\w*|"
    r"expense\s+ratio|category|large.?cap|mid.?cap|small.?cap|elss|ppf|epf|nps|fixed\s+deposit|"
    r"real\s+estate|margin|breakeven|risk.?reward|call\s+oi|put\s+oi|"
    r"parag\s+parikh|flexi.?cap|ifsc|swift|isin|bic|micr|hsn|cheque\w*)\b",
    re.IGNORECASE,
)
_GREETING_RE = re.compile(r"^(?:hi|hello|hey|good\s+(?:morning|afternoon|evening))[!.?\s]*$", re.IGNORECASE)
_FINANCE_FOLLOWUP_RE = re.compile(
    r"^(?:and\b|also\b|what\s+about\b|how\s+about\b|why\b|how\b|"
    r"what\s+does\s+(?:it|that|this)\b|what\s+if\b|compare\s+(?:it|that|this)\b|"
    r"explain\s+(?:it|that|this)\b|break\s+(?:it|that|this)\s+down\b|"
    r"can\s+you\s+(?:explain|compare|clarify)\s+(?:it|that|this)\b)",
    re.IGNORECASE,
)
# Case-sensitive on purpose: Python/JS keywords are lowercase, while finance prose
# such as "Let P = principal", "Class A: ..." or "Import duties ..." is capitalised.
_CODE_OUTPUT_RE = re.compile(
    r"```(?!chart\s*(?:\n|$))|(?:^|\n)\s*(?:import\s+\w+|from\s+\w+\s+import\s+|"
    r"def\s+\w+\s*\(|class\s+\w+\s*[:(]|print\s*\(|(?:const|let|var)\s+\w+\s*=)|"
    r"if\s+__name__\s*==",
    re.MULTILINE,
)
TRUNCATION_NOTICE = "\n\n_(This response was cut off at the model's length limit. Ask a narrower question for the rest.)_"
_INDMONEY_DATA_REQUEST_RE = re.compile(
    r"\b(?:my\s+(?:portfolio|net\s+worth|holdings|stocks?|funds?|sips?|watchlist|account)|"
    r"portfolio|holdings|net\s+worth|allocation|watchlist|sips?|option\s+chain|options?|"
    r"derivatives?|f\s*&\s*o|iron\s+condor|put\s+spread|call\s+spread|strangle|straddle|"
    r"greeks?|ohlc|candles?|chart|price|quotes?|analyst|target\s+price|sentiment|news|"
    r"breakout|support|resistance|vwap|market\s+cap|xirr|p\s*&\s*l|exposure|concentration|"
    r"sector\s+(?:breakdown|exposure|allocation)|underperform\w*|outperform\w*|"
    r"dues|expiry|nifty|sensex|"
    r"latest\s+news|live\s+(?:market|price)|current\s+price|top\s+\d+\s+.+funds?|"
    r"best\s+.+funds?|compare\s+.+funds?|deep\s+dive\s+.+funds?)\b",
    re.IGNORECASE,
)


def _blocked_request_reason(message: str, history: list[dict[str, str]]) -> str | None:
    """Apply request boundaries before any user text reaches the model."""
    if _CODE_REQUEST_RE.search(_FINANCE_CODE_TERM_RE.sub(" ", message)):
        return "I can answer finance questions, but I can’t write or debug code. " + FINANCE_ONLY_NOTICE

    if not _GREETING_RE.fullmatch(message.strip()):
        # Only the user's own recent turns establish finance context; assistant
        # replies (including FINANCE_ONLY_NOTICE itself) are full of finance words.
        context = " ".join(item.get("content", "") for item in history[-6:] if item.get("role") == "user")
        finance_topic_in_message = bool(_FINANCE_TOPIC_RE.search(message))
        finance_followup = bool(_FINANCE_FOLLOWUP_RE.search(message.strip()))
        finance_topic_in_context = bool(_FINANCE_TOPIC_RE.search(context))
        if not (finance_topic_in_message or (finance_followup and finance_topic_in_context)):
            return FINANCE_ONLY_NOTICE
    return None


def _requires_indmoney_data(message: str, history: list[dict[str, str]]) -> bool:
    """Identify account/live-data requests; general finance questions need no MCP call."""
    if _INDMONEY_DATA_REQUEST_RE.search(message):
        return True
    # Keep tool-backed follow-ups attached to a recent MCP result.
    recent = " ".join(item.get("content", "") for item in history[-4:])
    return bool(_FINANCE_FOLLOWUP_RE.match(message.strip()) and _INDMONEY_DATA_REQUEST_RE.search(recent))


def _contains_code_output(content: str) -> bool:
    """Reject code-shaped model output but allow the app's chart data blocks."""
    code_fences = re.findall(r"```([^\n`]*)", content)[::2]
    if any(label.strip().lower() != "chart" for label in code_fences):
        return True
    without_chart_blocks = re.sub(r"```chart\s*.*?```", "", content, flags=re.IGNORECASE | re.DOTALL)
    return bool(_CODE_OUTPUT_RE.search(without_chart_blocks))

PORTFOLIO_SYSTEM_PROMPT = """You are Portfolio Analyzer in CA Mode. Answer personal-finance, investment, portfolio, and market-data questions using the available INDmoney MCP tools. Stay within those topics; politely decline unrelated requests. Never write, explain, or debug programming code. The MCP tools are read-only: never claim to place trades, alter SIPs, move money, or change the account. Use earlier user and assistant messages for relevant follow-ups, but answer only the current request.

Answer only what the user asked. Keep the response proportional to the question. Do not add unsolicited next steps, action plans, recommendations, or suggestions for what to invest in. In particular, when asked to analyze an equity or holding, provide the requested analysis and stop; do not append future-investment ideas or a generic “what to do next” section. Offer next steps or future-investment ideas only when the user explicitly asks for them.

Choose tools from their names, descriptions, and input schemas. Resolve a named stock, index, derivative, or fund with `lookup_ind_keys` when another tool requires its INDmoney identifier. For portfolio health and allocation questions, use the net-worth snapshot, relevant allocation breakdowns, and holdings. For listed-stock price, chart, and technical questions, use live details and OHLC tools; use watchlist first for watchlist scans. For options questions, retrieve the option chain and, when relevant, Greeks history before calculating scenario payoffs. For fund discovery, use category search; for a named fund or comparisons, use fund details and request comparable return periods, benchmark, fee, allocation, or holdings data as needed. Use SIP tools for the requested SIP type. Chain multiple read-only calls when the question needs them, and stop once the requested facts are supported.

Use only tool-returned values; never invent holdings, balances, transactions, live quotes, historical prices, returns, analyst targets, news, option Greeks, margin, or benchmark values. If a tool fails or returns no data, say which requested data is unavailable. Compare returns only for matching periods and bases. For calculations, show the inputs and formula in plain language; calculate only from returned values or assumptions clearly supplied by the user. If a margin requirement or other value is not returned and cannot be derived from supplied data, say so instead of estimating it. Never claim a trade or strategy is suitable or tell the user to enter it. When discussing an options strategy, state that it is a hypothetical scenario, not a trade recommendation, and include the key loss exposure and assumptions.

Use clear headings and explain what the numbers mean. When useful numeric data is available, include a detailed, data-driven visualization in a `chart` fenced block. Supported formats are `bar` or `donut` with `data:[{"label":"...","value":123}]`, `line` with `series:[{"name":"Portfolio","data":[{"label":"Jan","value":2.3}]}]`, and `candlestick` with `candles:[{"label":"2026-01-01","open":100,"high":105,"low":98,"close":103}]`. Use a candlestick chart only with complete OHLC values returned by the market-data tool; otherwise use a line chart of returned closes. Example for a valid return comparison:
```chart
{"type":"line","title":"Cumulative returns vs benchmark","unit":"%","series":[{"name":"Portfolio","data":[{"label":"Jan","value":2.3},{"label":"Feb","value":4.1}]},{"name":"Benchmark","data":[{"label":"Jan","value":1.8},{"label":"Feb","value":3.2}]}]}
```
Use line charts for dated trends or return comparisons, bars for category/holding comparisons, and donuts only for allocation splits. Include about 5–10 useful items, keep series labels and time periods aligned, and chart only tool-returned values. Never present current balances as returns. If no suitable numeric data is available, explain why and do not fabricate a chart.

If explicitly asked what to invest in next, offer cautious, educational ideas to research based on the information actually available (for example, diversification, costs, risk, time horizon, liquidity, and overlap with existing holdings). Do not tell the user to buy or sell a specific security, promise returns, or claim an investment is suitable for them. Clearly separate observations from possibilities and mention missing information that affects the decision. Whenever discussing future investment ideas, end with: “This is not investment advice. Research each investment, its risks, costs, and fit for your circumstances before investing; consider consulting a qualified financial adviser.”"""

FINANCE_SYSTEM_PROMPT = """You are Finance Agent in CA Mode. Answer only finance questions, in clear and plain language; keep the answer proportional and do not add unsolicited next steps or investment suggestions. If asked for anything outside finance, briefly decline and invite a finance question. Never write, explain, or debug programming code, even if asked. Explain concepts, common terms, budgeting, saving, investing basics, and how financial products work. Do not claim to know the user's personal finances or INDmoney portfolio. Do not provide personalized investment, tax, or legal advice, and do not promise returns. Offer possible investments only if the user explicitly asks for ideas; then frame them as educational topics to research, discuss their risks and costs, and say: “This is not investment advice. Research each investment, its risks, costs, and fit for your circumstances before investing; consider consulting a qualified financial adviser.” When a question depends on the user's account, explain that they can switch to Portfolio Analyzer and connect INDmoney. Use earlier messages as context for finance follow-up questions."""

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


def _fit_groq_context(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    current_user_index: int | None = None,
) -> list[dict[str, Any]]:
    """Keep the newest chat context that fits Groq's input budget.

    The system prompt, the current user question (``current_user_index``; defaults
    to the last user message) and everything after it are always kept, so an
    injected retry instruction can never push the real question out of context.
    """
    if not messages:
        return messages

    compact_tools = tools
    system = messages[0]
    if current_user_index is None:
        current_user_index = next(
            (i for i in range(len(messages) - 1, 0, -1) if messages[i].get("role") == "user"),
            len(messages) - 1,
        )
    history = messages[1:current_user_index]
    current_user = dict(messages[current_user_index])
    suffix = [dict(item) for item in messages[current_user_index + 1:]]

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


# ── Agent event stream ─────────────────────────────────────────────────────────

AgentEvent = tuple[str, dict[str, Any]]


def _model_tools(tools: list[dict[str, Any]], provider: str) -> list[dict[str, Any]]:
    """Convert MCP tool definitions to OpenAI-compatible function tools."""
    model_tools = []
    for tool in tools:
        description = tool.get("description") or f"Call the {tool['name']} tool"
        # Groq's small input budget cannot afford long tool descriptions.
        if provider == "groq" and len(description) > 500:
            description = description[:497] + "..."
        model_tools.append({"type": "function", "function": {
            "name": tool["name"],
            "description": description,
            "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
        }})
    return model_tools


def _message_text(content: Any) -> str | None:
    """Flatten a chat-completions message content (string or text parts)."""
    if isinstance(content, list):
        return "\n".join(
            part["text"] for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return content if isinstance(content, str) else None


async def stream_agent(
    message: str,
    mcp_url: str,
    token: str | None,
    history: list[dict[str, str]],
    agent_type: str = "finance",
    provider: str = "openai",
) -> AsyncIterator[AgentEvent]:
    """
    Async generator that yields ``(event, data)`` tuples.

    Event types:
      • model_call  – { provider, model, attempt }  model endpoint called
      • tool_call   – { name, arguments }          tool is about to be called
      • tool_result – { name, result, error? }      tool returned
      • content     – { text }                      final answer text
      • error       – { message }                   fatal error
      • done        – {}                            stream finished
    """
    blocked_reason = _blocked_request_reason(message, history)
    if blocked_reason:
        yield "content", {"text": blocked_reason}
        yield "done", {}
        return

    config = provider_config(provider)
    session = None
    tools: list[dict[str, Any]] = []

    requires_indmoney_data = agent_type == "portfolio" and _requires_indmoney_data(message, history)
    if requires_indmoney_data:
        if not token:
            yield "error", {"message": "Connect your INDmoney account to use Portfolio Analyzer."}
            yield "done", {}
            return
        try:
            session = await fetch_mcp_session(mcp_url, token)
        except Exception as exc:
            yield "error", {"message": f"Could not connect to INDmoney: {exc}"}
            yield "done", {}
            return
        tools = filter_portfolio_tools(session["tools"])
        if not tools:
            yield "error", {"message": "INDmoney did not provide any of CA Mode's supported portfolio tools. Reconnect INDmoney or check the MCP server."}
            yield "done", {}
            return

    tool_names = {tool["name"] for tool in tools}
    model_tools = _model_tools(tools, provider)

    prompt = PORTFOLIO_SYSTEM_PROMPT if agent_type == "portfolio" else FINANCE_SYSTEM_PROMPT
    messages: list[dict[str, Any]] = [{"role": "system", "content": prompt}]
    messages.extend({"role": item["role"], "content": item["content"]} for item in history)
    messages.append({"role": "user", "content": message})
    current_user_index = len(messages) - 1

    retried_portfolio_tool_selection = False
    portfolio_tool_called = False

    try:
        async with httpx.AsyncClient(timeout=120) as client, httpx.AsyncClient(timeout=90) as mcp_client:
            for attempt in range(1, 13):
                try:
                    request_messages = (
                        _fit_groq_context(messages, model_tools, current_user_index=current_user_index)
                        if provider == "groq" else messages
                    )
                except RuntimeError as exc:
                    yield "error", {"message": str(exc)}
                    yield "done", {}
                    return

                payload: dict[str, Any] = {"model": config["model"], "messages": request_messages}
                if provider == "groq":
                    payload["max_tokens"] = GROQ_COMPLETION_TOKEN_LIMIT
                if model_tools:
                    payload["tools"] = model_tools
                    payload["tool_choice"] = "auto"

                yield "model_call", {"provider": config["label"], "model": config["model"], "attempt": attempt}
                try:
                    response = await client.post(
                        f"{config['base_url']}/chat/completions",
                        headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"},
                        json=payload,
                    )
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    yield "error", {"message": f"Could not connect to {config['label']}: {exc}"}
                    yield "done", {}
                    return

                if not response.is_success:
                    yield "error", {"message": f"{config['label']} request failed: HTTP {response.status_code} {response.text[:800]}"}
                    yield "done", {}
                    return

                completion = response.json()
                choices = completion.get("choices") if isinstance(completion, dict) else None
                choice_data = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
                choice = choice_data.get("message")
                if not isinstance(choice, dict):
                    yield "error", {"message": f"{config['label']} returned an unexpected response with no message. Try again or select another provider."}
                    yield "done", {}
                    return
                calls = choice.get("tool_calls") or []

                if not calls:
                    content = _message_text(choice.get("content"))

                    if requires_indmoney_data and not portfolio_tool_called:
                        if not retried_portfolio_tool_selection:
                            logger.warning("Portfolio model returned no INDmoney tool call; retrying")
                            retried_portfolio_tool_selection = True
                            messages.append({"role": "assistant", "content": content or ""})
                            messages.append({
                                "role": "user",
                                "content": "Do not tell me to run an account check. Use one of the available INDmoney tools now to retrieve the account data needed for my original question. After the tool returns, answer using that data only.",
                            })
                            continue
                        yield "error", {"message": f"{config['label']} did not call an INDmoney account tool. No portfolio summary was generated; try a stronger tool-capable model or another provider."}
                        yield "done", {}
                        return

                    if content and content.strip():
                        if _contains_code_output(content):
                            logger.warning("Blocked code-shaped model output for %s agent", agent_type)
                            content = FINANCE_ONLY_NOTICE
                        elif choice_data.get("finish_reason") == "length":
                            content += TRUNCATION_NOTICE
                        yield "content", {"text": content}
                        yield "done", {}
                        return

                    yield "error", {"message": f"{config['label']} returned an empty response. Check that the selected model is available and try again."}
                    yield "done", {}
                    return

                # Process tool calls
                messages.append({"role": "assistant", "content": choice.get("content"), "tool_calls": calls})
                for call in calls:
                    function = call.get("function", {})
                    name = function.get("name", "")
                    if name not in tool_names:
                        yield "error", {"message": f"Model requested an unknown INDmoney tool: {name}"}
                        yield "done", {}
                        return

                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        arguments = {}

                    yield "tool_call", {"name": name, "arguments": arguments}

                    try:
                        result = await call_mcp_tool(
                            mcp_url, token, session["protocolVersion"], session["sessionId"], name, arguments,
                            client=mcp_client,
                        )
                    except Exception as exc:
                        yield "tool_result", {"name": name, "error": str(exc)}
                        yield "error", {"message": f"INDmoney data request failed: {exc}"}
                        yield "done", {}
                        return
                    portfolio_tool_called = True
                    result_str = str(result)
                    yield "tool_result", {
                        "name": name,
                        "result": result_str[:4000] + ("…" if len(result_str) > 4000 else ""),
                    }
                    messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result_str})

    except Exception as exc:
        yield "error", {"message": str(exc)}
        yield "done", {}
        return

    yield "error", {"message": "Could not finish processing the INDmoney response within the tool-call limit."}
    yield "done", {}


# ── Non-streaming wrapper used by the HTTP API ─────────────────────────────────

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
    """Collect the answer and call trace from the agent event stream."""
    answer_parts: list[str] = []
    trace: list[dict[str, Any]] = []
    last_error: str | None = None
    active_model_step: dict[str, Any] | None = None

    def finish_model_step(status: str) -> None:
        if active_model_step and active_model_step.get("status") == "running":
            active_model_step["status"] = status
            active_model_step["duration_ms"] = round((time.perf_counter() - active_model_step.pop("_started")) * 1000)

    async for event_type, payload in stream_agent(message, mcp_url, token, history, agent_type, provider):
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
            arguments_text = json.dumps(payload.get("arguments", {}), ensure_ascii=False, separators=(",", ":"))
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

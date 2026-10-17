from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from app import agent


def test_only_ollama_openai_and_groq_are_supported():
    assert list(agent.PROVIDERS) == ["ollama", "openai", "groq"]
    with pytest.raises(RuntimeError, match="Unknown AI provider"):
        agent.provider_config("kimi")


def test_provider_config_reads_key_model_and_base_url(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("GROQ_MODEL", "test-model")
    monkeypatch.setenv("GROQ_BASE_URL", "https://groq.example/v1/")

    config = agent.provider_config("groq")

    assert config["api_key"] == "test-key"
    assert config["model"] == "test-model"
    assert config["base_url"] == "https://groq.example/v1"


def test_provider_config_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OpenAI API key is not configured"):
        agent.provider_config("openai")


def test_default_provider_uses_requested_configured_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test")
    monkeypatch.setenv("GROQ_API_KEY", "groq-test")
    monkeypatch.setenv("AI_PROVIDER", "groq")
    assert agent.default_provider() == "groq"


def test_default_provider_falls_back_to_configured_provider(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "groq-test")
    monkeypatch.setenv("AI_PROVIDER", "openai")
    assert agent.default_provider() == "groq"


def test_configured_providers_lists_every_choice_with_its_setup_hint(monkeypatch):
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    providers = agent.configured_providers()
    assert [item["id"] for item in providers] == ["ollama", "openai", "groq"]
    assert [item["configured"] for item in providers] == [False, True, False]
    assert providers[0]["setup"] == "set OLLAMA_MODEL in .env"
    assert providers[2]["setup"] == "add API key in .env"


def test_ollama_is_opted_into_by_model_and_needs_no_key(monkeypatch):
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="Set OLLAMA_MODEL in .env"):
        agent.provider_config("ollama")

    monkeypatch.setenv("OLLAMA_MODEL", "ca-mode")
    config = agent.provider_config("ollama")

    assert config["model"] == "ca-mode"
    assert config["base_url"] == "http://localhost:11434/v1"
    assert config["timeout"] == agent.LOCAL_TIMEOUT_SECONDS
    assert config["router_timeout"] == agent.LOCAL_TIMEOUT_SECONDS


def test_default_provider_prefers_ollama_when_configured(monkeypatch):
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test")
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    assert agent.default_provider() == "openai"

    monkeypatch.setenv("OLLAMA_MODEL", "ca-mode")
    assert agent.default_provider() == "ollama"


class FakeOllamaClient:
    """Answers chat completions and Ollama's /api/ps with the loaded model's context window."""

    def __init__(self, context_length, captured):
        self.context_length = context_length
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, headers, json):
        self.captured.append(url)
        return _completion("A budget tracks income and spending.")

    async def get(self, url):
        self.captured.append(url)
        return FakeResponse({"models": [{"name": "ca-mode:latest", "model": "ca-mode:latest", "context_length": self.context_length}]})


@pytest.mark.asyncio
async def test_ollama_refuses_to_answer_from_a_truncated_prompt(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "ca-mode:latest")
    captured = []
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeOllamaClient(4096, captured))

    with pytest.raises(agent.AgentRunError, match="4,096-token context window"):
        await agent.run_agent("What is a budget?", "https://unused", None, [], "finance", "ollama")

    assert captured == ["http://localhost:11434/v1/chat/completions", "http://localhost:11434/api/ps"]


@pytest.mark.asyncio
async def test_ollama_answers_when_its_context_window_fits(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "ca-mode:latest")
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeOllamaClient(32768, []))

    answer = await agent.run_agent("What is a budget?", "https://unused", None, [], "finance", "ollama")

    assert answer == "A budget tracks income and spending."


def test_groq_context_keeps_latest_user_question():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current question"},
    ]

    fitted = agent._fit_groq_context(messages, [])

    assert fitted[-1]["content"] == "current question"
    assert fitted[0]["role"] == "system"


def test_unrelated_question_does_not_inherit_finance_scope_from_history():
    history = [
        {"role": "user", "content": "Summarize my portfolio allocation."},
        {"role": "assistant", "content": "Your portfolio allocation is ..."},
    ]

    assert agent._blocked_request_reason("Who is the PM of India?", history) == agent.FINANCE_ONLY_NOTICE
    assert agent._blocked_request_reason("Why?", history) is None


@pytest.mark.parametrize("question", [
    "What's my total net worth right now?",
    "Show the Nifty option chain for next monthly expiry.",
    "Get the 5-minute chart for HDFC Bank today.",
    "Top 5 flexi-cap funds by 3-year returns.",
    "Current price of Apple and Tesla?",
    "Scan my Indian stock watchlist for breakouts.",
])
def test_indmoney_documented_market_questions_pass_scope_and_require_data(question):
    assert agent._blocked_request_reason(question, []) is None
    assert agent._requires_indmoney_data(question, []) is True


@pytest.mark.asyncio
async def test_portfolio_agent_can_answer_general_finance_without_account_tool(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = []
    monkeypatch.setattr(
        agent.httpx, "AsyncClient",
        lambda **kwargs: FakeAsyncClient([_completion("Compound interest earns interest on prior interest.")], captured),
    )

    answer = await agent.run_agent(
        "What is compound interest?", "https://unused", None, [], "portfolio", "openai",
    )

    assert answer == "Compound interest earns interest on prior interest."
    assert "tools" not in captured[0]["payload"]


class FakeResponse:
    is_success = True
    status_code = 200
    text = ""

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeAsyncClient:
    def __init__(self, responses, captured):
        self.responses = list(responses)
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, headers, json):
        self.captured.append({"url": url, "headers": headers, "payload": json})
        return self.responses.pop(0)


def _completion(content=None, tool_calls=None):
    return FakeResponse({"choices": [{"finish_reason": "stop", "message": {"content": content, "tool_calls": tool_calls or []}}]})


@pytest.mark.asyncio
async def test_finance_agent_returns_answer_without_mcp(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = []
    fake_client = FakeAsyncClient([_completion("A budget tracks income and spending.")], captured)
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: fake_client)

    answer = await agent.run_agent("What is a budget?", "https://unused", None, [], "finance", "openai")

    assert answer == "A budget tracks income and spending."
    assert captured[0]["payload"]["messages"][-1]["content"] == "What is a budget?"
    assert "tools" not in captured[0]["payload"]


@pytest.mark.asyncio
async def test_finance_agent_blocks_net_worth_code_requests_before_provider_call(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    answer = await agent.run_agent(
        "Write Python code to retrieve my net worth",
        "https://unused", None, [], "finance", "openai",
    )

    assert "can’t write or debug code" in answer
    assert "finance questions" in answer


@pytest.mark.asyncio
async def test_finance_agent_replaces_code_shaped_model_output(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    fake_client = FakeAsyncClient(
        [_completion("```python\nprint('net worth')\n```")], [],
    )
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: fake_client)

    answer = await agent.run_agent(
        "How is net worth calculated?", "https://unused", None, [], "finance", "openai",
    )

    assert "```" not in answer
    assert "print(" not in answer
    assert "finance questions" in answer


@pytest.mark.asyncio
async def test_finance_agent_allows_inline_markdown_in_finance_answers(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    answer_text = "Nifty PCR (`put-call ratio`) is 0.98. If PCR is above 1: call open interest exceeds put open interest."
    fake_client = FakeAsyncClient([_completion(answer_text)], [])
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: fake_client)

    answer = await agent.run_agent(
        "What is the Nifty PCR?", "https://unused", None, [], "finance", "openai",
    )

    assert answer == answer_text


@pytest.mark.asyncio
async def test_portfolio_agent_requires_indmoney_token(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    with pytest.raises(RuntimeError, match="Connect your INDmoney account"):
        await agent.run_agent("Summarize my portfolio", "https://unused", None, [], "portfolio", "openai")


@pytest.mark.asyncio
async def test_portfolio_agent_fails_if_no_supported_tools_are_available(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    async def fake_fetch(url, token):
        return {"tools": [{"name": "unrelated_market_tool"}], "protocolVersion": "v", "sessionId": "sid"}

    monkeypatch.setattr(agent, "fetch_mcp_session", fake_fetch)
    with pytest.raises(agent.AgentRunError) as exc_info:
        await agent.run_agent("Summarize my portfolio", "https://mcp", "token", [], "portfolio", "openai")

    assert "supported portfolio tools" in str(exc_info.value)
    assert "Reconnect INDmoney" in str(exc_info.value)
    # Only the routing decision ran; no model or tool call was made.
    assert [step["kind"] for step in exc_info.value.trace] == ["route"]


@pytest.mark.asyncio
async def test_portfolio_agent_calls_filtered_tool_then_answers(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    source_tools = [
        {"name": "networth_snapshot", "description": "Get portfolio total", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "unrelated_market_tool", "description": "Not needed", "inputSchema": {"type": "object", "properties": {}}},
    ]
    async def fake_fetch(url, token):
        return {"tools": source_tools, "protocolVersion": "v", "sessionId": "sid"}

    monkeypatch.setattr(agent, "fetch_mcp_session", fake_fetch)
    calls = []

    async def fake_call(url, token, protocol, session_id, name, arguments, *, client=None):
        calls.append((name, arguments))
        return "total current value: ₹42,000"

    monkeypatch.setattr(agent, "call_mcp_tool", fake_call)
    captured = []
    tool_call = {"id": "call-1", "function": {"name": "networth_snapshot", "arguments": "{}"}}
    fake_client = FakeAsyncClient([_completion(tool_calls=[tool_call]), _completion("Your portfolio is worth ₹42,000.")], captured)
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: fake_client)

    answer = await agent.run_agent("What is my portfolio worth?", "https://mcp", "token", [], "portfolio", "openai")

    assert answer == "Your portfolio is worth ₹42,000."
    assert calls == [("networth_snapshot", {})]
    assert [tool["function"]["name"] for tool in captured[0]["payload"]["tools"]] == ["networth_snapshot"]
    assert captured[1]["payload"]["messages"][-1]["role"] == "tool"


@pytest.mark.asyncio
async def test_portfolio_agent_rejects_when_model_never_calls_tool(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    async def fake_fetch(url, token):
        return {"tools": [{"name": "networth_snapshot"}], "protocolVersion": "v", "sessionId": "sid"}

    monkeypatch.setattr(agent, "fetch_mcp_session", fake_fetch)
    fake_client = FakeAsyncClient([_completion("Run account check."), _completion("Still no data.")], [])
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: fake_client)

    with pytest.raises(RuntimeError, match="did not call an INDmoney account tool"):
        await agent.run_agent("Show my portfolio", "https://mcp", "token", [], "portfolio", "openai")


@pytest.mark.asyncio
async def test_portfolio_agent_rejects_unknown_model_tool(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    async def fake_fetch(url, token):
        return {"tools": [{"name": "networth_snapshot"}], "protocolVersion": "v", "sessionId": "sid"}

    monkeypatch.setattr(agent, "fetch_mcp_session", fake_fetch)
    call = {"id": "call-x", "function": {"name": "unapproved_tool", "arguments": "{}"}}
    fake_client = FakeAsyncClient([_completion(tool_calls=[call])], [])
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: fake_client)

    with pytest.raises(RuntimeError, match="unknown INDmoney tool"):
        await agent.run_agent("Show my portfolio", "https://mcp", "token", [], "portfolio", "openai")


@pytest.mark.asyncio
async def test_portfolio_agent_reports_mcp_tool_call_failures(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    async def fake_fetch(url, token):
        return {"tools": [{"name": "networth_snapshot"}], "protocolVersion": "v", "sessionId": "sid"}

    async def failing_call(*args, **kwargs):
        raise RuntimeError("account temporarily unavailable")

    monkeypatch.setattr(agent, "fetch_mcp_session", fake_fetch)
    monkeypatch.setattr(agent, "call_mcp_tool", failing_call)
    tool_call = {"id": "call-1", "function": {"name": "networth_snapshot", "arguments": "{}"}}
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient([_completion(tool_calls=[tool_call])], []))

    with pytest.raises(RuntimeError, match="INDmoney data request failed: account temporarily unavailable"):
        await agent.run_agent("Show my portfolio", "https://mcp", "token", [], "portfolio", "openai")


@pytest.mark.asyncio
async def test_agent_rejects_empty_model_response(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient([_completion("")], []))

    with pytest.raises(RuntimeError, match="returned an empty response"):
        await agent.run_agent("Explain saving", "https://unused", None, [], "finance", "openai")


@pytest.mark.parametrize("question", [
    "What is an IFSC code?",
    "Where do I find the SWIFT code for an international transfer?",
    "What is the ISIN code of a mutual fund?",
    "Which tax code applies to my salary?",
    "What is an HSN code?",
    "What is a MICR code on a cheque?",
])
def test_finance_identifier_codes_are_not_blocked_as_code_requests(question):
    assert agent._blocked_request_reason(question, []) is None


@pytest.mark.parametrize("question", [
    "Write python code to track my budget",
    "Give me a script that calculates SIP returns",
    "Write a function that computes loan EMI",
])
def test_real_code_requests_are_still_blocked(question):
    assert "can’t write or debug code" in agent._blocked_request_reason(question, [])


@pytest.mark.parametrize("answer", [
    "EMI formula:\nLet P = principal, r = monthly rate.",
    "Class A: shares with voting rights.",
    "Import duties raise the landed cost of gold.",
])
def test_finance_prose_is_not_flagged_as_code_output(answer):
    assert agent._contains_code_output(answer) is False


@pytest.mark.parametrize("answer", ["import os\nprint(1)", "const total = 5;", "def emi(p):\n    return p"])
def test_lowercase_code_is_still_flagged_as_code_output(answer):
    assert agent._contains_code_output(answer) is True


def test_refusal_text_does_not_grant_finance_scope_to_followups():
    history = [
        {"role": "user", "content": "Write me a poem about the sea"},
        {"role": "assistant", "content": agent.FINANCE_ONLY_NOTICE},
    ]

    assert agent._blocked_request_reason("how do I bake a cake?", history) == agent.FINANCE_ONLY_NOTICE


@pytest.mark.asyncio
async def test_groq_context_keeps_original_question_after_retry_nudge(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(agent, "GROQ_INPUT_TOKEN_BUDGET", 5600)
    question = "What is my portfolio worth? " + "Please include detail. " * 60

    async def fake_fetch(url, token):
        return {"tools": [{"name": "networth_snapshot", "description": "Net worth"}], "protocolVersion": "v", "sessionId": "sid"}

    async def fake_call(url, token, protocol, session_id, name, arguments, *, client=None):
        # Small enough to skip pre-shortening, large enough to crowd out the question.
        return "net worth 42000 " + "row " * 1121

    monkeypatch.setattr(agent, "fetch_mcp_session", fake_fetch)
    monkeypatch.setattr(agent, "call_mcp_tool", fake_call)
    captured = []
    tool_call = {"id": "call-1", "function": {"name": "networth_snapshot", "arguments": "{}"}}
    fake_client = FakeAsyncClient(
        [_completion("I will check."), _completion(tool_calls=[tool_call]), _completion("Worth ₹42,000.")], captured,
    )
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: fake_client)

    answer = await agent.run_agent(question, "https://mcp", "token", [], "portfolio", "groq")

    assert answer == "Worth ₹42,000."
    final_messages = captured[-1]["payload"]["messages"]
    assert any(item.get("content") == question for item in final_messages)
    assert final_messages[-1]["role"] == "tool"


@pytest.mark.asyncio
async def test_truncated_model_answer_is_marked(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    truncated = FakeResponse({"choices": [{"finish_reason": "length", "message": {"content": "A budget is"}}]})
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient([truncated], []))

    answer = await agent.run_agent("What is a budget?", "https://unused", None, [], "finance", "openai")

    assert answer == "A budget is" + agent.TRUNCATION_NOTICE


@pytest.mark.asyncio
async def test_malformed_provider_response_reports_error(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient([FakeResponse({"choices": []})], []))

    with pytest.raises(agent.AgentRunError, match="unexpected response"):
        await agent.run_agent("What is a budget?", "https://unused", None, [], "finance", "openai")


def _snapshot_context(handler, *, cached=("networth_snapshot", "get_indian_stocks_details"), profile=None):
    return agent.PortfolioContext(
        local_tools=(agent.LocalTool(
            name="get_portfolio_snapshot", description="Saved snapshot",
            input_schema={"type": "object", "properties": {}}, handler=handler,
        ),),
        profile_context=profile,
        cached_mcp_tools=[{"name": name, "description": name} for name in cached],
    )


@pytest.mark.asyncio
async def test_portfolio_agent_answers_from_local_snapshot_without_mcp_init(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    async def fail_fetch(url, token):
        raise AssertionError("MCP must not be initialised when the snapshot answers the question")

    async def snapshot_handler(arguments):
        return '{"available": true, "networthInr": 4200000, "capturedAt": "2026-10-13T09:00+05:30"}'

    monkeypatch.setattr(agent, "fetch_mcp_session", fail_fetch)
    captured = []
    tool_call = {"id": "call-1", "function": {"name": "get_portfolio_snapshot", "arguments": "{}"}}
    fake_client = FakeAsyncClient([_completion(tool_calls=[tool_call]), _completion("₹42 lakh as of 13 Oct, 09:00 IST.")], captured)
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: fake_client)

    answer = await agent.run_agent(
        "What is my net worth?", "https://mcp", "token", [], "portfolio", "openai",
        context=_snapshot_context(snapshot_handler),
    )

    assert answer == "₹42 lakh as of 13 Oct, 09:00 IST."
    offered = [tool["function"]["name"] for tool in captured[0]["payload"]["tools"]]
    assert offered == ["get_portfolio_snapshot", "networth_snapshot", "get_indian_stocks_details"]
    assert '"networthInr": 4200000' in captured[1]["payload"]["messages"][-1]["content"]
    assert [step["name"] for step in answer.trace if step["kind"] == "tool"] == ["get_portfolio_snapshot"]


@pytest.mark.asyncio
async def test_portfolio_agent_opens_mcp_lazily_for_live_tool(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    fetches, calls, cached = [], [], []

    async def fake_fetch(url, token):
        fetches.append(token)
        return {"tools": [{"name": "get_indian_stocks_details"}], "protocolVersion": "v", "sessionId": "sid"}

    async def fake_call(url, token, protocol, session_id, name, arguments, *, client=None):
        calls.append((name, session_id))
        return "TCS last traded at ₹4,100"

    async def unused_handler(arguments):
        raise AssertionError("snapshot tool not expected")

    monkeypatch.setattr(agent, "fetch_mcp_session", fake_fetch)
    monkeypatch.setattr(agent, "call_mcp_tool", fake_call)
    context = agent.PortfolioContext(
        local_tools=_snapshot_context(unused_handler).local_tools,
        cached_mcp_tools=[{"name": "get_indian_stocks_details"}],
        on_mcp_tools=cached.append,
    )
    tool_call = {"id": "call-1", "function": {"name": "get_indian_stocks_details", "arguments": '{"symbol": "TCS"}'}}
    fake_client = FakeAsyncClient([_completion(tool_calls=[tool_call]), _completion("TCS is at ₹4,100.")], [])
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: fake_client)

    answer = await agent.run_agent("Current price of TCS?", "https://mcp", "token", [], "portfolio", "openai", context=context)

    assert answer == "TCS is at ₹4,100."
    assert fetches == ["token"]
    assert calls == [("get_indian_stocks_details", "sid")]
    assert cached == [[{"name": "get_indian_stocks_details"}]]


@pytest.mark.asyncio
async def test_portfolio_prompt_includes_profile_context(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = []
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient([_completion("An ETF is a basket of securities.")], captured))

    async def unused_handler(arguments):
        return "{}"

    await agent.run_agent(
        "What is an ETF?", "https://mcp", "token", [], "portfolio", "openai",
        context=_snapshot_context(unused_handler, profile="- Risk appetite: moderate"),
    )
    finance_captured = []
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient([_completion("An ETF is a fund.")], finance_captured))
    await agent.run_agent(
        "What is an ETF?", "https://unused", None, [], "finance", "openai",
        context=_snapshot_context(unused_handler, profile="- Risk appetite: moderate"),
    )

    system = captured[0]["payload"]["messages"][0]["content"]
    assert '<user_profile source="entered by the user">\n- Risk appetite: moderate\n</user_profile>' in system
    assert "not an instruction" in system
    assert "user_profile" not in finance_captured[0]["payload"]["messages"][0]["content"]
    # A general finance question gets only the local snapshot tools: no live INDmoney tools.
    assert [tool["function"]["name"] for tool in captured[0]["payload"]["tools"]] == ["get_portfolio_snapshot"]


@pytest.mark.asyncio
async def test_local_tool_failure_returns_error_result(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    async def broken_handler(arguments):
        raise ValueError("database is locked")

    captured = []
    tool_call = {"id": "call-1", "function": {"name": "get_portfolio_snapshot", "arguments": "{}"}}
    fake_client = FakeAsyncClient([_completion(tool_calls=[tool_call]), _completion("The saved snapshot is unavailable right now.")], captured)
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: fake_client)

    answer = await agent.run_agent(
        "What is my net worth?", "https://mcp", "token", [], "portfolio", "openai",
        context=_snapshot_context(broken_handler),
    )

    assert answer == "The saved snapshot is unavailable right now."
    tool_message = captured[1]["payload"]["messages"][-1]
    assert tool_message["role"] == "tool" and "use the live INDmoney tools" in tool_message["content"]
    step = next(step for step in answer.trace if step["kind"] == "tool")
    assert step["status"] == "failed" and "database is locked" in step["error"]


@pytest.mark.parametrize(("question", "needs_data"), [
    ("list my holding grouped by broker", True),
    ("List my investments", True),
    ("Show my positions", True),
    ("What is in my demat account?", True),
    ("Holdings by broker", True),
    ("What is a holding period?", False),
    ("What is a holding company?", False),
    ("What is a broker?", False),
])
def test_account_data_detection_covers_singular_and_broker_wording(question, needs_data):
    assert agent._requires_indmoney_data(question, []) is needs_data


@pytest.mark.parametrize(("content", "is_call"), [
    ('{\n"type": "get_portfolio_snapshot",\n"params": {}\n}\n\n{\n"type": "get_portfolio_snapshot",\n"params": {}\n}', True),
    ('```json\n{"name": "networth_holdings", "arguments": {}}\n```', True),
    ('```chart\n{"type":"bar","data":[{"label":"A","value":1}]}\n```', False),
    ('{"type": "bar", "title": "Allocation"}', False),
    ("Use get_portfolio_snapshot to see your holdings.", False),
])
def test_looks_like_tool_call(content, is_call):
    names = set(agent.IMPORTANT_PORTFOLIO_TOOL_NAMES) | {"get_portfolio_snapshot"}
    assert agent._looks_like_tool_call(content, names) is is_call


PSEUDO_CALL = '{\n"type": "get_portfolio_snapshot",\n"params": {}\n}'


@pytest.mark.asyncio
async def test_portfolio_agent_retries_with_tools_when_model_writes_a_tool_call(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    async def snapshot_handler(arguments):
        return '{"available": true, "data": {"networth_holdings": "Zerodha: INFY; INDmoney: TCS"}}'

    tool_call = {"id": "call-1", "function": {"name": "get_portfolio_snapshot", "arguments": "{}"}}
    captured = []
    fake_client = FakeAsyncClient([
        _completion(PSEUDO_CALL),
        _completion(tool_calls=[tool_call]),
        _completion("Zerodha holds INFY and INDmoney holds TCS."),
    ], captured)
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: fake_client)
    question = "Which broker has most of my money?"
    assert agent._requires_indmoney_data(question, []) is False

    answer = await agent.run_agent(
        question, "https://mcp", "token", [], "portfolio", "openai", context=_snapshot_context(snapshot_handler),
    )

    assert answer == "Zerodha holds INFY and INDmoney holds TCS."
    assert [tool["function"]["name"] for tool in captured[0]["payload"]["tools"]] == ["get_portfolio_snapshot"]
    # The rerun also offers the live INDmoney tools.
    assert "networth_snapshot" in {tool["function"]["name"] for tool in captured[1]["payload"]["tools"]}


@pytest.mark.asyncio
async def test_portfolio_agent_never_shows_a_written_tool_call(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = []
    fake_client = FakeAsyncClient([_completion(PSEUDO_CALL)] * 3, captured)
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: fake_client)

    async def never_called(arguments):
        raise AssertionError("The model never really called the tool")

    with pytest.raises(agent.AgentRunError, match="did not call an INDmoney account tool"):
        await agent.run_agent(
            "Which broker has most of my money?", "https://mcp", "token", [], "portfolio", "openai",
            context=_snapshot_context(never_called),
        )
    assert len(captured) == 3  # The text call, then one forced run with its single nudge; no loop.


@pytest.mark.asyncio
async def test_finance_agent_does_not_retry_on_json_looking_answers(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = []
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient([_completion(PSEUDO_CALL)], captured))

    await agent.run_agent("What is a budget?", "https://unused", None, [], "finance", "openai")

    assert len(captured) == 1


class ToolUseFailedResponse(FakeResponse):
    """Groq's HTTP 400 when the model calls a tool that was not offered."""

    is_success = False
    status_code = 400

    def __init__(self, failed_generation):
        super().__init__({"error": {
            "message": "Tool choice is none, but model called a tool", "type": "invalid_request_error",
            "code": "tool_use_failed", "failed_generation": failed_generation,
        }})
        self.text = json.dumps(self.payload)


REPO_BROWSER_CALL = '{"name": "repo_browser.run_code", "arguments": {"callable": "get_portfolio_snapshot", "arguments": {}}}'


@pytest.mark.asyncio
async def test_invalid_tool_call_naming_our_tool_reruns_with_live_tools(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")

    async def snapshot_handler(arguments):
        return '{"available": true, "networthInr": 4200000}'

    tool_call = {"id": "call-1", "function": {"name": "get_portfolio_snapshot", "arguments": "{}"}}
    captured = []
    fake_client = FakeAsyncClient([
        ToolUseFailedResponse(REPO_BROWSER_CALL), _completion(tool_calls=[tool_call]), _completion("Your net worth is ₹42,00,000."),
    ], captured)
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: fake_client)

    answer = await agent.run_agent("What is an ETF?", "https://mcp", "token", [], "portfolio", "groq",
                                   context=_snapshot_context(snapshot_handler))

    assert answer == "Your net worth is ₹42,00,000."
    assert "networth_snapshot" in {tool["function"]["name"] for tool in captured[1]["payload"]["tools"]}


@pytest.mark.asyncio
async def test_invalid_tool_call_is_retried_once_then_reported(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    captured = []
    fake_client = FakeAsyncClient([ToolUseFailedResponse('{"name": "python"}'), _completion("A budget tracks spending.")], captured)
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: fake_client)

    answer = await agent.run_agent("What is a budget?", "https://unused", None, [], "finance", "groq")

    assert answer == "A budget tracks spending."
    assert captured[0]["payload"] == captured[1]["payload"]

    captured.clear()
    failing = FakeAsyncClient([ToolUseFailedResponse('{"name": "python"}')] * 2, captured)
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: failing)
    with pytest.raises(agent.AgentRunError, match="tool_use_failed"):
        await agent.run_agent("What is a budget?", "https://unused", None, [], "finance", "groq")
    assert len(captured) == 2


# ── Trace redaction ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("text", "hidden", "kept"), [
    ('{"accessToken":"abc123","asset_type":"MF"}', "abc123", '"asset_type":"MF"'),
    ('{"client_secret": "s3cr3t", "max_tokens": 50}', "s3cr3t", '"max_tokens": 50'),
    ('{"pin":2468,"pan":"ABCDE1234F","name":"Asha"}', "2468", '"name":"Asha"'),
    ('{"holdings":[{"pan":"ABCDE1234F"}]', "ABCDE1234F", '"holdings"'),
    ('{"refresh_token":"cut-off-in-the-midd', "cut-off", '"refresh_token"'),
    ("401 Unauthorized: Bearer eyJhbGciOi.eyJzdWIi.c2lnbmF0dXJl", "eyJhbGciOi", "401 Unauthorized"),
    ("Holder PAN ABCDE1234F on file", "ABCDE1234F", "Holder PAN"),
])
def test_trace_text_masks_credentials_and_identifiers(text, hidden, kept):
    redacted = agent.redact_trace_text(text)

    assert hidden not in redacted
    assert kept in redacted


@pytest.mark.asyncio
async def test_trace_never_carries_secrets_from_tool_arguments_or_results(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    async def leaky_handler(arguments):
        return '{"available": true, "networthInr": 4200000, "sessionToken": "tok-999", "pan": "ABCDE1234F"}'

    tool_call = {"id": "call-1", "function": {"name": "get_portfolio_snapshot",
                                              "arguments": '{"apiKey": "sk-live-1"}'}}
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient(
        [_completion(tool_calls=[tool_call]), _completion("₹42 lakh.")], []))

    answer = await agent.run_agent("What is my net worth?", "https://mcp", "token", [], "portfolio", "openai",
                                   context=_snapshot_context(leaky_handler))

    step = next(item for item in answer.trace if item["kind"] == "tool")
    shown = json.dumps(step)
    for secret in ("tok-999", "ABCDE1234F", "sk-live-1"):
        assert secret not in shown
    assert "4200000" in step["result_preview"]  # the user's own figures stay visible


@pytest.mark.asyncio
async def test_rejected_tool_arguments_go_back_to_the_model_to_correct(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    rejection = ("MCP tool \"get_indian_stocks_ohlc\" failed: 1 validation error for get_indian_stocks_ohlcArguments "
                 "lookback Input should be '1h', '3h', '1d', '7d', '14d' or '1y' [input_value='30d']")
    calls = []

    async def fake_fetch(url, token):
        return {"tools": [{"name": "get_indian_stocks_ohlc"}], "protocolVersion": "v", "sessionId": "sid"}

    async def fake_call(url, token, protocol, session_id, name, arguments, *, client=None):
        calls.append(arguments["lookback"])
        if arguments["lookback"] == "30d":
            raise agent.McpToolError(rejection)
        return '{"candles": []}'

    monkeypatch.setattr(agent, "fetch_mcp_session", fake_fetch)
    monkeypatch.setattr(agent, "call_mcp_tool", fake_call)

    def ohlc_call(lookback):
        return {"id": f"call-{lookback}", "function": {"name": "get_indian_stocks_ohlc", "arguments": json.dumps(
            {"ind_key": "INDS01052", "interval": "1day", "lookback": lookback})}}

    captured = []
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient([
        _completion(tool_calls=[ohlc_call("30d")]), _completion(tool_calls=[ohlc_call("1y")]),
        _completion("INFY rose over the period."),
    ], captured))

    answer = await agent.run_agent("Show the INFY price for the last 30 days", "https://mcp", "token", [],
                                   "portfolio", "openai")

    assert str(answer) == "INFY rose over the period."
    assert calls == ["30d", "1y"]
    tool_messages = [json.loads(item["content"]) for item in captured[-1]["payload"]["messages"] if item.get("role") == "tool"]
    fed_back = tool_messages[0]
    assert "Input should be '1h'" in fed_back["error"] and "call it again" in fed_back["next_step"]
    steps = [step for step in answer.trace if step["kind"] == "tool"]
    assert [step["status"] for step in steps] == ["failed", "completed"]


@pytest.mark.asyncio
async def test_a_rejected_call_still_counts_as_using_a_tool(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    async def fake_fetch(url, token):
        return {"tools": [{"name": "get_indian_stocks_ohlc"}], "protocolVersion": "v", "sessionId": "sid"}

    async def rejecting_call(*args, **kwargs):
        raise agent.McpToolError('MCP tool "get_indian_stocks_ohlc" failed: unknown ind_key')

    monkeypatch.setattr(agent, "fetch_mcp_session", fake_fetch)
    monkeypatch.setattr(agent, "call_mcp_tool", rejecting_call)
    tool_call = {"id": "call-1", "function": {"name": "get_indian_stocks_ohlc", "arguments": "{}"}}
    captured = []
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient(
        [_completion(tool_calls=[tool_call]), _completion("INFY price history is unavailable right now.")], captured))

    answer = await agent.run_agent("Show INFY price history", "https://mcp", "token", [], "portfolio", "openai")

    assert str(answer) == "INFY price history is unavailable right now."
    assert len(captured) == 2  # no forced extra tool call after an honest "unavailable"


# ── Saved context ──────────────────────────────────────────────────────────────

SAVED = agent.SavedContext(
    text='Confirmed by the user (entered in CA Mode; not checked against any account):\n'
         '- Goal "Home": target INR 2500000 by 2032-03-31\n'
         'Calculated in code on 2026-10-16 from the goals above:\n'
         '- "Home": status behind; 65 months left; INR 2100000 still to save today (16% saved).\n'
         '  - No growth (0% a year): projected INR 2350000, short by INR 150000; needs INR 32308 a month.',
    items=("Goal: Home",),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", ["finance", "portfolio"])
async def test_saved_context_reaches_either_agent_as_labelled_data(monkeypatch, agent_type):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = []
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient([_completion("You need about ₹32,308 a month.")], captured))

    async def unused_handler(arguments):
        return "{}"

    answer = await agent.run_agent(
        "How much should I save each month for my home goal?", "https://mcp", "token", [], agent_type, "openai",
        context=_snapshot_context(unused_handler) if agent_type == "portfolio" else None, saved_context=SAVED,
    )

    system = captured[0]["payload"]["messages"][0]["content"]
    assert f'<saved_context source="saved by the user in CA Mode">\n{SAVED.text}\n</saved_context>' in system
    assert "never guess the user's income" in system and "never as expected or promised returns" in system
    assert ("cannot see the user's INDmoney account" in system) is (agent_type == "finance")
    assert [step["kind"] for step in answer.trace][:2] == ["route", "context"]
    assert answer.trace[1]["items"] == ["Goal: Home"]


@pytest.mark.asyncio
async def test_prompts_are_unchanged_without_saved_context(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = []
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient([_completion("A budget tracks spending.")], captured))

    answer = await agent.run_agent("What is a budget?", "https://unused", None, [], "finance", "openai")

    assert captured[0]["payload"]["messages"][0]["content"] == agent.FINANCE_SYSTEM_PROMPT
    assert "context" not in [step["kind"] for step in answer.trace]


@pytest.mark.asyncio
async def test_declined_requests_do_not_use_saved_context(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient([], []))

    answer = await agent.run_agent("Write a python script for my goals", "https://unused", None, [], "finance", "openai", saved_context=SAVED)

    assert answer == agent.CODE_REFUSAL_NOTICE
    assert [step["kind"] for step in answer.trace] == ["route"]


@pytest.mark.asyncio
async def test_goal_figures_from_saved_context_are_not_flagged_as_unverified(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    async def snapshot_handler(arguments):
        return '{"available": true, "figures": {"total_current_inr": "4200000"}}'

    tool_call = {"id": "call-1", "function": {"name": "get_portfolio_snapshot", "arguments": "{}"}}
    text = "Your portfolio is ₹42 lakh. For the home goal you are short by ₹1,50,000 with no growth; ₹32,308 a month closes it."
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient([_completion(tool_calls=[tool_call]), _completion(text)], []))
    context = _snapshot_context(snapshot_handler)

    with_saved = await agent.run_agent("Can my portfolio cover my home goal?", "https://mcp", "token", [], "portfolio", "openai", context=context, saved_context=SAVED)
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient([_completion(tool_calls=[tool_call]), _completion(text)], []))
    without = await agent.run_agent("Can my portfolio cover my home goal?", "https://mcp", "token", [], "portfolio", "openai", context=context)

    assert str(with_saved) == text
    assert str(without) != text  # The same figures with no saved context are flagged.


@pytest.mark.parametrize("question", [
    "Am I on track for my goals?",
    "How much should I save each month for my home goal?",
    "Is ₹5 lakh enough for the down payment?",
])
def test_goal_questions_pass_the_keyword_fallback(question):
    assert agent._keyword_category(question, []) == "general_finance"


# ── Finance Agent and goal-planning tools ─────────────────────────────────────

async def _goal_tool_handler(arguments):
    return json.dumps({"goals": [{"name": "Home", "plan": {"status": "behind"}}]})


def _finance_context_with_goal_and_account_tools():
    return agent.PortfolioContext(
        local_tools=(
            agent.LocalTool(name="get_goal_plans", description="Goal plans", input_schema={"type": "object", "properties": {}},
                            handler=_goal_tool_handler),
            agent.LocalTool(name="get_portfolio_snapshot", description="Saved snapshot",
                            input_schema={"type": "object", "properties": {}}, handler=_goal_tool_handler),
        ),
        profile_context="Risk appetite: high",
        cached_mcp_tools=[{"name": "networth_snapshot", "description": "x"}],
    )


@pytest.mark.asyncio
async def test_finance_agent_is_offered_only_the_goal_tools_and_can_call_them(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = []
    tool_call = {"id": "call-1", "function": {"name": "get_goal_plans", "arguments": "{}"}}
    fake_client = FakeAsyncClient([_completion(tool_calls=[tool_call]), _completion("Your home goal needs more.")], captured)
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: fake_client)

    answer = await agent.run_agent(
        "Can I reach my house goal?", "https://unused", None, [], "finance", "openai",
        context=_finance_context_with_goal_and_account_tools(),
    )

    assert answer == "Your home goal needs more."
    assert [tool["function"]["name"] for tool in captured[0]["payload"]["tools"]] == ["get_goal_plans"]
    system = captured[0]["payload"]["messages"][0]["content"]
    assert "Risk appetite" not in system
    assert captured[1]["payload"]["messages"][-1]["role"] == "tool"
    assert [step["name"] for step in answer.trace if step["kind"] == "tool"] == ["get_goal_plans"]


@pytest.mark.asyncio
async def test_finance_agent_cannot_call_a_snapshot_tool_it_was_not_given(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    tool_call = {"id": "call-1", "function": {"name": "get_portfolio_snapshot", "arguments": "{}"}}
    fake_client = FakeAsyncClient([_completion(tool_calls=[tool_call])], [])
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: fake_client)

    with pytest.raises(agent.AgentRunError, match="unknown INDmoney tool"):
        await agent.run_agent(
            "What is my net worth?", "https://unused", None, [], "finance", "openai",
            context=_finance_context_with_goal_and_account_tools(),
        )


@pytest.mark.asyncio
async def test_finance_agent_with_only_account_tools_in_its_context_sends_none(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = []
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient([_completion("A budget.")], captured))

    await agent.run_agent(
        "What is a budget?", "https://unused", None, [], "finance", "openai",
        context=_snapshot_context(_goal_tool_handler),
    )

    assert "tools" not in captured[0]["payload"]


def test_finance_tool_allowlist_is_exactly_the_goal_tools():
    assert agent.FINANCE_LOCAL_TOOL_NAMES == {"get_goal_plans", "plan_goal_what_if", "calculate_goal_plan"}
    assert agent._finance_context(None) is None


@pytest.mark.asyncio
async def test_saved_context_rules_point_to_the_what_if_tool_and_forbid_naming_funds(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = []
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient([_completion("ok")], captured))

    await agent.run_agent(
        "How is my goal?", "https://unused", None, [], "finance", "openai",
        saved_context=agent.SavedContext(text='- Goal "Home"', items=("Goal: Home",)),
    )

    system = captured[0]["payload"]["messages"][0]["content"]
    assert "plan_goal_what_if" in system and "never name, rank or describe specific funds" in system
    assert "Never save or change a goal yourself" in system


# ── Goal arithmetic comes from code ───────────────────────────────────────────

def _calculator_context():
    from app import goal_portfolio
    return agent.PortfolioContext(local_tools=(goal_portfolio.goal_calculator_tool(today=date(2026, 10, 16)),))


def _calculator_call():
    arguments = {"currency": "INR", "targetAmount": 5_000_000, "horizonYears": 5, "savedAmount": 500_000}
    return {"id": "call-1", "function": {"name": "calculate_goal_plan", "arguments": json.dumps(arguments)}}


@pytest.mark.asyncio
async def test_finance_agent_calculates_an_unsaved_goal_with_the_tool_and_its_figures_pass(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = []
    replies = [_completion(tool_calls=[_calculator_call()]), _completion("You would need about ₹75,000 a month.")]
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient(replies, captured))

    answer = await agent.run_agent(
        "I need 50 lakh in 5 years and have 5 lakh. How much a month?", "https://unused", None, [], "finance", "openai",
        context=_calculator_context(),
    )

    result = json.loads(captured[1]["payload"]["messages"][-1]["content"])
    assert result["saved"] is False and result["plan"]["scenarios"][0]["requiredMonthly"] == 75_000
    assert "couldn't match" not in answer
    assert agent.GOAL_TOOL_RULES in captured[0]["payload"]["messages"][0]["content"]
    assert [(step["name"], step["source"]) for step in answer.trace if step["kind"] == "tool"] == [("calculate_goal_plan", "local")]


@pytest.mark.asyncio
async def test_finance_agent_figures_that_no_goal_tool_returned_are_flagged(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    replies = [_completion(tool_calls=[_calculator_call()]), _completion("You would need about ₹61,000 a month.")]
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient(replies, []))

    answer = await agent.run_agent(
        "I need 50 lakh in 5 years and have 5 lakh. How much a month?", "https://unused", None, [], "finance", "openai",
        context=_calculator_context(),
    )

    assert "couldn't match these figures to CA Mode's goal calculations: ₹61,000" in answer
    assert "INDmoney" not in answer


@pytest.mark.asyncio
async def test_finance_agent_without_tools_gets_no_goal_tool_rules_or_number_check(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = []
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient([_completion("Section 80C allows ₹1,50,000.")], captured))

    answer = await agent.run_agent("What is the 80C tax deduction?", "https://unused", None, [], "finance", "openai")

    assert agent.GOAL_TOOL_RULES not in captured[0]["payload"]["messages"][0]["content"]
    assert answer == "Section 80C allows ₹1,50,000."


@pytest.mark.parametrize("question", [
    "I need 50 lakh in 5 years and have 5 lakh. How much a month?",
    "Can I get to 2 crore by 2045?",
])
def test_amounts_in_lakh_or_crore_read_as_finance_in_the_keyword_fallback(question):
    assert agent._keyword_category(question, []) == "general_finance"


QUESTION = "I need 50 lakh in 5 years and have 5 lakh. How much a month?"


@pytest.mark.asyncio
async def test_a_goal_answer_worked_out_by_the_model_is_retried_with_the_calculator(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = []
    replies = [
        _completion("Without growth you need (50,00,000 - 5,00,000) / 60 = ₹75,000 a month."),
        _completion(tool_calls=[_calculator_call()]),
        _completion("With no growth you need ₹75,000 a month."),
    ]
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient(replies, captured))

    answer = await agent.run_agent(QUESTION, "https://unused", None, [], "finance", "openai", context=_calculator_context())

    sent = captured[-1]["payload"]["messages"]
    assert sent[2:4] == [
        {"role": "assistant", "content": "Without growth you need (50,00,000 - 5,00,000) / 60 = ₹75,000 a month."},
        {"role": "user", "content": agent.GOAL_CALCULATOR_NUDGE},
    ]
    assert answer == "With no growth you need ₹75,000 a month."
    assert [step["name"] for step in answer.trace if step["kind"] == "tool"] == ["calculate_goal_plan"]


@pytest.mark.asyncio
async def test_a_model_that_still_skips_the_calculator_gets_its_figures_flagged(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    replies = [_completion("You need about ₹75,000 a month."), _completion("You need about ₹75,000 a month.")]
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient(replies, []))

    answer = await agent.run_agent(QUESTION, "https://unused", None, [], "finance", "openai", context=_calculator_context())

    assert "couldn't match these figures to CA Mode's goal calculations: ₹75,000" in answer


@pytest.mark.asyncio
async def test_a_clarifying_question_that_repeats_the_users_numbers_is_not_retried(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = []
    reply = "Is the 50 lakh in today's money, and what yearly return do you want to assume?"
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient([_completion(reply)], captured))

    answer = await agent.run_agent(QUESTION, "https://unused", None, [], "finance", "openai", context=_calculator_context())

    assert answer == reply and len(captured) == 1

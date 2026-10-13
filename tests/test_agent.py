from __future__ import annotations

import json

import httpx
import pytest

from app import agent


def test_only_openai_and_groq_are_supported():
    assert list(agent.PROVIDERS) == ["openai", "groq"]
    with pytest.raises(RuntimeError, match="Unknown AI provider"):
        agent.provider_config("ollama")
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


def test_configured_providers_only_lists_cloud_choices(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    providers = agent.configured_providers()
    assert [item["id"] for item in providers] == ["openai", "groq"]
    assert providers[0]["configured"] is True
    assert providers[1]["configured"] is False


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
    assert exc_info.value.trace == []


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
        return "row " * 1125  # Small enough to skip pre-shortening, large enough to crowd out the question.

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

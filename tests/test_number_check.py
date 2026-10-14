from __future__ import annotations

import json

import pytest

from app import agent, number_check, portfolio_math
from fixtures import indmoney_sample
from test_agent import FakeAsyncClient, _completion

SOURCES = [json.dumps({"figures": portfolio_math.compute_metrics(indmoney_sample.results())})]


@pytest.mark.parametrize("answer", [
    "Your net worth is ₹12,00,000 (₹12 lakh). P&L is ₹1,40,000 (16.87%) on ₹8.3 L invested.",
    "About ₹12.5 lakh in total, a ~16.9% gain, and the top 5 holdings make up 80%.",
    "That is ₹0.12 Cr of net worth.",
    "Apple is worth $1,200 (₹1,00,000).",
    "TCS is down ₹10,000 (-5%).",
    "Rs. 1,90,000 in TCS and INR 1,80,000 in Infosys.",
    "You hold 6 holdings across 3 brokers, bought since 2021.",  # Counts and years are not checked.
    "1. Infosys\n2. TCS\n3. HDFC Bank",  # List numbering is not checked.
    "See [the 2024 report](https://example.com/report-99.pdf).",  # Link targets are not checked.
])
def test_traceable_numbers_pass(answer):
    assert number_check.unverified_numbers(answer, SOURCES) == []


def test_invented_numbers_are_reported_once():
    answer = "Your net worth is ₹13,50,000 and you gained 22.4%. Again: ₹13,50,000."
    assert number_check.unverified_numbers(answer, SOURCES) == ["₹13,50,000", "22.4%"]


def test_numbers_from_plain_text_tool_results_and_the_question_count():
    sources = ["Net worth: ₹4,20,000 as of today", "I invest 25000 every month"]
    assert number_check.unverified_numbers("Net worth ₹4.2 lakh; your SIP is ₹25,000.", sources) == []


def test_warning_note_lists_at_most_six():
    note = number_check.warning_note([f"₹{i}" for i in range(8)])
    assert "₹0, ₹1, ₹2, ₹3, ₹4, ₹5 and others" in note


async def _snapshot_handler(arguments):
    return json.dumps({"figures": portfolio_math.compute_metrics(indmoney_sample.results())})


def _context():
    return agent.PortfolioContext(local_tools=(agent.LocalTool(
        name="get_portfolio_snapshot", description="Saved snapshot",
        input_schema={"type": "object", "properties": {}}, handler=_snapshot_handler,
    ),), cached_mcp_tools=[{"name": "networth_snapshot", "description": "Net worth"}])


@pytest.mark.asyncio
@pytest.mark.parametrize(("answer", "warned"), [
    ("Your P&L is ₹1,40,000 (16.87%).", False),
    ("Your P&L is ₹2,40,000 (26.87%).", True),
])
async def test_portfolio_answers_get_a_warning_for_untraceable_figures(monkeypatch, answer, warned):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    tool_call = {"id": "call-1", "function": {"name": "get_portfolio_snapshot", "arguments": "{}"}}
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient(
        [_completion(tool_calls=[tool_call]), _completion(answer)], []))

    result = await agent.run_agent("What is my P&L?", "https://mcp", "token", [], "portfolio", "openai", context=_context())

    assert result.startswith(answer)
    assert ("couldn't match these figures" in result) is warned
    if warned:
        assert "₹2,40,000, 26.87%" in result


@pytest.mark.asyncio
async def test_answers_without_tool_data_are_not_checked(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient(
        [_completion("An ETF costing ₹100 with a 0.2% fee.")], []))

    result = await agent.run_agent("What is an ETF?", "https://unused", None, [], "finance", "openai")

    assert result == "An ETF costing ₹100 with a 0.2% fee."

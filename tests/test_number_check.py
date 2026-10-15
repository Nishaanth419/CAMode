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


@pytest.mark.parametrize("answer", [
    "Infosys gained +₹79 000 this month.",
    "Infosys gained ₹79\u00a0000 and TCS ₹8\u00a089\u00a0000.",
    "Infosys gained ₹79\u202f000; your total is ₹8\u202f89\u202f000.",
    "Your total is ₹8 89 000.",
    "Net P&L is ₹\u202f79\u202f000 on ₹ 8,89,000.",  # gpt-oss puts a space after the sign.
])
def test_space_grouped_amounts_match(answer):
    sources = [json.dumps({"gain": 79000, "total": 889000})]
    assert number_check.unverified_numbers(answer, sources) == []


@pytest.mark.parametrize(("text", "numbers"), [
    ("in 2026 1,000 units", ["2026", "1,000"]),
    ("₹10 in 2026 100 units", ["10", "2026", "100"]),  # Plain spaces group digits only after a currency sign.
    ("as of\u202f2026-10-14 at 15\u202f14", ["2026", "10", "14", "15", "14"]),
    ("top 10 100%", ["10", "100"]),
    ("₹79 00", ["79", "00"]),  # The last group must have three digits.
])
def test_separate_numbers_are_not_merged(text, numbers):
    assert [m.group("number") for m in number_check._NUMBER_RE.finditer(text)] == numbers


def test_no_break_space_groups_parse_without_a_currency_sign():
    # gpt-oss tables put "₹" in the header and write cells as "470\u202f000".
    text = "| Broker | Value (₹) |\n| INDmoney | **470\u202f000** |"
    assert [m.group("number") for m in number_check._NUMBER_RE.finditer(text)][-1] == "470\u202f000"


def test_invented_space_grouped_amounts_are_reported_whole():
    answer = "You gained ₹97 000, or ₹\u202f98\u202f000."
    assert number_check.unverified_numbers(answer, [json.dumps({"gain": 79000})]) == [
        "₹97 000", "₹\u202f98\u202f000"]


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

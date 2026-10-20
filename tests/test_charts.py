from __future__ import annotations

import json
import re

import pytest

from app import agent, charts, main
from tests.test_agent import FakeAsyncClient, _completion


def _daily_candles(days: int = 3) -> list[dict]:
    rows = []
    for day in range(days):
        price = 280 + day
        rows.append({"datetime_ist": f"2026-10-{13 + day:02d}T00:00:00+05:30", "open": price, "high": price + 4,
                     "low": price - 2, "close": price + 1, "volume": 1000})
    return rows


def _ohlc_result(rows=None, **extra) -> str:
    return json.dumps({"ind_key": "INDS01052", "name": "Bharat Electronics Ltd", "interval": "1day",
                       "candles": _daily_candles() if rows is None else rows, "candles_total": 3, **extra})


def _chart(content: str) -> dict:
    match = re.search(r"```chart\n(.*?)\n```", content, re.DOTALL)
    assert match, content
    return json.loads(match.group(1))


# ── Reading OHLC results ───────────────────────────────────────────────────────

def test_ohlc_candles_reads_rows_and_the_instrument_name():
    candles, name = charts.ohlc_candles(_ohlc_result())

    assert name == "Bharat Electronics Ltd"
    assert candles == [["2026-10-13", 280.0, 284.0, 278.0, 281.0], ["2026-10-14", 281.0, 285.0, 279.0, 282.0],
                       ["2026-10-15", 282.0, 286.0, 280.0, 283.0]]


@pytest.mark.parametrize("wrap", [
    lambda inner: json.dumps({"result": inner}),               # the tool's declared output: a JSON string
    lambda inner: json.dumps({"data": {"series": json.loads(inner)}}),  # nested deeper
])
def test_ohlc_candles_finds_rows_wherever_they_are(wrap):
    candles, _ = charts.ohlc_candles(wrap(_ohlc_result()))

    assert [row[0] for row in candles] == ["2026-10-13", "2026-10-14", "2026-10-15"]


def test_ohlc_candles_drops_impossible_rows_sorts_and_keeps_intraday_times():
    rows = [
        {"datetime_ist": "2026-10-15T09:20:00+05:30", "open": "100", "high": "102", "low": "99", "close": "101"},
        {"datetime_ist": "2026-10-15T09:15:00+05:30", "open": 99, "high": 101, "low": 98, "close": 100},
        {"datetime_ist": "2026-10-15T09:25:00+05:30", "open": 100, "high": 90, "low": 99, "close": 101},  # high < open
        {"datetime_ist": None, "open": 1, "high": 1, "low": 1, "close": 1},
    ]

    candles, _ = charts.ohlc_candles(_ohlc_result(rows))

    assert candles == [["2026-10-15 09:15", 99.0, 101.0, 98.0, 100.0], ["2026-10-15 09:20", 100.0, 102.0, 99.0, 101.0]]


@pytest.mark.parametrize("result", ["not json", "{}", json.dumps({"candles": []}), json.dumps({"error": "bad key"})])
def test_ohlc_candles_returns_nothing_without_candles(result):
    assert charts.ohlc_candles(result) == ([], None)


# ── Grounding chart blocks ─────────────────────────────────────────────────────

def test_model_candles_are_replaced_by_the_tool_candles():
    content = ('## BEL\n```chart\n{"type":"candlestick","title":"BEL","candles":'
               '[{"label":"2026-10-15","open":1,"high":2,"low":0.5,"close":1.5}]}\n```\nIt rose.')

    grounded = charts.ground_candlestick_charts(content, [_ohlc_result()])

    chart = _chart(grounded)
    assert chart["title"] == "Bharat Electronics Ltd · 2026-10-13 to 2026-10-15"
    assert chart["ohlc"][-1] == ["2026-10-15", 282.0, 286.0, 280.0, 283.0]
    assert "candles" not in chart
    assert grounded.startswith("## BEL\n") and grounded.endswith("\nIt rose.")


def test_an_elided_chart_block_is_filled_from_the_tool():
    content = '```chart\n{"type":"candlestick","title":"SENSEX","candles":[{"label":"2025-10-15", ...}, ...]}\n```'

    chart = _chart(charts.ground_candlestick_charts(content, [_ohlc_result()]))

    assert len(chart["ohlc"]) == 3


def test_a_title_only_block_uses_the_model_title_when_the_tool_names_nothing():
    result = json.dumps({"candles": _daily_candles()})

    chart = _chart(charts.ground_candlestick_charts('```chart\n{"type":"candlestick","title":"BEL daily"}\n```', [result]))

    assert chart["title"] == "BEL daily"


def test_the_latest_ohlc_result_with_candles_wins():
    older = _ohlc_result(name="Older")
    empty = json.dumps({"candles": []})

    chart = _chart(charts.ground_candlestick_charts('```chart\n{"type":"candlestick"}\n```', [older, _ohlc_result(), empty]))

    assert chart["title"].startswith("Bharat Electronics Ltd")


def test_without_ohlc_data_a_candlestick_block_becomes_a_note_not_a_made_up_chart():
    grounded = charts.ground_candlestick_charts('Look:\n```chart\n{"type":"candlestick","candles":[]}\n```', [])

    assert "```chart" not in grounded
    assert charts.CANDLES_UNAVAILABLE_NOTE in grounded


def test_other_charts_are_left_alone():
    line = '```chart\n{"type":"line","title":"Net worth","series":[]}\n```'

    assert charts.ground_candlestick_charts(line, [_ohlc_result()]) == line


def test_a_year_of_candles_fits_in_a_saved_answer():
    rows = [{"datetime_ist": f"2026-{1 + day // 28:02d}-{1 + day % 28:02d}", "open": 12345.67, "high": 12399.99,
             "low": 12300.01, "close": 12350.55} for day in range(250)]

    grounded = charts.ground_candlestick_charts('```chart\n{"type":"candlestick"}\n```', [_ohlc_result(rows)])

    assert len(_chart(grounded)["ohlc"]) == 250
    assert len(grounded) < 24_000 - 4_000  # room left for the answer's text


def test_chart_data_is_left_out_of_the_history_the_model_sees():
    content = 'Here:\n```chart\n{"type":"candlestick","ohlc":[["2026-10-15",1,2,0.5,1.5]]}\n```\nDone.'

    assert charts.strip_chart_blocks(content) == f"Here:\n{charts.CHART_IN_HISTORY}\nDone."
    assert charts.strip_chart_blocks("No charts.") == "No charts."


# ── Through the agent ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_portfolio_answer_charts_the_candles_the_tool_returned(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    ohlc_tool = {"name": charts.OHLC_TOOL_NAME, "description": "candles", "inputSchema": {"type": "object", "properties": {}}}

    async def fake_fetch(url, token):
        return {"tools": [ohlc_tool], "protocolVersion": "v", "sessionId": "sid"}

    async def fake_call(url, token, protocol, session_id, name, arguments, *, client=None):
        return _ohlc_result()

    monkeypatch.setattr(agent, "fetch_mcp_session", fake_fetch)
    monkeypatch.setattr(agent, "call_mcp_tool", fake_call)
    tool_call = {"id": "call-1", "function": {"name": charts.OHLC_TOOL_NAME,
                                              "arguments": '{"ind_key":"INDS01052","interval":"1day","lookback":"1y"}'}}
    # The model elides the candles, as a local model did for a year of daily data.
    elided = '## BEL\n```chart\n{"type":"candlestick","title":"BEL","candles":[{"label":"2026-10-13", ...}]}\n```'
    captured = []
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient(
        [_completion(tool_calls=[tool_call]), _completion(elided)], captured))

    answer = await agent.run_agent("Show a candlestick chart of BEL", "https://mcp", "token",
                                   [{"role": "user", "content": "earlier"},
                                    {"role": "assistant", "content": 'Before:\n```chart\n{"type":"bar","data":[]}\n```'}],
                                   "portfolio", "openai")

    assert len(_chart(str(answer))["ohlc"]) == 3
    assert "⚠️" not in str(answer)  # every charted number came from the tool
    sent_history = captured[0]["payload"]["messages"][2]["content"]
    assert sent_history == f"Before:\n{charts.CHART_IN_HISTORY}"


def test_saved_answers_keep_a_full_chart(client, signed_in, monkeypatch):
    chat = client.post("/api/chats", json={"agentType": "finance"}).json()
    long_answer = "x" * 20_000

    async def fake_run_agent(message, mcp_url, token, history, agent_type, provider, *, context=None, saved_context=None, plan_parts=()):
        return long_answer

    monkeypatch.setattr(main, "run_agent", fake_run_agent)
    client.post("/api/chat", json={"message": "chart please", "chatId": chat["id"]})

    saved = client.get(f"/api/chats/{chat['id']}").json()["messages"][-1]["content"]
    assert len(saved) == 20_000

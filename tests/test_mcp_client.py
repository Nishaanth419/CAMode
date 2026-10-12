from __future__ import annotations

import json

import httpx
import pytest

from app import mcp_client


def test_filter_portfolio_tools_keeps_only_core_tools_in_stable_order():
    supplied = [
        {"name": "unrelated_market_tool"},
        {"name": "mf_sips", "inputSchema": {"type": "object"}},
        {"name": "networth_snapshot"},
        {"name": "networth_holdings"},
    ]

    filtered = mcp_client.filter_portfolio_tools(supplied)

    assert [tool["name"] for tool in filtered] == [
        "networth_snapshot", "networth_holdings", "mf_sips"
    ]
    assert {tool["name"] for tool in filtered}.issubset(mcp_client.IMPORTANT_PORTFOLIO_TOOL_NAMES)


def test_filter_portfolio_tools_handles_empty_and_malformed_entries():
    assert mcp_client.filter_portfolio_tools([]) == []
    assert mcp_client.filter_portfolio_tools([None, "bad", {"name": "unknown"}]) == []


def test_profile_review_gets_research_tools_but_not_the_full_server_catalog():
    supplied = [{"name": name} for name in (
        "networth_snapshot", "networth_holdings", "lookup_ind_keys", "get_mf_funds_details",
        "get_indian_stocks_details", "get_us_stocks_details", "get_indian_stocks_ohlc",
        "get_family_portfolio",
    )]

    filtered = mcp_client.filter_portfolio_tools(supplied, mcp_client.PORTFOLIO_PROFILE_TOOL_NAMES)

    assert [item["name"] for item in filtered] == [
        "networth_snapshot", "networth_holdings", "lookup_ind_keys", "get_mf_funds_details",
        "get_indian_stocks_details", "get_us_stocks_details",
    ]


def _response(payload, *, status=200, headers=None, content_type="application/json"):
    body = json.dumps(payload).encode()
    return httpx.Response(status, headers={"content-type": content_type, **(headers or {})}, content=body)


@pytest.mark.asyncio
async def test_fetch_mcp_session_initializes_and_lists_tools(monkeypatch):
    seen = []
    responses = [
        _response({"result": {"protocolVersion": "2025-03-26"}}, headers={"mcp-session-id": "session-1"}),
        _response({"result": {}}),
        _response({"result": {"tools": [{"name": "networth_snapshot"}]}}),
    ]

    async def fake_post(client, url, headers, payload):
        seen.append((url, headers, payload))
        return responses.pop(0)

    monkeypatch.setattr(mcp_client, "_post", fake_post)
    result = await mcp_client.fetch_mcp_session("https://mcp.example/mcp", "secret-token")

    assert result == {
        "tools": [{"name": "networth_snapshot"}],
        "sessionId": "session-1",
        "protocolVersion": "2025-03-26",
    }
    assert [item[2].get("method") for item in seen] == ["initialize", "notifications/initialized", "tools/list"]
    assert seen[0][1]["Authorization"] == "Bearer secret-token"
    assert seen[2][1]["mcp-session-id"] == "session-1"


@pytest.mark.asyncio
async def test_fetch_mcp_session_reports_unauthorized_error(monkeypatch):
    response = httpx.Response(
        401,
        headers={"www-authenticate": 'Bearer error="invalid_token"'},
        content=b'{"error":"invalid_token"}',
    )

    async def fake_post(client, url, headers, payload):
        return response

    monkeypatch.setattr(mcp_client, "_post", fake_post)
    with pytest.raises(RuntimeError, match="MCP initialize failed: 401"):
        await mcp_client.fetch_mcp_session("https://mcp.example/mcp", "expired")


def test_rpc_body_parses_server_sent_events():
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=b'data: {"jsonrpc":"2.0","result":{"ok":true}}\n\ndata: [DONE]\n',
    )

    assert mcp_client._rpc_body(response) == {"jsonrpc": "2.0", "result": {"ok": True}}


@pytest.mark.asyncio
async def test_call_mcp_tool_returns_text_content(monkeypatch):
    captured = {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers, json):
            captured.update(url=url, headers=headers, payload=json)
            return _response({"result": {"content": [{"type": "text", "text": "₹42,000"}]}})

    monkeypatch.setattr(mcp_client.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    result = await mcp_client.call_mcp_tool(
        "https://mcp.example/mcp", "token", "2025-03-26", "sid", "networth_snapshot", {"scope": "all"}
    )

    assert result == "₹42,000"
    assert captured["payload"]["params"] == {"name": "networth_snapshot", "arguments": {"scope": "all"}}
    assert captured["headers"]["mcp-session-id"] == "sid"


@pytest.mark.asyncio
async def test_call_mcp_tool_surfaces_mcp_errors(monkeypatch):
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return _response({"result": {"isError": True, "content": [{"text": "account unavailable"}]}})

    monkeypatch.setattr(mcp_client.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    with pytest.raises(RuntimeError, match="account unavailable"):
        await mcp_client.call_mcp_tool("https://mcp.example/mcp", "token", "v", None, "tool", {})

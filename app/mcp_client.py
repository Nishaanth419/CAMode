"""Small Streamable HTTP MCP client used for INDmoney tool discovery/calls."""

from __future__ import annotations

import json
from typing import Any

import httpx

DEFAULT_PROTOCOL_VERSION = "2025-03-26"

# Allow only INDmoney's documented read-only finance and market capabilities.
IMPORTANT_PORTFOLIO_TOOL_NAMES = (
    "lookup_ind_keys",
    "networth_snapshot",
    "networth_allocation_breakdown",
    "networth_holdings",
    "get_indian_stocks_ohlc",
    "get_indian_stocks_details",
    "get_indian_stocks_option_chain",
    "get_indian_stocks_greeks_history",
    "user_watchlist",
    "indian_stocks_sips",
    "get_mf_by_category",
    "get_mf_funds_details",
    "mf_sips",
    "get_us_stocks_details",
    # Retain this tool for INDmoney MCP deployments that expose it.
    "us_stocks_sips",
)


def filter_portfolio_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only documented read-only finance tools in a stable order."""
    by_name = {tool.get("name"): tool for tool in tools if isinstance(tool, dict)}
    return [by_name[name] for name in IMPORTANT_PORTFOLIO_TOOL_NAMES if name in by_name]


def _headers(token: str, protocol: str = DEFAULT_PROTOCOL_VERSION, session_id: str | None = None) -> dict[str, str]:
    result = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
        "MCP-Protocol-Version": protocol,
    }
    if session_id:
        result["mcp-session-id"] = session_id
    return result


def _sse_messages(text: str) -> list[dict[str, Any]]:
    """Parse JSON payloads from an event stream, joining multi-line ``data:`` fields."""
    messages: list[dict[str, Any]] = []
    data_lines: list[str] = []
    for line in [*text.splitlines(), ""]:
        if line.startswith("data:"):
            data_lines.append(line[5:].removeprefix(" "))
        elif not line.strip() and data_lines:
            payload = "\n".join(data_lines).strip()
            data_lines = []
            if payload and payload != "[DONE]":
                parsed = json.loads(payload)
                if isinstance(parsed, dict):
                    messages.append(parsed)
    return messages


def _rpc_body(response: httpx.Response) -> dict[str, Any] | None:
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        # Servers may stream notifications (e.g. progress) before the JSON-RPC response.
        messages = _sse_messages(response.text)
        return next((item for item in messages if "result" in item or "error" in item), messages[-1] if messages else None)
    if not response.content:
        return None
    return response.json()


async def _post(client: httpx.AsyncClient, url: str, headers: dict[str, str], payload: dict[str, Any]) -> httpx.Response:
    return await client.post(url, headers=headers, json=payload)


async def fetch_mcp_session(url: str, token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=45) as client:
        init = await _post(client, url, _headers(token), {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "ca-mode-agent", "version": "1.0.0"},
            },
        })
        if not init.is_success:
            challenge = init.headers.get("www-authenticate")
            hint = " INDmoney rejected the saved OAuth token; reconnect the account." if init.status_code == 401 else ""
            if init.status_code == 401 and not token:
                hint = " INDmoney requires OAuth sign-in; this request has no user token."
            raise RuntimeError(
                f"MCP initialize failed: {init.status_code} {init.reason_phrase}.{hint}"
                f"{f' WWW-Authenticate: {challenge}.' if challenge else ''}"
                f"{f' Response: {init.text[:500]}' if init.text else ''}"
            )
        result = _rpc_body(init)
        if not result or "result" not in result:
            raise RuntimeError(f"MCP initialize returned unexpected result: {result!r}")
        session_id = init.headers.get("mcp-session-id")
        protocol = result["result"].get("protocolVersion") or init.headers.get("MCP-Protocol-Version") or DEFAULT_PROTOCOL_VERSION
        headers = _headers(token, protocol, session_id)
        await _post(client, url, headers, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools_response = await _post(client, url, headers, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        })
        if not tools_response.is_success:
            raise RuntimeError(f"MCP tools/list failed: {tools_response.status_code} {tools_response.text[:500]}")
        tools_result = _rpc_body(tools_response)
        tools = (tools_result or {}).get("result", {}).get("tools")
        if not isinstance(tools, list):
            raise RuntimeError(f"MCP tools/list returned unexpected result: {tools_result!r}")
        return {"tools": tools, "sessionId": session_id, "protocolVersion": protocol}


class McpToolError(RuntimeError):
    """A tool ran and reported an error (``isError``), such as rejected arguments.

    MCP returns these as results so the model can correct the call; the agent passes
    them back instead of failing the request. Transport and JSON-RPC errors, and
    tool errors that read as an authentication failure (so the token is refreshed),
    stay plain ``RuntimeError``.
    """


def _reads_as_auth_failure(text: str) -> bool:
    lowered = text.lower()
    return "401" in lowered or any(term in lowered for term in ("invalid_token", "authentication required", "unauthorized"))


async def call_mcp_tool(
    url: str,
    token: str,
    protocol: str,
    session_id: str | None,
    name: str,
    arguments: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> Any:
    """Call one MCP tool; pass ``client`` to reuse a connection pool across calls."""
    payload = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    headers = _headers(token, protocol, session_id)
    if client is None:
        async with httpx.AsyncClient(timeout=90) as own_client:
            response = await _post(own_client, url, headers, payload)
    else:
        response = await _post(client, url, headers, payload)
    if not response.is_success:
        raise RuntimeError(f'MCP tools/call failed for "{name}": {response.status_code} {response.text[:500]}')
    rpc = _rpc_body(response) or {}
    if rpc.get("error"):
        raise RuntimeError(f'MCP tools/call error for "{name}": {rpc["error"].get("message", rpc["error"])}')
    result = rpc.get("result") or {}
    content = result.get("content")
    if result.get("isError") is True:
        rendered = "\n".join(str(item.get("text", item)) for item in content or [])
        message = f'MCP tool "{name}" failed: {rendered or result}'
        raise RuntimeError(message) if _reads_as_auth_failure(message) else McpToolError(message)
    if isinstance(content, list):
        return "\n".join(str(item.get("text", json.dumps(item, ensure_ascii=False))) for item in content)
    return result

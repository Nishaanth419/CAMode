"""MCP OAuth 2.1 for brokers whose hosted MCP server signs users in itself (Upstox, Dhan, Groww).

Generic steps only: discover the authorization server from the MCP server's 401 challenge (or its
well-known document), register this app dynamically (DCR), build a PKCE S256 authorization link,
exchange the code and refresh tokens. Every failure is an ``OAuthError`` whose message is safe to
show. INDmoney keeps its own, older flow in ``main.py``; it is not routed through here.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

TIMEOUT_SECONDS = 30
_RESOURCE_METADATA_RE = re.compile(r'resource_metadata="?([^",\s]+)"?', re.IGNORECASE)


class OAuthError(Exception):
    """A sign-in problem whose message is safe to show the user."""


def _https(url: Any) -> str:
    if not isinstance(url, str):
        raise OAuthError("The broker's sign-in details are incomplete.")
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise OAuthError("The broker's sign-in details are not usable.")
    return url


def pkce() -> tuple[str, str]:
    """A fresh PKCE verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


async def _json(client: httpx.AsyncClient, url: str) -> dict[str, Any] | None:
    response = await client.get(_https(url))
    if not response.is_success:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


async def discover(mcp_url: str, client: httpx.AsyncClient) -> dict[str, Any]:
    """The authorization server's metadata for an MCP server (needs authorization, token and registration endpoints)."""
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(_https(mcp_url)))
    resource_url = None
    probe = await client.post(
        mcp_url, headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "ca-mode", "version": "1.0.0"}}},
    )
    found = _RESOURCE_METADATA_RE.search(probe.headers.get("www-authenticate", ""))
    if found:
        resource_url = found.group(1)
    resource = await _json(client, resource_url or f"{origin}/.well-known/oauth-protected-resource")
    if resource is None and resource_url:
        resource = await _json(client, f"{origin}/.well-known/oauth-protected-resource")
    servers = (resource or {}).get("authorization_servers")
    issuer = servers[0] if isinstance(servers, list) and servers else origin
    base = _https(issuer).rstrip("/")
    metadata = await _json(client, f"{base}/.well-known/oauth-authorization-server")
    if metadata is None:
        metadata = await _json(client, f"{base}/.well-known/openid-configuration")
    if not metadata or not all(metadata.get(key) for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint")):
        raise OAuthError("The broker's sign-in does not support automatic setup.")
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        _https(metadata[key])
    return metadata


async def register(metadata: dict[str, Any], redirect_uri: str, client: httpx.AsyncClient) -> dict[str, str | None]:
    """Dynamic client registration; returns ``{clientId, clientSecret}`` (the secret may be absent for a public client)."""
    methods = metadata.get("token_endpoint_auth_methods_supported")
    method = "none" if isinstance(methods, list) and "none" in methods else "client_secret_post"
    response = await client.post(_https(metadata["registration_endpoint"]), json={
        "client_name": "CA Mode", "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": method,
    })
    if not response.is_success:
        raise OAuthError("The broker did not accept CA Mode's sign-in registration.")
    try:
        info = response.json()
    except ValueError:
        info = None
    if not isinstance(info, dict) or not info.get("client_id"):
        raise OAuthError("The broker did not accept CA Mode's sign-in registration.")
    secret = info.get("client_secret")
    return {"clientId": str(info["client_id"]), "clientSecret": str(secret) if secret else None}


def authorize_url(metadata: dict[str, Any], client_id: str, redirect_uri: str, state: str, challenge: str, resource: str) -> str:
    return _https(metadata["authorization_endpoint"]) + ("&" if "?" in metadata["authorization_endpoint"] else "?") + urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": resource,
    })


async def _token(token_endpoint: str, form: dict[str, str], client_info: dict[str, Any], client: httpx.AsyncClient) -> dict[str, Any]:
    form = {**form, "client_id": client_info["clientId"]}
    if client_info.get("clientSecret"):
        form["client_secret"] = client_info["clientSecret"]
    response = await client.post(_https(token_endpoint), data=form)
    if not response.is_success:
        raise OAuthError(f"The broker refused the sign-in (HTTP {response.status_code}).")
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or not body.get("access_token"):
        raise OAuthError("The broker's sign-in did not return an access token.")
    return body


async def exchange_code(
    token_endpoint: str, client_info: dict[str, Any], code: str, verifier: str, redirect_uri: str, resource: str,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    return await _token(token_endpoint, {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "code_verifier": verifier, "resource": resource,
    }, client_info, client)


async def refresh(
    token_endpoint: str, client_info: dict[str, Any], refresh_token: str, resource: str, client: httpx.AsyncClient,
) -> dict[str, Any]:
    return await _token(token_endpoint, {
        "grant_type": "refresh_token", "refresh_token": refresh_token, "resource": resource,
    }, client_info, client)

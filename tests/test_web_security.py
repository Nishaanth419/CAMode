"""Protections every response and write gets: same-origin writes, __Host- cookies, headers, body limits."""

from __future__ import annotations

from fastapi import Request
from fastapi.testclient import TestClient

from app import admin, main
from conftest import sign_up


def test_writes_from_another_site_are_refused(client):
    sign_up(client)
    other_site = {"Origin": "https://1-2-3-4.sslip.io"}

    refused = client.post("/auth/indmoney/disconnect", headers=other_site)
    assert refused.status_code == 403
    assert client.delete("/api/users/me/history", headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert client.post("/api/chats", json={}, headers={"Origin": "null"}).status_code == 403
    # Reading is unaffected, and the profile is still signed in.
    assert client.get("/api/session", headers=other_site).json()["user"]["displayName"] == "Asha"


def test_writes_from_the_app_itself_pass(client, monkeypatch):
    sign_up(client)
    assert client.post("/api/chats", json={}, headers={"Origin": "http://testserver"}).status_code == 201
    assert client.post("/api/chats", json={}, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 201
    monkeypatch.setenv("APP_ORIGIN", "https://camode.example.com")
    behind_proxy = {"Origin": "https://camode.example.com", "Host": "camode.example.com", "X-Forwarded-Proto": "https"}
    assert client.post("/api/chats", json={}, headers=behind_proxy).status_code != 403


def test_cookies_carry_the_host_prefix_over_https():
    with TestClient(main.app, base_url="https://testserver", follow_redirects=False) as client:
        client.get("/api/session")
        response = client.post("/api/users", json={"displayName": "Asha", "pin": "2468"})

        set_cookie = response.headers["set-cookie"]
        assert set_cookie.startswith(f"__Host-{main.SESSION_COOKIE}=")
        assert "Secure" in set_cookie and "Domain" not in set_cookie and "Path=/" in set_cookie
        assert client.get("/api/session").json()["user"]["displayName"] == "Asha"
        # A cookie a sibling host could set under the plain name is ignored.
        client.cookies.clear()
        client.cookies.set(main.SESSION_COOKIE, response.cookies.get(f"__Host-{main.SESSION_COOKIE}") or "x")
        assert client.get("/api/session").json()["user"] is None


def test_admin_cookie_follows_the_same_rule():
    secure = Request({"type": "http", "scheme": "https", "headers": [], "path": "/", "server": ("testserver", 80)})
    plain = Request({"type": "http", "scheme": "http", "headers": [], "path": "/", "server": ("testserver", 80)})
    assert main.admin_cookie_name(secure) == f"__Host-{admin.ADMIN_COOKIE}"
    assert main.admin_cookie_name(plain) == admin.ADMIN_COOKIE
    proxied = Request({"type": "http", "scheme": "http", "headers": [(b"x-forwarded-proto", b"https")], "path": "/", "server": ("testserver", 80)})
    assert main.session_cookie_name(proxied) == f"__Host-{main.SESSION_COOKIE}"


def test_security_headers_on_pages_and_api(client):
    for path in ("/", "/privacy", "/api/session", "/healthz"):
        headers = client.get(path).headers
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert "script-src 'self' https://cdn.plot.ly" in headers["content-security-policy"]
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert "strict-transport-security" not in headers  # Plain HTTP here.


def test_oversized_bodies_are_refused_before_they_are_read(client):
    big = "x" * (main.MAX_BODY_BYTES + 1)
    response = client.post("/api/users", content=big, headers={"Content-Type": "application/json"})
    assert response.status_code == 413

    def chunks():  # No Content-Length: counted while it streams in.
        for _ in range(main.MAX_BODY_BYTES // 1024 + 2):
            yield b"x" * 1024

    assert client.post("/api/users", content=chunks(), headers={"Content-Type": "application/json"}).status_code == 413


def test_statement_uploads_keep_their_larger_limit(client):
    sign_up(client)
    body = b'{"password": "", "pdfBase64": "' + b"A" * (main.MAX_BODY_BYTES * 2) + b'"}'
    assert client.post("/api/cas", content=body, headers={"Content-Type": "application/json"}).status_code != 413


def test_api_schema_is_not_published(client):
    assert client.get("/openapi.json").status_code == 404

from __future__ import annotations

import asyncio

import httpx
import pytest

from app import logos

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 50
DAY = 24 * 60 * 60 * 1000


@pytest.mark.parametrize(
    ("asset", "code", "expected"),
    [
        ("MF", "120503", "mf:120503"),
        ("STOCK", " infy ", "in:INFY"),
        ("IND_STOCK", "M&M", "in:M&M"),
        ("US_STOCK", "brk.b", "us:BRK.B"),
        ("IND_STOCK", "INE009A01021", None),
        ("MF", "INF179K01UT0", None),
        ("IND_STOCK", "../x", None),
        ("IND_STOCK", "..", None),
        ("IND_STOCK", "-", None),
        (None, "INFY", None),
    ],
)
def test_logo_key_cases(asset, code, expected):
    assert logos.logo_key(asset, code) == expected


def test_file_name_stays_inside_dir():
    for asset, code in [("MF", "120503"), ("STOCK", "infy"), ("IND_STOCK", "M&M"), ("US_STOCK", "brk.b"), ("IND_STOCK", "A..B")]:
        key = logos.logo_key(asset, code)
        name = logos.file_name(key)
        assert ".." not in name and "/" not in name
        assert (logos.logos_dir() / (name + ".png")).resolve().parent == logos.logos_dir().resolve()


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _fetch(handler, url="https://x.test/a.png"):
    async def run():
        async with _client(handler) as client:
            return await logos.download(url, client)

    return asyncio.run(run())


def test_download_accepts_png_and_rejects_others():
    ok = lambda body, status=200: (lambda request: httpx.Response(status, content=body))  # noqa: E731
    assert _fetch(ok(PNG)) == (PNG, "png")
    assert _fetch(ok(b"\xff\xd8\xff" + b"0" * 20))[1] == "jpg"
    assert _fetch(ok(b"RIFF\x00\x00\x00\x00WEBPVP8 "))[1] == "webp"
    assert _fetch(ok(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")) is None
    assert _fetch(ok(b"<html></html>")) is None
    assert _fetch(ok(b"")) is None
    assert _fetch(ok(PNG, 404)) is None
    assert _fetch(ok(PNG), url="http://x.test/a.png") is None
    assert _fetch(ok(PNG + b"0" * (200_001 - len(PNG)))) is None
    assert _fetch(ok(PNG + b"0" * (200_000 - len(PNG)))) is not None

    def to_http(request):
        return httpx.Response(302, headers={"location": "http://evil.test/a.png"})

    assert _fetch(to_http) is None

    def to_https(request):
        if request.url.host == "x.test":
            return httpx.Response(302, headers={"location": "https://y.test/a.png"})
        return httpx.Response(200, content=PNG)

    assert _fetch(to_https) == (PNG, "png")


def test_retry_windows():
    assert logos.is_due("in:INFY", 0)
    logos.record("in:INFY", "missing", None, 0)
    assert not logos.is_due("in:INFY", 29 * DAY)
    assert logos.is_due("in:INFY", 30 * DAY)
    logos.record("in:TCS", "failed", None, 0)
    assert not logos.is_due("in:TCS", DAY - 1)
    assert logos.is_due("in:TCS", DAY)
    (logos.logos_dir() / "in-HDFC.png").write_bytes(PNG)
    logos.record("in:HDFC", "found", "in-HDFC.png", 0)
    assert not logos.is_due("in:HDFC", 365 * DAY)
    assert logos.cached_path("in:HDFC") is not None
    (logos.logos_dir() / "in-HDFC.png").unlink()
    assert logos.cached_path("in:HDFC") is None
    assert logos.is_due("in:HDFC", 1)


def test_with_logos_only_cached():
    (logos.logos_dir() / "in-INFY.png").write_bytes(PNG)
    logos.record("in:INFY", "found", "in-INFY.png", 0)
    logos.record("in:TCS", "missing", None, 0)
    rows = [
        {"asset_type": "IND_STOCK", "code": "INFY"},
        {"asset_type": "STOCK", "code": "TCS"},
        {"asset_type": "IND_STOCK", "code": "INE009A01021"},
    ]
    assert logos.with_logos(rows, "u1") is rows
    assert rows[0]["logo"] == "/api/logos/in:INFY?s=" + logos.sign("u1", "in:INFY")
    assert "logo" not in rows[1] and "logo" not in rows[2]


def test_logo_signature_is_per_profile_and_per_key():
    signature = logos.sign("u1", "in:INFY")
    assert len(signature) == 22 and "=" not in signature  # 16 bytes, base64url without padding
    assert signature != logos.sign("u2", "in:INFY")
    assert signature != logos.sign("u1", "in:TCS")
    assert logos.signature_ok("u1", "in:INFY", signature)
    for user_id, key, given in [("u2", "in:INFY", signature), ("u1", "in:TCS", signature), ("u1", "in:INFY", ""),
                                ("u1", "in:INFY", None), ("u1", "in:INFY", signature[:-1]), ("u1", "in:INFY", "é" * 22),
                                ("", "in:INFY", signature)]:
        assert not logos.signature_ok(user_id, key, given)


@pytest.mark.parametrize("code", ["²", "١٢٣", "１２３", "1" * 13])
def test_mf_code_must_be_ascii_digits(code):
    assert logos.logo_key("MF", code) is None


@pytest.mark.parametrize("code", ["straße", "ﬁnd", "ß"])
def test_stock_symbol_must_be_ascii(code):
    assert logos.logo_key("IND_STOCK", code) is None
    assert logos.logo_key("US_STOCK", code) is None


def test_download_never_raises_on_malformed_input():
    def bad_location(request):
        return httpx.Response(302, headers={"location": "http://[bad"})

    assert _fetch(bad_location) is None
    assert _fetch(lambda r: httpx.Response(200, content=PNG), url="https://[bad") is None


def test_download_sends_no_client_default_headers():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=PNG)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer x"},
            cookies={"sid": "1"},
        ) as client:
            return await logos.download("https://x.test/a.png", client)

    assert asyncio.run(run()) == (PNG, "png")
    assert "authorization" not in seen[0].headers and "cookie" not in seen[0].headers
    assert seen[0].headers["host"] == "x.test"


def test_download_over_a_real_connection(monkeypatch):
    """A real socket (h11 needs a Host header; MockTransport does not check it)."""
    import http.server
    import threading

    seen_hosts = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen_hosts.append(self.headers.get("Host"))
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG)))
            self.end_headers()
            self.wfile.write(PNG)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(logos, "_is_https", lambda url: url.startswith(("https://", "http://127.0.0.1:")))
    try:
        async def run():
            async with httpx.AsyncClient() as client:
                return await logos.download(f"http://127.0.0.1:{server.server_address[1]}/a.png", client)

        assert asyncio.run(run()) == (PNG, "png")
    finally:
        server.shutdown()
        server.server_close()
    assert seen_hosts == [f"127.0.0.1:{server.server_address[1]}"]


def test_download_ignores_client_level_auth_on_every_hop():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.host == "x.test":
            return httpx.Response(302, headers={"location": "https://y.test/a.png"})
        return httpx.Response(200, content=PNG)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=("u", "p")) as client:
            return await logos.download("https://x.test/a.png", client)

    assert asyncio.run(run()) == (PNG, "png")
    assert len(seen) == 2
    assert all("authorization" not in request.headers for request in seen)


# ── Sources and automatic fill ─────────────────────────────────────────────────

import json  # noqa: E402

from tests.fixtures import indmoney_funds  # noqa: E402


@pytest.fixture
def logo_transport(monkeypatch):
    """Every logos HTTP client goes through one MockTransport; returns the list of requests seen."""
    seen: list[httpx.Request] = []
    state = {"handler": lambda request: httpx.Response(200, content=PNG)}
    real = httpx.AsyncClient

    def handler(request):
        seen.append(request)
        return state["handler"](request)

    def make(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(logos.httpx, "AsyncClient", make)
    seen_handler = state
    seen_handler["requests"] = seen
    return seen_handler


def test_fill_fund_logos_batches_and_records(logo_transport):
    ids = [str(100 + n) for n in range(12)]
    calls = []

    async def call(name, arguments):
        calls.append((name, arguments))
        wanted = arguments["fund_ids"].split(",")
        # The last id of the second batch is missing from the reply.
        return indmoney_funds.details({fund_id: None for fund_id in wanted if fund_id != "111"})

    saved = asyncio.run(logos.fill_fund_logos(ids, call, now_ms=1000))

    assert [len(arguments["fund_ids"].split(",")) for _, arguments in calls] == [10, 2]
    assert all(name == "get_mf_funds_details" for name, _ in calls)
    assert saved == 11
    assert logos.status_of("mf:100")["status"] == "found"
    assert logos.status_of("mf:111")["status"] == "missing"
    assert logos.cached_path("mf:100").is_file()


def test_fill_fund_logos_marks_no_image_missing_and_failures_failed(logo_transport):
    reply = json.dumps({"success": True, "data": [
        {"fund_id": "1", "data": {"fund_detail": {}}},
        {"fund_id": "2", "data": {"fund_detail": {"image_url": "https://example.invalid/x.png"}}},
    ]})
    logo_transport["handler"] = lambda request: httpx.Response(500)

    async def call(name, arguments):
        return reply

    assert asyncio.run(logos.fill_fund_logos(["1", "2"], call, now_ms=1000)) == 0
    assert logos.status_of("mf:1")["status"] == "missing"
    assert logos.status_of("mf:2")["status"] == "failed"

    async def broken(name, arguments):
        raise RuntimeError("down")

    assert asyncio.run(logos.fill_fund_logos(["3"], broken, now_ms=1000)) == 0
    assert logos.status_of("mf:3")["status"] == "failed"


def test_fill_fund_logos_skips_fresh_rows(logo_transport):
    logos.record("mf:100", "found", "mf-100.png", 1000)
    (logos.logos_dir() / "mf-100.png").write_bytes(PNG)
    calls = []

    async def call(name, arguments):
        calls.append(arguments)
        return indmoney_funds.details({fund_id: None for fund_id in arguments["fund_ids"].split(",")})

    asyncio.run(logos.fill_fund_logos(["100", "101"], call, now_ms=2000))
    assert calls == [{"fund_ids": "101"}]


def test_fill_stock_logos_needs_token(logo_transport, monkeypatch):
    assert asyncio.run(logos.fill_stock_logos(["in:INFY"], now_ms=1000)) == 0
    assert logo_transport["requests"] == []

    monkeypatch.setenv("LOGO_DEV_TOKEN", "pk_test")
    replies = {"INFY.NS": httpx.Response(200, content=PNG), "AAPL": httpx.Response(404),
               "BAD.NS": httpx.Response(200, content=b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")}
    logo_transport["handler"] = lambda request: replies[request.url.path.rsplit("/", 1)[1]]

    saved = asyncio.run(logos.fill_stock_logos(["in:INFY", "us:AAPL", "in:BAD"], now_ms=1000))

    assert saved == 1
    assert logos.status_of("in:INFY")["status"] == "found"
    assert logos.status_of("us:AAPL")["status"] == "missing"
    assert logos.status_of("in:BAD")["status"] == "failed"
    for request in logo_transport["requests"]:
        assert request.url.host == "img.logo.dev"
        assert dict(request.url.params).keys() <= {"token", "format", "size", "fallback"}
        assert request.url.params["token"] == "pk_test"
        assert not request.headers.get("authorization")


def test_same_key_fetched_once(logo_transport, monkeypatch):
    monkeypatch.setenv("LOGO_DEV_TOKEN", "pk_test")

    async def both():
        return await asyncio.gather(
            logos.fill_stock_logos(["in:INFY"], now_ms=1000), logos.fill_stock_logos(["in:INFY"], now_ms=1000),
        )

    assert sorted(asyncio.run(both())) == [0, 1]
    assert len(logo_transport["requests"]) == 1


def test_downloads_never_exceed_the_concurrency_limit(logo_transport, monkeypatch):
    monkeypatch.setenv("LOGO_DEV_TOKEN", "pk_test")
    running = {"now": 0, "peak": 0}
    real_download = logos._download

    async def counting(url, client):
        running["now"] += 1
        running["peak"] = max(running["peak"], running["now"])
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        try:
            return await real_download(url, client)
        finally:
            running["now"] -= 1

    monkeypatch.setattr(logos, "_download", counting)
    asyncio.run(logos.fill_stock_logos([f"in:S{n}" for n in range(12)], now_ms=1000))
    assert running["peak"] <= logos.LOGO_CONCURRENCY


def test_schedule_fill_does_nothing_when_disabled():
    assert logos.schedule_fill([{"asset_type": "IND_STOCK", "code": "INFY"}]) is None


def test_schedule_fill_logs_and_swallows_failures(monkeypatch, caplog):
    monkeypatch.setenv("LOGOS_ENABLED", "true")

    async def boom(*args, **kwargs):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(logos, "fill_stock_logos", boom)

    async def run():
        task = logos.schedule_fill([{"asset_type": "IND_STOCK", "code": "INFY"}])
        await task
        return task

    task = asyncio.run(run())
    assert task.exception() is None
    assert any(record.message == "logo_fill_failed" and record.error == "RuntimeError" for record in caplog.records)
    assert not logos._pending


def test_capture_survives_logo_failure(monkeypatch):
    from tests.test_snapshots import at, fake_indmoney, make_user
    from app import snapshots

    monkeypatch.setenv("LOGOS_ENABLED", "true")

    async def boom(*args, **kwargs):
        raise RuntimeError("logo host down")

    monkeypatch.setattr(logos, "fill_fund_logos", boom)
    monkeypatch.setattr(logos, "fill_stock_logos", boom)
    user_id = make_user()
    fake_indmoney(monkeypatch, offered=(*snapshots.SNAPSHOT_TOOL_NAMES, "get_mf_funds_details"))

    async def run():
        result = await snapshots.capture_snapshot(user_id, mcp_url="https://mcp", now=at(13, 9, 2))
        await asyncio.gather(*logos._pending)
        return result

    result = asyncio.run(run())
    assert result["asOfDate"] == "2026-10-13"
    assert snapshots.latest_snapshot(user_id) is not None


def test_capture_hands_the_fund_ids_to_the_fill(monkeypatch):
    from tests.test_snapshots import at, fake_indmoney, make_user
    from app import snapshots

    monkeypatch.setenv("LOGOS_ENABLED", "true")
    seen = {}

    async def funds(ids, call, **kwargs):
        seen["ids"] = sorted(ids)
        seen["call"] = call
        return 0

    async def stocks(keys, **kwargs):
        seen["stocks"] = sorted(keys)
        return 0

    monkeypatch.setattr(logos, "fill_fund_logos", funds)
    monkeypatch.setattr(logos, "fill_stock_logos", stocks)
    user_id = make_user()
    fake_indmoney(monkeypatch, offered=(*snapshots.SNAPSHOT_TOOL_NAMES, "get_mf_funds_details"))

    async def run():
        await snapshots.capture_snapshot(user_id, mcp_url="https://mcp", now=at(13, 9, 2))
        await asyncio.gather(*logos._pending)

    asyncio.run(run())
    assert seen["ids"] == ["118989", "120503"]
    assert "in:INFY" in seen["stocks"]
    assert callable(seen["call"])


def test_same_fund_fetched_once(logo_transport):
    calls = []

    async def call(name, arguments):
        calls.append(arguments)
        await asyncio.sleep(0)
        return indmoney_funds.details({fund_id: None for fund_id in arguments["fund_ids"].split(",")})

    async def both():
        return await asyncio.gather(logos.fill_fund_logos(["100"], call, now_ms=1000), logos.fill_fund_logos(["100"], call, now_ms=1000))

    assert sorted(asyncio.run(both())) == [0, 1]
    assert calls == [{"fund_ids": "100"}] and len(logo_transport["requests"]) == 1


def test_concurrency_cap_is_shared_across_fills(logo_transport, monkeypatch):
    monkeypatch.setenv("LOGO_DEV_TOKEN", "pk_test")
    running = {"now": 0, "peak": 0}
    real_download = logos._download

    async def counting(url, client):
        running["now"] += 1
        running["peak"] = max(running["peak"], running["now"])
        for _ in range(3):
            await asyncio.sleep(0)
        try:
            return await real_download(url, client)
        finally:
            running["now"] -= 1

    monkeypatch.setattr(logos, "_download", counting)

    async def call(name, arguments):
        return indmoney_funds.details({fund_id: None for fund_id in arguments["fund_ids"].split(",")})

    async def run():
        await asyncio.gather(
            logos.fill_stock_logos([f"in:A{n}" for n in range(8)], now_ms=1000),
            logos.fill_stock_logos([f"us:B{n}" for n in range(8)], now_ms=1000),
            logos.fill_fund_logos([str(n) for n in range(1, 9)], call, now_ms=1000),
        )

    asyncio.run(run())
    assert 0 < running["peak"] <= logos.LOGO_CONCURRENCY


def test_a_failing_fund_fill_does_not_stop_the_stock_fill(monkeypatch, caplog):
    monkeypatch.setenv("LOGOS_ENABLED", "true")
    ran = []

    async def boom(*args, **kwargs):
        raise RuntimeError("x")

    async def stocks(keys, **kwargs):
        ran.append(keys)
        return 0

    async def call(name, arguments):
        return "{}"

    monkeypatch.setattr(logos, "fill_fund_logos", boom)
    monkeypatch.setattr(logos, "fill_stock_logos", stocks)

    async def run():
        await logos.schedule_fill(
            [{"asset_type": "MF", "code": "100"}, {"asset_type": "IND_STOCK", "code": "INFY"}], fund_call=call,
        )

    asyncio.run(run())
    assert ran == [["in:INFY"]]
    assert any(r.message == "logo_fill_failed" and r.error == "RuntimeError" for r in caplog.records)


# ── Serving ────────────────────────────────────────────────────────────────────


def _cache_infy():
    (logos.logos_dir() / "in-INFY.png").write_bytes(PNG)
    logos.record("in:INFY", "found", "in-INFY.png", 0)


def test_logo_route(client):
    assert client.get("/api/logos/in:INFY").status_code == 401
    assert client.get("/api/logos/in:INFY").json()["profileRequired"] is True


def _logo_url(user_id, key):
    import urllib.parse

    return "/api/logos/" + urllib.parse.quote(key, safe=":") + "?s=" + logos.sign(user_id, key)


def test_logo_route_serves_cached_file_only(client, signed_in):
    _cache_infy()
    response = client.get(_logo_url(signed_in["id"], "in:INFY"))
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, max-age=604800"
    assert response.content == PNG
    for key in ["in:TCS", "in:..%2Fx", "x", "mf:abc", "in:infy", "in:INE009A01021"]:
        missing = client.get(f"/api/logos/{key}?s={logos.sign(signed_in['id'], key)}")
        assert missing.status_code == 404
        assert missing.headers["content-type"].startswith("application/json")


def test_logo_route_handles_ampersand_keys(client, signed_in):
    (logos.logos_dir() / "in-M_and_M.png").write_bytes(PNG)
    logos.record("in:M&M", "found", "in-M_and_M.png", 0)
    assert client.get(_logo_url(signed_in["id"], "in:M&M")).status_code == 200


def test_logo_route_needs_this_profiles_signature(client, signed_in):
    from tests.conftest import sign_up

    _cache_infy()
    url_a = _logo_url(signed_in["id"], "in:INFY")
    assert client.get(url_a).status_code == 200
    unknown = client.get(_logo_url(signed_in["id"], "in:TCS"))
    for bad in ["/api/logos/in:INFY", "/api/logos/in:INFY?s=", "/api/logos/in:INFY?s=garbled%%",
                "/api/logos/in:INFY?s=" + logos.sign(signed_in["id"], "in:TCS")]:
        response = client.get(bad)
        assert response.status_code == 404
        assert response.json() == unknown.json()  # a bad signature reads exactly like an uncached key
    client.post("/api/session/logout")
    other = sign_up(client, "Ravi", "1357")
    assert other["id"] != signed_in["id"]
    assert client.get(url_a).status_code == 404
    assert client.get(url_a).json() == unknown.json()
    assert client.get(_logo_url(other["id"], "in:INFY")).status_code == 200


def test_portfolio_and_broker_holdings_carry_logo(client, signed_in):
    from datetime import datetime

    from app import snapshots
    from fixtures import indmoney_sample

    snapshots.store_snapshot(
        signed_in["id"], datetime(2026, 10, 13, 9, 0, tzinfo=snapshots.SNAPSHOT_TIMEZONE), indmoney_sample.results(), {},
    )
    _cache_infy()
    for holdings in (
        client.get("/api/portfolio").json()["holdings"],
        client.get("/api/portfolio").json()["figures"]["top_holdings"],
        client.get("/api/holdings/combined").json()["sources"][0]["holdings"],
        client.get("/api/holdings/combined").json()["combined"]["holdings"],
    ):
        url = _logo_url(signed_in["id"], "in:INFY")
        assert [h["logo"] for h in holdings if "logo" in h] == [url]
        assert next(h for h in holdings if h["code"] == "INFY")["logo"] == url
        assert client.get(url).status_code == 200


def test_deleted_file_drops_logo(client, signed_in):
    _cache_infy()
    (logos.logos_dir() / "in-INFY.png").unlink()
    assert "logo" not in logos.with_logos([{"asset_type": "IND_STOCK", "code": "INFY"}], "u1")[0]
    assert client.get(_logo_url(signed_in["id"], "in:INFY")).status_code == 404
    assert logos.is_due("in:INFY", 1)


def test_with_logos_returns_holdings_unchanged_on_database_error(monkeypatch, caplog):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(logos.db, "connect_db", boom)
    rows = [{"asset_type": "IND_STOCK", "code": "INFY"}]
    with caplog.at_level("WARNING"):
        assert logos.with_logos(rows, "u1") is rows
    assert "logo" not in rows[0]
    assert any(r.message == "logo_lookup_failed" and r.error == "RuntimeError" for r in caplog.records)


def test_with_logos_chunks_large_lists(monkeypatch):
    _cache_infy()
    monkeypatch.setattr(logos, "_KEY_CHUNK", 3)
    rows = [{"asset_type": "MF", "code": str(100 + n)} for n in range(10)] + [{"asset_type": "IND_STOCK", "code": "INFY"}]
    logos.with_logos(rows, "u1")
    assert rows[-1]["logo"].startswith("/api/logos/in:INFY?s=")
    big = [{"asset_type": "MF", "code": str(n)} for n in range(5000)] + [{"asset_type": "IND_STOCK", "code": "INFY"}]
    monkeypatch.setattr(logos, "_KEY_CHUNK", logos._KEY_CHUNK_DEFAULT)
    assert logos.with_logos(big, "u1")[-1]["logo"].startswith("/api/logos/in:INFY?s=")

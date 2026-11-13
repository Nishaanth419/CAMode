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
    assert logos.with_logos(rows) is rows
    assert rows[0]["logo"] == "/api/logos/in:INFY"
    assert "logo" not in rows[1] and "logo" not in rows[2]

"""Company and fund-house logos for holdings: keys, the shared cache table and a safe download.

A logo key comes from the asset type and the code only (an MF scheme code, an NSE symbol, a US
ticker), never from a holding's name. The ``logos`` table and the ``logos/`` folder under the
data directory are shared by every profile and hold no profile data: a key, the outcome of the
last fetch and a file name. Downloads are https only, capped at ``MAX_LOGO_BYTES`` and accepted
only when the bytes are a PNG, JPEG or WebP image (never SVG). ``LOGOS_ENABLED=false`` turns
logos off.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import time
import urllib.parse
import weakref
from pathlib import Path
from typing import Any, Awaitable, Callable, Final, Literal

import httpx

from . import db, vault
from .portfolio_math import canonical_asset_type

logger = logging.getLogger(__name__)

MAX_LOGO_BYTES: Final = 200_000
LOGO_TIMEOUT_SECONDS: Final = 10
MISSING_RETRY_MS: Final = 30 * 24 * 60 * 60 * 1000
FAILED_RETRY_MS: Final = 24 * 60 * 60 * 1000
MAX_REDIRECTS: Final = 3
LOGO_CONCURRENCY: Final = 4
FUND_BATCH: Final = 10
# logo.dev ticker API (https://www.logo.dev/docs/logo-images/ticker): /ticker/SYMBOL?token=KEY, NSE symbols
# as SYMBOL.NS. Options (https://www.logo.dev/docs/logo-images/introduction): format=png (SVG is not
# served), size, fallback=404 (else a generated monogram comes back with 200).
LOGO_DEV_URL: Final = "https://img.logo.dev/ticker/{symbol}"
LOGO_DEV_OPTIONS: Final = "format=png&size=96&fallback=404"

# Same pattern as brokers.ISIN_RE (brokers imports this module, so it is copied here).
_ISIN_RE: Final = re.compile(r"^IN[A-Z0-9]{9}[0-9]$")
_MF_CODE_RE: Final = re.compile(r"^[0-9]{1,12}$")
_SYMBOL_RE: Final = re.compile(r"^[A-Z0-9&.-]{1,20}$")
_ALNUM_RE: Final = re.compile(r"[A-Z0-9]")

Status = Literal["found", "missing", "failed"]


def enabled() -> bool:
    return os.getenv("LOGOS_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


def logo_key(asset_type: str | None, code: str | None) -> str | None:
    """The cache key for a holding, from its asset type and code; None when it has no safe one."""
    kind = canonical_asset_type(asset_type)
    text = str(code or "").strip()
    if not kind or not text or not text.isascii():
        return None
    text = text.upper()
    if not text:
        return None
    if kind == "MF":
        return f"mf:{text}" if _MF_CODE_RE.match(text) else None
    if kind in {"IND_STOCK", "US_STOCK"}:
        if not _SYMBOL_RE.match(text) or not _ALNUM_RE.search(text) or _ISIN_RE.match(text):
            return None
        return f"{'in' if kind == 'IND_STOCK' else 'us'}:{text}"
    return None


def logos_dir() -> Path:
    path = db.DATA_DIR / "logos"
    path.mkdir(parents=True, exist_ok=True)
    return path


def file_name(key: str, extension: str = "") -> str:
    """The file name for a key (plus ``.extension`` when given); never a path separator or ``..``."""
    stem = key.replace(":", "-").replace("&", "_and_").replace(".", "_")
    return f"{stem}.{extension}" if extension else stem


def status_of(key: str) -> dict[str, Any] | None:
    with db.connect_db() as conn:
        row = conn.execute("SELECT status, file, checked_at FROM logos WHERE key = ?", (key,)).fetchone()
    return dict(row) if row else None


def record(key: str, status: Status, file: str | None, now_ms: int) -> None:
    with db.connect_db() as conn:
        conn.execute(
            "INSERT INTO logos (key, status, file, checked_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET status = excluded.status, file = excluded.file, "
            "checked_at = excluded.checked_at",
            (key, status, file, now_ms),
        )


def _file_ok(file: str | None) -> Path | None:
    if not file or Path(file).name != file:
        return None
    path = logos_dir() / file
    try:
        return path if path.is_file() and path.stat().st_size > 0 else None
    except OSError:
        return None


def is_due(key: str, now_ms: int) -> bool:
    """True when nothing is known for the key or its retry window has passed."""
    row = status_of(key)
    if row is None:
        return True
    if row["status"] == "found":
        return _file_ok(row["file"]) is None
    window = MISSING_RETRY_MS if row["status"] == "missing" else FAILED_RETRY_MS
    return now_ms - row["checked_at"] >= window


def cached_path(key: str) -> Path | None:
    row = status_of(key)
    if row is None or row["status"] != "found":
        return None
    return _file_ok(row["file"])


def _extension(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp"
    return None


_REQUEST_HEADERS: Final = {"Accept": "image/png,image/jpeg,image/webp", "User-Agent": "ca-mode-logos"}


def _is_https(url: str) -> bool:
    return urllib.parse.urlsplit(url).scheme == "https"


async def download(url: str, client: httpx.AsyncClient) -> tuple[bytes, str] | None:
    """(content, extension) for an https image of at most MAX_LOGO_BYTES, else None."""
    return (await _download(url, client))[0]


async def _download(url: str, client: httpx.AsyncClient) -> tuple[tuple[bytes, str] | None, bool]:
    """``download`` plus whether the host answered 404 (no logo exists, as opposed to a failed fetch)."""
    not_found = False
    current = url
    try:
        for _ in range(MAX_REDIRECTS + 1):
            if not _is_https(current):
                return None, False
            # A fresh request per hop, never ``client.build_request``: the client's default headers and
            # cookies (an API token) must never reach a logo host, least of all after a redirect.
            # ``httpx.Request`` still sets Host from the URL, which a real connection requires.
            request = httpx.Request(
                "GET", current, headers=_REQUEST_HEADERS,
                extensions={"timeout": httpx.Timeout(LOGO_TIMEOUT_SECONDS).as_dict()},
            )
            response = await client.send(request, stream=True, follow_redirects=False, auth=None)
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return None, False
                    current = str(response.url.join(location))
                    continue
                if response.status_code != 200:
                    return None, response.status_code == 404
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_LOGO_BYTES:
                        return None, False
            finally:
                await response.aclose()
            extension = _extension(bytes(body))
            return ((bytes(body), extension) if extension else None), False
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        logger.info("logo_download_failed")
    return None, not_found


_KEY_CHUNK_DEFAULT: Final = 500
_KEY_CHUNK = _KEY_CHUNK_DEFAULT
_ASSET_TYPE_FOR_PREFIX: Final = {"mf": "MF", "in": "IND_STOCK", "us": "US_STOCK"}


def valid_key(key: str) -> bool:
    """True only for a string ``logo_key`` could have produced (the route's check)."""
    prefix, _, rest = key.partition(":")
    kind = _ASSET_TYPE_FOR_PREFIX.get(prefix)
    return bool(kind and rest) and logo_key(kind, rest) == key


_SIGNING_LABEL: Final = b"logo-url-v1"
_SIGNATURE_BYTES: Final = 16


def _signature(signing_key: bytes, user_id: str, key: str) -> str:
    digest = hmac.new(signing_key, f"{user_id}:{key}".encode(), hashlib.sha256).digest()[:_SIGNATURE_BYTES]
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def sign(user_id: str, key: str) -> str:
    """The ``s`` value of one profile's link to one logo (the cache is shared; links are not)."""
    return _signature(vault.derived_key(_SIGNING_LABEL), user_id, key)


def signature_ok(user_id: str, key: str, signature: str | None) -> bool:
    """True when ``signature`` is this profile's signature for ``key`` (constant-time compare)."""
    if not signature or not user_id:
        return False
    return hmac.compare_digest(sign(user_id, key).encode(), signature.encode("utf-8", "replace"))


def with_logos(holdings: list[dict], user_id: str) -> list[dict]:
    """Add ``logo`` (the app's own URL, signed for ``user_id``) to each holding whose logo is cached; same list back.

    The signature keeps another profile from asking whether a ticker or fund is cached, which would
    tell it that someone on this copy holds it. A failure here (a locked database, say) logs
    ``logo_lookup_failed`` and returns the holdings as they were.
    """
    try:
        keyed = [
            (holding, logo_key(holding.get("asset_type"), holding.get("code") or holding.get("investment_code")))
            for holding in holdings
        ]
        keys = sorted({key for _, key in keyed if key})
        if not keys:
            return holdings
        files: dict[str, str | None] = {}
        with db.connect_db() as conn:
            for start in range(0, len(keys), _KEY_CHUNK):
                chunk = keys[start:start + _KEY_CHUNK]
                rows = conn.execute(
                    f"SELECT key, file FROM logos WHERE status = 'found' AND key IN ({','.join('?' * len(chunk))})",
                    chunk,
                ).fetchall()
                files.update({row["key"]: row["file"] for row in rows})
        found = [(holding, key) for holding, key in keyed if key and _file_ok(files.get(key))]
        signing_key = vault.derived_key(_SIGNING_LABEL) if found else b""
    except Exception as exc:
        logger.warning("logo_lookup_failed", extra={"error": type(exc).__name__})
        return holdings
    for holding, key in found:
        holding["logo"] = (
            "/api/logos/" + urllib.parse.quote(key, safe=":") + "?s=" + _signature(signing_key, user_id, key)
        )
    return holdings


# ── Automatic fill ─────────────────────────────────────────────────────────────

# asyncio primitives belong to one event loop (the tests start several): one limiter and one lock per key per loop.
_loop_state: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, tuple[asyncio.Semaphore, dict[str, asyncio.Lock]]] = (
    weakref.WeakKeyDictionary()
)


def _state() -> tuple[asyncio.Semaphore, dict[str, asyncio.Lock]]:
    loop = asyncio.get_running_loop()
    current = _loop_state.get(loop)
    if current is None:
        current = (asyncio.Semaphore(LOGO_CONCURRENCY), {})
        _loop_state[loop] = current
    return current


def _key_lock(key: str) -> asyncio.Lock:
    return _state()[1].setdefault(key, asyncio.Lock())


_pending: set[asyncio.Task] = set()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _save(key: str, content: bytes, extension: str, now_ms: int) -> None:
    name = file_name(key, extension)
    target = logos_dir() / name
    temporary = target.with_name(name + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(target)
    record(key, "found", name, now_ms)


async def _fetch_and_record(key: str, url: str | None, client: httpx.AsyncClient, now_ms: int) -> bool:
    """Download one key's logo and record the outcome; True when saved. Never raises for a bad logo."""
    if not url:
        record(key, "missing", None, now_ms)
        return False
    async with _state()[0]:
        got, not_found = await _download(url, client)
    if got is None:
        record(key, "missing" if not_found else "failed", None, now_ms)
        return False
    _save(key, got[0], got[1], now_ms)
    return True


async def fill_fund_logos(
    fund_ids: list[str], call: Callable[[str, dict], Awaitable[str]], *, now_ms: int | None = None,
) -> int:
    """Fetch the fund-house logo of each due ``mf:`` key through INDmoney's fund details; the count saved."""
    now = _now_ms() if now_ms is None else now_ms
    candidates = sorted({fund_id.strip().upper() for fund_id in fund_ids if logo_key("MF", fund_id)})
    if not candidates:
        return 0
    async with contextlib.AsyncExitStack() as stack:
        # Locks in sorted order (no two fills can wait on each other); a key another fill finished meanwhile is skipped.
        for fund_id in candidates:
            await stack.enter_async_context(_key_lock(f"mf:{fund_id}"))
        ids = [fund_id for fund_id in candidates if is_due(f"mf:{fund_id}", now)]
        if not ids:
            return 0
        return await _fill_locked_funds(ids, call, now)


async def _fill_locked_funds(ids: list[str], call: Callable[[str, dict], Awaitable[str]], now: int) -> int:
    saved = 0
    async with httpx.AsyncClient() as client:
        for start in range(0, len(ids), FUND_BATCH):
            batch = ids[start:start + FUND_BATCH]
            try:
                reply = await call("get_mf_funds_details", {"fund_ids": ",".join(batch)})
                data = (json.loads(reply) if isinstance(reply, str) else reply).get("data") or []
                urls = {}
                for item in data:
                    detail = ((item.get("data") or {}).get("fund_detail") or {}) if isinstance(item, dict) else {}
                    urls[str(item.get("fund_id")).strip().upper()] = str(detail.get("image_url") or "").strip() or None
            except Exception as exc:
                logger.info("logo_fund_details_failed", extra={"error": type(exc).__name__})
                for fund_id in batch:
                    record(f"mf:{fund_id}", "failed", None, now)
                continue
            results = await asyncio.gather(
                *(_fetch_and_record(f"mf:{fund_id}", urls.get(fund_id), client, now) for fund_id in batch),
                return_exceptions=True,
            )
            saved += sum(1 for result in results if result is True)
    return saved


async def fill_stock_logos(keys: list[str], *, now_ms: int | None = None) -> int:
    """Fetch logo.dev logos for due ``in:`` / ``us:`` keys; needs LOGO_DEV_TOKEN. The count saved."""
    token = os.getenv("LOGO_DEV_TOKEN", "").strip()
    if not token:
        return 0
    now = _now_ms() if now_ms is None else now_ms
    wanted = sorted({key for key in keys if key.startswith(("in:", "us:"))})

    async def one(key: str, client: httpx.AsyncClient) -> bool:
        async with _key_lock(key):
            if not is_due(key, now):  # another fill finished it while this one waited
                return False
            market, symbol = key.split(":", 1)
            ticker = symbol + ".NS" if market == "in" else symbol
            url = LOGO_DEV_URL.format(symbol=urllib.parse.quote(ticker, safe=".")) + "?" + urllib.parse.urlencode(
                {"token": token}) + "&" + LOGO_DEV_OPTIONS
            return await _fetch_and_record(key, url, client, now)

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*(one(key, client) for key in wanted), return_exceptions=True)
    return sum(1 for result in results if result is True)


async def _fill(holdings: list[dict], fund_call: Callable[[str, dict], Awaitable[str]] | None) -> None:
    try:
        keys = {logo_key(h.get("asset_type"), h.get("code") or h.get("investment_code")) for h in holdings}
        keys.discard(None)
    except Exception as exc:
        logger.warning("logo_fill_failed", extra={"error": type(exc).__name__})
        return
    if fund_call is not None:
        try:
            await fill_fund_logos([key[3:] for key in keys if key.startswith("mf:")], fund_call)
        except Exception as exc:
            logger.warning("logo_fill_failed", extra={"error": type(exc).__name__})
    try:
        await fill_stock_logos([key for key in keys if not key.startswith("mf:")])
    except Exception as exc:
        logger.warning("logo_fill_failed", extra={"error": type(exc).__name__})


def schedule_fill(holdings: list[dict], *, fund_call: Callable[[str, dict], Awaitable[str]] | None = None) -> asyncio.Task | None:
    """Fill missing logos in the background (the task is returned for tests); never raises."""
    if not enabled():
        return None
    try:
        task = asyncio.get_running_loop().create_task(_fill(list(holdings), fund_call))
    except Exception as exc:
        logger.warning("logo_fill_failed", extra={"error": type(exc).__name__})
        return None
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task

"""Company and fund-house logos for holdings: keys, the shared cache table and a safe download.

A logo key comes from the asset type and the code only (an MF scheme code, an NSE symbol, a US
ticker), never from a holding's name. The ``logos`` table and the ``logos/`` folder under the
data directory are shared by every profile and hold no profile data: a key, the outcome of the
last fetch and a file name. Downloads are https only, capped at ``MAX_LOGO_BYTES`` and accepted
only when the bytes are a PNG, JPEG or WebP image (never SVG). ``LOGOS_ENABLED=false`` turns
logos off.
"""

from __future__ import annotations

import logging
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any, Final, Literal

import httpx

from . import db
from .portfolio_math import canonical_asset_type

logger = logging.getLogger(__name__)

MAX_LOGO_BYTES: Final = 200_000
LOGO_TIMEOUT_SECONDS: Final = 10
MISSING_RETRY_MS: Final = 30 * 24 * 60 * 60 * 1000
FAILED_RETRY_MS: Final = 24 * 60 * 60 * 1000
MAX_REDIRECTS: Final = 3

# Same pattern as brokers.ISIN_RE (brokers imports this module, so it is copied here).
_ISIN_RE: Final = re.compile(r"^IN[A-Z0-9]{9}[0-9]$")
_SYMBOL_RE: Final = re.compile(r"^[A-Z0-9&.-]{1,20}$")
_ALNUM_RE: Final = re.compile(r"[A-Z0-9]")

Status = Literal["found", "missing", "failed"]


def enabled() -> bool:
    return os.getenv("LOGOS_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


def logo_key(asset_type: str | None, code: str | None) -> str | None:
    """The cache key for a holding, from its asset type and code; None when it has no safe one."""
    kind = canonical_asset_type(asset_type)
    text = str(code or "").strip().upper()
    if not kind or not text:
        return None
    if kind == "MF":
        return f"mf:{text}" if text.isdigit() else None
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


async def download(url: str, client: httpx.AsyncClient) -> tuple[bytes, str] | None:
    """(content, extension) for an https image of at most MAX_LOGO_BYTES, else None."""
    current = url
    try:
        for _ in range(MAX_REDIRECTS + 1):
            if urllib.parse.urlsplit(current).scheme != "https":
                return None
            async with client.stream(
                "GET", current, follow_redirects=False, timeout=LOGO_TIMEOUT_SECONDS
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return None
                    current = str(response.url.join(location))
                    continue
                if response.status_code != 200:
                    return None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_LOGO_BYTES:
                        return None
            extension = _extension(bytes(body))
            return (bytes(body), extension) if extension else None
    except httpx.HTTPError:
        logger.info("logo_download_failed", extra={"host": urllib.parse.urlsplit(url).hostname})
    return None


def with_logos(holdings: list[dict]) -> list[dict]:
    """Add ``logo`` (the app's own URL) to each holding whose logo is cached; same list back."""
    keyed = [
        (holding, logo_key(holding.get("asset_type"), holding.get("code") or holding.get("investment_code")))
        for holding in holdings
    ]
    keys = sorted({key for _, key in keyed if key})
    if not keys:
        return holdings
    with db.connect_db() as conn:
        rows = conn.execute(
            f"SELECT key, file FROM logos WHERE status = 'found' AND key IN ({','.join('?' * len(keys))})",
            keys,
        ).fetchall()
    files = {row["key"]: row["file"] for row in rows}
    for holding, key in keyed:
        if key and _file_ok(files.get(key)):
            holding["logo"] = "/api/logos/" + urllib.parse.quote(key, safe=":")
    return holdings

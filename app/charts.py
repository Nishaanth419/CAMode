"""Charts in answers, built from tool data instead of numbers the model copied.

A year of daily candles is about 250 rows. The model copies them into a ``chart``
block unreliably: it elides rows ("first and last few shown") or invents them, and
the broken block draws nothing. So :func:`ground_candlestick_charts` replaces every
candlestick block, whatever candles it holds (or none), with the candles of the
latest ``get_indian_stocks_ohlc`` result, in the compact ``ohlc`` row form the
frontend reads. What the model wrote is never trusted.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

OHLC_TOOL_NAME: Final = "get_indian_stocks_ohlc"
# The OHLC tool returns at most 250 candles; the frontend draws at most 300.
MAX_CANDLES: Final = 300
CANDLES_UNAVAILABLE_NOTE: Final = (
    "_No candlestick chart: the market-data tool returned no complete OHLC candles for this answer._"
)
CHART_IN_HISTORY: Final = "[chart shown to the user]"

_CHART_BLOCK: Final = re.compile(r"```chart[ \t]*\n(.*?)```", re.IGNORECASE | re.DOTALL)
_DATE_KEYS: Final = ("datetime_ist", "datetime", "date", "timestamp", "time", "label")
_NAME_KEYS: Final = ("display_name", "name", "instrument_name", "trading_symbol", "symbol")
_PRICE_KEYS: Final = ("open", "high", "low", "close")


def _loads(text: Any) -> Any:
    if not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", ""))
        except ValueError:
            return None
    return None


def _label(value: Any) -> str | None:
    """``2025-10-15`` for a daily candle, ``2025-10-15 09:15`` for an intraday one."""
    if not isinstance(value, str) or not re.match(r"\d{4}-\d{2}-\d{2}", value):
        return None
    day, rest = value[:10], value[11:16]
    return day if not rest or rest == "00:00" else f"{day} {rest}"


def _is_candle_list(value: Any) -> bool:
    return (isinstance(value, list) and bool(value)
            and all(isinstance(row, dict) and all(key in row for key in _PRICE_KEYS) for row in value[:5]))


def _find_candles(value: Any, depth: int = 0) -> tuple[list[dict[str, Any]], dict[str, Any] | None] | None:
    """The first list of OHLC rows inside a parsed result, with the object that holds it."""
    if depth > 6:
        return None
    if isinstance(value, dict):
        for item in value.values():
            if _is_candle_list(item):
                return item, value
        for item in value.values():
            found = _find_candles(_loads(item) if isinstance(item, str) else item, depth + 1)
            if found:
                return found
    elif _is_candle_list(value):
        return value, None
    elif isinstance(value, list):
        for item in value:
            found = _find_candles(item, depth + 1)
            if found:
                return found
    return None


def ohlc_candles(result: str) -> tuple[list[list[Any]], str | None]:
    """``[[label, open, high, low, close], ...]`` oldest first, and the instrument name, from an OHLC result.

    Rows without a date or with impossible prices (high below the open or close, low
    above them) are dropped rather than repaired.
    """
    parsed = _loads(result)
    found = _find_candles(parsed)
    if not found:
        return [], None
    rows, holder = found
    candles: dict[str, list[Any]] = {}
    for row in rows:
        label = next((_label(row.get(key)) for key in _DATE_KEYS if _label(row.get(key))), None)
        prices = [_number(row.get(key)) for key in _PRICE_KEYS]
        if label is None or any(price is None for price in prices):
            continue
        open_, high, low, close = prices
        if high < max(open_, close, low) or low > min(open_, close, high):
            continue
        candles[label] = [label, open_, high, low, close]  # a repeated date keeps its last row
    ordered = [candles[label] for label in sorted(candles)][-MAX_CANDLES:]
    sources = [holder, parsed] if isinstance(parsed, dict) else [holder]
    name = next((str(source[key]).strip()[:80] for source in sources if isinstance(source, dict)
                 for key in _NAME_KEYS if isinstance(source.get(key), str) and source[key].strip()), None)
    return ordered, name


def _is_candlestick_block(body: str, spec: Any) -> bool:
    if isinstance(spec, dict):
        return spec.get("type") == "candlestick"
    # Unparseable (often elided with "..."): treat it as a candlestick chart if it says so.
    return "candlestick" in body.lower() or '"candles"' in body


def ground_candlestick_charts(content: str, ohlc_results: list[str]) -> str:
    """Replace every candlestick ``chart`` block with candles from the latest OHLC result that has any."""
    candles: list[list[Any]] = []
    name: str | None = None
    for result in reversed(ohlc_results):
        candles, name = ohlc_candles(result)
        if candles:
            break

    def replace(match: re.Match[str]) -> str:
        body = match.group(1)
        spec = _loads(body.strip())
        if not _is_candlestick_block(body, spec):
            return match.group(0)
        if not candles:
            return CANDLES_UNAVAILABLE_NOTE
        model_title = spec.get("title") if isinstance(spec, dict) and isinstance(spec.get("title"), str) else None
        # The tool's instrument name, when it gives one, so a chart of the wrong instrument says so.
        title = f"{name} · {candles[0][0]} to {candles[-1][0]}" if name else (model_title or "Price candles")
        chart = {"type": "candlestick", "title": title[:120], "unit": "₹", "ohlc": candles}
        return "```chart\n" + json.dumps(chart, ensure_ascii=False, separators=(",", ":")) + "\n```"

    return _CHART_BLOCK.sub(replace, content)


def strip_chart_blocks(content: str) -> str:
    """Earlier answers as the model sees them in history: chart data is for the user, not the model."""
    return _CHART_BLOCK.sub(CHART_IN_HISTORY, content) if "```chart" in content.lower() else content

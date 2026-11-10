"""Logging set-up: structured fields on every line, and what the access log may record.

Log calls pass their fields with ``extra=`` (``logger.info("admin_plan_changed", extra={"admin": email,
"user_id": ...})``). The standard formatter drops them, which left the admin audit trail empty, so
``configure()`` installs ``KeyValueFormatter``: the usual ``LEVEL:logger:message`` followed by each field
as ``key=value`` (values with spaces or quotes are JSON-quoted, newlines never split a line).

uvicorn's access log writes each request line with its query string. OAuth callbacks
(``/auth/indmoney/callback``, ``/auth/google/callback``, the brokers' callbacks) carry a one-time
``code`` and ``state`` there, so query strings are dropped from every line, and the healthcheck
and readiness probes, which run every 30 seconds, are not logged at all.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Final

# Probes that would otherwise fill the log (the Docker healthcheck calls /healthz every 30 seconds).
QUIET_PATHS: Final = frozenset({"/healthz", "/readyz"})


# Attributes every LogRecord has; anything else on a record came from ``extra=``.
_STANDARD_ATTRS: Final = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "taskName"}


def _value(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))
    if not text or any(char.isspace() or char in "\"=" for char in text):
        return json.dumps(text, ensure_ascii=False)
    return text


class KeyValueFormatter(logging.Formatter):
    """``LEVEL:logger:message key=value ...``: the fields passed with ``extra=`` are written, not dropped."""

    def __init__(self) -> None:
        super().__init__("%(levelname)s:%(name)s:%(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        fields = [f"{key}={_value(value)}" for key, value in vars(record).items()
                  if key not in _STANDARD_ATTRS and not key.startswith("_")]
        if fields:
            head, newline, rest = line.partition("\n")  # a traceback stays after the fields
            line = f"{head} {' '.join(fields)}{newline}{rest}"
        return line


def configure() -> None:
    """Log at ``LOG_LEVEL`` to stderr with structured fields, and filter uvicorn's access log."""
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    for handler in logging.getLogger().handlers:
        # The stderr handler basicConfig made (or an earlier one); test and library handlers keep their own format.
        if type(handler) is logging.StreamHandler and not isinstance(handler.formatter, KeyValueFormatter):
            handler.setFormatter(KeyValueFormatter())
    install_access_log_filter()


class AccessLogFilter(logging.Filter):
    """Drop probe requests and strip query strings from uvicorn's access log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn.access: (client_addr, method, full_path, http_version, status_code)
        if not isinstance(args, tuple) or len(args) < 3 or not isinstance(args[2], str):
            return True
        path, _, _query = args[2].partition("?")
        if path in QUIET_PATHS:
            return False
        record.args = (*args[:2], path, *args[3:])
        return True


def install_access_log_filter() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(existing, AccessLogFilter) for existing in access.filters):
        access.addFilter(AccessLogFilter())

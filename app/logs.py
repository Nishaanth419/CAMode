"""Logging set-up: what the access log may record.

uvicorn's access log writes each request line with its query string. OAuth callbacks
(``/auth/indmoney/callback``, ``/auth/google/callback``, the brokers' callbacks) carry a one-time
``code`` and ``state`` there, so query strings are dropped from every line, and the healthcheck
and readiness probes, which run every 30 seconds, are not logged at all.
"""

from __future__ import annotations

import logging
from typing import Final

# Probes that would otherwise fill the log (the Docker healthcheck calls /healthz every 30 seconds).
QUIET_PATHS: Final = frozenset({"/healthz", "/readyz"})


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

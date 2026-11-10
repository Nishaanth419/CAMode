from __future__ import annotations

import logging

import pytest

from app import db, logs


def access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("203.0.113.9:5000", "GET", path, "1.1", 302), None,
    )


def test_access_log_drops_oauth_codes_and_states():
    record = access_record("/auth/google/callback?code=4%2F0secret&state=abc123")

    assert logs.AccessLogFilter().filter(record) is True
    assert record.getMessage() == '203.0.113.9:5000 - "GET /auth/google/callback HTTP/1.1" 302'


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/healthz?x=1"])
def test_access_log_skips_probes(path):
    assert logs.AccessLogFilter().filter(access_record(path)) is False


def test_access_log_filter_is_installed_once():
    logs.install_access_log_filter()
    logs.install_access_log_filter()

    filters = [item for item in logging.getLogger("uvicorn.access").filters if isinstance(item, logs.AccessLogFilter)]
    assert len(filters) == 1


def test_a_second_process_cannot_serve_the_same_data(tmp_path, monkeypatch):
    import subprocess
    import sys

    # This process holds the lock (the test app has started); a second Python process must be refused.
    monkeypatch.setattr(db, "_process_lock", None)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    db.claim_single_process()
    db.claim_single_process()  # once per process: a second call in the same process is fine

    code = (
        "import sys; from pathlib import Path; from app import db; db.DATA_DIR = Path(sys.argv[1]);\n"
        "try:\n    db.claim_single_process()\nexcept db.SecondProcessError as exc:\n    print('refused', exc); sys.exit(3)\n"
    )
    other = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, timeout=60)

    assert other.returncode == 3, other.stderr
    assert "--workers 1" in other.stdout
    db._process_lock.close()

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("APP_DATA_DIR", tempfile.mkdtemp(prefix="camode-tests-"))

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "DATABASE_PATH", tmp_path / "codemode.sqlite3")
    main.initialize_db()


@pytest.fixture
def client():
    with TestClient(main.app, follow_redirects=False) as test_client:
        yield test_client

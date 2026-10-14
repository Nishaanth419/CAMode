from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("APP_DATA_DIR", tempfile.mkdtemp(prefix="camode-tests-"))
os.environ["SNAPSHOT_SCHEDULER_ENABLED"] = "false"

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import pytest
from fastapi.testclient import TestClient

from app import db, main, privacy, users


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "codemode.sqlite3")
    # Production scrypt cost (~128 MiB per hash) would make every sign-in test slow.
    monkeypatch.setattr(users, "SCRYPT_N", 2**10)
    db.initialize_db()


@pytest.fixture
def client():
    with TestClient(main.app, follow_redirects=False) as test_client:
        yield test_client


def sign_up(
    test_client: TestClient, name: str = "Asha", pin: str = "2468", *, storage_mode: str | None = "device",
) -> dict:
    """Create a profile through the API and leave the client signed in as it.

    Unless ``storage_mode`` is None, the profile also accepts the privacy notice with that mode.
    """
    test_client.get("/api/session")
    response = test_client.post("/api/users", json={"displayName": name, "pin": pin})
    assert response.status_code == 201, response.text
    if storage_mode is not None:
        accepted = test_client.post("/api/privacy/consent", json={
            "policyVersion": privacy.POLICY_VERSION, "storageMode": storage_mode,
        })
        assert accepted.status_code == 200, accepted.text
    return response.json()["user"]


@pytest.fixture
def signed_in(client):
    return sign_up(client)

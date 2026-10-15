from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import agent, investigations, main, privacy, snapshots, unlock, users
from app.db import connect_db
from tests.conftest import sign_up
from tests.fixtures.snapshot_pairs import baseline_rows, build_results, later_rows, mutate_summary
from tests.test_agent import FakeAsyncClient, _completion


def _signed_in_user(client):
    sid = client.cookies.get(main.SESSION_COOKIE)
    user = users.get_user(users.session_user_id(sid))
    user["keys"] = unlock.keys_for_session(user, sid)
    return user


def _store_pair(user_id, *, latest_results=None):
    now = datetime.now(timezone.utc)
    snapshots.store_snapshot(user_id, now - timedelta(days=1), build_results(baseline_rows()), {})
    snapshots.store_snapshot(user_id, now, latest_results or build_results(later_rows()), {})


def _run(client, **body):
    return client.post("/api/investigations", json=body)


# ── API and persistence ────────────────────────────────────────────────────────

def test_investigation_compares_snapshots_and_is_saved(client, signed_in):
    _store_pair(signed_in["id"])

    response = _run(client)

    assert response.status_code == 201
    record = response.json()
    assert response.headers["Location"] == f"/api/investigations/{record['id']}"
    assert record["status"] == "completed"
    assert record["saved"] is True
    assert record["request"] == {"baselineDate": None, "trigger": "api"}
    result = record["result"]
    net_worth = next(item for item in result["findings"] if item["id"] == "net_worth")
    assert (net_worth["previousInr"], net_worth["currentInr"], net_worth["changeInr"], net_worth["changePct"]) == (
        810000.0, 889000.0, 79000.0, 9.75)
    assert net_worth["verified"] is True
    assert len(net_worth["sourceSnapshotIds"]) == 2
    assert result["comparison"]["previous"]["currency"] == "INR"
    assert next(item for item in result["findings"] if item["id"] == "market_effect")["kind"] == "estimate"
    assert result["changes"]["topRising"][0]["name"] == "Parag Parikh Flexi Cap Fund"
    assert result["changes"]["topFalling"][0]["name"] == "Tata Consultancy Services"
    # Shares of the change are computed here so the model never has to (54,000 / 79,000; -10,000 / 79,000).
    assert result["changes"]["topRising"][0]["shareOfChangePct"] == 68.35
    assert result["changes"]["topFalling"][0]["shareOfChangePct"] == -12.66
    assert result["changes"]["shareOfChangeDenominator"] == "change in total current value (₹79,000.00)"
    assert "of the earlier invested amount" in next(item for item in result["findings"] if item["id"] == "invested")["claim"]
    assert any("HDFC Bank" in note for note in result["limitations"])  # names the holding without a cost basis
    assert {check["status"] for check in result["checks"]} == {"passed"}
    assert result["warnings"] == []
    assert any("not why" in note for note in result["limitations"])
    assert [step["name"] for step in record["steps"]] == [
        "get_latest_snapshot", "get_previous_snapshot", "normalize_snapshots", "calculate_portfolio_change",
        "calculate_allocation", "validate_snapshot_consistency"]
    assert all(step["status"] == "completed" and "durationMs" in step for step in record["steps"])

    saved = client.get(response.headers["Location"])
    assert saved.status_code == 200
    assert saved.json()["result"] == result
    listed = client.get("/api/investigations").json()["investigations"]
    assert [(item["id"], item["status"]) for item in listed] == [(record["id"], "completed")]


def test_saved_investigation_is_sealed_at_rest(client, signed_in):
    _store_pair(signed_in["id"])
    record = _run(client).json()

    with connect_db() as connection:
        row = connection.execute("SELECT status, data FROM investigations WHERE id = ?", (record["id"],)).fetchone()

    assert row["status"] == "completed"
    assert row["data"].startswith("sealed:v1:")
    assert "889000" not in row["data"]


def test_conflicting_totals_mark_findings_unverified(client, signed_in):
    _store_pair(signed_in["id"], latest_results=mutate_summary(build_results(later_rows()), total_networth=950000))

    result = _run(client).json()["result"]

    assert next(item for item in result["findings"] if item["id"] == "net_worth")["verified"] is False
    assert any("disagree" in warning for warning in result["warnings"])
    assert "unverified" in result["summary"]


def test_no_snapshot_is_insufficient_data_not_no_change(client, signed_in):
    record = _run(client).json()

    assert record["status"] == "insufficient_data"
    assert record["result"]["findings"] == []
    assert record["result"]["checks"][0]["check"] == "history_available"
    assert record["result"]["checks"][0]["status"] == "failed"
    assert "not the same as no change" in record["result"]["limitations"][0]


def test_single_snapshot_is_insufficient_data_with_latest_reference(client, signed_in):
    snapshots.store_snapshot(signed_in["id"], datetime.now(timezone.utc), build_results(later_rows()), {})

    record = _run(client).json()

    assert record["status"] == "insufficient_data"
    assert record["result"]["comparison"]["current"]["asOfDate"]
    assert [step["name"] for step in record["steps"]] == ["get_latest_snapshot", "get_previous_snapshot"]


def test_baseline_date_selects_snapshot_on_or_before_it(client, signed_in):
    now = datetime.now(timezone.utc)
    for days_ago in (20, 10, 0):
        snapshots.store_snapshot(signed_in["id"], now - timedelta(days=days_ago), build_results(baseline_rows()), {})
    baseline = (now - timedelta(days=12)).astimezone(snapshots.SNAPSHOT_TIMEZONE).date().isoformat()
    expected = (now - timedelta(days=20)).astimezone(snapshots.SNAPSHOT_TIMEZONE).date().isoformat()

    record = _run(client, baselineDate=baseline).json()

    assert record["result"]["comparison"]["previous"]["asOfDate"] == expected
    assert next(item for item in record["result"]["findings"] if item["id"] == "net_worth")["direction"] == "unchanged"


@pytest.mark.parametrize("body", [{"baselineDate": "13/10/2026"}, {"baselineDate": 20261013}, {"since": "2026-10-01"}])
def test_invalid_request_body_is_rejected(client, signed_in, body):
    assert _run(client, **body).status_code == 422


def test_delete_removes_saved_investigation(client, signed_in):
    _store_pair(signed_in["id"])
    investigation_id = _run(client).json()["id"]

    assert client.delete(f"/api/investigations/{investigation_id}").json() == {"deleted": True}
    assert client.get(f"/api/investigations/{investigation_id}").status_code == 404
    assert client.delete(f"/api/investigations/{investigation_id}").status_code == 404


def test_only_newest_investigations_are_kept(client, signed_in, monkeypatch):
    monkeypatch.setattr(investigations, "KEEP_INVESTIGATIONS", 2)
    ids = [_run(client).json()["id"] for _ in range(3)]

    with connect_db() as connection:
        kept = {row["id"] for row in connection.execute("SELECT id FROM investigations").fetchall()}

    assert ids[0] not in kept
    assert len(kept) == 2


# ── Gates and isolation ────────────────────────────────────────────────────────

def test_investigations_require_a_profile(client):
    client.get("/api/session")

    assert _run(client).status_code == 401
    assert client.get("/api/investigations").status_code == 401


def test_investigations_require_unlocked_history(client, signed_in):
    unlock._unlocked.clear()

    response = _run(client)

    assert response.status_code == 403
    assert response.json()["unlockRequired"] is True


def test_investigations_require_current_consent(client):
    sign_up(client, storage_mode=None)

    response = _run(client)

    assert response.status_code == 403
    assert response.json()["consentRequired"] is True


def test_other_profiles_cannot_read_an_investigation(client, signed_in):
    _store_pair(signed_in["id"])
    investigation_id = _run(client).json()["id"]

    sign_up(client, name="Ravi", pin="1357")

    assert client.get(f"/api/investigations/{investigation_id}").status_code == 404
    assert client.delete(f"/api/investigations/{investigation_id}").status_code == 404
    assert client.get("/api/investigations").json() == {"investigations": []}


def test_private_mode_runs_are_not_saved(client):
    sign_up(client, storage_mode="private")

    response = _run(client)

    assert response.status_code == 200  # nothing was created
    assert "Location" not in response.headers
    assert response.json()["saved"] is False
    assert response.json()["status"] == "insufficient_data"
    with connect_db() as connection:
        assert connection.execute("SELECT COUNT(*) FROM investigations").fetchone()[0] == 0


# ── Privacy lifecycle ──────────────────────────────────────────────────────────

def test_export_includes_investigations(client, signed_in):
    _store_pair(signed_in["id"])
    investigation_id = _run(client).json()["id"]

    exported = client.post("/api/users/me/export", json={"pin": "2468"}).json()

    assert [item["id"] for item in exported["investigations"]] == [investigation_id]


@pytest.mark.parametrize("remove", ["clear_history", "private_mode", "disconnect", "delete_profile"])
def test_investigations_are_deleted_with_history(client, signed_in, remove):
    _store_pair(signed_in["id"])
    _run(client)

    if remove == "clear_history":
        client.delete("/api/users/me/history")
    elif remove == "private_mode":
        client.post("/api/privacy/consent", json={"policyVersion": privacy.POLICY_VERSION, "storageMode": "private"})
    elif remove == "disconnect":
        users.clear_indmoney(signed_in["id"])
    else:
        users.delete_user(signed_in["id"])

    with connect_db() as connection:
        assert connection.execute("SELECT COUNT(*) FROM investigations").fetchone()[0] == 0


# ── Bounded execution and failures ─────────────────────────────────────────────

def test_deadline_ends_run_as_failed_with_timed_out_step(client, signed_in, monkeypatch):
    monkeypatch.setattr(investigations, "DEADLINE_SECONDS", 0.05)

    def slow_latest(*args, **kwargs):
        time.sleep(0.3)

    monkeypatch.setattr(snapshots, "latest_snapshot", slow_latest)

    record = _run(client).json()

    assert record["status"] == "failed"
    assert record["error"].startswith("Timed out.")
    assert record["steps"][0] == {**record["steps"][0], "status": "failed", "error": "Timed out."}
    assert client.get(f"/api/investigations/{record['id']}").json()["status"] == "failed"


def test_transient_database_lock_is_retried_once(client, signed_in, monkeypatch):
    _store_pair(signed_in["id"])
    real, calls = snapshots.latest_snapshot, []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    monkeypatch.setattr(snapshots, "latest_snapshot", flaky)

    assert _run(client).json()["status"] == "completed"
    assert len(calls) == 2


def test_other_database_errors_are_not_retried(client, signed_in, monkeypatch):
    calls = []

    def broken(*args, **kwargs):
        calls.append(1)
        raise sqlite3.OperationalError("no such table: portfolio_snapshots")

    monkeypatch.setattr(snapshots, "latest_snapshot", broken)

    assert _run(client).json()["status"] == "failed"
    assert len(calls) == 1


def test_unexpected_errors_are_reported_without_internal_details(client, signed_in, monkeypatch):
    _store_pair(signed_in["id"])

    def leaky(*args, **kwargs):
        raise RuntimeError("cannot open /Users/someone/.data/codemode.sqlite3 with token abc123")

    monkeypatch.setattr(snapshots, "previous_snapshot", leaky)

    record = _run(client).json()

    assert record["status"] == "failed"
    assert "abc123" not in json.dumps(record)
    assert "/Users/" not in json.dumps(record)
    assert record["steps"][-1]["error"] == "Unexpected error while reading saved data."


def test_storage_failure_before_start_returns_503(client, signed_in, monkeypatch):
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(investigations, "_save", fail)

    response = _run(client)

    assert response.status_code == 503
    assert "disk" not in response.json()["error"]


def test_storage_failure_after_run_keeps_result_but_marks_unsaved(client, signed_in, monkeypatch):
    _store_pair(signed_in["id"])
    real = investigations._save

    def fail_updates(user_id, record, keys, *, insert):
        if not insert:
            raise sqlite3.OperationalError("disk I/O error")
        real(user_id, record, keys, insert=insert)

    monkeypatch.setattr(investigations, "_save", fail_updates)

    response = _run(client)

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["saved"] is False
    assert "Location" not in response.headers


def test_interrupted_run_reads_as_failed(client, signed_in):
    user = _signed_in_user(client)
    record = {"id": "stuck", "kind": investigations.KIND, "status": "running", "createdAt": users.now_ms(),
              "request": {}, "metadata": {}, "steps": [], "saved": True}
    investigations._save(user["id"], record, user["keys"], insert=True)
    with connect_db() as connection:
        connection.execute("UPDATE investigations SET updated_at = 0 WHERE id = 'stuck'")

    shown = client.get("/api/investigations/stuck").json()

    assert shown["status"] == "failed"
    assert "interrupted" in shown["error"]


@pytest.mark.asyncio
async def test_cancelled_run_is_recorded_and_cancellation_propagates(client, signed_in, monkeypatch):
    user = _signed_in_user(client)

    def slow_latest(*args, **kwargs):
        time.sleep(0.3)

    monkeypatch.setattr(snapshots, "latest_snapshot", slow_latest)
    task = asyncio.create_task(investigations.investigate_portfolio_change(user))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    saved = investigations.list_investigations(user["id"], user["keys"])
    assert saved[0]["status"] == "cancelled"


# ── Agent tool ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(("arguments", "message"), [
    ({"baseline_date": "last week"}, "YYYY-MM-DD"),
    ({"baseline_date": 5}, "string"),
    ({"since": "2026-10-01"}, "Unknown argument"),
])
async def test_tool_rejects_invalid_arguments_without_running(client, signed_in, arguments, message):
    tool = investigations.investigation_tool(_signed_in_user(client))

    result = json.loads(await tool.handler(arguments))

    assert message in result["error"]
    with connect_db() as connection:
        assert connection.execute("SELECT COUNT(*) FROM investigations").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_portfolio_agent_runs_investigation_and_links_it_in_trace(client, signed_in, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    _store_pair(signed_in["id"])
    user = _signed_in_user(client)
    captured = []
    tool_call = {"id": "call-1", "function": {"name": "investigate_portfolio_change", "arguments": "{}"}}
    answer_text = "Net worth rose by ₹79,000.00 (+9.75%), led by Parag Parikh Flexi Cap Fund."
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient(
        [_completion(tool_calls=[tool_call]), _completion(answer_text)], captured))
    context = agent.PortfolioContext(
        local_tools=(investigations.investigation_tool(user, provider="openai"),),
        cached_mcp_tools=[{"name": "networth_snapshot", "description": "live"}],
    )

    answer = await agent.run_agent("Why did my portfolio change?", "https://mcp", "token", [], "portfolio", "openai",
                                   context=context)

    assert str(answer) == answer_text
    step = next(item for item in answer.trace if item["kind"] == "tool")
    assert step["source"] == "local"
    assert step["status"] == "completed"
    record = investigations.get_investigation(user["id"], step["investigation_id"], user["keys"])
    assert record["request"]["trigger"] == "agent"
    assert record["metadata"]["provider"] == "openai"
    tool_message = json.loads(captured[1]["payload"]["messages"][-1]["content"])
    assert tool_message["investigationId"] == record["id"]
    assert tool_message["findings"][0]["changeInr"] == 79000.0
    assert "raw" not in tool_message
    assert len(captured[1]["payload"]["messages"][-1]["content"]) < 6000  # fits Groq's tool-result limit


@pytest.mark.asyncio
async def test_repeated_tool_calls_in_one_request_reuse_the_first_run(client, signed_in):
    _store_pair(signed_in["id"])
    tool = investigations.investigation_tool(_signed_in_user(client))

    first = await tool.handler({})
    second = await tool.handler({})

    assert first == second
    with connect_db() as connection:
        assert connection.execute("SELECT COUNT(*) FROM investigations").fetchone()[0] == 1


def test_agent_payload_stays_under_groq_limit_with_long_names():
    long_claim = "A holding with a very long descriptive fund name, direct plan, growth option " * 3
    record = {"id": "x", "status": "completed", "saved": True, "result": {
        "summary": "s", "comparison": {}, "warnings": ["w" * 300] * 5, "limitations": ["l" * 300] * 5,
        "findings": [{"id": f"f{i}", "claim": long_claim, "kind": "observed", "verified": True} for i in range(11)],
        "changes": {"topRising": [{"name": long_claim, "changeInr": 1.0}] * 5,
                    "topFalling": [{"name": long_claim, "changeInr": -1.0}] * 5}}}

    payload = investigations.agent_payload(record)

    assert len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) <= investigations.MAX_AGENT_PAYLOAD_CHARS
    assert payload["shortened"]
    assert payload["findings"] and payload["warnings"]


def test_agent_payload_omits_id_of_unsaved_run():
    payload = investigations.agent_payload({"id": "x", "status": "insufficient_data", "saved": False, "result": {}})

    assert "investigationId" not in payload

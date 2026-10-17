from __future__ import annotations

import pytest

from app import agent, agent_runs, privacy, unlock, users
from app.db import connect_db
from tests.conftest import sign_up
from tests.test_agent import FakeAsyncClient, _completion


@pytest.fixture
def provider(monkeypatch):
    """A scripted OpenAI-compatible provider: each chat request takes the next reply."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    replies: list = []
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient(replies, []))
    return replies


def ask(client, message="What is a budget?", replies=None, **body):
    chat = client.post("/api/chats", json={"agentType": "finance"}).json()
    return chat["id"], client.post("/api/chat", json={
        "message": message, "chatId": chat["id"], "agentType": "finance", "aiProvider": "openai", **body,
    })


def run_count():
    with connect_db() as connection:
        return connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]


def test_an_answer_saves_its_run_sealed_and_links_it_from_the_chat(client, signed_in, provider):
    provider.append(_completion("A budget tracks income and spending."))

    chat_id, response = ask(client)

    assert response.status_code == 200
    run_id = response.headers["X-Agent-Run"]
    message = client.get(f"/api/chats/{chat_id}").json()["messages"][-1]
    assert message["runId"] == run_id
    listed = client.get("/api/runs").json()["runs"]
    assert [item["id"] for item in listed] == [run_id]
    assert listed[0] == {**listed[0], "chatId": chat_id, "question": "What is a budget?", "status": "answered",
                         "task": "general_finance", "verdict": "met", "agentType": "finance"}
    assert listed[0]["summary"]["modelCalls"] == 1
    record = client.get(f"/api/runs/{run_id}").json()
    assert [event["kind"] for event in record["events"]][-1] == "termination"
    # Only the status and times are plain; the question and events are sealed with the profile key.
    with connect_db() as connection:
        raw = connection.execute("SELECT data FROM agent_runs").fetchone()["data"]
    assert raw.startswith("sealed:v1:") and "budget" not in raw


def test_a_failed_answer_saves_its_run_with_the_error(client, signed_in, provider):
    provider.append(_completion(""))

    _, response = ask(client, "Explain saving")

    assert response.status_code == 502
    run_id = response.json()["runId"]
    assert response.headers["X-Agent-Run"] == run_id
    record = client.get(f"/api/runs/{run_id}").json()
    assert record["status"] == "failed" and "empty response" in record["error"]
    assert record["events"][-1] == {**record["events"][-1], "kind": "termination", "code": "empty_response"}


def test_private_mode_never_saves_a_run(client, provider):
    sign_up(client, storage_mode="private")
    provider.append(_completion("A budget tracks income and spending."))

    _, response = ask(client)

    assert response.status_code == 200 and "X-Agent-Run" not in response.headers
    assert run_count() == 0
    assert client.get("/api/runs").json()["runs"] == []


def test_runs_need_an_unlocked_profile_and_stay_with_their_profile(client, signed_in, provider):
    provider.append(_completion("A budget tracks income and spending."))
    run_id = ask(client)[1].headers["X-Agent-Run"]

    unlock._unlocked.clear()
    assert client.get("/api/runs").json()["unlockRequired"] is True
    assert client.get(f"/api/runs/{run_id}").status_code == 403

    client.post("/api/session/logout")
    sign_up(client, "Ravi", "1357")
    assert client.get(f"/api/runs/{run_id}").status_code == 404
    assert client.delete(f"/api/runs/{run_id}").status_code == 404
    assert client.get("/api/runs").json()["runs"] == []


def test_a_run_can_be_deleted_and_goes_with_its_chat(client, signed_in, provider):
    provider.extend([_completion("One."), _completion("Two.")])
    first_chat, first = ask(client)
    _, second = ask(client)

    assert client.delete(f"/api/runs/{second.headers['X-Agent-Run']}").json() == {"deleted": True}
    client.delete(f"/api/chats/{first_chat}")

    assert run_count() == 0


@pytest.mark.parametrize("remove", ["clear_history", "private_mode", "disconnect", "delete_profile"])
def test_runs_are_deleted_with_history(client, signed_in, provider, remove):
    provider.append(_completion("A budget tracks income and spending."))
    ask(client)
    assert run_count() == 1

    if remove == "clear_history":
        client.delete("/api/users/me/history")
    elif remove == "private_mode":
        client.post("/api/privacy/consent", json={"policyVersion": privacy.POLICY_VERSION, "storageMode": "private"})
    elif remove == "disconnect":
        users.clear_indmoney(signed_in["id"])
    else:
        users.delete_user(signed_in["id"])

    assert run_count() == 0


def test_export_includes_runs(client, signed_in, provider):
    provider.append(_completion("A budget tracks income and spending."))
    run_id = ask(client)[1].headers["X-Agent-Run"]

    exported = client.post("/api/users/me/export", json={"pin": "2468"}).json()

    assert [item["id"] for item in exported["agentRuns"]] == [run_id]
    assert exported["agentRuns"][0]["events"]


def test_only_the_newest_runs_are_kept_and_none_older_than_a_chat(client, signed_in, provider, monkeypatch):
    monkeypatch.setattr(agent_runs, "KEEP_RUNS", 2)
    provider.extend([_completion("One."), _completion("Two."), _completion("Three.")])
    for _ in range(3):
        ask(client)
    assert run_count() == 2

    with connect_db() as connection:
        connection.execute("UPDATE agent_runs SET created_at = created_at - ?", (agent_runs.MAX_AGE_MS + 1,))
    assert client.get("/api/runs").json()["runs"] == []
    provider.append(_completion("Four."))
    ask(client)
    assert run_count() == 1


def test_the_question_and_error_are_redacted_and_capped(client, signed_in, provider):
    provider.append(_completion("Noted."))
    question = "My PAN is ABCDE1234F. " + "x" * 400

    run_id = ask(client, question)[1].headers["X-Agent-Run"]

    record = client.get(f"/api/runs/{run_id}").json()
    assert "ABCDE1234F" not in record["question"] and len(record["question"]) <= agent_runs.MAX_QUESTION_CHARS + 20


def test_a_database_error_never_fails_the_answer(client, signed_in, provider, monkeypatch):
    import sqlite3

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(agent_runs, "_prune", broken)
    provider.append(_completion("A budget tracks income and spending."))

    _, response = ask(client)

    assert response.status_code == 200 and "X-Agent-Run" not in response.headers

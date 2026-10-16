"""Goals, decisions and the saved-context API: persistence, validation, access rules and the old-goal move."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from conftest import sign_up
from fastapi.testclient import TestClient

from app import db, main, memory, privacy, snapshots, unlock, users

GOAL = {
    "name": "Home down payment", "kind": "home", "currency": "INR", "targetAmount": 2_500_000,
    "targetDate": "2032-03-31", "savedAmount": 400_000, "monthlyContribution": 30_000, "assumedReturnPct": 9,
    "notes": "Flat near work",
}

PLAN_INPUTS = {key: GOAL[key] for key in ("currency", "targetAmount", "targetDate", "savedAmount", "monthlyContribution")}


def raw(sql: str, *params) -> list[tuple]:
    with db.connect_db() as connection:
        return [tuple(row) for row in connection.execute(sql, params).fetchall()]


def create_goal(client, **changes) -> dict:
    response = client.post("/api/goals", json={**GOAL, **changes})
    assert response.status_code == 201, response.text
    return response.json()


# ── Goals: CRUD and persistence ───────────────────────────────────────────────

def test_goal_lifecycle_is_sealed_and_returns_a_plan(client, signed_in):
    assert client.get("/api/goals").json()["goals"] == []

    created = client.post("/api/goals", json=GOAL)

    assert created.status_code == 201
    goal = created.json()
    assert created.headers["Location"] == f"/api/goals/{goal['id']}"
    assert {key: goal[key] for key in GOAL} == GOAL
    assert goal["source"] == "user_entered" and goal["createdAt"] == goal["updatedAt"]
    assert goal["plan"]["currency"] == "INR" and goal["plan"]["scenarios"]
    (stored,) = raw("SELECT data FROM financial_goals WHERE user_id = ?", signed_in["id"])
    assert stored[0].startswith("sealed:v1:") and "Home down payment" not in stored[0]

    edited = client.put(f"/api/goals/{goal['id']}", json={**GOAL, "monthlyContribution": 45_000, "notes": "  "})
    assert edited.status_code == 200
    assert edited.json()["monthlyContribution"] == 45_000 and edited.json()["notes"] is None
    assert client.get(f"/api/goals/{goal['id']}").json()["monthlyContribution"] == 45_000
    assert [item["id"] for item in client.get("/api/goals").json()["goals"]] == [goal["id"]]

    assert client.delete(f"/api/goals/{goal['id']}").json() == {"deleted": True}
    assert client.get(f"/api/goals/{goal['id']}").status_code == 404
    assert client.delete(f"/api/goals/{goal['id']}").status_code == 404
    assert client.put(f"/api/goals/{goal['id']}", json=GOAL).status_code == 404


@pytest.mark.parametrize("changes", [
    {"currency": None},
    {"currency": "JPY"},
    {"targetAmount": 0},
    {"savedAmount": -1},
    {"targetDate": "1999-12-31"},
    {"targetDate": "2034"},
    {"assumedReturnPct": 31},
    {"kind": "yacht"},
    {"name": "   "},
    {"notes": "x" * 501},
    {"salary": 100_000},
])
def test_goal_input_is_validated(client, signed_in, changes):
    body = {key: value for key, value in {**GOAL, **changes}.items() if value is not None}
    assert client.post("/api/goals", json=body).status_code == 422


def test_goal_without_a_return_assumption_is_saved_without_inventing_one(client, signed_in):
    body = {key: value for key, value in GOAL.items() if key != "assumedReturnPct"}

    goal = client.post("/api/goals", json=body).json()

    assert goal["assumedReturnPct"] is None
    assert [item["name"] for item in goal["plan"]["scenarios"]] == ["no_growth"]


def test_goal_limit_is_enforced(client, signed_in):
    for index in range(memory.MAX_GOALS):
        create_goal(client, name=f"Goal {index}")

    response = client.post("/api/goals", json=GOAL)

    assert response.status_code == 409
    assert "up to 10 goals" in response.json()["error"]


def test_goal_plan_preview_saves_nothing(client, signed_in):
    body = PLAN_INPUTS

    preview = client.post("/api/goals/plan", json={**body, "annualReturnPcts": [0, 7]})

    assert preview.status_code == 200
    assert [item["annualReturnPct"] for item in preview.json()["scenarios"]] == [0, 7]
    assert raw("SELECT COUNT(*) FROM financial_goals") == [(0,)]
    assert client.post("/api/goals/plan", json={**body, "annualReturnPcts": [-25]}).status_code == 422
    assert client.post("/api/goals/plan", json={**body, "annualReturnPcts": [1, 2, 3, 4, 5]}).status_code == 422
    assert client.post("/api/goals/plan", json={**body, "currency": None}).status_code == 422


# ── Access rules ──────────────────────────────────────────────────────────────

def test_goal_routes_require_a_profile(client):
    client.get("/api/session")
    for method, path in (("GET", "/api/goals"), ("POST", "/api/goals"), ("GET", "/api/goals/x"), ("PUT", "/api/goals/x"),
                         ("DELETE", "/api/goals/x"), ("GET", "/api/decisions"), ("GET", "/api/context"),
                         ("DELETE", "/api/context"), ("POST", "/api/goals/plan")):
        body = PLAN_INPUTS if path.endswith("/plan") else GOAL if method in ("POST", "PUT") else None
        response = client.request(method, path, json=body)
        assert response.status_code == 401, (method, path)
        assert response.json()["profileRequired"] is True


def test_goals_need_the_pin_after_a_restart(client, signed_in):
    goal = create_goal(client)
    unlock._unlocked.clear()

    assert client.get("/api/goals").json()["unlockRequired"] is True
    assert client.get(f"/api/goals/{goal['id']}").status_code == 403
    assert client.get("/api/context").status_code == 403


def test_saving_goals_and_decisions_needs_the_current_notice_but_reading_and_deleting_do_not(client):
    sign_up(client, storage_mode=None)

    assert client.post("/api/goals", json=GOAL).json()["consentRequired"] is True
    assert client.post("/api/decisions", json={"text": "Index funds only"}).json()["consentRequired"] is True
    assert client.get("/api/goals").status_code == 200
    assert client.delete("/api/context").status_code == 200


def test_profiles_cannot_see_or_change_each_others_goals_and_decisions():
    with TestClient(main.app) as asha, TestClient(main.app) as ravi:
        sign_up(asha, "Asha")
        sign_up(ravi, "Ravi")
        goal = create_goal(asha)
        decision = asha.post("/api/decisions", json={"text": "Index funds only", "goalId": goal["id"]}).json()

        assert ravi.get("/api/goals").json()["goals"] == []
        assert ravi.get(f"/api/goals/{goal['id']}").status_code == 404
        assert ravi.put(f"/api/goals/{goal['id']}", json=GOAL).status_code == 404
        assert ravi.delete(f"/api/goals/{goal['id']}").status_code == 404
        assert ravi.put(f"/api/decisions/{decision['id']}", json={"text": "changed"}).status_code == 404
        assert ravi.delete(f"/api/decisions/{decision['id']}").status_code == 404
        # A decision cannot point at another profile's goal.
        assert ravi.post("/api/decisions", json={"text": "x", "goalId": goal["id"]}).status_code == 422
        assert asha.get(f"/api/goals/{goal['id']}").status_code == 200
        assert len(asha.get("/api/decisions").json()["decisions"]) == 1


# ── Decisions ─────────────────────────────────────────────────────────────────

def test_decision_lifecycle(client, signed_in):
    goal = create_goal(client)

    created = client.post("/api/decisions", json={
        "text": " Keep 6 months of expenses liquid ", "decidedOn": "2026-09-01", "goalId": goal["id"],
    })

    assert created.status_code == 201
    decision = created.json()
    assert decision["text"] == "Keep 6 months of expenses liquid"
    assert decision["source"] == "user_entered" and decision["decidedOn"] == "2026-09-01"
    (stored,) = raw("SELECT data FROM financial_decisions WHERE user_id = ?", signed_in["id"])
    assert "expenses" not in stored[0]

    updated = client.put(f"/api/decisions/{decision['id']}", json={"text": "Keep 9 months liquid"})
    assert updated.json()["text"] == "Keep 9 months liquid" and updated.json()["goalId"] is None
    assert client.delete(f"/api/decisions/{decision['id']}").json() == {"deleted": True}
    assert client.get("/api/decisions").json()["decisions"] == []


@pytest.mark.parametrize("body", [
    {"text": ""},
    {"text": "x" * 501},
    {"text": "ok", "decidedOn": (date.today() + timedelta(days=3)).isoformat()},
    {"text": "ok", "goalId": "missing"},
    {"text": "ok", "goalId": "bad id!"},
    {"text": "ok", "source": "assistant"},
])
def test_decision_input_is_validated(client, signed_in, body):
    assert client.post("/api/decisions", json=body).status_code == 422


def test_a_deleted_goal_leaves_its_decisions_in_place(client, signed_in):
    goal = create_goal(client)
    client.post("/api/decisions", json={"text": "Use the bonus for this", "goalId": goal["id"]})

    client.delete(f"/api/goals/{goal['id']}")

    assert client.get("/api/decisions").json()["decisions"][0]["goalId"] == goal["id"]


# ── Inspecting and removing saved context ─────────────────────────────────────

def test_context_endpoint_separates_confirmed_observed_and_inferred(client, signed_in):
    client.put("/api/profile", json={"riskAppetite": "moderate"})
    create_goal(client)
    client.post("/api/decisions", json={"text": "No more small caps"})
    captured = datetime(2026, 10, 13, 9, 0, tzinfo=snapshots.SNAPSHOT_TIMEZONE)
    snapshots.store_snapshot(signed_in["id"], captured, {"networth_snapshot": "{}"}, {})

    body = client.get("/api/context").json()

    confirmed = body["confirmed"]
    assert confirmed["preferences"]["fields"] == {"riskAppetite": "moderate"}
    assert confirmed["preferences"]["source"] == "user_entered" and confirmed["preferences"]["updatedAt"]
    assert [goal["name"] for goal in confirmed["goals"]] == ["Home down payment"]
    assert [item["text"] for item in confirmed["decisions"]] == ["No more small caps"]
    assert body["observed"]["latestSnapshot"]["source"] == "INDmoney"
    assert body["observed"]["latestSnapshot"]["asOfDate"] == "2026-10-13"
    assert body["observed"]["casStatements"] == {"count": 0, "latest": None}
    assert body["inferred"]["stored"] is False


def test_clearing_saved_context_keeps_chats_and_snapshots(client, signed_in, monkeypatch):
    async def fake_run_agent(*args, **kwargs):
        return "Noted."

    monkeypatch.setattr(main, "run_agent", fake_run_agent)
    client.put("/api/profile", json={"riskAppetite": "moderate"})
    create_goal(client)
    client.post("/api/decisions", json={"text": "No more small caps"})
    chat = client.post("/api/chats", json={"agentType": "finance"}).json()
    client.post("/api/chat", json={"message": "What is an index fund?", "chatId": chat["id"]})

    assert client.delete("/api/context").json() == {"cleared": True}

    body = client.get("/api/context").json()["confirmed"]
    assert body["goals"] == [] and body["decisions"] == [] and body["preferences"]["fields"] == {}
    assert len(client.get("/api/chats").json()["chats"]) == 1


def test_chats_never_write_saved_context(client, signed_in, monkeypatch):
    async def fake_run_agent(*args, **kwargs):
        return "You decided to save ₹50,000 a month for a house by 2030."

    monkeypatch.setattr(main, "run_agent", fake_run_agent)
    chat = client.post("/api/chats", json={"agentType": "finance"}).json()
    client.post("/api/chat", json={"message": "I am 34 and earn 2 lakh a month; I want a house by 2030.", "chatId": chat["id"]})

    assert raw("SELECT COUNT(*) FROM financial_goals") == [(0,)]
    assert raw("SELECT COUNT(*) FROM financial_decisions") == [(0,)]
    assert client.get("/api/profile").json()["profile"] == {}


def test_export_includes_goals_and_decisions(client, signed_in):
    create_goal(client)
    client.post("/api/decisions", json={"text": "No more small caps"})
    unlock._unlocked.clear()

    exported = client.post("/api/users/me/export", json={"pin": "2468"}).json()

    assert [goal["name"] for goal in exported["goals"]] == ["Home down payment"]
    assert [item["text"] for item in exported["decisions"]] == ["No more small caps"]


def test_deleting_the_profile_deletes_goals_and_decisions(client, signed_in):
    create_goal(client)
    client.post("/api/decisions", json={"text": "No more small caps"})

    client.request("DELETE", "/api/users/me", json={"pin": "2468"})

    assert raw("SELECT COUNT(*) FROM financial_goals") == [(0,)]
    assert raw("SELECT COUNT(*) FROM financial_decisions") == [(0,)]


def test_the_notice_mentions_goals_decisions_and_that_inferences_are_not_saved():
    text = " ".join(section["body"] for section in privacy.NOTICE)
    assert "goals and decisions" in text
    assert "never saves details the assistant infers" in text


# ── Moving goals saved by earlier versions ────────────────────────────────────

LEGACY_GOALS = [
    {"id": "home", "name": "Buy a home", "kind": "home", "targetInr": 5_000_000, "targetYear": 2034,
     "savedInr": 800_000, "monthlyInr": 25_000, "expectedReturnPct": 10},
    {"id": "bad", "name": "Broken", "targetInr": 0, "targetYear": 2030},
]


def test_goals_from_the_old_profile_list_move_to_their_own_table_on_unlock(client, signed_in):
    client.put("/api/profile", json={"riskAppetite": "high"})
    user = users.get_user(signed_in["id"])
    keys = unlock.keys_for_session(user, client.cookies.get(main.SESSION_COOKIE))
    stored = {**users.stored_profile(user, keys), "goals": LEGACY_GOALS}
    with db.connect_db() as connection:
        connection.execute("UPDATE users SET profile = ? WHERE id = ?", (keys.seal(json.dumps(stored)), user["id"]))
    unlock._unlocked.clear()

    assert client.post("/api/session/unlock", json={"pin": "2468"}).status_code == 200

    (goal,) = client.get("/api/goals").json()["goals"]
    assert goal["id"] == "home" and goal["currency"] == "INR" and goal["targetDate"] == "2034-12-31"
    assert (goal["targetAmount"], goal["savedAmount"], goal["monthlyContribution"], goal["assumedReturnPct"]) == (
        5_000_000, 800_000, 25_000, 10,
    )
    assert "31 December" in goal["migratedFrom"]["note"]
    assert client.get("/api/profile").json()["profile"] == {"riskAppetite": "high"}
    assert "goals" not in users.stored_profile(users.get_user(user["id"]), keys)
    # Unlocking again does not duplicate them.
    unlock._unlocked.clear()
    client.post("/api/session/unlock", json={"pin": "2468"})
    assert len(client.get("/api/goals").json()["goals"]) == 1

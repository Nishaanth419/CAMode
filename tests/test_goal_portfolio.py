"""Assigning INDmoney holdings to goals: allocations, valuation, plans with the mapped value, gates and cleanup."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import pytest
from conftest import sign_up
from fastapi.testclient import TestClient

from app import db, goal_portfolio, investigation_math, main, memory, privacy, snapshots, unlock, users
from tests.fixtures import indmoney_sample

GOAL = {
    "name": "Home down payment", "kind": "home", "currency": "INR", "targetAmount": 5_000_000,
    "targetDate": "2034-12-31", "savedAmount": 100_000, "monthlyContribution": 20_000, "assumedReturnPct": 9,
}
INFY = "IND_STOCK|INFY|INDmoney"        # 180,000
TCS = "IND_STOCK|TCS|INDmoney"          # 190,000
HDFCBANK = "IND_STOCK|HDFCBANK|Zerodha"  # 130,000
FLEXI = "MF|120503|-"                   # 375,000
INDEX = "MF|118989|-"                   # 125,000


def raw(sql: str, *params) -> list[tuple]:
    with db.connect_db() as connection:
        return [tuple(row) for row in connection.execute(sql, params).fetchall()]


def add_goal(client, **changes) -> dict:
    response = client.post("/api/goals", json={**GOAL, **changes})
    assert response.status_code == 201, response.text
    return response.json()


def store(user_id: str, *, when: datetime | None = None, drop: tuple[str, ...] = ()) -> None:
    results = {key: text for key, text in indmoney_sample.results().items() if key not in drop}
    snapshots.store_snapshot(user_id, when or datetime.now(timezone.utc), results, {})


def assign(client, key: str, goals=(), general: int = 0):
    return client.put("/api/goals/allocations", json={
        "holdingKey": key, "goals": [{"goalId": goal_id, "pct": pct} for goal_id, pct in goals], "generalWealthPct": general,
    })


def holding(client, key: str) -> dict:
    return next(item for item in client.get("/api/goals/allocations").json()["holdings"] if item["key"] == key)


def goal_view(client, goal_id: str) -> dict:
    return client.get(f"/api/goals/{goal_id}").json()


# ── Reading and assigning ──────────────────────────────────────────────────────

def test_holdings_list_with_values_and_nothing_assigned(client, signed_in):
    store(signed_in["id"])

    body = client.get("/api/goals/allocations").json()

    assert body["available"] is True and body["asOfDate"] and body["stale"] is False
    assert {item["key"] for item in body["holdings"]} == {INFY, TCS, HDFCBANK, FLEXI, INDEX, "US_STOCK|AAPL|INDmoney"}
    assert [item["currentValueInr"] for item in body["holdings"]][0] == 375_000
    infy = next(item for item in body["holdings"] if item["key"] == INFY)
    assert infy["goals"] == [] and infy["unassignedPct"] == 100 and infy["unassignedValueInr"] == 180_000
    assert infy["broadClass"] == "equity_direct"
    assert body["fundData"]["available"] is True and "shortlist" not in body


def test_assigning_one_holding_shows_in_the_plan_once(client, signed_in):
    store(signed_in["id"])
    goal = add_goal(client)
    before = goal_view(client, goal["id"])["plan"]

    response = assign(client, FLEXI, [(goal["id"], 40)], general=10)

    assert response.status_code == 200, response.text
    row = response.json()
    assert row["goals"] == [{"goalId": goal["id"], "goalName": GOAL["name"], "pct": 40, "valueInr": 150_000}]
    assert row["generalWealthPct"] == 10 and row["unassignedPct"] == 50 and row["unassignedValueInr"] == 187_500
    view = goal_view(client, goal["id"])
    assert view["mapping"]["mappedValueInr"] == 150_000 and view["mapping"]["counted"] is True
    assert view["plan"]["startingAmount"] == 250_000 and view["plan"]["savedAmount"] == 100_000
    assert view["plan"]["mappedAmount"] == 150_000
    assert view["plan"]["remainingGap"] == before["remainingGap"] - 150_000
    assert view["mapping"]["holdings"][0]["review"]["verdict"] in {"allowed", "caution", "unsuitable"}
    assert view["mapping"]["overlap"]["status"] == "not_assessable"
    # An empty split removes the assignment.
    assert assign(client, FLEXI).status_code == 200
    assert goal_view(client, goal["id"])["mapping"]["holdings"] == []


def test_a_split_across_two_goals_rounds_down_and_never_double_counts(client, signed_in):
    store(signed_in["id"])
    first, second = add_goal(client, name="Home"), add_goal(client, name="Education", kind="education")

    assert assign(client, INFY, [(first["id"], 60), (second["id"], 40)]).status_code == 200
    assert assign(client, TCS, [(first["id"], 33)], general=33).status_code == 200

    one, two = goal_view(client, first["id"]), goal_view(client, second["id"])
    assert one["mapping"]["mappedValueInr"] == 108_000 + 62_700  # 60% of 180,000; 33% of 190,000
    assert two["mapping"]["mappedValueInr"] == 72_000
    assert one["plan"]["startingAmount"] == 100_000 + 108_000 + 62_700
    assert two["plan"]["startingAmount"] == 100_000 + 72_000
    infy_total = 108_000 + 72_000
    assert infy_total <= 180_000
    tcs = holding(client, TCS)
    assert tcs["goals"][0]["valueInr"] == 62_700 and tcs["unassignedPct"] == 34
    shares = tcs["goals"][0]["valueInr"] + int(190_000 * 33 / 100) + tcs["unassignedValueInr"]
    assert shares <= 190_000


def test_general_wealth_is_not_counted_to_any_goal(client, signed_in):
    store(signed_in["id"])
    goal = add_goal(client)
    assert assign(client, INFY, general=100).status_code == 200
    view = goal_view(client, goal["id"])
    assert view["mapping"]["mappedValueInr"] == 0 and view["plan"]["mappedAmount"] is None
    assert holding(client, INFY)["unassignedPct"] == 0


def test_unassigned_holdings_and_totals_are_reported(client, signed_in):
    store(signed_in["id"])
    goal = add_goal(client)
    assign(client, INFY, [(goal["id"], 50)])
    assign(client, TCS, general=40)

    summary = client.get("/api/goals").json()["portfolio"]

    assert summary["available"] is True and summary["generalWealthValueInr"] == 76_000
    assert summary["unassignedCount"] == 6  # every holding keeps an unassigned share
    assert summary["unassignedValueInr"] == (
        90_000 + 114_000 + 130_000 + 375_000 + 125_000 + 100_000
    )
    assert summary["missingCount"] == 0 and summary["asOfDate"]


def test_goals_overview_keeps_its_old_keys_and_adds_fund_data(client, signed_in):
    store(signed_in["id"])
    add_goal(client)

    body = client.get("/api/goals").json()

    assert {"asOf", "maxGoals", "goals"} <= set(body)
    assert body["fundData"]["source"] and "shortlist" not in json.dumps(body).lower()
    goal = body["goals"][0]
    assert {"plan", "followUps", "suitability", "mapping"} <= set(goal)
    assert goal["plan"]["feasibility"]["status"] and "alternatives" in goal["plan"]
    assert {key: goal[key] for key in GOAL} == GOAL


# ── Validation ─────────────────────────────────────────────────────────────────

def test_more_than_100_percent_is_rejected(client, signed_in):
    store(signed_in["id"])
    first, second = add_goal(client, name="Home"), add_goal(client, name="Car", kind="car")
    response = assign(client, INFY, [(first["id"], 70), (second["id"], 40)])
    assert response.status_code == 422 and "at most 100%" in response.text
    assert assign(client, INFY, [(first["id"], 90)], general=11).status_code == 422
    assert assign(client, INFY, [(first["id"], 50), (first["id"], 10)]).status_code == 422
    assert assign(client, INFY, [(first["id"], 0)]).status_code == 422
    assert raw("SELECT COUNT(*) FROM goal_allocations") == [(0,)]


def test_unknown_goal_and_unknown_holding_are_rejected(client, signed_in):
    store(signed_in["id"])
    goal = add_goal(client)
    assert assign(client, INFY, [("nosuchgoal", 10)]).status_code == 422
    assert assign(client, "IND_STOCK|NOPE|INDmoney", [(goal["id"], 10)]).status_code == 422


def test_no_snapshot_is_a_conflict_and_reads_say_why(client, signed_in):
    goal = add_goal(client)
    assert assign(client, INFY, [(goal["id"], 10)]).status_code == 409
    body = client.get("/api/goals/allocations").json()
    assert body["available"] is False and "No portfolio snapshot" in body["reason"]
    view = goal_view(client, goal["id"])
    assert view["mapping"]["available"] is False and view["plan"]["startingAmount"] == 100_000


def test_private_mode_has_no_holdings_to_assign(client):
    sign_up(client, storage_mode="private")
    add_goal(client)
    body = client.get("/api/goals/allocations").json()
    assert body["available"] is False and "Private mode" in body["reason"]


# ── Access ─────────────────────────────────────────────────────────────────────

def test_allocation_routes_need_a_profile_an_unlock_and_consent_to_write(client):
    assert client.get("/api/goals/allocations").status_code == 401
    assert client.put("/api/goals/allocations", json={"holdingKey": INFY}).status_code == 401
    user = sign_up(client)
    store(user["id"])
    goal = add_goal(client)
    assert assign(client, INFY, [(goal["id"], 10)]).status_code == 200

    unlock._unlocked.clear()
    assert client.get("/api/goals/allocations").json()["unlockRequired"] is True
    assert client.put("/api/goals/allocations", json={"holdingKey": INFY}).json()["unlockRequired"] is True
    assert client.post("/api/session/unlock", json={"pin": "2468"}).status_code == 200


def test_writing_needs_consent_but_reading_does_not(client):
    sign_up(client, storage_mode=None)
    assert client.get("/api/goals/allocations").status_code == 200
    assert client.put("/api/goals/allocations", json={"holdingKey": INFY}).json()["consentRequired"] is True


def test_profiles_cannot_see_or_assign_each_others_holdings():
    with TestClient(main.app) as asha, TestClient(main.app) as ravi:
        asha_user, ravi_user = sign_up(asha, "Asha"), sign_up(ravi, "Ravi")
        store(asha_user["id"])
        goal = add_goal(asha)
        assert assign(asha, INFY, [(goal["id"], 30)]).status_code == 200

        assert ravi.get("/api/goals/allocations").json()["available"] is False
        assert assign(ravi, INFY, [(goal["id"], 30)]).status_code == 409
        store(ravi_user["id"])
        assert assign(ravi, INFY, [(goal["id"], 30)]).status_code == 422  # Asha's goal does not exist for Ravi
        assert holding(ravi, INFY)["goals"] == []
        assert holding(asha, INFY)["goals"][0]["pct"] == 30


def test_allocations_are_sealed_at_rest(client, signed_in):
    store(signed_in["id"])
    goal = add_goal(client)
    assign(client, INFY, [(goal["id"], 30)])
    ((stored,),) = raw("SELECT data FROM goal_allocations WHERE user_id = ?", signed_in["id"])
    assert stored.startswith("sealed:v1:") and "Infosys" not in stored and goal["id"] not in stored


# ── Snapshots that change ──────────────────────────────────────────────────────

def test_a_holding_missing_from_a_newer_snapshot_is_not_counted_and_is_warned_about(client, signed_in):
    now = datetime.now(timezone.utc)
    store(signed_in["id"], when=now - timedelta(days=1))
    goal = add_goal(client)
    assign(client, FLEXI, [(goal["id"], 100)])
    assert goal_view(client, goal["id"])["mapping"]["mappedValueInr"] == 375_000
    # Today's partial capture has no mutual-fund holdings at all.
    store(signed_in["id"], when=now, drop=("networth_holdings:MF",))

    view = goal_view(client, goal["id"])

    assert view["mapping"]["mappedValueInr"] == 0 and view["mapping"]["counted"] is False
    assert view["mapping"]["missing"][0]["valueInr"] is None
    assert "not fetched" in view["mapping"]["missing"][0]["reason"]
    assert view["plan"]["startingAmount"] == 100_000
    assert any("value is unknown" in warning for warning in view["plan"]["warnings"])
    listing = client.get("/api/goals/allocations").json()
    assert listing["missing"][0]["key"] == FLEXI and FLEXI not in {item["key"] for item in listing["holdings"]}
    assert client.get("/api/goals").json()["portfolio"]["missingCount"] == 1


def test_a_sold_holding_is_missing_not_zero(client, signed_in):
    store(signed_in["id"])
    goal = add_goal(client)
    assign(client, TCS, [(goal["id"], 100)])
    sold = {key: text for key, text in indmoney_sample.results().items()}
    rows = json.loads(sold["networth_holdings:IND_STOCK"])
    rows["holdings"] = [row for row in rows["holdings"] if row["investment_code"] != "TCS"]
    sold["networth_holdings:IND_STOCK"] = json.dumps(rows)
    snapshots.store_snapshot(signed_in["id"], datetime.now(timezone.utc), sold, {})

    missing = goal_view(client, goal["id"])["mapping"]["missing"]

    assert missing[0]["name"] == "Tata Consultancy Services" and "no longer reported" in missing[0]["reason"]


def test_an_old_snapshot_is_flagged_stale(client, signed_in):
    store(signed_in["id"], when=datetime.now(timezone.utc) - timedelta(days=3))
    goal = add_goal(client)
    assign(client, INFY, [(goal["id"], 50)])
    view = goal_view(client, goal["id"])
    assert client.get("/api/goals/allocations").json()["stale"] is True
    assert view["mapping"]["stale"] is True and any("is old" in warning for warning in view["plan"]["warnings"])


def test_a_dollar_goal_shows_the_mapped_value_but_does_not_count_it(client, signed_in):
    store(signed_in["id"])
    goal = add_goal(client, currency="USD", targetAmount=100_000)
    assign(client, INFY, [(goal["id"], 50)])

    view = goal_view(client, goal["id"])

    assert view["mapping"]["mappedValueInr"] == 90_000 and view["mapping"]["counted"] is False
    assert view["plan"]["startingAmount"] == 100_000 and view["plan"]["mappedAmount"] is None
    assert any("never converted" in warning for warning in view["plan"]["warnings"])


def test_one_holding_dominating_a_goal_is_flagged(client, signed_in):
    store(signed_in["id"])
    goal = add_goal(client)
    assign(client, FLEXI, [(goal["id"], 100)])
    assign(client, INDEX, [(goal["id"], 100)])
    concentration = goal_view(client, goal["id"])["mapping"]["concentration"]
    assert concentration["warning"] is True and concentration["key"] == FLEXI and concentration["sharePct"] == 75.0

    assign(client, INDEX, [(goal["id"], 100)])
    assign(client, TCS, [(goal["id"], 100)])
    assign(client, INFY, [(goal["id"], 100)])
    assert goal_view(client, goal["id"])["mapping"]["concentration"]["warning"] is False


# ── Cleanup paths ──────────────────────────────────────────────────────────────

def test_deleting_a_goal_releases_its_holdings(client, signed_in):
    store(signed_in["id"])
    first, second = add_goal(client, name="Home"), add_goal(client, name="Car", kind="car")
    assign(client, INFY, [(first["id"], 50), (second["id"], 30)])
    assign(client, TCS, [(first["id"], 100)])

    assert client.delete(f"/api/goals/{first['id']}").status_code == 200

    assert holding(client, INFY)["goals"][0]["goalId"] == second["id"]
    assert holding(client, TCS)["goals"] == []
    (row,) = raw("SELECT data FROM goal_allocations WHERE user_id = ?", signed_in["id"])
    keys = unlock.keys_for_session(users.get_user(signed_in["id"]), client.cookies.get(main.SESSION_COOKIE))
    assert list(json.loads(keys.open(row[0]))["holdings"]) == [INFY]


def test_export_contains_the_allocations(client, signed_in):
    store(signed_in["id"])
    goal = add_goal(client)
    assign(client, INFY, [(goal["id"], 25)], general=5)
    unlock._unlocked.clear()

    exported = client.post("/api/users/me/export", json={"pin": "2468"}).json()

    (item,) = exported["goalAllocations"]
    assert item["holdingKey"] == INFY and item["goals"] == {goal["id"]: 25} and item["generalWealthPct"] == 5


@pytest.mark.parametrize("remove", ["clear_context", "disconnect", "delete_profile"])
def test_allocations_are_removed_with_context_connection_and_profile(client, signed_in, remove):
    store(signed_in["id"])
    goal = add_goal(client)
    assign(client, INFY, [(goal["id"], 25)])

    if remove == "clear_context":
        client.delete("/api/context")
    elif remove == "disconnect":
        users.clear_indmoney(signed_in["id"])
        assert len(client.get("/api/goals").json()["goals"]) == 1  # the goal itself stays
    else:
        client.request("DELETE", "/api/users/me", json={"pin": "2468"})

    assert raw("SELECT COUNT(*) FROM goal_allocations") == [(0,)]


def test_concurrent_assignments_to_different_holdings_both_survive(client, signed_in):
    store(signed_in["id"])
    goal = add_goal(client)
    keys = unlock.keys_for_session(users.get_user(signed_in["id"]), client.cookies.get(main.SESSION_COOKIE))
    holding_keys = [INFY, TCS, HDFCBANK, FLEXI, INDEX]
    barrier = threading.Barrier(len(holding_keys))

    def write(key: str) -> None:
        barrier.wait()
        entry = {"name": key, "assetType": "X", "broker": None, "goals": {goal["id"]: 10}, "generalWealthPct": 0}
        memory.set_allocation(signed_in["id"], keys, key, entry)

    with ThreadPoolExecutor(max_workers=len(holding_keys)) as pool:
        list(pool.map(write, holding_keys))

    assert set(memory.get_allocations(signed_in["id"], keys)) == set(holding_keys)


def test_the_holding_limit_is_enforced(client, signed_in):
    goal = add_goal(client)
    keys = unlock.keys_for_session(users.get_user(signed_in["id"]), client.cookies.get(main.SESSION_COOKIE))
    entry = {"name": "A", "assetType": "X", "broker": None, "goals": {goal["id"]: 10}, "generalWealthPct": 0}
    memory.set_allocation(signed_in["id"], keys, "a", entry, max_holdings=1)
    memory.set_allocation(signed_in["id"], keys, "a", entry, max_holdings=1)  # replacing is not adding
    with pytest.raises(memory.LimitReachedError):
        memory.set_allocation(signed_in["id"], keys, "b", entry, max_holdings=1)


# ── Plan preview and identity ──────────────────────────────────────────────────

def test_plan_preview_returns_suitability_and_follow_ups_and_saves_nothing(client, signed_in):
    body = {"currency": "INR", "targetAmount": 1_000_000, "horizonYears": 2, "flexibility": "fixed"}

    preview = client.post("/api/goals/plan", json=body)

    assert preview.status_code == 200, preview.text
    data = preview.json()
    states = {item["id"]: item["verdict"] for item in data["suitability"]["categories"]}
    assert states["large_flexi_equity"] == "unsuitable" and data["suitability"]["enoughInformation"] is False
    assert "liquidityNeed" in {item["field"] for item in data["followUps"]}
    assert data["feasibility"]["status"] and client.get("/api/goals").json()["goals"] == []
    assert raw("SELECT COUNT(*) FROM goal_allocations") == [(0,)]


def test_the_editor_preview_counts_the_goals_assigned_holdings_like_the_saved_goal(client, signed_in):
    store(signed_in["id"])
    goal = add_goal(client)
    assign(client, FLEXI, [(goal["id"], 40)])
    inputs = {key: GOAL[key] for key in ("currency", "targetAmount", "targetDate", "savedAmount", "monthlyContribution", "assumedReturnPct")}

    preview = client.post("/api/goals/plan", json={**inputs, "goalId": goal["id"]}).json()
    saved = goal_view(client, goal["id"])["plan"]

    assert preview["mappedAmount"] == saved["mappedAmount"] == 150_000
    assert preview["startingAmount"] == saved["startingAmount"] and preview["scenarios"] == saved["scenarios"]
    # Without the id (a new goal) nothing is assigned yet; a dollar preview never counts rupee holdings.
    assert client.post("/api/goals/plan", json=inputs).json()["mappedAmount"] is None
    assert client.post("/api/goals/plan", json={**inputs, "currency": "USD", "goalId": goal["id"]}).json()["mappedAmount"] is None
    assert client.post("/api/goals/plan", json={**inputs, "goalId": "nope"}).status_code == 404


def test_holding_keys_match_the_investigation_identity():
    snapshot = investigation_math.normalize_snapshot({
        "asOfDate": "2026-10-16", "capturedAt": 1_760_000_000_000, "networthInr": 1, "results": indmoney_sample.results(),
    })
    ours = set()
    for row in indmoney_sample.IND_STOCK + indmoney_sample.MF + indmoney_sample.US_STOCK:
        ours.add(goal_portfolio.holding_key(row["asset_type"], row["investment_code"], row["investment"], row["broker"]))
    assert ours == {item.holding_id for item in snapshot.holdings}
    assert goal_portfolio.holding_key("MF", None, "Some  Fund!", None) == "MF|name:some fund|-"


def test_share_values_round_down():
    from decimal import Decimal
    assert goal_portfolio.share_value(Decimal("100.99"), 50) == 50
    assert goal_portfolio.share_value(Decimal("3"), 33) == 0
    assert goal_portfolio.share_value(Decimal("190000"), 33) == 62_700


# ── Agent tools ────────────────────────────────────────────────────────────────

TODAY = date(2026, 10, 16)


def run(coroutine):
    import asyncio
    return asyncio.run(coroutine)


def tools_for(client, *, include_holdings: bool):
    user = main.current_user(type("Request", (), {"cookies": client.cookies})())
    return {tool.name: tool for tool in goal_portfolio.goal_tools(user, include_holdings=include_holdings, today=TODAY)}


def test_get_goal_plans_with_and_without_holdings(client, signed_in):
    store(signed_in["id"])
    goal = add_goal(client)
    assign(client, FLEXI, [(goal["id"], 40)])

    with_holdings = json.loads(run(tools_for(client, include_holdings=True)["get_goal_plans"].handler({})))
    without = json.loads(run(tools_for(client, include_holdings=False)["get_goal_plans"].handler({})))

    item = with_holdings["goals"][0]
    assert item["holdings"]["assignedValueInr"] == 150_000 and item["holdings"]["snapshotDate"]
    assert item["holdings"]["holdings"][0]["name"] == "Parag Parikh Flexi Cap Fund"
    assert item["plan"]["mappedAmount"] == 150_000 and item["plan"]["feasibility"]["status"]
    assert with_holdings["fundData"]["source"] and "shortlist" not in json.dumps(with_holdings)
    assert goal["id"] not in json.dumps(with_holdings) and "id" not in item
    assert "holdings" not in without["goals"][0] and without["goals"][0]["assignedHoldings"] == 1
    assert "differ from the Goals page" in without["goals"][0]["assignedHoldingsNote"]
    assert without["goals"][0]["plan"]["mappedAmount"] is None
    assert "150000" not in json.dumps(without) and "Parag" not in json.dumps(without)


def test_get_goal_plans_filters_by_name_and_reports_unknown_names(client, signed_in):
    add_goal(client, name="Home down payment")
    add_goal(client, name="Japan trip", kind="travel")
    tool = tools_for(client, include_holdings=False)["get_goal_plans"]

    one = json.loads(run(tool.handler({"goalName": "japan"})))
    unknown = json.loads(run(tool.handler({"goalName": "Yacht"})))

    assert [item["name"] for item in one["goals"]] == ["Japan trip"]
    assert unknown["error"] == "No goal has that name." and unknown["goalNames"] == ["Home down payment", "Japan trip"]


def test_the_goal_plans_payload_fits_groqs_limit_with_ten_goals(client, signed_in):
    store(signed_in["id"])
    for index in range(10):
        goal = add_goal(client, name=f"Goal number {index}", notes="x" * 300)
        assign(client, [INFY, TCS, HDFCBANK, FLEXI, INDEX][index % 5], [(goal["id"], 10)])

    payload = run(tools_for(client, include_holdings=True)["get_goal_plans"].handler({}))

    data = json.loads(payload)
    assert len(payload) <= goal_portfolio.MAX_TOOL_PAYLOAD_CHARS
    assert data["goals"] and (len(data["goals"]) == 10 or "left out to fit" in data["truncated"])
    assert all("id" not in item for item in data["goals"])


def test_a_goal_that_does_not_fit_loses_reasons_first_then_later_goals(client, signed_in):
    for index in range(10):
        add_goal(client, name=f"Goal number {index}")
    data = json.loads(run(tools_for(client, include_holdings=False)["get_goal_plans"].handler({})))
    if "truncated" in data:
        assert "left out to fit" in data["truncated"]
    assert all(
        "reasons" not in category for item in data["goals"] for category in item["suitability"]["categories"]
    ) or len(data["goals"]) == 10


def test_what_if_changes_nothing_and_says_so(client, signed_in):
    goal = add_goal(client)
    before = raw("SELECT data, updated_at FROM financial_goals")
    tool = tools_for(client, include_holdings=False)["plan_goal_what_if"]

    result = json.loads(run(tool.handler({"goalName": "Home", "monthlyContribution": 80_000, "lumpSum": 200_000, "extraMonths": 12})))

    assert result["saved"] is False and "Not saved" in result["note"]
    assert result["changes"] == {"monthlyContribution": 80_000, "extraMonths": 12, "lumpSum": 200_000}
    assert result["plan"]["monthlyContribution"] == 80_000 and result["plan"]["savedAmount"] == 300_000
    assert result["plan"]["targetDate"] == "2035-12-31" and "suitability" in result
    assert raw("SELECT data, updated_at FROM financial_goals") == before
    assert client.get(f"/api/goals/{goal['id']}").json()["monthlyContribution"] == 20_000


def test_what_if_rejects_unknown_names_and_invalid_overrides(client, signed_in):
    add_goal(client, name="Home")
    add_goal(client, name="Home loan prepayment", kind="other")
    tool = tools_for(client, include_holdings=False)["plan_goal_what_if"]

    assert "No goal has that name" in json.loads(run(tool.handler({"goalName": "Yacht"})))["error"]
    assert json.loads(run(tool.handler({})))["error"] == "No goal has that name."
    ambiguous = json.loads(run(tool.handler({"goalName": "hom"})))
    assert ambiguous["error"] == "Name one goal." and "Home loan prepayment" in ambiguous["goalNames"]
    assert "inflation" in json.loads(run(tool.handler({"goalName": "Home", "inflationPct": 40})))["error"]
    assert "cannot be negative" in json.loads(run(tool.handler({"goalName": "Home", "lumpSum": -5})))["error"]
    assert "extraMonths" in json.loads(run(tool.handler({"goalName": "Home", "extraMonths": 9999})))["error"]
    assert "date" in json.loads(run(tool.handler({"goalName": "Home", "targetDate": "soon"})))["error"]


def test_an_unreadable_snapshot_leaves_the_goal_tools_working(client, signed_in, monkeypatch):
    store(signed_in["id"])
    add_goal(client)

    def broken(*args, **kwargs):
        raise RuntimeError("database disk image is malformed")

    monkeypatch.setattr(snapshots, "latest_snapshot", broken)

    data = json.loads(run(tools_for(client, include_holdings=True)["get_goal_plans"].handler({})))

    assert data["goals"][0]["holdings"] == {"available": False, "reason": goal_portfolio.UNREADABLE_REASON}
    assert data["goals"][0]["plan"]["startingAmount"] == 100_000
    assert client.get("/api/goals").status_code == 200


def test_the_goal_tool_descriptions_name_no_funds():
    tools = goal_portfolio.goal_tools({"id": "x", "keys": None}, include_holdings=False)
    tools.append(goal_portfolio.goal_calculator_tool())
    assert [tool.name for tool in tools] == ["get_goal_plans", "plan_goal_what_if", "calculate_goal_plan"]
    assert all(len(tool.description) < 500 for tool in tools)


# ── Chat wiring ────────────────────────────────────────────────────────────────

def _chat_with(client, agent_type, monkeypatch, message="Can I reach my house goal?"):
    seen = []

    async def fake_run_agent(message, mcp_url, token, history, agent_type, provider, *, context=None, saved_context=None):
        seen.append((context, saved_context))
        return "ok"

    monkeypatch.setattr(main, "run_agent", fake_run_agent)
    chat = client.post("/api/chats", json={"agentType": agent_type}).json()
    response = client.post("/api/chat", json={"message": message, "chatId": chat["id"], "agentType": agent_type})
    assert response.status_code == 200, response.text
    return seen[0]


def test_portfolio_analyzer_gets_the_goal_tools_beside_its_snapshot_tools(client, signed_in, monkeypatch):
    from tests.test_api import connect_user_to_indmoney
    connect_user_to_indmoney(signed_in["id"])
    add_goal(client)

    context, saved = _chat_with(client, "portfolio", monkeypatch)

    names = [tool.name for tool in context.local_tools]
    assert {"get_goal_plans", "plan_goal_what_if", "get_portfolio_snapshot", "investigate_portfolio_change"} <= set(names)
    assert saved is not None


def test_profiles_without_goals_get_no_goal_tools(client, signed_in, monkeypatch):
    from tests.test_api import connect_user_to_indmoney
    connect_user_to_indmoney(signed_in["id"])

    context, _ = _chat_with(client, "portfolio", monkeypatch)
    assert not {"get_goal_plans", "plan_goal_what_if"} & {tool.name for tool in context.local_tools}
    finance_context, _ = _chat_with(client, "finance", monkeypatch)
    # Only the calculator, which reads nothing saved: a goal question without a saved goal can still be calculated.
    assert [tool.name for tool in finance_context.local_tools] == ["calculate_goal_plan"]


@pytest.mark.parametrize(("message", "offered"), [
    ("I need 50 lakh for a house in 5 years and have 5 lakh saved. How much a month?", True),
    ("How much should I save each month to have 20,00,000 by 2031?", True),
    ("Is my retirement corpus enough?", True),
    ("What is an ELSS fund?", False),
    ("Explain how a sweep-in fixed deposit works", False),
])
def test_the_goal_calculator_is_offered_only_for_goal_arithmetic(client, signed_in, monkeypatch, message, offered):
    context, _ = _chat_with(client, "finance", monkeypatch, message)
    names = [tool.name for tool in context.local_tools] if context else []
    assert names == (["calculate_goal_plan"] if offered else [])


# ── Calculator for goals described in chat ─────────────────────────────────────

CALC_TODAY = date(2026, 10, 16)


def calculate(**arguments):
    return goal_portfolio.calculate_goal(arguments, CALC_TODAY)


def test_the_calculator_plans_a_goal_described_in_chat_and_saves_nothing(client, signed_in):
    result = calculate(currency="INR", targetAmount=5_000_000.0, horizonYears=5, savedAmount=500_000, monthlyContribution=40_000)

    assert result["saved"] is False and "Nothing is saved" in result["note"]
    assert result["inputs"]["targetDate"] == "2031-10-16"
    no_growth = result["plan"]["scenarios"][0]
    assert no_growth["projectedAmount"] == 500_000 + 40_000 * 60
    assert no_growth["requiredMonthly"] == 75_000 and no_growth["additionalMonthly"] == 35_000
    assert result["plan"]["feasibility"]["status"] == "shortfall"
    assert {item["kind"] for item in result["plan"]["alternatives"]} >= {"increase_monthly", "extend_deadline"}
    assert result["fundData"] and len(json.dumps(result)) < goal_portfolio.MAX_TOOL_PAYLOAD_CHARS
    assert memory.count_goals(signed_in["id"]) == 0


def test_the_calculator_asks_for_missing_inputs_instead_of_guessing():
    result = calculate(targetAmount=2_000_000)

    assert result["plan"]["status"] == "insufficient_data"
    assert set(result["plan"]["missing"]) == {"target date", "currency"}
    assert result["plan"]["scenarios"] == []
    assert result["followUps"][:2] == ["Which currency is this goal in?", "When do you need the money?"]


def test_the_calculator_rules_out_equity_for_a_short_fixed_goal():
    result = calculate(currency="INR", targetAmount=1_500_000, horizonYears=2, flexibility="fixed",
                       riskTolerance="high", lossCapacity="low", liquidityNeed="low")

    verdicts = {item["category"]: item["verdict"] for item in result["suitability"]["categories"]}
    assert verdicts["Large-cap, flexi-cap and index equity"] == "unsuitable"
    assert verdicts["Mid-cap and small-cap equity"] == "unsuitable"
    assert verdicts["Liquid and overnight"] == "allowed"


@pytest.mark.parametrize(("arguments", "message"), [
    ({"currency": "INR", "targetAmount": 100, "targetDate": "2030-01-01", "horizonYears": 3}, "not both"),
    ({"currency": "INR", "targetAmount": -5, "horizonYears": 3}, "targetAmount"),
    ({"currency": "INR", "targetAmount": 100, "horizonYears": 3, "annualReturnPct": 45}, "annualReturnPct"),
    ({"currency": "INR", "targetAmount": 100, "targetDate": "next year"}, "targetDate"),
    ({"currency": "JPY", "targetAmount": 100, "horizonYears": 3}, "currency"),
    ({"currency": "INR", "targetAmount": 100, "horizonYears": 2.5}, "horizonYears"),
])
def test_the_calculator_returns_an_error_the_model_can_fix(arguments, message):
    result = calculate(**arguments)
    assert set(result) == {"error"} and message in result["error"]


def test_the_calculator_reads_placeholder_arguments_as_not_given():
    # Arguments the local model sent in the Phase 1 traces; each call used to be rejected and retried.
    result = calculate(annualReturnPct=10, currency="₹", flexibility="", horizonYears=8, inflationPct=0,
                       liquidityNeed="High", lossCapacity="Low", monthlyContribution=0, riskTolerance="Moderate",
                       savedAmount=500_000.0, targetAmount=5_000_000, targetDate="")

    assert "error" not in result
    assert result["inputs"]["currency"] == "INR" and result["inputs"]["targetDate"] == "2034-10-16"
    assert result["plan"]["scenarios"][0]["requiredMonthly"] == 46_875
    # Not the exact choices, so never taken as the user's answers: the rules use their cautious values instead.
    assert set(result["ignoredInputs"]) == {"liquidityNeed", "lossCapacity", "riskTolerance"}
    assert "'Moderate' is not one of low, medium, high" in result["ignoredInputs"]["riskTolerance"]
    assert set(result["suitability"]["assumedCautiously"]) >= {"liquidityNeed", "lossCapacity", "riskTolerance", "flexibility"}


@pytest.mark.parametrize(("currency", "expected"), [
    ("₹", "INR"), ("Rs.", "INR"), ("rs", "INR"), ("inr", "INR"), (" INR ", "INR"), ("$", "USD"), ("€", "EUR"),
    ("£", "GBP"), ("gbp", "GBP"),
])
def test_the_calculator_accepts_currency_symbols_and_any_case(currency, expected):
    result = calculate(currency=currency, targetAmount=1_000_000, horizonYears=4)
    assert result["inputs"]["currency"] == expected


def test_the_calculator_keeps_exact_suitability_answers_and_reports_nothing_ignored():
    result = calculate(currency="INR", targetAmount=1_500_000, horizonYears=2, flexibility="fixed", riskTolerance="high")
    assert "ignoredInputs" not in result
    assert "riskTolerance" not in result["suitability"]["assumedCautiously"]


@pytest.mark.parametrize("user_text", [
    "I want ₹50 lakh for a house in 8 years. I have ₹5 lakh saved.",
    "I need Rs. 50,00,000 in 8 years", "I need Rs50,00,000 in 8 years", "50 LAKH in 8 years", "half a crore in 8 years",
    "2 lacs a year for 8 years",
])
def test_a_goal_written_in_rupees_is_planned_in_inr_when_the_call_has_no_currency(user_text):
    result = goal_portfolio.calculate_goal(
        {"targetAmount": 5_000_000, "horizonYears": 8, "savedAmount": 500_000}, CALC_TODAY, user_text=user_text,
    )

    assert result["inputs"]["currency"] == "INR"
    assert result["currencyNote"] == goal_portfolio.CURRENCY_FROM_TEXT_NOTE
    assert result["plan"]["scenarios"][0]["requiredMonthly"] == 46_875
    assert "Which currency is this goal in?" not in result["followUps"]


@pytest.mark.parametrize("user_text", [
    "",
    "I need 50,00,000 in 8 years",  # No currency words at all.
    "I need ₹50 lakh or $60,000 in 8 years",  # Two currencies: ask rather than pick one.
    "Is 40 lakh in USD enough?",
    "I sleep 8 hrs and save for 8 yrs",  # "hrs"/"yrs" are not "Rs".
])
def test_the_calculator_still_asks_for_the_currency_when_the_text_is_not_plainly_rupees(user_text):
    result = goal_portfolio.calculate_goal({"targetAmount": 5_000_000, "horizonYears": 8}, CALC_TODAY, user_text=user_text)

    assert result["plan"]["status"] == "insufficient_data" and "currency" in result["plan"]["missing"]
    assert "currencyNote" not in result


def test_a_currency_the_model_gives_is_kept_over_the_rupee_default():
    result = goal_portfolio.calculate_goal(
        {"currency": "USD", "targetAmount": 60_000, "horizonYears": 8}, CALC_TODAY, user_text="₹50 lakh in 8 years",
    )
    assert result["inputs"]["currency"] == "USD" and "currencyNote" not in result


def test_the_chat_gives_the_calculator_the_users_own_words(client, signed_in, monkeypatch):
    context, _ = _chat_with(client, "finance", monkeypatch, "I want ₹50 lakh for a house in 8 years. How much a month?")
    calculator = next(tool for tool in context.local_tools if tool.name == "calculate_goal_plan")

    result = json.loads(run(calculator.handler({"targetAmount": 5_000_000, "horizonYears": 8})))

    assert result["inputs"]["currency"] == "INR" and "currencyNote" in result


def test_the_calculator_flags_a_high_return_assumption():
    result = calculate(currency="INR", targetAmount=5_000_000, horizonYears=5, monthlyContribution=60_000, annualReturnPct=24)
    assert any("high return to assume" in warning for warning in result["plan"]["warnings"])


def test_what_if_cannot_move_a_date_the_goal_does_not_have(client, signed_in):
    add_goal(client, name="Someday", status="draft", targetDate=None)
    tool = tools_for(client, include_holdings=False)["plan_goal_what_if"]

    result = json.loads(run(tool.handler({"goalName": "Someday", "extraMonths": 6})))

    assert "no target date" in result["error"]


def test_a_saved_goal_and_its_preview_question_a_return_its_categories_may_not_reach(client, signed_in):
    goal = add_goal(client, targetDate=(date.today().replace(day=1) + timedelta(days=800)).isoformat(),
                    assumedReturnPct=12, flexibility="fixed", lossCapacity="low")
    body = {key: goal[key] for key in ("currency", "targetAmount", "targetDate", "savedAmount", "monthlyContribution",
                                       "assumedReturnPct", "flexibility", "lossCapacity")}

    for plan in (goal["plan"], client.post("/api/goals/plan", json=body).json()):
        assert any("Equity is unsuitable for this goal" in warning for warning in plan["warnings"])
    # A long, flexible goal that can take losses keeps equity, so there is nothing to question.
    relaxed = add_goal(client, name="Retirement", kind="retirement", targetDate="2050-12-31", flexibility="flexible",
                       lossCapacity="high", riskTolerance="high", liquidityNeed="low")
    assert not any("Equity is unsuitable" in warning for warning in relaxed["plan"]["warnings"])

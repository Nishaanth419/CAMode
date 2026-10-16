"""Choosing which saved context reaches the agents, and how it is labelled."""

from __future__ import annotations

from datetime import date

import pytest

from app import context, main
from app.context import retrieve_context, select_decisions, select_goals

TODAY = date(2026, 10, 16)
HOME = {"id": "g1", "name": "Home down payment", "kind": "home"}
CAR = {"id": "g2", "name": "New car", "kind": None}
TRIP = {"id": "g3", "name": "Japan", "kind": "travel"}
GOALS = [HOME, CAR, TRIP]


@pytest.mark.parametrize(("message", "names"), [
    ("How is my home down payment going?", ["Home down payment"]),
    ("Can I afford a bigger house sooner?", ["Home down payment"]),
    ("What about my car?", ["New car"]),
    ("Will the Japan trip fit in my budget?", ["Japan"]),
    ("Am I on track for my goals?", ["Home down payment", "New car", "Japan"]),
    ("How much should I be saving for retirement?", ["Home down payment", "New car", "Japan"]),
    ("What is an ETF?", []),
    ("What is my net worth today?", []),
])
def test_select_goals(message, names):
    assert [goal["name"] for goal in select_goals(GOALS, message)] == names


def test_follow_up_questions_use_the_users_previous_turns():
    history = [{"role": "user", "content": "Tell me about my car goal"}, {"role": "assistant", "content": "Your home..."}]

    text = context.request_text("And if I add 5,000 more a month?", history)

    assert [goal["name"] for goal in select_goals(GOALS, text)] == ["New car"]


@pytest.mark.parametrize(("message", "expected"), [
    ("And if I add 5,000 more a month?", True),
    ("Is that enough?", True),
    ("What if returns are lower?", True),
    ("What is an index fund?", False),
    ("Explain ELSS funds", False),
])
def test_is_follow_up(message, expected):
    assert context.is_follow_up(message) is expected


def test_select_decisions():
    decisions = [
        {"id": "d1", "text": "Keep six months of expenses in a liquid fund", "updatedAt": 1},
        {"id": "d2", "text": "Put the annual bonus towards the flat", "goalId": "g1", "updatedAt": 2},
        {"id": "d3", "text": "Stop buying smallcap funds", "updatedAt": 3},
    ]

    assert [item["id"] for item in select_decisions(decisions, "How is it going?", {"g1"})] == ["d2"]
    assert [item["id"] for item in select_decisions(decisions, "Should I add more smallcap exposure?", set())] == ["d3"]
    assert [item["id"] for item in select_decisions(decisions, "What did I decide last time?", set())] == ["d3", "d2", "d1"]
    assert select_decisions(decisions, "What is an ETF?", set()) == []


# ── Retrieval for a signed-in profile ─────────────────────────────────────────

GOAL_BODY = {
    "name": "Home down payment", "kind": "home", "currency": "INR", "targetAmount": 2_500_000,
    "targetDate": "2032-03-31", "savedAmount": 400_000, "monthlyContribution": 30_000, "assumedReturnPct": 9,
}


def current(client):
    return main.current_user(type("Request", (), {"cookies": client.cookies})())


def test_nothing_is_retrieved_for_unrelated_requests_or_empty_profiles(client, signed_in):
    assert retrieve_context(current(client), "Am I on track for my goals?", [], today=TODAY) is None

    client.post("/api/goals", json=GOAL_BODY)

    assert retrieve_context(current(client), "What is an ETF?", [], today=TODAY) is None


def test_retrieved_context_separates_confirmed_entries_from_calculated_figures(client, signed_in):
    client.put("/api/profile", json={"riskAppetite": "moderate", "investmentHorizonYears": 8})
    client.post("/api/goals", json={**GOAL_BODY, "notes": "Near the office"})
    goal_id = client.get("/api/goals").json()["goals"][0]["id"]
    client.post("/api/decisions", json={"text": "Put each bonus into this goal", "decidedOn": "2026-09-01", "goalId": goal_id})
    client.post("/api/decisions", json={"text": "Never take a personal loan"})

    saved = retrieve_context(current(client), "Am I on track for my home goal?", [], today=TODAY)

    text = saved.text
    confirmed, calculated = text.split("Calculated in code on 2026-10-16")
    assert confirmed.startswith("Confirmed by the user (entered in CA Mode; not checked against any account):")
    assert "risk appetite: moderate; investment horizon (years): 8" in confirmed
    assert ('Goal "Home down payment": target INR 2500000 by 2032-03-31; earmarked savings INR 400000; '
            "planned monthly contribution INR 30000; return assumption 9% a year") in confirmed
    assert 'notes: "Near the office"' in confirmed
    assert 'Decision recorded 2026-09-01 (goal "Home down payment"): "Put each bonus into this goal"' in confirmed
    assert "personal loan" not in text
    assert "not forecasts or promised returns" in calculated
    assert "65 months left; INR 2100000 still to save today (16% saved)" in calculated
    assert ("No growth (0% a year): projected INR 2350000, short by INR 150000; "
            "needs INR 32308 a month (INR 2308 more than planned)") in calculated
    assert "Your assumption (9% a year)" in calculated
    assert "Not saved: anything not listed here" in text
    assert saved.items == ("Profile preferences", "Goal: Home down payment", "Decision: Put each bonus into this goal")


def test_goals_in_other_currencies_are_labelled_and_never_converted(client, signed_in):
    client.post("/api/goals", json={
        **GOAL_BODY, "name": "US college", "kind": "education", "currency": "USD", "targetAmount": 80_000,
    })

    saved = retrieve_context(current(client), "How is my college goal doing?", [], today=TODAY)

    assert "target USD 80000" in saved.text and "INR" not in saved.text.split("Calculated in code")[1].split("Assumptions")[0]


def test_preferences_alone_come_with_risk_and_allocation_questions(client, signed_in):
    client.put("/api/profile", json={"riskAppetite": "low"})

    saved = retrieve_context(current(client), "Is my allocation too aggressive?", [], today=TODAY)

    assert saved.items == ("Profile preferences",)
    assert "Calculated in code" not in saved.text


def test_long_context_is_cut_to_the_limit(client, signed_in, monkeypatch):
    monkeypatch.setattr(context, "MAX_CONTEXT_CHARS", 900)
    for index in range(6):
        client.post("/api/goals", json={**GOAL_BODY, "name": f"Goal number {index}", "notes": "n" * 300})

    saved = retrieve_context(current(client), "Review all my goals", [], today=TODAY)

    assert len(saved.text) <= 900
    assert saved.text.endswith("(more saved context not shown)")


def test_previous_turns_count_only_for_follow_ups(client, signed_in):
    client.post("/api/goals", json=GOAL_BODY)
    history = [{"role": "user", "content": "Am I on track for my home goal?"}, {"role": "assistant", "content": "Yes."}]

    follow_up = retrieve_context(current(client), "And if I stop contributing for a year?", history, today=TODAY)
    new_topic = retrieve_context(current(client), "What is an index fund?", history, today=TODAY)

    assert follow_up.items == ("Goal: Home down payment",)
    assert new_topic is None


def test_finance_agent_chat_receives_relevant_saved_context(client, signed_in, monkeypatch):
    client.post("/api/goals", json=GOAL_BODY)
    seen = []

    async def fake_run_agent(message, mcp_url, token, history, agent_type, provider, *, context=None, saved_context=None):
        seen.append((agent_type, context, saved_context))
        return "ok"

    monkeypatch.setattr(main, "run_agent", fake_run_agent)
    chat = client.post("/api/chats", json={"agentType": "finance"}).json()
    client.post("/api/chat", json={"message": "How much more do I need for the house?", "chatId": chat["id"]})

    ((agent_type, portfolio_context, saved),) = seen
    # A profile with a goal gives the Finance Agent the goal tools and nothing that touches an account.
    assert agent_type == "finance"
    assert [tool.name for tool in portfolio_context.local_tools] == ["get_goal_plans", "plan_goal_what_if"]
    assert portfolio_context.cached_mcp_tools is None and portfolio_context.profile_context is None
    assert saved.items == ("Goal: Home down payment",)


def test_finance_agent_chat_without_goals_gets_no_context(client, signed_in, monkeypatch):
    seen = []

    async def fake_run_agent(message, mcp_url, token, history, agent_type, provider, *, context=None, saved_context=None):
        seen.append(context)
        return "ok"

    monkeypatch.setattr(main, "run_agent", fake_run_agent)
    chat = client.post("/api/chats", json={"agentType": "finance"}).json()
    client.post("/api/chat", json={"message": "What is an ETF?", "chatId": chat["id"]})

    assert seen == [None]


# ── Goal planning in the rendered context ─────────────────────────────────────

def test_rendered_context_has_feasibility_alternatives_suitability_follow_ups_and_the_fund_line(client, signed_in):
    client.post("/api/goals", json={**GOAL_BODY, "monthlyContribution": 5_000, "flexibility": "fixed"})

    text = retrieve_context(current(client), "Am I on track for my home goal?", [], today=TODAY).text

    calculated = text.split("Calculated in code on 2026-10-16")[1]
    assert "No fund-level data: do not name or rank specific funds." in calculated
    assert "Feasibility: The planned contributions fall short" in calculated
    assert "raise the monthly contribution to INR" in calculated and "(with no growth)" in calculated
    assert "(if your 9% a year assumption held)" in calculated
    assert "Category suitability from CA Mode's fixed rules (not advice)" in calculated
    assert "unsuitable: " in calculated and "Not enough information: cautious values were assumed for" in calculated
    assert "Still unknown (ask the user the most important one rather than assuming):" in calculated
    assert "user said: flexibility fixed" in text
    assert text.count("No fund-level data") == 1


def test_a_draft_goal_is_labelled_and_lists_what_is_missing(client, signed_in):
    client.post("/api/goals", json={"name": "Someday home", "kind": "home", "status": "draft"})

    text = retrieve_context(current(client), "How is my home goal?", [], today=TODAY).text

    assert 'Goal "Someday home" (draft): target amount or date not given' in text
    assert 'cannot be calculated; missing' in text and "Which currency is this goal in?" in text


def test_inflation_is_stated_and_the_old_no_inflation_assumption_is_replaced(client, signed_in):
    client.post("/api/goals", json={**GOAL_BODY, "inflationPct": 6})

    text = retrieve_context(current(client), "How is my home goal?", [], today=TODAY).text

    assert "inflation 6% a year (the target is in today's money)" in text
    assert "in today's money becomes INR" in text
    assert "inflation is not added" not in text and "reads its target as today's money" in text


def _assign_holding(client, user_id, goal_id):
    from datetime import datetime, timezone

    from app import snapshots
    from tests.fixtures import indmoney_sample
    snapshots.store_snapshot(user_id, datetime.now(timezone.utc), indmoney_sample.results(), {})
    response = client.put("/api/goals/allocations", json={
        "holdingKey": "MF|120503|-", "goals": [{"goalId": goal_id, "pct": 30}],
    })
    assert response.status_code == 200, response.text


def test_holding_values_reach_portfolio_analyzer_but_the_finance_agent_only_sees_a_count(client, signed_in):
    client.post("/api/goals", json=GOAL_BODY)
    goal_id = client.get("/api/goals").json()["goals"][0]["id"]
    _assign_holding(client, signed_in["id"], goal_id)

    portfolio = retrieve_context(current(client), "How is my home goal?", [], today=TODAY, include_holdings=True).text
    finance = retrieve_context(current(client), "How is my home goal?", [], today=TODAY).text

    assert "Assigned INDmoney holdings (snapshot " in portfolio and "Parag Parikh Flexi Cap Fund 30% = INR 112500" in portfolio
    assert "counted toward the plan: INR 112500" in portfolio
    assert "112500" not in finance and "Parag" not in finance
    assert "1 INDmoney holdings are assigned to this goal; their value is not shown here" in finance
    assert "Portfolio Analyzer can include it" in finance


def test_no_saved_context_leaves_the_prompt_unchanged(client, signed_in):
    assert retrieve_context(current(client), "What is an ETF?", [], today=TODAY, include_holdings=True) is None

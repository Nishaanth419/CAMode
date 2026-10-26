"""Monthly credits for the Individual plan: periods, charging by tokens, top-ups, the chat and the CLI."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app import credits, limits, main, plans
from app.agent import AgentResult
from app.db import connect_db
from scripts import plans as plans_cli
from tests.test_limits import ask, new_chat

IST = timezone(timedelta(hours=5, minutes=30))
MONTHLY = plans.PLANS["individual"].monthly_credits


def at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, tzinfo=IST)


def answer_using(monkeypatch, *token_pairs: tuple[int, int], reply="Diversification spreads risk."):
    """Answer every question with model calls that used these (prompt, completion) tokens."""
    events = [
        {"seq": index, "atMs": index * 100, "kind": "model_result", "attempt": 1, "status": "completed",
         "promptTokens": p, "completionTokens": c}
        for index, (p, c) in enumerate(token_pairs, start=1)
    ]

    async def fake_run_agent(message, *args, **kwargs):
        return AgentResult(reply, [], events=events)

    monkeypatch.setattr(main, "run_agent", fake_run_agent)


def set_used(user_id: str, *, used: int | None = None, top_up: int | None = None) -> None:
    with connect_db() as connection:
        if used is not None:
            connection.execute("UPDATE users SET credits_used = ? WHERE id = ?", (used, user_id))
        if top_up is not None:
            connection.execute("UPDATE users SET topup_credits = ? WHERE id = ?", (top_up, user_id))


# ── Months ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("started", "today", "expected"), [
    (date(2026, 10, 14), date(2026, 10, 14), (date(2026, 10, 14), date(2026, 11, 14))),
    (date(2026, 10, 14), date(2026, 11, 13), (date(2026, 10, 14), date(2026, 11, 14))),
    (date(2026, 10, 14), date(2026, 11, 14), (date(2026, 11, 14), date(2026, 12, 14))),
    (date(2026, 12, 20), date(2027, 1, 5), (date(2026, 12, 20), date(2027, 1, 20))),
    # A plan started on the 31st renews on the last day of shorter months.
    (date(2027, 1, 31), date(2027, 2, 15), (date(2027, 1, 31), date(2027, 2, 28))),
    (date(2027, 1, 31), date(2027, 2, 28), (date(2027, 2, 28), date(2027, 3, 31))),
])
def test_the_month_runs_from_the_day_the_plan_started(started, today, expected):
    assert credits.period_bounds(started, today) == expected


@pytest.mark.parametrize(("tokens", "cost"), [(None, 1), (0, 1), (1, 1), (1_000, 1), (1_001, 2), (6_500, 7)])
def test_an_answer_costs_its_tokens_in_whole_credits_and_at_least_one(tokens, cost):
    assert credits.credits_for_tokens(tokens) == cost


def test_tokens_are_summed_across_model_calls():
    events = [
        {"kind": "model_result", "promptTokens": 1_200, "completionTokens": 300},
        {"kind": "tool_call", "promptTokens": 9_999},
        {"kind": "model_result", "promptTokens": 2_000, "completionTokens": 450},
        {"kind": "model_result", "status": "failed"},
    ]

    assert credits.tokens_used(events) == 3_950
    assert credits.tokens_used([{"kind": "model_result"}]) is None
    assert credits.tokens_used(None) is None


# ── Balance ─────────────────────────────────────────────────────────────────────

def test_free_profiles_have_no_credits(client, signed_in):
    assert client.get("/api/session").json()["usage"]["credits"] is None


def test_the_individual_plan_starts_a_month_of_credits(client, signed_in):
    assert plans.set_plan(signed_in["id"], "individual")

    usage = client.get("/api/session").json()["usage"]

    today = credits.local_today(limits.local_now())
    assert usage["limit"] is None
    assert usage["credits"] == {
        "monthly": MONTHLY, "used": 0, "monthlyRemaining": MONTHLY, "topUp": 0, "remaining": MONTHLY,
        "renewsOn": credits.period_bounds(today, today)[1].isoformat(),
    }


def test_monthly_credits_are_used_before_top_ups_and_never_go_below_zero(signed_in):
    plans.set_plan(signed_in["id"], "individual")
    set_used(signed_in["id"], used=MONTHLY - 5, top_up=10)
    now = limits.local_now()

    after = credits.charge(signed_in["id"], MONTHLY, 8, now=now)
    assert (after.monthly_left, after.top_up) == (0, 7)

    last = credits.charge(signed_in["id"], MONTHLY, 50, now=now)
    assert (last.monthly_left, last.top_up, last.remaining) == (0, 0, 0)
    with pytest.raises(credits.CreditsExhausted):
        credits.ensure_available(signed_in["id"], MONTHLY, now=now)


def test_a_new_month_renews_credits_and_keeps_top_ups(signed_in):
    plans.set_plan(signed_in["id"], "individual")
    started = date(2026, 10, 14)
    with connect_db() as connection:
        credits.start_plan(connection, signed_in["id"], today=started)
    credits.charge(signed_in["id"], MONTHLY, 40, now=at(date(2026, 10, 20)))
    set_used(signed_in["id"], top_up=25)

    same_month = credits.balance(signed_in["id"], MONTHLY, now=at(date(2026, 11, 13)))
    next_month = credits.balance(signed_in["id"], MONTHLY, now=at(date(2026, 11, 14)))

    assert (same_month.used, same_month.top_up) == (40, 25)
    assert (next_month.used, next_month.top_up, next_month.renews_on) == (0, 25, date(2026, 12, 14))


def test_setting_the_same_plan_again_keeps_the_month(signed_in):
    plans.set_plan(signed_in["id"], "individual")
    set_used(signed_in["id"], used=120)

    plans.set_plan(signed_in["id"], "individual")

    assert credits.balance(signed_in["id"], MONTHLY, now=limits.local_now()).used == 120


# ── Chat ────────────────────────────────────────────────────────────────────────

def test_an_answer_is_charged_by_its_tokens(client, signed_in, monkeypatch):
    plans.set_plan(signed_in["id"], "individual")
    answer_using(monkeypatch, (2_500, 400), (1_800, 350))  # 5,050 tokens: 6 credits
    chat_id = new_chat(client)

    answered = ask(client, chat_id)

    assert answered.status_code == 200
    assert answered.headers["X-Credits-Charged"] == "6"
    assert answered.headers["X-Credits-Remaining"] == str(MONTHLY - 6)
    assert client.get("/api/session").json()["usage"]["credits"]["used"] == 6


def test_a_question_without_an_answer_costs_nothing(client, signed_in, monkeypatch):
    plans.set_plan(signed_in["id"], "individual")
    answer_using(monkeypatch, (5_000, 500), reply="")
    chat_id = new_chat(client)

    ask(client, chat_id)

    assert client.get("/api/session").json()["usage"]["credits"]["used"] == 0


def test_used_up_credits_stop_answers_and_point_to_top_ups(client, signed_in, monkeypatch):
    plans.set_plan(signed_in["id"], "individual")
    set_used(signed_in["id"], used=MONTHLY)
    calls = []

    async def fake_run_agent(message, *args, **kwargs):
        calls.append(message)
        return "never sent"

    monkeypatch.setattr(main, "run_agent", fake_run_agent)
    chat_id = new_chat(client)

    blocked = ask(client, chat_id)

    assert blocked.status_code == 402
    body = blocked.json()
    assert body["creditsExhausted"] is True
    assert body["pricingUrl"] == "/pricing"
    assert body["error"].startswith("You've used this month's credits. They renew on ")
    assert calls == []


def test_top_up_credits_answer_once_the_month_is_used(client, signed_in, monkeypatch):
    plans.set_plan(signed_in["id"], "individual")
    set_used(signed_in["id"], used=MONTHLY, top_up=3)
    answer_using(monkeypatch, (900, 100))
    chat_id = new_chat(client)

    answered = ask(client, chat_id)

    assert answered.status_code == 200
    assert answered.headers["X-Credits-Remaining"] == "2"


def test_the_team_plan_has_no_credits(client, signed_in, monkeypatch):
    plans.set_plan(signed_in["id"], "team")
    answer_using(monkeypatch, (50_000, 5_000))
    chat_id = new_chat(client)

    answered = ask(client, chat_id)

    assert "X-Credits-Remaining" not in answered.headers
    assert client.get("/api/session").json()["usage"]["credits"] is None


# ── CLI and pricing page ────────────────────────────────────────────────────────

def test_the_cli_tops_up_profiles_on_a_plan_with_credits(signed_in, capsys):
    assert plans_cli.main(["topup", signed_in["id"], "500"]) == 1
    assert "no credits to top up" in capsys.readouterr().err

    plans.set_plan(signed_in["id"], "individual")
    assert plans_cli.main(["topup", signed_in["id"], "500"]) == 0
    assert plans_cli.main(["topup", signed_in["id"], "0"]) == 1

    assert credits.balance(signed_in["id"], MONTHLY, now=limits.local_now()).top_up == 500


def test_the_pricing_page_states_the_credits(client):
    html = client.get("/pricing").text

    assert f"{MONTHLY:,} credits a month (1 credit = 1,000 tokens)" in html
    assert "top-up credits are kept until used" in html

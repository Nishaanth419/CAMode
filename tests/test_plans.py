"""Plans: Free is capped by DAILY_QUESTION_LIMIT, paid plans are not; the pricing page and the plans CLI."""

from __future__ import annotations

from html import escape

import pytest

from app import accounts, plans, privacy, users
from app.db import connect_db
from scripts import plans as plans_cli
from tests.conftest import sign_up
from tests.test_limits import answer_with, ask, new_chat, stored_count


# ── Plans and the daily cap ─────────────────────────────────────────────────────

def test_new_profiles_are_on_the_free_plan(client, signed_in):
    assert users.get_user(signed_in["id"])["plan"] == "free"
    assert client.get("/api/session").json()["usage"] == {
        "plan": "free", "planName": "Free", "limit": None, "used": None, "remaining": None, "resetsAt": None,
        "credits": None,
    }


def test_session_reports_free_questions_left_today(client, signed_in, monkeypatch):
    monkeypatch.setenv("DAILY_QUESTION_LIMIT", "5")
    answer_with(monkeypatch)
    chat_id = new_chat(client)

    answered = ask(client, chat_id)

    assert answered.headers["X-Questions-Remaining"] == "4"
    usage = client.get("/api/session").json()["usage"]
    assert usage["plan"] == "free"
    assert (usage["limit"], usage["used"], usage["remaining"]) == (5, 1, 4)
    assert usage["resetsAt"].endswith("T00:00+05:30")


def test_free_plan_stops_at_the_limit_and_points_to_pricing(client, signed_in, monkeypatch):
    monkeypatch.setenv("DAILY_QUESTION_LIMIT", "5")
    calls = answer_with(monkeypatch)
    chat_id = new_chat(client)

    for _ in range(5):
        assert ask(client, chat_id).status_code == 200
    blocked = ask(client, chat_id)

    assert blocked.status_code == 429
    assert blocked.json()["dailyLimitReached"] is True
    assert blocked.json()["pricingUrl"] == "/pricing"
    assert len(calls) == 5
    assert client.get("/api/session").json()["usage"]["remaining"] == 0


@pytest.mark.parametrize("plan", ["individual", "team"])
def test_paid_plans_skip_the_daily_cap(client, signed_in, monkeypatch, plan):
    monkeypatch.setenv("DAILY_QUESTION_LIMIT", "5")
    calls = answer_with(monkeypatch)
    assert plans.set_plan(signed_in["id"], plan)
    chat_id = new_chat(client)

    for _ in range(8):
        response = ask(client, chat_id)
        assert response.status_code == 200
        assert "X-Questions-Remaining" not in response.headers

    assert len(calls) == 8
    assert stored_count(signed_in["id"]) == {}
    usage = client.get("/api/session").json()["usage"]
    assert usage["plan"] == plan
    assert usage["limit"] is None


def test_moving_back_to_free_brings_the_cap_back(client, signed_in, monkeypatch):
    monkeypatch.setenv("DAILY_QUESTION_LIMIT", "1")
    answer_with(monkeypatch)
    plans.set_plan(signed_in["id"], "individual")
    chat_id = new_chat(client)
    assert ask(client, chat_id).status_code == 200

    plans.set_plan(signed_in["id"], "free")

    assert ask(client, chat_id).status_code == 200
    assert ask(client, chat_id).status_code == 429


def test_set_plan_refuses_unknown_plans_and_profiles(signed_in):
    with pytest.raises(plans.UnknownPlan):
        plans.set_plan(signed_in["id"], "enterprise")
    assert plans.set_plan("no-such-profile", "team") is False


def test_an_unknown_stored_plan_counts_as_free(signed_in):
    with connect_db() as connection:
        connection.execute("UPDATE users SET plan = 'gold' WHERE id = ?", (signed_in["id"],))

    user = users.get_user(signed_in["id"])
    assert plans.plan_of(user).id == "free"
    assert plans.has_daily_cap(user) is True


def test_export_includes_the_plan(client, signed_in):
    plans.set_plan(signed_in["id"], "individual")

    body = client.post("/api/users/me/export", json={"pin": "2468"}).json()

    assert body["plan"] == "individual"


def test_notice_mentions_the_stored_plan():
    text = " ".join(section["body"] for section in privacy.NOTICE)

    assert "Your profile's plan (Free, Individual or Team) is stored" in text
    assert "applies only to the Free plan" in text


# ── Pricing page ────────────────────────────────────────────────────────────────

def test_pricing_page_lists_every_plan(client):
    response = client.get("/pricing")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert "<title>Pricing · CA Mode</title>" in html
    assert 'name="robots"' not in html
    for plan in plans.PLANS.values():
        assert escape(plan.name) in html
        assert escape(plan.price) in html
    assert "₹599" in html
    assert "excluding GST" in html
    assert "privately hosted" in html
    assert "Refund policy" in html
    assert "Ask within 30 days of paying" in html
    assert "Why a team costs more than one person" in html
    assert "10 questions a day" in html


def test_pricing_page_never_offers_more_than_the_code_allowance(client, monkeypatch):
    monkeypatch.setenv("DAILY_QUESTION_LIMIT", "50")

    assert f"{plans.FREE_DAILY_QUESTIONS} questions a day, starting again at midnight IST" in client.get("/pricing").text


def test_pricing_page_shows_this_copys_free_allowance(client, monkeypatch):
    monkeypatch.setenv("DAILY_QUESTION_LIMIT", "3")

    assert "3 questions a day, starting again at midnight IST" in client.get("/pricing").text


def test_pricing_buttons_email_the_configured_contact(client, monkeypatch):
    monkeypatch.setenv("PRICING_CONTACT_EMAIL", "sales@camode.example")

    html = client.get("/pricing").text

    assert 'href="mailto:sales@camode.example?subject=CA%20Mode%20Individual%20plan">Choose Individual</a>' in html
    assert 'href="mailto:sales@camode.example?subject=CA%20Mode%20Team%20or%20organisation%20plan">Contact us</a>' in html
    assert "Ask whoever runs this copy" not in html


@pytest.mark.parametrize("value", ["", "not-an-email", 'x@y.z"><script>'])
def test_pricing_page_has_no_email_buttons_without_a_valid_contact(client, monkeypatch, value):
    monkeypatch.setenv("PRICING_CONTACT_EMAIL", value)

    html = client.get("/pricing").text

    assert "mailto:" not in html
    assert "<script>" not in html
    assert "Ask whoever runs this copy of CA Mode to change your plan." in html


# ── python -m scripts.plans ─────────────────────────────────────────────────────

def test_cli_sets_a_plan_for_an_id_starting_with_a_dash(monkeypatch):
    # Profile ids are URL-safe base64, so about 1 in 64 starts with "-", which argparse took for an option.
    monkeypatch.setattr(users, "random_token", lambda: "-dash-leading-profile-id")
    profile = users.create_user("Asha", users.hash_pin("2468"))

    assert plans_cli.main(["set", profile["id"], "team"]) == 0

    assert users.get_user(profile["id"])["plan"] == "team"


def test_cli_sets_a_plan_by_name_id_or_email(client, capsys):
    asha = sign_up(client, "Asha")
    ravi = sign_up(client, "Ravi", "1357")
    accounts.link(ravi["id"], accounts.GoogleIdentity(subject="google-ravi", email="Ravi@Example.com"))

    assert plans_cli.main(["set", "asha", "individual"]) == 0
    assert plans_cli.main(["set", "ravi@example.com", "team"]) == 0
    assert plans_cli.main(["set", asha["id"], "free"]) == 0

    assert users.get_user(asha["id"])["plan"] == "free"
    assert users.get_user(ravi["id"])["plan"] == "team"
    capsys.readouterr()
    assert plans_cli.main(["list"]) == 0
    listing = capsys.readouterr().out
    assert "Ravi@Example.com" in listing
    assert "team" in listing


def test_cli_refuses_unknown_and_ambiguous_profiles(capsys):
    first = users.create_user("Asha", users.hash_pin("2468"), unique_name=False)
    users.create_user("Asha", users.hash_pin("1357"), unique_name=False)

    assert plans_cli.main(["set", "Nobody", "team"]) == 1
    assert plans_cli.main(["set", "Asha", "team"]) == 1
    assert "2 profiles are called 'Asha'" in capsys.readouterr().err
    assert users.get_user(first["id"])["plan"] == "free"

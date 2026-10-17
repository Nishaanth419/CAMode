"""Anonymous page-view counts: storage, retention, the counting route and its gates."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app import page_views, privacy
from app.db import connect_db
from tests.conftest import sign_up

TODAY = date(2026, 10, 17)


def stored_rows() -> list[tuple[str, str, int]]:
    with connect_db() as connection:
        return [tuple(row) for row in connection.execute("SELECT day, route, views FROM page_views ORDER BY day, route")]


def test_record_adds_to_one_row_per_day_and_page():
    page_views.record("goals", today=TODAY)
    page_views.record("goals", today=TODAY)
    page_views.record("home", today=TODAY)
    page_views.record("goals", today=TODAY - timedelta(days=1))

    assert stored_rows() == [("2026-10-16", "goals", 1), ("2026-10-17", "goals", 2), ("2026-10-17", "home", 1)]


def test_table_holds_only_day_route_and_count():
    with connect_db() as connection:
        columns = [row["name"] for row in connection.execute("PRAGMA table_info(page_views)")]

    assert columns == ["day", "route", "views"]


def test_record_drops_counts_past_the_retention_period():
    oldest_kept = TODAY - timedelta(days=page_views.RETENTION_DAYS - 1)
    page_views.record("home", today=oldest_kept - timedelta(days=1))
    page_views.record("home", today=oldest_kept)
    page_views.record("chat", today=TODAY)

    assert [row[0] for row in stored_rows()] == [oldest_kept.isoformat(), TODAY.isoformat()]


def test_counts_covers_the_requested_days_and_clamps_the_range():
    for offset in range(5):
        page_views.record("home", today=TODAY - timedelta(days=offset))

    assert [row["day"] for row in page_views.counts(days=2, today=TODAY)] == ["2026-10-17", "2026-10-16"]
    assert len(page_views.counts(days=0, today=TODAY)) == 1
    assert len(page_views.counts(days=10_000, today=TODAY)) == 5


def test_totals_sum_per_page_most_viewed_first():
    rows = [
        {"day": "2026-10-17", "route": "home", "views": 2},
        {"day": "2026-10-17", "route": "goals", "views": 3},
        {"day": "2026-10-16", "route": "home", "views": 4},
        {"day": "2026-10-16", "route": "chat", "views": 3},
    ]

    assert page_views.totals(rows) == {"home": 6, "chat": 3, "goals": 3}


def test_route_counts_a_view_for_a_saving_consenting_profile(client, signed_in):
    response = client.post("/api/page-views", json={"route": "portfolio"})

    assert response.status_code == 200
    assert response.json() == {"counted": True}
    assert [(route, views) for _, route, views in stored_rows()] == [("portfolio", 1)]


def test_route_requires_a_profile(client):
    response = client.post("/api/page-views", json={"route": "home"})

    assert response.status_code == 401
    assert response.json()["profileRequired"] is True
    assert stored_rows() == []


@pytest.mark.parametrize("storage_mode", ["private", None])
def test_route_skips_private_and_non_consenting_profiles(client, storage_mode):
    sign_up(client, storage_mode=storage_mode)

    response = client.post("/api/page-views", json={"route": "home"})

    assert response.status_code == 200
    assert response.json() == {"counted": False}
    assert stored_rows() == []


def test_route_skips_profiles_that_accepted_an_older_notice(client, signed_in, monkeypatch):
    monkeypatch.setattr(privacy, "POLICY_VERSION", "2099-01-01.1")

    assert client.post("/api/page-views", json={"route": "home"}).json() == {"counted": False}
    assert stored_rows() == []


def test_route_can_be_turned_off(client, signed_in, monkeypatch):
    monkeypatch.setenv("PAGE_VIEWS_ENABLED", "false")

    assert client.post("/api/page-views", json={"route": "home"}).json() == {"counted": False}
    assert stored_rows() == []


@pytest.mark.parametrize("body", [
    {"route": "activity/abc123"},
    {"route": "admin"},
    {"route": "home", "userId": "someone"},
    {},
])
def test_route_rejects_anything_but_a_known_page(client, signed_in, body):
    response = client.post("/api/page-views", json=body)

    assert response.status_code == 422
    assert stored_rows() == []


def test_counts_outlive_profile_deletion_because_they_name_no_profile(client, signed_in):
    client.post("/api/page-views", json={"route": "goals"})

    deleted = client.request("DELETE", "/api/users/me", json={"pin": "2468"})

    assert deleted.status_code == 200
    assert [(route, views) for _, route, views in stored_rows()] == [("goals", 1)]


def test_notice_describes_the_counts():
    text = " ".join(section["body"] for section in privacy.NOTICE)

    assert "daily count each time you open one of its pages" in text
    assert "never sent anywhere" in text
    assert "Private mode is never counted" in text

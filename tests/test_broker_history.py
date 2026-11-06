"""Broker holdings history: daily sealed rows, scheduler, combined net worth, privacy paths."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import broker_history, brokers, mcp_client, privacy, snapshots, users
from app.broker_history import BrokerDay
from app.db import connect_db
from app.sealing import ProfileKeys
from tests.fixtures import indmoney_sample as sample
from tests.fixtures import kite_sample as kite
from tests.test_brokers import connect, fake_kite, logged_in  # noqa: F401  (fixtures)

ZERODHA = brokers.BROKERS["zerodha"]
ACME = "INE000A01011"


def rows():
    return ZERODHA.normalise("get_holdings", kite.HOLDINGS) + ZERODHA.normalise("get_mf_holdings", kite.MF_HOLDINGS)


def zday(total="12470.5", *, day="2026-11-05", carried=False, held=None, missing=0):
    return BrokerDay("zerodha", "Zerodha", day, 1, None if total is None else Decimal(total),
                     tuple(rows() if held is None else held), carried, missing)


def im(code, name, value, broker, asset_type="IND_STOCK"):
    return sample._row(code, name, asset_type, 0, value, 1, broker)


INDMONEY_ROWS = [
    im("INFY", "Infosys", 180000, "INDmoney"),
    im("HDFCBANK", "HDFC Bank", 130000, "Zerodha"),       # dropped by the label rule
    im(ACME, "Acme Bank Ltd", 1443, None),                # dropped by the ISIN rule (generic label)
    im("INE000B01012", "Sample IT", 10000, "INDstocks"),  # another named broker: kept
]
NETWORTH = Decimal("1200000")


# ── The pure rule ──────────────────────────────────────────────────────────────

def test_combined_is_networth_minus_dropped_rows_plus_the_broker_total():
    result = broker_history.combine_day(NETWORTH, INDMONEY_ROWS, [zday()])
    assert result["combinedInr"] == float(NETWORTH - Decimal(130000) - Decimal(1443) + Decimal("12470.5"))
    assert result["indmoneyDroppedInr"] == 131443.0 and result["partial"] is False
    assert result["sources"] == [{"id": "zerodha", "name": "Zerodha", "asOf": "2026-11-05", "carried": False, "totalInr": 12470.5}]


def test_no_usable_broker_means_no_combined_value():
    assert broker_history.combine_day(NETWORTH, INDMONEY_ROWS, []) is None
    assert broker_history.combine_day(NETWORTH, INDMONEY_ROWS, [zday(None)]) is None


def test_a_broker_total_that_missed_prices_marks_the_day_partial():
    assert broker_history.combine_day(NETWORTH, INDMONEY_ROWS, [zday(missing=1)])["partial"] is True


def test_days_without_an_indmoney_snapshot_use_brokers_only_and_are_partial():
    result = broker_history.combine_day(None, None, [zday()])
    assert result["combinedInr"] == 12470.5 and result["partial"] is True


def test_an_unreadable_networth_or_dropped_value_is_missing_never_zero():
    assert broker_history.combine_day(None, INDMONEY_ROWS, [zday()])["combinedInr"] is None
    broken = [{**INDMONEY_ROWS[1], "market_value": None}, INDMONEY_ROWS[0]]
    assert broker_history.combine_day(NETWORTH, broken, [zday()])["combinedInr"] is None


def test_a_broker_without_rows_replaces_nothing():
    result = broker_history.combine_day(NETWORTH, INDMONEY_ROWS, [zday("500", held=[])])
    assert result["combinedInr"] == 1200500.0


def test_the_latest_earlier_snapshot_within_three_days_is_carried():
    days = {"2026-11-03": {"zerodha": zday(day="2026-11-03")}}
    found = broker_history.effective_days(days, date(2026, 11, 5))
    assert [(day.day, day.carried) for day in found] == [("2026-11-03", True)]
    assert broker_history.effective_days(days, date(2026, 11, 7)) == []
    exact = {**days, "2026-11-05": {"zerodha": zday(day="2026-11-05")}}
    assert [(d.day, d.carried) for d in broker_history.effective_days(exact, date(2026, 11, 5))] == [("2026-11-05", False)]


def point(day, networth, results):
    return {"date": day, "capturedAt": 1, "networthInr": networth, "_snapshot": None, "_results": results}


def indmoney_results(rows_):
    return {"networth_snapshot": "{}", "networth_holdings:IND_STOCK": json.dumps(sample.holdings(rows_))}


def test_merge_adds_broker_only_days_and_leaves_plain_history_alone():
    points = [point("2026-11-04", 1000.0, indmoney_results(INDMONEY_ROWS))]
    assert broker_history.merge_history(points, {}, date(2026, 11, 1)) is points
    days = {"2026-11-05": {"zerodha": zday()}}
    merged = broker_history.merge_history(points, days, date(2026, 11, 1))
    assert [item["date"] for item in merged] == ["2026-11-04", "2026-11-05"]
    assert merged[0]["combinedInr"] == 1000.0 and merged[0]["combined"]["sources"][0]["id"] == "indmoney"
    assert merged[1]["networthInr"] is None and merged[1]["combinedInr"] == 12470.5 and merged[1]["combined"]["partial"] is True


# ── Storage ────────────────────────────────────────────────────────────────────

def rows_in_db(table="broker_snapshots"):
    with connect_db() as connection:
        return connection.execute(f"SELECT * FROM {table}").fetchall()


def test_a_live_fetch_stores_one_sealed_row_per_day(client, signed_in, fake_kite):
    logged_in(client, fake_kite)
    client.get("/api/brokers/zerodha/holdings?refresh=1")
    client.get("/api/brokers/zerodha/holdings?refresh=1")
    stored = rows_in_db()
    assert len(stored) == 1
    row = stored[0]
    assert (row["user_id"], row["broker"]) == (signed_in["id"], "zerodha") and row["data"].startswith("sealed:v1:")
    assert "ACMEBANK" not in row["data"] and "12470" not in row["data"]
    assert set(row.keys()) == {"user_id", "broker", "local_date", "captured_at", "data"}
    exported = client.post("/api/users/me/export", json={"pin": "2468"}).json()
    history = exported["brokers"][0]["history"]
    assert len(history) == 1 and history[0]["totalInr"] == 12470.5 and len(history[0]["holdings"]) == 3


def test_private_mode_stores_no_history(client, fake_kite):
    from tests.conftest import sign_up
    sign_up(client, storage_mode="private")
    logged_in(client, fake_kite)
    assert client.get("/api/brokers/zerodha/holdings?refresh=1").json()["available"] is True
    assert rows_in_db() == []


def test_a_failed_history_write_never_loses_the_holdings(client, signed_in, fake_kite, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("disk full")
    monkeypatch.setattr(broker_history, "store_day", boom)
    logged_in(client, fake_kite)
    assert client.get("/api/brokers/zerodha/holdings?refresh=1").json()["available"] is True


def test_retention_prunes_old_broker_days(client, signed_in):
    keys = ProfileKeys(public_key=users.get_user(signed_in["id"])["publicKey"])
    old = int((datetime.now(timezone.utc) - timedelta(days=400)).timestamp() * 1000)
    broker_history.store_day(signed_in["id"], "zerodha", [], 1.0, keys, old)
    broker_history.store_day(signed_in["id"], "zerodha", [], 2.0, keys)
    assert len(rows_in_db()) == 1


def history_in_db(client, signed_in, fake_kite):
    logged_in(client, fake_kite)
    client.get("/api/brokers/zerodha/holdings?refresh=1")
    assert len(rows_in_db()) == 1


def test_disconnecting_a_broker_deletes_its_history(client, signed_in, fake_kite):
    history_in_db(client, signed_in, fake_kite)
    client.post("/api/brokers/zerodha/disconnect")
    assert rows_in_db() == []


def test_clearing_history_and_private_mode_delete_it(client, signed_in, fake_kite):
    history_in_db(client, signed_in, fake_kite)
    assert client.request("DELETE", "/api/users/me/history", json={"pin": "2468"}).status_code in (200, 204)
    assert rows_in_db() == []
    history_in_db(client, signed_in, fake_kite)
    client.post("/api/privacy/consent", json={"policyVersion": privacy.POLICY_VERSION, "storageMode": "private"})
    assert rows_in_db() == []


def test_indmoney_disconnect_keeps_it_and_profile_deletion_removes_it(client, signed_in, fake_kite):
    history_in_db(client, signed_in, fake_kite)
    client.post("/auth/indmoney/disconnect")
    assert len(rows_in_db()) == 1
    users.delete_user(signed_in["id"])
    assert rows_in_db() == []


# ── History API and the agent tool ─────────────────────────────────────────────

def seed_days(user_id):
    """INDmoney snapshots yesterday and today; Zerodha only today."""
    keys = ProfileKeys(public_key=users.get_user(user_id)["publicKey"])
    now = datetime.now(timezone.utc)
    results = indmoney_results(INDMONEY_ROWS)
    results["networth_snapshot"] = json.dumps({**sample.SNAPSHOT, "total_networth": 1200000})
    for offset in (1, 0):
        snapshots.store_snapshot(user_id, now - timedelta(days=offset), results, {})
    broker_history.store_day(user_id, "zerodha", rows(), 12470.5, keys, int(now.timestamp() * 1000))
    return float(NETWORTH - Decimal(131443) + Decimal("12470.5"))


def test_history_route_adds_combined_only_with_broker_history(client, signed_in):
    plain = client.get("/api/snapshots?days=7").json()
    assert plain["includesBrokers"] is False and all("combinedInr" not in point for point in plain["history"])
    expected = seed_days(signed_in["id"])
    body = client.get("/api/snapshots?days=7").json()
    assert body["includesBrokers"] is True
    yesterday, today = body["history"][-2:]
    assert today["networthInr"] == 1200000.0 and today["combinedInr"] == expected
    # Yesterday has no Zerodha snapshot within 3 days earlier, so it is INDmoney only.
    assert yesterday["combinedInr"] == 1200000.0


def test_history_tool_returns_combined_with_a_note(client, signed_in):
    seed_days(signed_in["id"])
    tool = next(tool for tool in snapshots.snapshot_tools(signed_in["id"], _unlocked_keys(client, signed_in))
                if tool.name == "get_networth_history")
    payload = json.loads(asyncio.run(tool.handler({"days": 7})))
    assert "combinedNote" in payload and payload["points"][-1]["combinedInr"] is not None
    assert len(json.dumps(payload)) < 5500


def test_history_tool_has_no_combined_fields_without_brokers(client, signed_in):
    snapshots.store_snapshot(signed_in["id"], datetime.now(timezone.utc), sample.results(), {})
    tool = next(tool for tool in snapshots.snapshot_tools(signed_in["id"], _unlocked_keys(client, signed_in))
                if tool.name == "get_networth_history")
    payload = json.loads(asyncio.run(tool.handler({"days": 7})))
    assert "combinedNote" not in payload and "combinedInr" not in payload["points"][0]


def _unlocked_keys(client, signed_in):
    from app import unlock
    private_key = next(key for owner, key in unlock._unlocked.values() if owner == signed_in["id"])
    return ProfileKeys(public_key=users.get_user(signed_in["id"])["publicKey"], private_key=private_key)


# ── Scheduler ──────────────────────────────────────────────────────────────────

def test_due_connections_follow_schedule_status_consent_and_backoff(client, signed_in, fake_kite):
    broker_history._attempted.clear()
    logged_in(client, fake_kite)
    now = snapshots.local_now()
    assert broker_history.due_connections(now) == [(signed_in["id"], "zerodha")]
    brokers.set_status(signed_in["id"], "zerodha", "needs_login")
    assert broker_history.due_connections(now) == []
    brokers.set_status(signed_in["id"], "zerodha", "connected")
    broker_history._attempted[(signed_in["id"], "zerodha")] = int(now.timestamp() * 1000)
    assert broker_history.due_connections(now) == []
    assert broker_history.due_connections(now + timedelta(minutes=61)) == [(signed_in["id"], "zerodha")]
    broker_history._attempted.clear()
    # Captured since the latest scheduled time: not due until the next one.
    keys = ProfileKeys(public_key=users.get_user(signed_in["id"])["publicKey"])
    broker_history.store_day(signed_in["id"], "zerodha", [], 1.0, keys, int(now.timestamp() * 1000))
    assert broker_history.due_connections(now) == []
    assert broker_history.due_connections(now + timedelta(days=1)) == [(signed_in["id"], "zerodha")]


def test_private_and_unconsented_profiles_are_never_due(client, fake_kite):
    from tests.conftest import sign_up
    broker_history._attempted.clear()
    private = sign_up(client, storage_mode="private")
    brokers.save_connection(private["id"], "zerodha", session_id="s", protocol="2025-03-26", status="connected")
    client.post("/api/session/logout")
    unconsented = sign_up(client, name="Ben", pin="1357", storage_mode=None)
    brokers.save_connection(unconsented["id"], "zerodha", session_id="s", protocol="2025-03-26", status="connected")
    assert broker_history.due_connections(snapshots.local_now()) == []


def test_the_scheduler_seals_with_the_public_key_and_the_data_reads_back(client, signed_in, fake_kite):
    broker_history._attempted.clear()
    logged_in(client, fake_kite)
    assert asyncio.run(broker_history.refresh_due()) == 1
    assert len(rows_in_db()) == 1 and rows_in_db()[0]["data"].startswith("sealed:v1:")
    exported = client.post("/api/users/me/export", json={"pin": "2468"}).json()
    assert exported["brokers"][0]["history"][0]["totalInr"] == 12470.5


def test_a_failed_scheduled_fetch_marks_status_and_is_not_retried_within_the_hour(client, signed_in, fake_kite):
    broker_history._attempted.clear()
    logged_in(client, fake_kite)
    fake_kite.logged_in = False
    assert asyncio.run(broker_history.refresh_due()) == 0
    assert brokers.get_connection(signed_in["id"], "zerodha")["status"] == "needs_login"
    assert rows_in_db() == []
    calls = len(fake_kite.calls)
    brokers.set_status(signed_in["id"], "zerodha", "connected")
    assert asyncio.run(broker_history.refresh_due()) == 0
    assert len(fake_kite.calls) == calls  # within the hour: no new call


def test_an_unreachable_broker_is_not_a_crash(client, signed_in, fake_kite):
    broker_history._attempted.clear()
    logged_in(client, fake_kite)
    fake_kite.error = mcp_client.McpHttpError("boom", 503)
    assert asyncio.run(broker_history.refresh_due()) == 0
    assert brokers.get_connection(signed_in["id"], "zerodha")["status"] == "connected"

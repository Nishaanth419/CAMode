from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import db, privacy, snapshots, users
from app.sealing import ProfileKeys
from fixtures import indmoney_sample

IST = snapshots.SNAPSHOT_TIMEZONE


def make_user(name: str = "Asha", *, connected: bool = True, storage_mode: str | None = "device") -> str:
    """Create a profile, optionally connected to INDmoney and consenting with ``storage_mode``."""
    user = users.create_user(name, users.hash_pin("2468"))
    user_id = user["id"]
    if storage_mode is not None:
        privacy.record_consent(user, privacy.ConsentRequest(
            policyVersion=privacy.POLICY_VERSION, storageMode=storage_mode,
        ))
    if connected:
        users.save_indmoney(user_id, {"accessToken": "token", "refreshToken": "refresh",
                                      "expiresAt": users.now_ms() + 3_600_000,
                                      "clientId": "client", "clientSecret": "secret"})
    return user_id


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=IST)


@pytest.mark.parametrize(("text", "expected"), [
    ('{"net_worth": 4200000}', 4200000.0),
    ('{"data": {"networth": {"total": "₹12,34,567.50"}}}', 1234567.5),
    ('{"summary": {"total_value": 99}, "assets": [{"current_value": 5}]}', 99.0),
    ('{"totalNetWorth": "Rs. 1,000"}', 1000.0),
    ("Your net worth is ₹ 5,00,000 as of today", 500000.0),
    ('{"networth": true}', None),
    ("nothing useful here", None),
    (None, None),
])
def test_extract_networth_inr_handles_json_and_text(text, expected):
    assert snapshots.extract_networth_inr(text) == expected


def test_latest_scheduled_time_uses_ist():
    before_nine = datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc)  # 08:30 IST
    after_nine = datetime(2026, 10, 13, 3, 31, tzinfo=timezone.utc)  # 09:01 IST

    assert snapshots.latest_scheduled_time(before_nine) == at(12, 9)
    assert snapshots.latest_scheduled_time(after_nine) == at(13, 9)


def fake_indmoney(monkeypatch, *, failing: set[str] = frozenset(), offered: tuple[str, ...] = snapshots.SNAPSHOT_TOOL_NAMES):
    calls = []

    async def fake_token(user_id, *, force_refresh=False):
        return "token"

    async def fake_fetch(url, token):
        tools = [{"name": name, "description": name} for name in offered]
        return {"tools": [*tools, {"name": "unsupported_write_tool"}], "protocolVersion": "v", "sessionId": "sid"}

    async def fake_call(url, token, protocol, session_id, name, arguments, *, client=None):
        calls.append((name, arguments, client is not None))
        if name in failing or f"{name}:{arguments.get('asset_type')}" in failing:
            raise RuntimeError(f"{name} timed out")
        if name == "networth_snapshot":
            return json.dumps(indmoney_sample.SNAPSHOT)
        rows = {"IND_STOCK": indmoney_sample.IND_STOCK, "MF": indmoney_sample.MF}.get(arguments["asset_type"], [])
        return json.dumps(indmoney_sample.holdings(rows))

    monkeypatch.setattr(users, "user_access_token", fake_token)
    monkeypatch.setattr(snapshots, "fetch_mcp_session", fake_fetch)
    monkeypatch.setattr(snapshots, "call_mcp_tool", fake_call)
    return calls


def test_capture_snapshot_stores_row_and_caches_tools(monkeypatch):
    user_id = make_user()
    calls = fake_indmoney(monkeypatch)

    result = asyncio.run(snapshots.capture_snapshot(user_id, mcp_url="https://mcp", now=at(13, 9, 2)))

    assert result["asOfDate"] == "2026-10-13"
    assert result["networthInr"] == 1200000.0
    # The summary first, then holdings for each asset type it shows as held.
    assert calls[0][:2] == ("networth_snapshot", {})
    assert sorted(arguments["asset_type"] for name, arguments, _ in calls[1:]) == ["EPF", "IND_STOCK", "MF", "US_STOCK"]
    assert all(name == "networth_holdings" for name, _, _ in calls[1:])
    assert all(shared for _, _, shared in calls)
    stored = snapshots.latest_snapshot(user_id)
    assert json.loads(stored["results"]["networth_holdings:IND_STOCK"])["holdings"][0]["investment"] == "Infosys"
    assert stored["errors"] == {}
    cached = users.get_user(user_id)["mcpTools"]
    assert [tool["name"] for tool in cached] == list(snapshots.SNAPSHOT_TOOL_NAMES)


def test_capture_snapshot_keeps_partial_results(monkeypatch):
    user_id = make_user()
    fake_indmoney(monkeypatch, failing={"networth_holdings:MF"})

    result = asyncio.run(snapshots.capture_snapshot(user_id, mcp_url="https://mcp", now=at(13, 9)))

    assert result["networthInr"] == 1200000.0
    assert result["errors"] == {"networth_holdings:MF": "networth_holdings timed out"}

    fake_indmoney(monkeypatch, offered=("networth_snapshot",))
    result = asyncio.run(snapshots.capture_snapshot(user_id, mcp_url="https://mcp", now=at(13, 10)))
    assert result["errors"]["networth_holdings:IND_STOCK"] == "INDmoney did not offer this tool."

    fake_indmoney(monkeypatch, failing=set(snapshots.SNAPSHOT_TOOL_NAMES))
    with pytest.raises(snapshots.SnapshotError, match="no portfolio data"):
        asyncio.run(snapshots.capture_snapshot(user_id, mcp_url="https://mcp", now=at(14, 9)))


def test_due_users_respect_schedule_and_backoff():
    never_captured = make_user("Asha")
    captured_today = make_user("Ravi")
    not_connected = make_user("Meera", connected=False)
    snapshots.store_snapshot(captured_today, at(13, 9, 1), {"networth_snapshot": "{}"}, {})

    assert set(snapshots.due_user_ids(at(13, 9, 30))) == {never_captured}
    assert not_connected not in snapshots.due_user_ids(at(13, 9, 30))
    # Ravi is due again only after the next 09:00.
    assert set(snapshots.due_user_ids(at(14, 8, 59))) == {never_captured}
    assert set(snapshots.due_user_ids(at(14, 9, 0))) == {never_captured, captured_today}

    # A failed attempt at 09:05 waits an hour before retrying.
    attempt_ms = int(at(13, 9, 5).timestamp() * 1000)
    with db.connect_db() as connection:
        connection.execute("UPDATE users SET snapshot_attempted_at = ? WHERE id = ?", (attempt_ms, never_captured))
    assert never_captured not in snapshots.due_user_ids(at(13, 9, 30))
    assert never_captured in snapshots.due_user_ids(at(13, 10, 6))


def test_run_scheduler_refreshes_due_users_until_stopped(monkeypatch):
    refreshed = []

    async def fake_refresh_due_users(*, mcp_url, now=None):
        refreshed.append(now)
        if len(refreshed) == 2:
            stop.set()
        return 0

    monkeypatch.setattr(snapshots, "refresh_due_users", fake_refresh_due_users)
    stop = asyncio.Event()

    async def run():
        await asyncio.wait_for(
            snapshots.run_scheduler(stop, mcp_url="https://mcp", poll_seconds=0.01, clock=lambda: at(13, 9)),
            timeout=2,
        )

    asyncio.run(run())
    assert refreshed == [at(13, 9), at(13, 9)]


def test_snapshot_local_tools_return_latest_and_history():
    user_id = make_user()
    today = datetime.now(IST).replace(hour=9, minute=0, second=0, microsecond=0)
    for days_ago, value in ((3, 100), (1, 110), (0, 125)):
        snapshots.store_snapshot(
            user_id, today - timedelta(days=days_ago),
            {**indmoney_sample.results(), "networth_snapshot": json.dumps({**indmoney_sample.SNAPSHOT, "total_networth": value})},
            {},
        )
    tools = {tool.name: tool for tool in snapshots.snapshot_tools(user_id, ProfileKeys())}

    latest = json.loads(asyncio.run(tools["get_portfolio_snapshot"].handler({})))
    earlier = json.loads(asyncio.run(tools["get_portfolio_snapshot"].handler({"date": (today - timedelta(days=2)).date().isoformat()})))
    invalid = json.loads(asyncio.run(tools["get_portfolio_snapshot"].handler({"date": "13/10/2026"})))
    history = json.loads(asyncio.run(tools["get_networth_history"].handler({"days": 7})))

    assert latest["figures"]["totals"]["net_worth_inr"] == 125
    assert latest["figures"]["pnl"]["pnl_inr"] == 140000.0 and "raw" not in latest
    assert "all_holdings" not in latest["figures"]
    assert latest["capturedAt"].endswith("09:00+05:30")
    assert earlier["figures"]["totals"]["net_worth_inr"] == 100
    every = json.loads(asyncio.run(tools["get_portfolio_snapshot"].handler({"include_all_holdings": True})))
    assert len(every["figures"]["all_holdings"]) == 6
    assert "error" in invalid
    assert [point["networthInr"] for point in history["points"]] == [100, 110, 125]
    assert "networthSnapshot" not in history["points"][0]


def test_networth_history_falls_back_to_raw_text_and_samples():
    user_id = make_user()
    today = datetime.now(IST).replace(hour=9, minute=0, second=0, microsecond=0)
    for days_ago in range(60):
        snapshots.store_snapshot(user_id, today - timedelta(days=days_ago), {"networth_snapshot": "Total assets look healthy"}, {})
    tool = next(tool for tool in snapshots.snapshot_tools(user_id, ProfileKeys()) if tool.name == "get_networth_history")

    history = json.loads(asyncio.run(tool.handler({"days": 90})))

    assert len(history["points"]) == snapshots.MAX_HISTORY_POINTS
    assert history["points"][-1]["date"] == today.date().isoformat()
    assert history["points"][0]["networthSnapshot"] == "Total assets look healthy"


def test_snapshot_retention_prunes_old_rows(monkeypatch):
    monkeypatch.setattr(snapshots, "SNAPSHOT_RETENTION_DAYS", 2)
    user_id = make_user()
    for day in (10, 11, 12, 13):
        snapshots.store_snapshot(user_id, at(day, 9), {"networth_snapshot": "{}"}, {})

    with db.connect_db() as connection:
        dates = [row[0] for row in connection.execute(
            "SELECT as_of_date FROM portfolio_snapshots WHERE user_id = ? ORDER BY as_of_date", (user_id,))]
    assert dates == ["2026-10-11", "2026-10-12", "2026-10-13"]


def test_profile_context_lists_fields_and_snapshot_time():
    user_id = make_user()
    users.update_profile(user_id, users.ProfileFields(riskAppetite="low", monthlyInvestmentInr=5000), ProfileKeys())
    snapshots.store_snapshot(user_id, at(13, 9), {"networth_snapshot": "{}"}, {})
    fields = users.profile_fields(users.get_user(user_id), ProfileKeys())

    text = snapshots.profile_context(fields, snapshots.latest_snapshot(user_id))

    assert "Risk appetite: low" in text
    assert "Monthly investment budget (INR): 5000" in text
    assert "captured 2026-10-13T09:00+05:30" in text
    assert "No saved portfolio snapshot" in snapshots.profile_context(fields, None)


def test_timezone_falls_back_to_fixed_ist_without_tz_data(monkeypatch):
    def missing(name):
        raise snapshots.ZoneInfoNotFoundError(name)

    monkeypatch.setattr(snapshots, "ZoneInfo", missing)
    fallback = snapshots._load_timezone("Asia/Kolkata")

    assert fallback.utcoffset(None) == timedelta(hours=5, minutes=30)
    assert snapshots._load_timezone("Mars/Olympus").utcoffset(None) == timedelta(hours=5, minutes=30)

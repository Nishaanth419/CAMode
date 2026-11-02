"""Build frontend/demo/data.json: what the in-browser demo (frontend/js/demo.js) serves instead of the API.

A made-up investor is created in a throwaway database: four months of daily INDmoney snapshots, goals, a
recorded decision and a few chats answered by the real agent. INDmoney is replaced by those snapshots, so
nothing here touches a real account. Every GET the app makes is then recorded as the server answered it.

    OLLAMA_MODEL=ca-mode python -m scripts.build_demo_data

The chats need the configured model (Ollama by default) and take a few minutes.
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

os.environ["APP_DATA_DIR"] = tempfile.mkdtemp(prefix="camode-demo-")
os.environ["APP_MASTER_KEY"] = Fernet.generate_key().decode()
os.environ["SNAPSHOT_SCHEDULER_ENABLED"] = "false"
os.environ["BACKUP_DIR"] = os.path.join(os.environ["APP_DATA_DIR"], "backups")
for name in ("DAILY_QUESTION_LIMIT", "RATE_LIMITS_ENABLED", "TURNSTILE_SITE_KEY", "GOOGLE_CLIENT_ID", "ADMIN_EMAILS"):
    os.environ.pop(name, None)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import agent, main, mcp_client, portfolio_math, privacy, snapshots, users  # noqa: E402
from app.portfolio_math import HOLDINGS_PREFIX  # noqa: E402

OUTPUT = REPOSITORY_ROOT / "frontend" / "demo" / "data.json"
DAYS = 120
PIN = "2468"

# (code, name, asset type, broker, units on day 0, unit price on day 0, cost per unit, yearly drift, wobble phase)
HOLDINGS = [
    ("INFY", "Infosys", "IND_STOCK", "INDmoney", 60, 1520.0, 1390.0, 0.10, 0.0),
    ("TCS", "Tata Consultancy Services", "IND_STOCK", "INDmoney", 25, 3980.0, 4120.0, -0.04, 1.3),
    ("HDFCBANK", "HDFC Bank", "IND_STOCK", "Zerodha", 90, 1640.0, 0.0, 0.12, 2.1),
    ("RELIANCE", "Reliance Industries", "IND_STOCK", "INDmoney", 40, 2860.0, 2410.0, 0.07, 3.4),
    ("120503", "Parag Parikh Flexi Cap Fund", "MF", None, 3900.0, 78.4, 61.2, 0.15, 0.6),
    ("118989", "HDFC Index Fund Nifty 50", "MF", None, 1450.0, 212.0, 180.5, 0.11, 2.7),
    ("119551", "Axis Liquid Fund", "MF", None, 52.0, 2810.0, 2760.0, 0.065, 0.0),
    ("AAPL", "Apple", "US_STOCK", "INDmoney", 6, 19500.0, 15800.0, 0.14, 1.9),
]
USD_INR = 84.0
EPF = (210000, 238000)
LIABILITIES = 45000
# Each month's SIP buys this much more of the flexi cap fund.
SIP_INR = 10000


def price(base: float, drift: float, phase: float, day: int) -> float:
    return base * (1 + drift * day / 365 + 0.025 * math.sin(day / 11 + phase) + 0.01 * math.sin(day / 3.1 + phase))


def holdings_on(day: int, start: date) -> dict[str, list[dict[str, Any]]]:
    sips = sum(1 for d in range(day + 1) if (start + timedelta(days=d)).day == 5)
    rows: dict[str, list[dict[str, Any]]] = {}
    for code, name, asset_type, broker, units, base, cost, drift, phase in HOLDINGS:
        unit_price = price(base, drift, phase, day)
        previous = price(base, drift, phase, max(day - 1, 0))
        invested = units * cost
        if code == "120503":
            units += sips * SIP_INR / unit_price
            invested += sips * SIP_INR
        value = round(units * unit_price)
        row = {
            "investment_code": code, "investment": name, "asset_type": "STOCK" if asset_type == "IND_STOCK" else asset_type, "assetclass_l2": "Debt" if code == "119551" else "Equity",
            "invested_amount": round(invested), "market_value": value, "holding_percent": 0, "total_pnl": value - round(invested),
            "pnl_per": round((value - invested) / invested * 100, 2) if invested else 0, "total_units": round(units, 3),
            "unit_price": round(unit_price, 2), "broker": broker, "market_cap": "Large Cap" if asset_type == "IND_STOCK" else None,
            "one_day_change": round(units * (unit_price - previous)),
            "one_day_change_percentage": round((unit_price - previous) / previous * 100, 2),
        }
        if asset_type == "US_STOCK":
            row.update(invested_value_usd=round(invested / USD_INR, 2), current_value_usd=round(value / USD_INR, 2),
                       one_day_change_usd=round(row["one_day_change"] / USD_INR, 2))
        rows.setdefault(asset_type, []).append(row)
    return rows


def results_on(day: int, start: date) -> dict[str, str]:
    rows_by_type = holdings_on(day, start)
    investments = []
    for asset_type, rows in rows_by_type.items():
        invested = sum(r["invested_amount"] for r in rows)
        current = sum(r["market_value"] for r in rows)
        investments.append({"asset_type": "STOCK" if asset_type == "IND_STOCK" else asset_type, "invested_value": invested,
                            "current_value": current, "return": current - invested,
                            "return_percentage": round((current - invested) / invested * 100, 2) if invested else 0})
    investments.append({"asset_type": "EPF", "invested_value": EPF[0], "current_value": EPF[1] + day * 60,
                        "return": EPF[1] + day * 60 - EPF[0], "return_percentage": 0})
    current = sum(item["current_value"] for item in investments)
    summary = {
        "total_invested": sum(item["invested_value"] for item in investments), "total_current_value": current,
        "total_networth": current - LIABILITIES, "investments": investments,
        "liabilities": {"total_loan_balance": LIABILITIES, "total_credit_card_due": 0, "total": LIABILITIES, "loans": [], "credit_cards": []},
    }
    results = {"networth_snapshot": json.dumps(summary)}
    for asset_type, rows in rows_by_type.items():
        summary_row = {"total_value": sum(r["market_value"] for r in rows), "invested": sum(r["invested_amount"] for r in rows),
                       "one_day_change": sum(r["one_day_change"] for r in rows), "one_day_change_percentage": 0}
        if asset_type == "US_STOCK":
            summary_row.update(total_value_usd=sum(r["current_value_usd"] for r in rows),
                               invested_usd=sum(r["invested_value_usd"] for r in rows),
                               one_day_change_usd=sum(r["one_day_change_usd"] for r in rows))
        results[HOLDINGS_PREFIX + asset_type] = json.dumps({"holdings": rows, "asset_summary": summary_row, "source_status": {"holdings": "ok"}})
    return results


# ── INDmoney stand-in: today's made-up snapshot answers the portfolio tools ──────────────────────────
START = snapshots.local_now().date() - timedelta(days=DAYS - 1)
TODAY_RESULTS = results_on(DAYS - 1, START)
TOOLS = json.loads((REPOSITORY_ROOT / "tests" / "fixtures" / "indmoney_tools.json").read_text())


async def fake_session(url: str, token: str) -> dict[str, Any]:
    return {"tools": TOOLS, "sessionId": "demo", "protocolVersion": mcp_client.DEFAULT_PROTOCOL_VERSION}


async def fake_call(url, token, protocol, session_id, name, arguments, *, client=None):
    if name == "networth_snapshot":
        return TODAY_RESULTS["networth_snapshot"]
    if name == "networth_holdings":
        key = HOLDINGS_PREFIX + (portfolio_math.canonical_asset_type(arguments.get("asset_type")) or "")
        return TODAY_RESULTS.get(key) or json.dumps({"holdings": [], "asset_summary": {"total_value": 0, "invested": 0}})
    raise mcp_client.McpToolError(f'MCP tool "{name}" failed: not available in the demo.')


async def fake_token(user_id: str, *, force_refresh: bool = False) -> str:
    return "demo-token"


for module in (agent, snapshots, main):
    if hasattr(module, "fetch_mcp_session"):
        module.fetch_mcp_session = fake_session
    if hasattr(module, "call_mcp_tool"):
        module.call_mcp_tool = fake_call
users.user_access_token = fake_token

CHATS = [
    ("finance", "I want ₹15 lakh for a house down payment in 5 years. How much should I invest each month if I expect 12% a year?"),
    ("finance", "Should I keep my emergency fund in a liquid fund or a fixed deposit?"),
    ("portfolio", "What is my net worth, and how is it split across asset types?"),
    ("portfolio", "Which of my mutual funds is the largest, and how much have I gained on it?"),
]


ATTEMPTS = 4


def presentable(answer: str) -> bool:
    """A recorded answer is shown to every visitor, so keep only ones the app's own checks raised nothing on."""
    text = answer.strip()
    return len(text) > 200 and not text.startswith(("{", "[")) and "⚠️" not in text


def ok(response, what: str) -> Any:
    if response.status_code >= 400:
        raise SystemExit(f"{what} failed: {response.status_code} {response.text[:500]}")
    return response.json() if "json" in response.headers.get("content-type", "") else response.text


def main_build() -> None:
    recorded: dict[str, Any] = {}

    def record(client: TestClient, path: str) -> Any:
        response = client.get(path)
        body = response.json() if "json" in response.headers.get("content-type", "") else response.text
        recorded[f"GET {path}"] = {"status": response.status_code, "body": body}
        return body

    with TestClient(main.app, follow_redirects=False) as client:
        client.get("/api/session")
        ok(client.post("/api/users", json={"displayName": "Demo", "pin": PIN}), "Create profile")
        ok(client.post("/api/privacy/consent", json={"policyVersion": privacy.POLICY_VERSION, "storageMode": "device"}), "Consent")
        user_id = main.signed_in_user_id(client.cookies.get(main.SESSION_COOKIE))
        ok(client.put("/api/profile", json={"riskAppetite": "moderate", "investmentHorizonYears": 10, "ageBand": "25-34",
                                            "monthlyInvestmentInr": 25000, "primaryGoal": "Buy a home"}), "Profile")

        for day in range(DAYS):
            captured = datetime.combine(START + timedelta(days=day), time(9, 0), tzinfo=snapshots.SNAPSHOT_TIMEZONE)
            snapshots.store_snapshot(user_id, captured, results_on(day, START), {})

        in_years = lambda years: (date.today().replace(day=1) + timedelta(days=365 * years)).isoformat()  # noqa: E731
        home = ok(client.post("/api/goals", json={
            "name": "Home down payment", "kind": "home", "currency": "INR", "targetAmount": 1500000, "targetDate": in_years(5),
            "savedAmount": 250000, "monthlyContribution": 15000, "assumedReturnPct": 11, "flexibility": "some",
            "liquidityNeed": "low", "riskTolerance": "medium", "lossCapacity": "medium",
        }), "Home goal")
        emergency = ok(client.post("/api/goals", json={
            "name": "Emergency fund", "kind": "emergency", "currency": "INR", "targetAmount": 300000, "targetDate": in_years(1),
            "savedAmount": 120000, "monthlyContribution": 10000, "assumedReturnPct": 6.5, "flexibility": "fixed",
            "liquidityNeed": "high", "riskTolerance": "low", "lossCapacity": "low",
        }), "Emergency goal")
        ok(client.post("/api/goals", json={
            "name": "Europe trip", "kind": "travel", "currency": "INR", "targetAmount": 400000, "targetDate": in_years(2),
            "savedAmount": 60000, "monthlyContribution": 8000, "assumedReturnPct": 7, "flexibility": "flexible",
        }), "Trip goal")
        allocations = ok(client.get("/api/goals/allocations"), "Allocations")
        for holding in allocations.get("holdings", []):
            name = holding.get("name", "")
            if "Flexi Cap" in name:
                ok(client.put("/api/goals/allocations", json={"holdingKey": holding["key"], "goals": [{"goalId": home["id"], "pct": 60}], "generalWealthPct": 40}), "Allocate")
            elif "Liquid" in name:
                ok(client.put("/api/goals/allocations", json={"holdingKey": holding["key"], "goals": [{"goalId": emergency["id"], "pct": 100}], "generalWealthPct": 0}), "Allocate")
        ok(client.post("/api/decisions", json={"text": "Keep six months of expenses in a liquid fund before adding to stocks.", "goalId": emergency["id"]}), "Decision")

        for agent_type, question in CHATS:
            for attempt in range(1, ATTEMPTS + 1):
                chat = ok(client.post("/api/chats", json={"agentType": agent_type}), "Chat")
                print(f"Asking ({agent_type}, attempt {attempt}): {question}", flush=True)
                response = client.post("/api/chat", json={"chatId": chat["id"], "message": question, "agentType": agent_type})
                if response.status_code < 400 and presentable(response.text):
                    break
                print(f"  not kept: {response.status_code} {response.text[:200]!r}", flush=True)
                client.delete(f"/api/chats/{chat['id']}")
                if run_id := response.headers.get("X-Agent-Run"):
                    client.delete(f"/api/runs/{run_id}")

        session = record(client, "/api/session")
        session["profiles"] = []
        for path in ("/api/providers", "/api/profile", "/api/privacy", "/api/portfolio", "/api/goals", "/api/goals/allocations",
                     "/api/decisions", "/api/context", "/api/cas", "/api/backups", "/api/tools"):
            record(client, path)
        for days in (7, 30, 90, 180, 365):
            record(client, f"/api/snapshots?days={days}")
        for goal in record(client, "/api/goals").get("goals", []):
            record(client, f"/api/goals/{goal['id']}")
        for chat in record(client, "/api/chats").get("chats", []):
            record(client, f"/api/chats/{chat['id']}")
        runs = record(client, "/api/runs?limit=50")
        record(client, "/api/investigations?limit=50")
        for run in (runs or {}).get("runs", []):
            record(client, f"/api/runs/{run['id']}")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(recorded, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"Wrote {len(recorded)} responses to {OUTPUT.relative_to(REPOSITORY_ROOT)}")


if __name__ == "__main__":
    main_build()

"""The inspection scripts print layouts and counts, never values."""

from __future__ import annotations

import json

import pytest

from app import brokers, mcp_client, users
from scripts import inspect_broker_shapes as broker_script
from scripts import inspect_indmoney_shapes as indmoney_script
from tests.fixtures import kite_sample as kite
from tests.test_brokers import FakeKite

SECRETS = [
    "ACMEBANK", "SAMPLEIT", "INE000A01011", "INE000B01012", "Sample Flexi Cap", "INF000X01AB1", "1234/56",
    "Sample Person", "sample@example.invalid", "AB1234", "kitemcp-secret-1", "120.25", "2100.5", "2026-11-05",
]


@pytest.fixture
def fake(monkeypatch):
    fake = FakeKite()
    fake.logged_in = True
    monkeypatch.setattr(brokers.mcp_client, "fetch_mcp_session", fake.fetch_mcp_session)
    monkeypatch.setattr(brokers.mcp_client, "call_mcp_tool", fake.call_mcp_tool)
    return fake


def connect(user_id, status="connected"):
    brokers.save_connection(user_id, "zerodha", session_id="kitemcp-secret-1", protocol="2025-03-26", status=status)


def test_broker_script_prints_layouts_without_any_value(signed_in, fake, capsys):
    connect(signed_in["id"])
    assert broker_script.main(["--broker", "zerodha"]) == 0
    out = capsys.readouterr().out
    assert "=== get_profile" in out and "=== get_holdings" in out and "=== get_mf_holdings" in out
    assert '"tradingsymbol": "string"' in out and '"quantity": "number"' in out and '"list_of": 3' in out
    assert '"last_price_date": "date string 9999-99-99"' in out
    for secret in SECRETS:
        assert secret not in out
    assert fake.called == ["get_profile", "get_holdings", "get_mf_holdings"]


def test_broker_script_never_calls_a_write_tool(signed_in, fake, capsys):
    connect(signed_in["id"])
    broker_script.main([])
    assert not set(fake.called) & brokers.WRITE_TOOLS and set(fake.called) <= set(brokers.BROKERS["zerodha"].read_tools)


def test_broker_script_masks_keys_that_look_like_data():
    shaped = broker_script.layout({"data": {"ACMEBANK": {"qty": 1}, "SAMPLEIT": {"qty": 2}}, "total": "1,200.50"})
    text = json.dumps(shaped)
    assert "ACMEBANK" not in text and "SAMPLEIT" not in text and shaped["total"] == "numeric string"


def test_broker_script_masks_codes_symbol_maps_and_words_in_dates():
    sample = {
        "data": {"zd1234": {"qty": 5, "avg": 10.5}, "infy": {"qty": 1, "avg": 2.0}, "tcs": {"qty": 3, "avg": 4.0}},
        "asOf": "12 Reliance 2026", "when": "12 Decathlon 2026", "d1": "2026-11-06", "d2": "06 Nov 2026",
        "return_1yr": 3.2, "t1_quantity": 1, "zd1234": 7,
    }
    printed = json.dumps(broker_script.layout(sample))
    for value in ("zd1234", "infy", "tcs", "Reliance", "Decathlon", "10.5"):
        assert value not in printed
    assert '"return_1yr": "number"' in printed and '"t1_quantity": "number"' in printed
    assert "date string 9999-99-99" in printed and "date string 99 Nov 9999" in printed


def test_broker_script_does_not_print_non_json_text():
    text = broker_script.describe("Hello Sample Person\nsecret line")
    assert "Sample" not in text and "secret" not in text and "2 lines" in text


@pytest.mark.parametrize("failure", ["status", "expired", "gone"])
def test_an_ended_login_says_reconnect_and_exits_non_zero(signed_in, fake, capsys, failure):
    connect(signed_in["id"], status="needs_login" if failure == "status" else "connected")
    if failure == "expired":
        fake.logged_in = False
    if failure == "gone":
        fake.gone = True
    # The `=` form: a random profile id can start with "-", which argparse would read as another option.
    assert broker_script.main([f"--profile={signed_in['id']}"]) == 1
    out = capsys.readouterr().out
    assert "reconnect Zerodha first" in out
    for secret in SECRETS:
        assert secret not in out


def test_an_error_prints_only_its_type(signed_in, fake, capsys):
    connect(signed_in["id"])
    fake.error = RuntimeError("secret ACMEBANK detail")
    assert broker_script.main([]) == 1
    out = capsys.readouterr().out
    assert "error: RuntimeError" in out and "ACMEBANK" not in out


def test_no_connection_exits_non_zero(signed_in, capsys):
    assert broker_script.main([]) == 1
    assert "No profile has a broker connection" in capsys.readouterr().out


def test_indmoney_codes_counts_patterns_only(capsys):
    rows = [
        {"investment_code": "INE002A01018", "investment": "Reliance Industries", "asset_type": "IND_STOCK",
         "broker": "Zerodha", "market_value": 987654.5},
        {"investment_code": "ine009a01021", "investment": "Infosys Limited", "asset_type": "IND_STOCK",
         "broker": "Zerodha", "market_value": 111},
        {"investment_code": "HDFCBANK", "investment": "HDFC Bank", "asset_type": "IND_STOCK", "broker": "INDmoney"},
        {"investment_code": "120503", "investment": "Parag Parikh Flexi Cap", "asset_type": "MF", "broker": None},
        {"investment_code": "", "investment": "Unnamed", "asset_type": "MF", "broker": None},
    ]
    counts = indmoney_script.code_counts({"holdings": rows})
    assert counts["IND_STOCK | Zerodha"] == {"rows": 2, "isin_shaped": 2, "patterns": {"AAA999A99999": 2}}
    assert counts["IND_STOCK | INDmoney"] == {"rows": 1, "isin_shaped": 0, "patterns": {"AAAAAAAA": 1}}
    assert counts["MF | None"]["patterns"] == {"999999": 1, "(none)": 1}
    indmoney_script.print_codes({"result": {"holdings": rows}})
    out = capsys.readouterr().out
    for secret in ["INE002A01018", "INE009A01021", "HDFCBANK", "Reliance", "Infosys", "Parag", "987654", "120503"]:
        assert secret not in out
    assert "isin_shaped=2" in out and "AAA999A99999" in out

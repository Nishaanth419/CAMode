from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest

from app import analysis_tools as at
from tests.fixtures import indmoney_market as market

NOW = datetime(2026, 10, 27, 18, 0)


def store_with_chain() -> tuple[at.ResultStore, str]:
    store = at.ResultStore()
    return store, store.add("get_indian_stocks_option_chain", {"ind_key": "INDI00012"}, json.dumps(market.chain()))


def test_large_results_reach_the_model_as_an_outline_with_short_lists_in_full():
    store, rid = store_with_chain()
    view = json.loads(store.view(rid))
    assert view["resultId"] == rid and view["chars"] > at.COMPACT_OVER_CHARS
    outline = view["outline"]
    assert outline["expiry_list"] == market.EXPIRIES  # short scalar list kept in full
    assert outline["option_chain_data"]["listOf"] == 61 and "put_data" in outline["option_chain_data"]["firstItem"]
    assert outline["entity_details"]["live_price"] == market.SPOT
    assert len(store.view(rid)) < 4000


def test_short_results_are_shown_unchanged():
    store = at.ResultStore()
    rid = store.add("lookup_ind_keys", {}, json.dumps(market.lookup_nifty()))
    assert store.view(rid) == json.dumps(market.lookup_nifty())


def test_inspect_filters_projects_and_sorts_rows():
    store, rid = store_with_chain()
    result = at.inspect(store, {"result_id": rid, "path": "option_chain_data",
                                "where": [{"field": "put_data.delta", "op": "between", "value": [-0.2, -0.1]}],
                                "fields": ["strike_price", "put_data.delta", "put_data.buy_price", "put_data.derivative_id"],
                                "sort_by": "strike_price"})
    rows = result["returned"]
    assert result["rows"] == 61 and result["matched"] == len(rows) > 0
    assert all(-0.2 <= row["put_data.delta"] <= -0.1 for row in rows)
    assert [row["strike_price"] for row in rows] == sorted(row["strike_price"] for row in rows)
    assert set(rows[0]) == {"strike_price", "put_data.delta", "put_data.buy_price", "put_data.derivative_id"}


def test_inspect_never_invents_absent_fields():
    store, rid = store_with_chain()
    first = at.inspect(store, {"result_id": rid, "path": "option_chain_data.0", "fields": ["call_data.delta", "call_data.oi"]})
    assert first["value"] == {"call_data.oi": 120000}  # the deepest call has no delta at all
    unpriced = at.inspect(store, {"result_id": rid, "path": "option_chain_data",
                                  "where": [{"field": "call_data.buy_price", "op": "lt", "value": 1e9}]})
    assert unpriced["matched"] == 60  # the row without a quote does not match a numeric condition


@pytest.mark.parametrize("arguments, error", [
    ({"result_id": "r9"}, "no result 'r9'"),
    ({"result_id": "r1", "path": "nope"}, "nothing at path"),
    ({"result_id": "r1", "path": "option_chain_data", "where": [{"field": "strike_price", "op": "between", "value": 1}]}, None),
])
def test_inspect_errors(arguments, error):
    store, _ = store_with_chain()
    if error is None:
        with pytest.raises(ValueError, match="between"):
            at.inspect(store, arguments)
    else:
        assert error in at.inspect(store, arguments)["error"]


def test_calculate_is_exact_and_names_carry_forward():
    result = at.calculate({"expressions": [
        {"name": "credit", "expr": "89.075 + 81.9 - 54.7 - 52.95"}, {"name": "loss", "expr": "250 - credit"},
        {"name": "loss_rs", "expr": "round(loss * 65, 2)"}, {"name": "lots", "expr": "floor(5000000 * 1.0 / (22659.25 * 65))"},
        {"name": "share", "expr": "375000 / 1250000 * 100"}, {"name": "biggest", "expr": "max([30, 15.2, 14.4])"}]})
    assert result["results"] == {"credit": 63.325, "loss": 186.675, "loss_rs": 12133.88, "lots": 3.0, "share": 30.0, "biggest": 30.0}


@pytest.mark.parametrize("expr, error", [
    ("1/0", "division by zero"), ("__import__('os')", "unsupported"), ("undefined + 1", "unknown name"),
    ("50,00,000 *", "not a valid expression"), ("2 ** 0.5", "whole numbers"),
])
def test_calculate_refuses_with_the_reason(expr, error):
    assert error in at.calculate({"expressions": [{"name": "x", "expr": expr}]})["error"]


def test_price_levels_reads_stored_candles_by_ind_key():
    store = at.ResultStore()
    store.add("get_indian_stocks_ohlc", {"ind_key": "INDS1", "interval": "1day", "lookback": "1y"}, json.dumps(market.breakout_series("INDS1")))
    result = at.price_levels(store, {"ind_keys": ["INDS1", "INDS9"], "lookback_sessions": 14}, NOW)
    first, missing = result["levels"]["r1"], result["levels"]["ind_key:INDS9"]
    assert first["indKey"] == "INDS1" and first["priorHigh"] == 1020.1 and first["closeAbovePriorHigh"]
    assert "fetch the candles first" in missing["error"]


def test_tool_specs_run_and_return_json():
    store, rid = store_with_chain()
    specs = {spec.name: spec for spec in at.tools(store, lambda: NOW)}
    assert set(specs) == at.ANALYSIS_TOOL_NAMES
    out = json.loads(asyncio.run(specs["option_strategy"].handler({"legs": [
        {"type": "put", "strike": 22650, "premium": 429.55, "quantity": 1},
        {"type": "put", "strike": 21550, "premium": 81.15, "quantity": -1}], "lot_size": 65})))
    assert out["netPremiumPoints"] == 348.4 and out["netPremiumRupees"] == 22646.0
    assert "error" in json.loads(asyncio.run(specs["inspect_result"].handler({"result_id": rid, "path": "option_chain_data",
                                                                               "where": [{"field": "strike_price", "op": "gt", "value": "abc"}]})))


def test_date_facts_marks_the_last_listed_date_of_each_month():
    from datetime import date

    result = at.date_facts({"dates": market.EXPIRIES}, date(2026, 10, 28))
    assert result["firstLastInMonthAfterToday"] == "2026-11-23"  # the holiday-moved November monthly, not the 3 Nov weekly
    nov3 = result["dates"][0]
    assert (nov3["weekday"], nov3["daysFromToday"], nov3["weekdaysFromToday"], nov3["lastListedInItsMonth"]) == ("Tuesday", 6, 4, False)
    assert "unreadable" in at.date_facts({"dates": ["soon"]}, date(2026, 10, 28))["error"]


def test_conditions_can_be_written_as_text():
    store, rid = store_with_chain()
    result = at.inspect(store, {"result_id": rid, "path": "option_chain_data",
                                "where": ["put_data.delta between -0.2 -0.1", "strike_price < 21700"], "fields": ["strike_price"]})
    assert result["matched"] > 0 and all(row["strike_price"] < 21700 for row in result["returned"])
    with pytest.raises(ValueError, match="cannot read"):
        at.inspect(store, {"result_id": rid, "path": "option_chain_data", "where": ["strike_price"]})

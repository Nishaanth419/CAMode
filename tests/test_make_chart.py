"""make_chart: charts built from stored results in code, ref blocks grounded, typed chart numbers dropped."""

from __future__ import annotations

import asyncio
import json

import pytest

from app import analysis_tools as at
from app import charts, portfolio_math
from tests.fixtures import indmoney_market as market
from tests.fixtures import indmoney_sample as sample


def snapshot_store() -> tuple[at.ResultStore, str]:
    store = at.ResultStore()
    figures = portfolio_math.compute_metrics(sample.results())
    payload = {"available": True, "asOfDate": "2026-10-26", "figures": figures}
    return store, store.add("get_portfolio_snapshot", {}, json.dumps(payload))


def test_bar_chart_reads_the_numbers_from_the_result():
    store, rid = snapshot_store()
    out = at.make_chart(store, {"type": "bar", "title": "Top holdings", "result_id": rid, "path": "figures.top_holdings",
                                "label_field": "name", "value_field": "current_value_inr", "unit": "₹", "limit": 2})
    assert out["chartId"] == "c1" and out["notShown"] > 0
    spec = store.charts["c1"]
    assert spec == {"type": "bar", "title": "Top holdings", "unit": "₹", "data": [
        {"label": "Parag Parikh Flexi Cap Fund", "value": 375000.0}, {"label": "Tata Consultancy Services", "value": 190000.0}]}
    assert '{"ref": "c1"}' in out["placement"]


def test_a_path_tail_resolves_to_the_one_real_path():
    """The local model asked for top_holdings six times when the list is figures.top_holdings."""
    store, rid = snapshot_store()
    out = at.make_chart(store, {"type": "donut", "result_id": rid, "path": "by_asset_type",
                                "label_field": "asset_type", "value_field": "share_of_portfolio_pct"})
    assert out["path"] == "figures.by_asset_type"
    inspected = at.inspect(store, {"result_id": rid, "path": "top_holdings", "fields": ["name"], "limit": 1})
    assert inspected["resolvedPath"] == "figures.top_holdings"
    assert inspected["returned"] == [{"name": "Parag Parikh Flexi Cap Fund"}]


def test_an_unknown_path_lists_the_paths_that_hold_lists():
    store, rid = snapshot_store()
    out = at.inspect(store, {"result_id": rid, "path": "all_holdings"})
    assert out["error"] == "nothing at path 'all_holdings'"
    assert "figures.top_holdings" in out["pathsWithLists"] and "figures.by_asset_type" in out["pathsWithLists"]
    assert "outline" not in out


def test_inspect_names_fields_no_row_has():
    store, rid = snapshot_store()
    out = at.inspect(store, {"result_id": rid, "path": "figures.top_holdings", "fields": ["name", "value_inr"]})
    assert out["fieldsInNoRow"] == ["value_inr"] and "current_value_inr" in out["fieldsOfFirstRow"]


def test_rest_is_grouped_exactly_into_other():
    store, rid = snapshot_store()
    rows = portfolio_math.compute_metrics(sample.results())["top_holdings"]
    out = at.make_chart(store, {"type": "donut", "result_id": rid, "path": "figures.top_holdings", "label_field": "name",
                                "value_field": "current_value_inr", "limit": 1, "group_rest": True})
    data = store.charts[out["chartId"]]["data"]
    assert data[-1] == {"label": f"Other ({len(rows) - 1})", "value": sum(row["current_value_inr"] for row in rows[1:])}
    assert "notShown" not in out


def test_line_chart_over_dated_points_skips_missing_values():
    store = at.ResultStore()
    rid = store.add("get_networth_history", {}, json.dumps(market.history(
        [("2026-10-01", 1000000), ("2026-10-02", None), ("2026-10-03", 1100000)])))
    out = at.make_chart(store, {"type": "line", "result_id": rid, "path": "points", "label_field": "date",
                                "value_fields": ["networthInr"], "series_names": ["Net worth"]})
    series = store.charts[out["chartId"]]["series"]
    assert series == [{"name": "Net worth", "data": [{"label": "2026-10-01", "value": 1000000.0},
                                                     {"label": "2026-10-03", "value": 1100000.0}]}]
    assert out["skippedRows"] == 1


@pytest.mark.parametrize(("arguments", "error"), [
    ({"type": "scatter"}, "type must be one of"),
    ({"type": "bar", "result_id": "r9"}, "no result 'r9'"),
    ({"type": "bar", "result_id": "r1", "path": "figures.top_holdings", "label_field": "name", "value_field": "value_inr"},
     "no row has the field 'value_inr'"),
    ({"type": "bar", "result_id": "r1", "path": "figures.top_holdings", "label_field": "name",
      "value_fields": ["current_value_inr", "invested_inr"]}, "one value_field"),
    ({"type": "donut", "result_id": "r1", "path": "figures.top_holdings", "label_field": "name", "value_field": "pnl_inr"},
     "negative"),
    ({"type": "bar", "result_id": "r1", "path": "figures.totals", "label_field": "name", "value_field": "x"}, "no row has"),
])
def test_make_chart_errors(arguments, error):
    store, _ = snapshot_store()
    assert error in at.make_chart(store, arguments)["error"]


def test_make_chart_is_offered_with_the_analysis_tools():
    store, rid = snapshot_store()
    specs = {spec.name: spec for spec in at.tools(store, lambda: None)}
    out = json.loads(asyncio.run(specs["make_chart"].handler({"type": "bar", "result_id": rid, "path": "figures.by_broker",
                                                               "label_field": "broker", "value_field": "current_value_inr"})))
    assert out["chartId"] == "c1"


def test_calculate_accepts_the_local_models_aliases_and_name_case():
    out = at.calculate({"expressions": [{"id": "top5", "value": 40.15}, {"id": "rest", "value": "100.0-Top5"}]})
    assert out["results"] == {"top5": 40.15, "rest": 59.85}


# ── Grounding the answer ──────────────────────────────────────────────────────

SPEC = {"type": "bar", "title": "Top", "unit": "₹", "data": [{"label": "A", "value": 1.0}]}


def test_ref_blocks_become_the_chart():
    content, result = charts.ground_tool_charts('Here:\n```chart\n{"ref": "c1"}\n```\nDone.', {"c1": SPEC}, strict=True)
    assert '"data":[{"label":"A","value":1.0}]' in content and "ref" not in content
    assert result == {"check": "charts", "outcome": "passed", "detail": "1 made, 1 placed, 0 appended"}


def test_typed_chart_numbers_are_dropped_and_unplaced_charts_appended():
    typed = '```chart\n{"type":"bar","data":[{"label":"A","value":99}]}\n```'
    content, result = charts.ground_tool_charts(f"Text\n{typed}", {"c1": SPEC}, strict=True)
    assert "99" not in content and charts.HAND_WRITTEN_NOTE in content
    assert content.rstrip().endswith("```") and '"value":1.0' in content
    assert result["outcome"] == "warning" and "1 handWritten" in result["detail"]


def test_unknown_ref_and_candlesticks():
    candles = '```chart\n{"type":"candlestick","ohlc":[]}\n```'
    content, result = charts.ground_tool_charts(f'```chart\n{{"ref": "c7"}}\n```\n{candles}', {}, strict=True)
    assert charts.UNKNOWN_REF_NOTE in content and candles in content and result["outcome"] == "warning"


def test_previous_loop_keeps_model_charts():
    typed = '```chart\n{"type":"bar","data":[{"label":"A","value":99}]}\n```'
    assert charts.ground_tool_charts(typed, {}, strict=False) == (typed, None)
    assert charts.hand_written_charts(typed) == 1 and charts.hand_written_charts('```chart\n{"ref":"c1"}\n```') == 0


@pytest.mark.parametrize(("message", "asks"), [
    ("Show that as a chart.", True),
    ("Plot my net worth over the last year", True),
    ("Visualise my allocation as a donut", True),
    ("Show a candlestick chart of Reliance", False),  # candles come from the OHLC tool
    ("What is my net worth?", False),
    ("Which holdings dragged my portfolio down?", False),
    ("Show my top 10 holdings in a bar graph", True),
    ("Draw a pie of my asset types", True),
    ("Can you give me a chart of my mutual funds?", True),
    ("For each, get the daily chart over the last 14 days.", False),  # price data, not a drawn chart
    ("Show me the price chart of TCS", False),
])
def test_asks_for_chart(message, asks):
    assert charts.asks_for_chart(message) is asks

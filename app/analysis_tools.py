"""General tools the agent uses for any multi-step analysis: reading large results, and exact maths.

None of these knows about a particular question; the agent decides what to fetch, what to look up in
it and what to compute.

- A ``ResultStore`` keeps every INDmoney result of one answer in full. A result over
  ``COMPACT_OVER_CHARS`` reaches the model as an outline (its keys, list lengths, short scalar lists
  such as an expiry list, a sample row) with a ``resultId``, because a 60-strike option chain or a year of
  candles would fill a local model's context. ``inspect_result`` then filters a list in it by field
  conditions and returns only the fields asked for, so the model reads exactly the rows it needs.
- ``calculate`` evaluates arithmetic exactly (``Decimal``), with named steps that later steps can use.
- ``option_strategy`` analyses any set of option legs (``options_math``).
- ``price_levels`` measures prior-session highs and lows over stored daily candles (``price_action``).
- ``date_facts`` gives calendar facts about dates (weekday, days and weekdays from today, the last listed date of
  each month), so the model does not reason about calendars itself.
- ``make_chart`` builds a bar, donut or line chart from a list in a stored result: the model names the result,
  the path and the fields, and the numbers are read here, never typed by the model. The answer carries only a
  ``{"ref": "c1"}`` chart block, which ``charts.ground_tool_charts`` replaces with the chart.
- A path that is not found but is the unique tail of a real path (``top_holdings`` for ``figures.top_holdings``)
  is resolved to it; otherwise the error lists the paths that hold lists.

Figures these tools return are tool results, so the answer's number check accepts them, and the model
never has to do arithmetic itself.
"""

from __future__ import annotations

import ast
import json
import operator
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, DivisionByZero, InvalidOperation
from typing import Any, Final

from . import options_math, price_action

COMPACT_OVER_CHARS: Final = 2500
MAX_ROWS: Final = 40
MAX_FIELDS: Final = 12
MAX_EXPRESSIONS: Final = 30
SCALAR_LIST_LIMIT: Final = 24
CHART_TYPES: Final = ("bar", "donut", "line")
MAX_CHART_ROWS: Final = 24  # the frontend draws at most 24 bars or slices
MAX_LINE_POINTS: Final = 100  # and 100 points per line
MAX_SERIES: Final = 4
ANALYSIS_TOOL_NAMES: Final = frozenset({"inspect_result", "calculate", "option_strategy", "price_levels", "date_facts",
                                        "make_chart"})


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[str]]


def _loads(text: str) -> Any:
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    if isinstance(value, dict) and isinstance(value.get("result"), str):
        try:
            return json.loads(value["result"])
        except ValueError:
            return value
    return value


def _outline(value: Any, depth: int = 0) -> Any:
    """Keys, list lengths and the shape of a list's first item; short scalar lists and scalars near the top in full."""
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        return {key: _outline(item, depth + 1) for key, item in list(value.items())[:40]}
    if isinstance(value, list):
        if all(not isinstance(item, (dict, list)) for item in value) and len(value) <= SCALAR_LIST_LIMIT:
            return value
        return {"listOf": len(value), "firstItem": _outline(value[0], depth + 1) if value else None}
    if isinstance(value, str) and len(value) > 80:
        return value[:80] + "…"
    return value if depth <= 3 else type(value).__name__


@dataclass(slots=True)
class ResultStore:
    """Every tool result of one answer, by id (``r1``, ``r2``...), in full."""

    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    charts: dict[str, dict[str, Any]] = field(default_factory=dict)  # make_chart's charts by id (c1, c2...)

    def add(self, tool: str, arguments: dict[str, Any], text: str) -> str:
        result_id = f"r{len(self.results) + 1}"
        self.results[result_id] = {"tool": tool, "arguments": arguments, "text": text, "value": _loads(text)}
        return result_id

    def view(self, result_id: str) -> str:
        """What the model reads for a result: the result itself when short, else an outline to query."""
        entry = self.results[result_id]
        if len(entry["text"]) <= COMPACT_OVER_CHARS:
            # The id goes with it: without one the model believed a short result could not be charted or inspected.
            return f"{entry['text']}\n[resultId: {result_id}]"
        if entry["value"] is None:
            return json.dumps({"resultId": result_id, "chars": len(entry["text"]), "note": "not JSON; first part shown",
                               "text": entry["text"][:COMPACT_OVER_CHARS]}, ensure_ascii=False)
        return json.dumps({
            "resultId": result_id, "tool": entry["tool"], "chars": len(entry["text"]),
            "outline": _outline(entry["value"]),
            "howToRead": (f"This result is too large to show in full. Call inspect_result with result_id \"{result_id}\", "
                          "the path of a list in the outline, conditions on its fields and the fields you need."),
        }, ensure_ascii=False, separators=(",", ":"), default=str)

    def listing(self) -> str:
        """``r1 (networth_holdings), r2 (...)``: what the model can pass as result_id."""
        return ", ".join(f"{rid} ({entry['tool']})" for rid, entry in self.results.items()) or "none"

    def texts(self) -> list[str]:
        return [entry["text"] for entry in self.results.values()]


def _get(value: Any, path: str) -> Any:
    for part in [p for p in path.split(".") if p]:
        if isinstance(value, list) and part.lstrip("-").isdigit():
            index = int(part)
            value = value[index] if -len(value) <= index < len(value) else None
        elif isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def _paths(value: Any, prefix: str = "", depth: int = 0) -> list[tuple[str, Any]]:
    """Every dotted path to a dict key (not inside lists), with its value."""
    if depth > 6 or not isinstance(value, dict):
        return []
    found = []
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        found.append((path, item))
        found.extend(_paths(item, path, depth + 1))
    return found


def resolve_path(value: Any, path: str) -> tuple[Any, str, dict[str, Any]]:
    """The value at ``path``, the path actually used, and an error to return when there is nothing there.

    A path that is the tail of exactly one real path is taken as that path; otherwise the error lists the
    paths of lists in the result, so the model can pick one instead of repeating the same call.
    """
    if not path:
        return value, path, {}
    target = _get(value, path)
    if target is not None:
        return target, path, {}
    candidates = [(full, item) for full, item in _paths(value) if full.endswith("." + path) and item is not None]
    if len(candidates) == 1:
        return candidates[0][1], candidates[0][0], {}
    lists = [full for full, item in _paths(value) if isinstance(item, list)]
    last = path.rsplit(".", 1)[-1].lower()
    lists.sort(key=lambda full: last not in full.lower())
    error: dict[str, Any] = {"error": f"nothing at path {path!r}"}
    if candidates:
        error["didYouMean"] = [full for full, _ in candidates][:8]
    elif lists:
        error["pathsWithLists"] = lists[:12]
    else:
        error["outline"] = _outline(value)
    return None, path, error


_MISSING: Final = object()


def _field(row: Any, path: str) -> Any:
    value = row
    for part in [p for p in path.split(".") if p]:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return _MISSING
    return value


def _number(value: Any) -> Decimal | None:
    return options_math.dec(value) if not isinstance(value, (dict, list)) else None


_SYMBOLS: Final = {">": "gt", ">=": "ge", "<": "lt", "<=": "le", "==": "eq", "=": "eq", "!=": "ne"}


def _condition(raw: Any) -> dict[str, Any]:
    """A condition as {field, op, value}: given as such, or as text such as "put_data.delta between -0.2 -0.1"."""
    if isinstance(raw, dict):
        return raw
    parts = str(raw).split()
    if len(parts) < 2:
        raise ValueError(f"cannot read the condition {raw!r}; write e.g. \"strike_price >= 22000\"")
    field, op, rest = parts[0], _SYMBOLS.get(parts[1], parts[1]), parts[2:]
    if op == "between":
        if len(rest) != 2:
            raise ValueError(f"between needs two values in {raw!r}")
        return {"field": field, "op": op, "value": rest}
    return {"field": field, "op": op, **({"value": " ".join(rest)} if rest else {})}


def _matches(row: Any, condition: dict[str, Any]) -> bool:
    value = _field(row, str(condition.get("field", "")))
    op = str(condition.get("op", "eq"))
    if op == "exists":
        return value is not _MISSING and value is not None
    if value is _MISSING or value is None:
        return False
    target = condition.get("value")
    if op == "contains":
        return str(target).lower() in str(value).lower()
    if op in ("eq", "ne"):
        a, b = _number(value), _number(target)
        same = a == b if a is not None and b is not None else str(value).lower() == str(target).lower()
        return same if op == "eq" else not same
    number = _number(value)
    if number is None:
        return False
    if op == "between":
        if not isinstance(target, list) or len(target) != 2:
            raise ValueError("between needs a value of [low, high]")
        low, high = sorted(options_math.dec(item) or Decimal(0) for item in target)
        return low <= number <= high
    bound = options_math.dec(target)
    if bound is None:
        raise ValueError(f"{op} needs a numeric value")
    return {"lt": number < bound, "le": number <= bound, "gt": number > bound, "ge": number >= bound}.get(op, False)


def _project(row: Any, fields: list[str]) -> Any:
    if not fields:
        return row
    projected = {}
    for path in fields:
        value = _field(row, path)
        if value is not _MISSING:
            projected[path] = value
    return projected


def inspect(store: ResultStore, arguments: dict[str, Any]) -> dict[str, Any]:
    result_id = str(arguments.get("result_id", ""))
    if result_id not in store.results:
        return {"error": f"no result {result_id!r}; results so far: {', '.join(store.results) or 'none'}"}
    value = store.results[result_id]["value"]
    if value is None:
        return {"error": f"{result_id} is not JSON"}
    asked = str(arguments.get("path") or "")
    target, path, error = resolve_path(value, asked)
    if error:
        return error
    resolved = {"resolvedPath": path} if path != asked else {}
    fields = [str(item) for item in (arguments.get("fields") or [])][:MAX_FIELDS]
    if not isinstance(target, list):
        return {"resultId": result_id, "path": path, **resolved, "value": _project(target, fields)}
    conditions = arguments.get("where") or []
    if isinstance(conditions, (str, dict)):
        conditions = [conditions]
    if not isinstance(conditions, list):
        return {"error": "where must be a list of conditions such as \"put_data.delta between -0.2 -0.1\""}
    parsed = [_condition(item) for item in conditions]
    rows = [row for row in target if all(_matches(row, c) for c in parsed)]
    sort_by = arguments.get("sort_by")
    if sort_by:
        keyed = [(row, _number(_field(row, str(sort_by)))) for row in rows]
        rows = [row for row, key in sorted((pair for pair in keyed if pair[1] is not None), key=lambda pair: pair[1],
                                           reverse=bool(arguments.get("descending")))]
    limit = min(int(arguments.get("limit") or 20), MAX_ROWS)
    absent = [name for name in fields if rows and all(_field(row, name) is _MISSING for row in rows)]
    return {"resultId": result_id, "path": path, **resolved, "rows": len(target), "matched": len(rows),
            "returned": [_project(row, fields) for row in rows[:limit]],
            **({"fieldsInNoRow": absent, "fieldsOfFirstRow": _row_fields(rows[0])} if absent else {}),
            **({"note": f"{len(rows) - limit} more matched; narrow the conditions or raise limit"} if len(rows) > limit else {}),
            "absentFields": "a field missing from a row was not reported by the tool (not quoted), never zero"}


def _row_fields(row: Any) -> list[str]:
    return [str(key) for key in row][:30] if isinstance(row, dict) else []


# ── make_chart ────────────────────────────────────────────────────────────────

def _chart_rows(target: Any) -> list[Any] | None:
    """A list to chart: the list itself, or a mapping of names to numbers as ``{"key", "value"}`` rows."""
    if isinstance(target, list):
        return target
    if isinstance(target, dict) and target and all(not isinstance(item, (dict, list)) for item in target.values()):
        return [{"key": key, "value": item} for key, item in target.items()]
    return None


def make_chart(store: ResultStore, arguments: dict[str, Any]) -> dict[str, Any]:
    """A chart spec from a stored list, registered under a chart id; the numbers never pass through the model."""
    kind = str(arguments.get("type") or "").lower()
    if kind == "pie":
        kind = "donut"
    if kind not in CHART_TYPES:
        return {"error": f"type must be one of {', '.join(CHART_TYPES)}"}
    result_id = str(arguments.get("result_id", ""))
    if result_id not in store.results:
        return {"error": f"no result {result_id!r}; results so far: {', '.join(store.results) or 'none'}"}
    value = store.results[result_id]["value"]
    if value is None:
        return {"error": f"{result_id} is not JSON"}
    target, path, error = resolve_path(value, str(arguments.get("path") or ""))
    if error:
        return error
    rows = _chart_rows(target)
    if rows is None or not rows:
        return {"error": f"{path or 'the result'} is not a list of rows", "pathsWithLists":
                [full for full, item in _paths(value) if isinstance(item, list)][:12]}
    label_field = str(arguments.get("label_field") or "")
    raw_values = arguments.get("value_fields") or arguments.get("value_field") or []
    value_fields = [str(item) for item in ([raw_values] if isinstance(raw_values, str) else raw_values) if item]
    if not label_field or not value_fields:
        return {"error": "give label_field and value_field (value_fields for several lines)", "fieldsOfFirstRow": _row_fields(rows[0])}
    if kind != "line" and len(value_fields) > 1:
        return {"error": f"a {kind} chart shows one value_field; use a line chart for several series"}
    value_fields = value_fields[:MAX_SERIES]
    for name in (label_field, *value_fields):
        if all(_field(row, name) is _MISSING for row in rows):
            return {"error": f"no row has the field {name!r}", "fieldsOfFirstRow": _row_fields(rows[0])}
    conditions = arguments.get("where") or []
    if isinstance(conditions, (str, dict)):
        conditions = [conditions]
    parsed = [_condition(item) for item in conditions] if isinstance(conditions, list) else []
    rows = [row for row in rows if all(_matches(row, c) for c in parsed)]

    def label(row: Any) -> str | None:
        text = _field(row, label_field)
        return None if text is _MISSING or text is None or str(text).strip() == "" else str(text).strip()[:80]

    def number(row: Any, name: str) -> Decimal | None:
        found = _field(row, name)
        return None if found is _MISSING else _number(found)

    title = str(arguments.get("title") or "")[:120]
    unit = str(arguments.get("unit") or "")[:16]
    skipped = 0
    if kind == "line":
        names = [str(item)[:60] for item in arguments.get("series_names") or []]
        points = [row for row in rows if label(row) is not None]
        points = points[-MAX_LINE_POINTS:]
        series = []
        for index, name in enumerate(value_fields):
            data = [{"label": label(row), "value": float(found)} for row in points if (found := number(row, name)) is not None]
            skipped += len(points) - len(data)
            series.append({"name": names[index] if index < len(names) and names[index] else name, "data": data})
        series = [item for item in series if len(item["data"]) > 1]
        if not series:
            return {"error": "a line needs at least two rows with a label and a numeric value"}
        spec = {"type": "line", "title": title, "unit": unit, "series": series}
        summary: dict[str, Any] = {"series": [{"name": item["name"], "points": len(item["data"]),
                                               "first": item["data"][0], "last": item["data"][-1]} for item in series]}
    else:
        valued = [(row, number(row, value_fields[0])) for row in rows]
        skipped = sum(1 for row, found in valued if found is None or label(row) is None)
        valued = [(row, found) for row, found in valued if found is not None and label(row) is not None]
        sort_by = arguments.get("sort_by")
        if sort_by:
            keyed = [(row, found, _number(_field(row, str(sort_by))) if str(sort_by) != value_fields[0] else found)
                     for row, found in valued]
            valued = [(row, found) for row, found, key in sorted((item for item in keyed if item[2] is not None),
                                                                 key=lambda item: item[2], reverse=bool(arguments.get("descending")))]
        limit = max(1, min(int(arguments.get("limit") or 10), MAX_CHART_ROWS))
        grouped = bool(arguments.get("group_rest"))
        if grouped:
            limit = min(limit, MAX_CHART_ROWS - 1)  # room for the "Other" bar or slice
        shown, rest = valued[:limit], valued[limit:]
        data = [{"label": label(row), "value": float(found)} for row, found in shown]
        if rest and grouped:
            # Summed exactly here, so the share of everything outside the shown rows is never the model's arithmetic.
            data.append({"label": f"Other ({len(rest)})", "value": float(sum((found for _, found in rest), Decimal(0)))})
        if not data:
            return {"error": f"no row has both a label in {label_field!r} and a number in {value_fields[0]!r}"}
        if kind == "donut" and any(item["value"] < 0 for item in data):
            return {"error": "a donut cannot show negative values; use a bar chart"}
        spec = {"type": kind, "title": title, "unit": unit, "data": data}
        summary = {"rows": data, **({"notShown": len(rest)} if rest and not grouped else {})}
    chart_id = f"c{len(store.charts) + 1}"
    store.charts[chart_id] = spec
    return {"chartId": chart_id, "type": kind, "title": title, "resultId": result_id, "path": path, **summary,
            **({"skippedRows": skipped, "skippedNote": "rows with no value reported are left out, never drawn as zero"} if skipped else {}),
            "placement": f"Put a chart block containing only {{\"ref\": \"{chart_id}\"}} where the chart should appear. "
                         "The chart is drawn from the tool data; never write its numbers into the block."}


# ── calculate ─────────────────────────────────────────────────────────────────

_BINARY: Final = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
                  ast.Mod: operator.mod}


def _round(value: Decimal, places: Decimal = Decimal(0)) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-int(places)), rounding=ROUND_HALF_UP)


_FUNCTIONS: Final = {
    "min": lambda *a: min(a), "max": lambda *a: max(a), "abs": abs, "round": _round, "sum": lambda *a: sum(a, Decimal(0)),
    "floor": lambda v: v.to_integral_value(rounding=ROUND_FLOOR), "ceil": lambda v: v.to_integral_value(rounding=ROUND_CEILING),
    "sqrt": lambda v: v.sqrt(),
}


def _evaluate(node: ast.AST, names: dict[str, Decimal]) -> Decimal:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, names)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return Decimal(str(node.value))
    if isinstance(node, ast.Name):
        if node.id in names:
            return names[node.id]
        folded = {key.lower(): item for key, item in names.items()}
        if node.id.lower() in folded:
            return folded[node.id.lower()]
        raise ValueError(f"unknown name {node.id!r}; define it in an earlier expression (names so far: {', '.join(names) or 'none'})")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _evaluate(node.operand, names)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        return _BINARY[type(node.op)](_evaluate(node.left, names), _evaluate(node.right, names))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
        exponent = _evaluate(node.right, names)
        if exponent != exponent.to_integral_value() or abs(exponent) > 400:
            raise ValueError("powers must be whole numbers up to 400")
        return _evaluate(node.left, names) ** int(exponent)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS and not node.keywords:
        args = []
        for arg in node.args:
            args.extend(_evaluate(item, names) for item in arg.elts) if isinstance(arg, (ast.List, ast.Tuple)) else args.append(_evaluate(arg, names))
        return _FUNCTIONS[node.func.id](*args)
    raise ValueError(f"unsupported expression part: {ast.dump(node)[:60]}")


def calculate(arguments: dict[str, Any]) -> dict[str, Any]:
    expressions = arguments.get("expressions")
    if isinstance(expressions, dict):
        expressions = [{"name": key, "expr": value} for key, value in expressions.items()]
    if not isinstance(expressions, list) or not expressions:
        return {"error": "expressions must be a list of {name, expr}"}
    if len(expressions) > MAX_EXPRESSIONS:
        return {"error": f"at most {MAX_EXPRESSIONS} expressions"}
    names: dict[str, Decimal] = {}
    results: dict[str, Any] = {}
    for index, item in enumerate(expressions, start=1):
        # The local model also writes {"id": ..., "value": ...} or {"label": ..., "expression": ...}.
        name = str(next((item[key] for key in ("name", "id", "label") if isinstance(item, dict) and item.get(key)), f"x{index}"))
        expr = str(next((item[key] for key in ("expr", "expression", "value", "formula")
                         if isinstance(item, dict) and item.get(key) is not None), "")) if isinstance(item, dict) else str(item)
        if not name.isidentifier():
            return {"error": f"name {name!r} must be a simple identifier"}
        try:
            value = _evaluate(ast.parse(expr.replace("₹", ""), mode="eval"), names)
            if not value.is_finite():
                raise ValueError("the result is not finite")
            results[name] = float(_round(value, Decimal(4)))
        except (ZeroDivisionError, DivisionByZero):
            return {"error": f"{name}: division by zero", "computed": results}
        except SyntaxError:
            return {"error": f"{name}: not a valid expression (write plain numbers such as 5000000, no thousands separators)",
                    "computed": results}
        except (ValueError, InvalidOperation, TypeError, OverflowError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "the value is out of range"
            return {"error": f"{name}: {reason}", "computed": results}
        names[name] = value
    return {"results": results, "note": "exact decimal arithmetic; values with more than 4 decimals are rounded to 4"}


# ── option_strategy and price_levels ──────────────────────────────────────────

def option_strategy(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        legs = options_math.parse_legs(arguments.get("legs"))
    except options_math.StrategyError as exc:
        return {"error": str(exc)}
    lot_size = options_math.dec(arguments.get("lot_size"))
    lots = options_math.dec(arguments.get("lots")) or Decimal(1)
    scenarios = [p for p in (options_math.dec(item) for item in arguments.get("scenario_prices") or []) if p is not None and p > 0][:12]
    days = options_math.dec(arguments.get("days")) if arguments.get("days") is not None else None
    if lot_size is not None and lot_size <= 0:
        return {"error": "lot_size must be positive"}
    return options_math.analyze(legs, lot_size=lot_size, lots=lots, scenarios=scenarios, days=days)


def price_levels(store: ResultStore, arguments: dict[str, Any], now: datetime) -> dict[str, Any]:
    ids = [str(item) for item in arguments.get("result_ids") or [] if item]
    for key in [str(item) for item in arguments.get("ind_keys") or [] if item]:
        # The newest candle result fetched for that instrument in this answer.
        found = [rid for rid, entry in store.results.items()
                 if entry["tool"] == "get_indian_stocks_ohlc" and str(entry["arguments"].get("ind_key")) == key]
        ids.append(found[-1] if found else f"ind_key:{key}")
    if not ids:
        return {"error": "give ind_keys (or result_ids) of get_indian_stocks_ohlc results fetched in this answer"}
    lookback = int(arguments.get("lookback_sessions") or 14)
    tolerance = options_math.dec(arguments.get("touch_tolerance_pct")) or Decimal(1)
    if not 2 <= lookback <= 240:
        return {"error": "lookback_sessions must be between 2 and 240"}
    out = {}
    for result_id in [str(item) for item in ids][:25]:
        entry = store.results.get(result_id)
        if entry is None or entry["tool"] != "get_indian_stocks_ohlc":
            out[result_id] = {"error": "no get_indian_stocks_ohlc result for this in this answer; fetch the candles first"}
            continue
        out[result_id] = {"indKey": entry["arguments"].get("ind_key"),
                          **price_action.levels(entry["text"], now, lookback=lookback, touch_tolerance_pct=tolerance)}
    return {"lookbackSessions": lookback, "touchTolerancePct": float(tolerance), "levels": out,
            "rule": "the latest completed session against the sessions before it (never including itself); a daily candle for today is left out until 15:30 IST"}


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    for fmt, length in (("%Y%m%d", 8), ("%Y-%m-%d", 10), ("%d %b %Y", 11)):
        try:
            return datetime.strptime(text[:length], fmt).date()
        except ValueError:
            continue
    return None


def date_facts(arguments: dict[str, Any], today: date) -> dict[str, Any]:
    raw = arguments.get("dates")
    if not isinstance(raw, list) or not raw:
        return {"error": "dates must be a non-empty list (YYYYMMDD or YYYY-MM-DD)"}
    parsed = [(str(item), _parse_date(item)) for item in raw[:60]]
    bad = [text for text, day in parsed if day is None]
    if bad:
        return {"error": f"unreadable dates: {', '.join(bad[:5])}"}
    days = sorted({day for _, day in parsed if day})
    last_in_month = {max(d for d in days if (d.year, d.month) == (day.year, day.month)) for day in days}

    def weekdays_until(day: date) -> int:
        count, current = 0, today
        while current < day:
            current += timedelta(days=1)
            count += current.weekday() < 5
        return count

    rows = [{"date": day.isoformat(), "weekday": day.strftime("%A"), "daysFromToday": (day - today).days,
             "weekdaysFromToday": weekdays_until(day) if day > today else 0, "lastListedInItsMonth": day in last_in_month}
            for day in days]
    upcoming_last = [row["date"] for row in rows if row["lastListedInItsMonth"] and row["daysFromToday"] > 0]
    return {"today": today.isoformat(), "dates": rows,
            "firstLastInMonthAfterToday": upcoming_last[0] if upcoming_last else None,
            "note": "weekdays exclude Saturdays and Sundays only; exchange holidays are not known here"}


def tools(store: ResultStore, now: Callable[[], datetime]) -> list[ToolSpec]:
    async def run_inspect(arguments: dict[str, Any]) -> str:
        try:
            return json.dumps(inspect(store, arguments), ensure_ascii=False, default=str)
        except (ValueError, TypeError) as exc:
            return json.dumps({"error": str(exc)})

    async def run_calculate(arguments: dict[str, Any]) -> str:
        return json.dumps(calculate(arguments), ensure_ascii=False)

    async def run_strategy(arguments: dict[str, Any]) -> str:
        return json.dumps(option_strategy(arguments), ensure_ascii=False)

    async def run_levels(arguments: dict[str, Any]) -> str:
        return json.dumps(price_levels(store, arguments, now()), ensure_ascii=False)

    async def run_dates(arguments: dict[str, Any]) -> str:
        return json.dumps(date_facts(arguments, now().date()), ensure_ascii=False)

    async def run_chart(arguments: dict[str, Any]) -> str:
        try:
            return json.dumps(make_chart(store, arguments), ensure_ascii=False, default=str)
        except (ValueError, TypeError) as exc:
            return json.dumps({"error": str(exc)})

    condition = {"type": "string", "description": (
        "\"<field> <op> <value>\": a dotted field inside a row and one of >, >=, <, <=, ==, !=, between (two values), "
        "contains, exists; e.g. \"put_data.delta between -0.2 -0.1\" or \"strike_price >= 22000\"")}
    return [
        ToolSpec("inspect_result",
                 "Read rows of a large earlier tool result by its resultId: give the path of a list in its outline, optional "
                 "conditions on row fields, the fields to return and an optional sort. Use it instead of guessing what a large "
                 "result holds.",
                 {"type": "object", "required": ["result_id"], "properties": {
                     "result_id": {"type": "string"}, "path": {"type": "string", "description": "Dotted path to a list, e.g. option_chain_data"},
                     "where": {"type": "array", "items": condition}, "fields": {"type": "array", "items": {"type": "string"}},
                     "sort_by": {"type": "string"}, "descending": {"type": "boolean"}, "limit": {"type": "integer"}}},
                 run_inspect),
        ToolSpec("calculate",
                 "Exact arithmetic on figures from tool results. Give named expressions evaluated in order; a later expression "
                 "may use earlier names. Write plain numbers (5000000, not 50,00,000). Supports + - * / % **, parentheses, min, max, abs, round(x, places), floor, ceil, sum, "
                 "sqrt. Use it for every figure you derive (sums, differences, percentages, ratios, quantities) instead of "
                 "working it out yourself.",
                 {"type": "object", "required": ["expressions"], "properties": {"expressions": {"type": "array", "items": {
                     "type": "object", "required": ["name", "expr"],
                     "properties": {"name": {"type": "string"}, "expr": {"type": "string"}}}}}},
                 run_calculate),
        ToolSpec("option_strategy",
                 "Exact analysis of any option position at expiry: net credit or debit, maximum profit and loss (or unlimited), "
                 "breakevens, profit or loss at scenario prices, rupee amounts for a lot size, and a theta estimate over some days "
                 "when each leg's theta is given. Legs: type call or put, strike, premium, quantity (positive bought, negative "
                 "sold, in lots).",
                 {"type": "object", "required": ["legs"], "properties": {
                     "legs": {"type": "array", "items": {"type": "object", "required": ["type", "strike", "premium", "quantity"],
                                                          "properties": {"type": {"type": "string", "enum": ["call", "put"]},
                                                                         "strike": {"type": "number"}, "premium": {"type": "number"},
                                                                         "quantity": {"type": "number"}, "theta": {"type": "number"}}}},
                     "lot_size": {"type": "number"}, "lots": {"type": "number"},
                     "scenario_prices": {"type": "array", "items": {"type": "number"}}, "days": {"type": "number"}}},
                 run_strategy),
        ToolSpec("price_levels",
                 "For daily candles already fetched in this answer with get_indian_stocks_ohlc (give their ind_keys, or "
                 "resultIds): the latest completed session's "
                 "close against the highest high and lowest low of the lookback_sessions before it, and how many of those "
                 "sessions had a low near that lowest low. Fetch a long enough window (1y for daily rules over sessions).",
                 {"type": "object", "properties": {
                     "ind_keys": {"type": "array", "items": {"type": "string"}},
                     "result_ids": {"type": "array", "items": {"type": "string"}},
                     "lookback_sessions": {"type": "integer"}, "touch_tolerance_pct": {"type": "number"}}},
                 run_levels),
        ToolSpec("date_facts",
                 "Calendar facts for a list of dates (YYYYMMDD or YYYY-MM-DD), e.g. an option chain's expiry_list: weekday, days "
                 "and weekdays from today, which date is the last listed in its month (a monthly expiry), and the first such "
                 "date after today.",
                 {"type": "object", "required": ["dates"], "properties": {"dates": {"type": "array", "items": {"type": "string"}}}},
                 run_dates),
        ToolSpec("make_chart",
                 "Draw a bar, donut or line chart from a list in an earlier tool result. Give its resultId, the path of the "
                 "list, the field to label each bar, slice or point with, and the field (or, for a line, fields) holding the "
                 "numbers. The numbers are read from the result in code. Returns a chartId: put a chart block containing "
                 "only {\"ref\": \"<chartId>\"} in the answer where the chart goes. Bars compare items, donuts show the "
                 "parts of a whole, lines show values over dates.",
                 {"type": "object", "required": ["type", "result_id", "path", "label_field"], "properties": {
                     "type": {"type": "string", "enum": list(CHART_TYPES)},
                     "title": {"type": "string"},
                     "result_id": {"type": "string"},
                     "path": {"type": "string", "description": "Dotted path to the list, e.g. figures.top_holdings or points"},
                     "label_field": {"type": "string", "description": "Field of each row to label it with, e.g. name or date"},
                     "value_field": {"type": "string", "description": "Field of each row holding the number (bar, donut, line)"},
                     "value_fields": {"type": "array", "items": {"type": "string"}, "description": "Several number fields, one line each"},
                     "series_names": {"type": "array", "items": {"type": "string"}},
                     "unit": {"type": "string", "description": "e.g. ₹ or %"},
                     "where": {"type": "array", "items": condition},
                     "sort_by": {"type": "string"}, "descending": {"type": "boolean"},
                     "limit": {"type": "integer", "description": f"Bars or slices to show (default 10, at most {MAX_CHART_ROWS})"},
                     "group_rest": {"type": "boolean", "description": "Add the rows past limit as one 'Other' bar or slice"}}},
                 run_chart),
    ]

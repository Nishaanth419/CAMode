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
ANALYSIS_TOOL_NAMES: Final = frozenset({"inspect_result", "calculate", "option_strategy", "price_levels", "date_facts"})


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

    def add(self, tool: str, arguments: dict[str, Any], text: str) -> str:
        result_id = f"r{len(self.results) + 1}"
        self.results[result_id] = {"tool": tool, "arguments": arguments, "text": text, "value": _loads(text)}
        return result_id

    def view(self, result_id: str) -> str:
        """What the model reads for a result: the result itself when short, else an outline to query."""
        entry = self.results[result_id]
        if len(entry["text"]) <= COMPACT_OVER_CHARS:
            return entry["text"]
        if entry["value"] is None:
            return json.dumps({"resultId": result_id, "chars": len(entry["text"]), "note": "not JSON; first part shown",
                               "text": entry["text"][:COMPACT_OVER_CHARS]}, ensure_ascii=False)
        return json.dumps({
            "resultId": result_id, "tool": entry["tool"], "chars": len(entry["text"]),
            "outline": _outline(entry["value"]),
            "howToRead": (f"This result is too large to show in full. Call inspect_result with result_id \"{result_id}\", "
                          "the path of a list in the outline, conditions on its fields and the fields you need."),
        }, ensure_ascii=False, separators=(",", ":"), default=str)

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
    path = str(arguments.get("path") or "")
    target = _get(value, path) if path else value
    if target is None:
        return {"error": f"nothing at path {path!r}", "outline": _outline(value)}
    fields = [str(item) for item in (arguments.get("fields") or [])][:MAX_FIELDS]
    if not isinstance(target, list):
        return {"resultId": result_id, "path": path, "value": _project(target, fields)}
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
    return {"resultId": result_id, "path": path, "rows": len(target), "matched": len(rows),
            "returned": [_project(row, fields) for row in rows[:limit]],
            **({"note": f"{len(rows) - limit} more matched; narrow the conditions or raise limit"} if len(rows) > limit else {}),
            "absentFields": "a field missing from a row was not reported by the tool (not quoted), never zero"}


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
        if node.id not in names:
            raise ValueError(f"unknown name {node.id!r}; define it in an earlier expression")
        return names[node.id]
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
        name = str(item.get("name") or f"x{index}") if isinstance(item, dict) else f"x{index}"
        expr = str(item.get("expr", "")) if isinstance(item, dict) else str(item)
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
    ]

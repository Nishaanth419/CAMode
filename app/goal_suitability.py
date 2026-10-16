"""Which kinds of investment fit a goal, from fixed rules over the goal's own inputs.

Pure functions with no I/O. The rules are data (``CATEGORIES`` and the checks in
``_rules``) so they can be read, tested and changed in one place; the model only
explains the output and never decides. Suitability is judged for broad categories,
never for named funds: CA Mode holds no reliable, dated scheme data (see
``FUND_DATA``), and the category descriptions carry no returns or risk figures
because none can be sourced here. Nothing in this module is advice.

Missing inputs never make a category look safer: each one is replaced by its most
cautious value, listed in ``assumed``, and ``enoughInformation`` is False.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from typing import Any, Final

from .goal_planning import PlanningError, _target_date, months_until

RULES_VERSION: Final = 1
NOTE: Final = "Category suitability from CA Mode's fixed rules, not advice or a recommendation of any fund."

# riskLevel orders categories from 1 (least exposed to a fall) to 6. cautionFromMonths: shorter horizons make the
# category unsuitable; allowedFromMonths: shorter than this (but not below cautionFrom) is a caution.
CATEGORIES: Final = (
    {"id": "liquid_overnight", "label": "Liquid and overnight", "riskLevel": 1, "cautionFromMonths": 0, "allowedFromMonths": 0,
     "examples": "Liquid and overnight funds, savings accounts, short fixed deposits."},
    {"id": "short_duration_debt", "label": "Short-duration debt", "riskLevel": 2, "cautionFromMonths": 6, "allowedFromMonths": 12,
     "examples": "Ultra-short, short-duration and money-market debt funds."},
    {"id": "conservative_hybrid", "label": "Conservative hybrid", "riskLevel": 3, "cautionFromMonths": 24, "allowedFromMonths": 36,
     "examples": "Hybrid funds that hold mostly debt with a small share of equity."},
    {"id": "balanced_multi_asset", "label": "Balanced and multi-asset", "riskLevel": 4, "cautionFromMonths": 36, "allowedFromMonths": 60,
     "examples": "Balanced-advantage and multi-asset funds that mix equity, debt and other assets."},
    {"id": "large_flexi_equity", "label": "Large-cap, flexi-cap and index equity", "riskLevel": 5, "cautionFromMonths": 36,
     "allowedFromMonths": 60, "examples": "Large-cap, flexi-cap and broad market index funds."},
    {"id": "mid_small_equity", "label": "Mid-cap and small-cap equity", "riskLevel": 6, "cautionFromMonths": 60,
     "allowedFromMonths": 84, "examples": "Mid-cap and small-cap equity funds."},
)
EQUITY_CATEGORY_IDS: Final = ("large_flexi_equity", "mid_small_equity")

# The highest riskLevel each risk tolerance accepts without a caution.
RISK_CAP: Final = {"low": 3, "medium": 5, "high": 6}
# Used for any input the user did not give: never the optimistic answer.
CAUTIOUS_DEFAULTS: Final = {
    "flexibility": "fixed", "liquidityNeed": "high", "riskTolerance": "low", "lossCapacity": "low",
}
# The answers each input accepts.
CAUTIOUS_DEFAULTS_OPTIONS: Final = {
    "flexibility": ("fixed", "some", "flexible"),
    "liquidityNeed": ("low", "medium", "high"),
    "riskTolerance": ("low", "medium", "high"),
    "lossCapacity": ("low", "medium", "high"),
}
_INPUT_LABELS: Final = {
    "horizon": "time to the target date", "flexibility": "flexibility", "liquidityNeed": "liquidity need",
    "riskTolerance": "risk tolerance", "lossCapacity": "capacity for loss",
}

FUND_DATA: Final = {
    "available": False,
    "reason": (
        "CA Mode has no reliable, dated scheme data (category, expense ratio, benchmark, risk level, returns), "
        "so it does not rank or name funds."
    ),
    "missingIntegration": (
        "INDmoney's get_mf_by_category and get_mf_funds_details tools are allowlisted but their response layout is "
        "undocumented; a parser needs a recorded live response (scripts/inspect_indmoney_shapes.py) with an as-of "
        "date before funds can be ranked."
    ),
}

_SEVERITY: Final = {"allowed": 0, "caution": 1, "unsuitable": 2}


def _assume(inputs: dict[str, Any], assumed: list[str], goal: Mapping[str, Any]) -> None:
    """Fill each input the goal lacks with its cautious value and record it in ``assumed``."""
    for name, cautious in CAUTIOUS_DEFAULTS.items():
        if goal.get(name) in CAUTIOUS_DEFAULTS_OPTIONS[name]:
            inputs[name] = goal[name]
        else:
            inputs[name] = cautious
            assumed.append(name)


def _horizon_months(goal: Mapping[str, Any], today: date) -> int | None:
    try:
        target = _target_date(goal.get("targetDate"))
    except PlanningError:
        return None
    return None if target is None else months_until(target, today)


def _given(name: str, assumed: list[str]) -> str:
    """How a reason refers to an input: plain when the user gave it, flagged when it was assumed."""
    label = _INPUT_LABELS[name]
    return f"{label} (assumed cautiously because it was not given)" if name in assumed else label


def _rules(category: Mapping[str, Any], inputs: Mapping[str, Any], assumed: list[str]) -> list[tuple[str, str]]:
    """Every rule that fires for one category as (verdict, reason); the category's verdict is the worst of them."""
    level, months = category["riskLevel"], inputs["horizonMonths"]
    fired: list[tuple[str, str]] = []
    # Without a date the horizon is taken as zero months, so say that rather than "only 0 months".
    when = ("No target date was given, so the goal is treated as due now (the cautious assumption)"
            if "horizon" in assumed else f"Only {months} months to the target date")
    if months < category["cautionFromMonths"]:
        fired.append(("unsuitable", f"{when}; this category needs at least {category['cautionFromMonths']} months."))
    elif months < category["allowedFromMonths"]:
        fired.append(("caution", f"{when}; this category is better suited to {category['allowedFromMonths']} months or more."))

    cap = RISK_CAP[inputs["riskTolerance"]]
    risk = _given("riskTolerance", assumed)
    if level >= cap + 2:
        fired.append(("unsuitable", f"Its swings are well above a {inputs['riskTolerance']} {risk}."))
    elif level == cap + 1:
        fired.append(("caution", f"Its swings are a step above a {inputs['riskTolerance']} {risk}."))

    loss = inputs["lossCapacity"]
    if loss == "low" and level >= 4:
        if inputs["flexibility"] == "fixed":
            fired.append(("unsuitable", f"A fall would hurt this goal ({_given('lossCapacity', assumed)} is low) and the "
                                        f"goal cannot move ({_given('flexibility', assumed)} is fixed)."))
        else:
            fired.append(("caution", f"A fall would hurt this goal ({_given('lossCapacity', assumed)} is low)."))

    liquidity = inputs["liquidityNeed"]
    if liquidity == "high" and level >= 3:
        fired.append(("unsuitable", f"The money may be needed at short notice ({_given('liquidityNeed', assumed)} is "
                                    "high), and this category's value can be down when it is."))
    elif liquidity == "high" and level == 2:
        fired.append(("caution", f"The money may be needed at short notice ({_given('liquidityNeed', assumed)} is high)."))
    elif liquidity == "medium" and level >= 5:
        fired.append(("caution", f"Some chance of needing the money early ({_given('liquidityNeed', assumed)} is medium)."))

    if inputs["flexibility"] == "fixed" and level >= 4:
        fired.append(("caution", "A fall near a date that cannot move cannot be waited out "
                                 f"({_given('flexibility', assumed)} is fixed)."))
    return fired


def evaluate(goal: Mapping[str, Any], today: date) -> dict[str, Any]:
    """Judge each investment category for ``goal`` as of ``today``; see the module docstring for the rules."""
    assumed: list[str] = []
    inputs: dict[str, Any] = {}
    horizon = _horizon_months(goal, today)
    if horizon is None:
        horizon = 0
        assumed.append("horizon")
    inputs["horizonMonths"] = horizon
    _assume(inputs, assumed, goal)
    categories = []
    for category in CATEGORIES:
        fired = _rules(category, inputs, assumed)
        verdict = max((item[0] for item in fired), key=_SEVERITY.__getitem__, default="allowed")
        reasons = [reason for _, reason in fired] or ["No rule restricts this category for the inputs given."]
        categories.append({
            "id": category["id"], "label": category["label"], "examples": category["examples"],
            "riskLevel": category["riskLevel"], "verdict": verdict, "reasons": reasons,
        })
    return {
        "rulesVersion": RULES_VERSION,
        "horizonMonths": horizon,
        "inputs": inputs,
        "assumed": assumed,
        "assumedLabels": [_INPUT_LABELS[name] for name in assumed],
        "enoughInformation": not assumed,
        "capitalPreservation": inputs["lossCapacity"] == "low" or inputs["flexibility"] == "fixed",
        "categories": categories,
        "equitySuitable": any(
            item["verdict"] != "unsuitable" for item in categories if item["id"] in EQUITY_CATEGORY_IDS
        ),
        "note": NOTE,
    }


# ── Reviewing holdings against a goal ──────────────────────────────────────────

# Broad class of a holding -> the categories it can fall in, depending on what the scheme or instrument really is.
HOLDING_CLASSES: Final = {
    "cash_like": {"label": "Cash-like", "categories": ("liquid_overnight",), "note": None},
    "debt": {
        "label": "Debt", "categories": ("liquid_overnight", "short_duration_debt"),
        "note": "Long-duration and credit-risk debt is riskier than this range.",
    },
    "hybrid": {"label": "Hybrid", "categories": ("conservative_hybrid", "balanced_multi_asset"), "note": None},
    "equity_fund": {"label": "Equity fund", "categories": ("large_flexi_equity", "mid_small_equity"), "note": None},
    "equity_direct": {
        "label": "Direct equity", "categories": ("large_flexi_equity", "mid_small_equity"),
        "note": "A single company adds single-company risk on top of this range.",
    },
    "gold": {"label": "Gold", "categories": (), "note": None},
    "unknown": {"label": "Unknown", "categories": (), "note": None},
}

_CLASS_RULES: Final = (
    ("hybrid", re.compile(r"hybrid|balanced|multi.?asset")),
    ("equity", re.compile(r"equit")),
    ("debt", re.compile(r"debt|bond|fixed income")),
    ("gold", re.compile(r"gold|commodit")),
    ("cash_like", re.compile(r"liquid|cash|money market")),
)
_TYPE_TOKENS: Final = (
    ("equity_direct", {"stock", "stocks", "share", "shares"}),
    ("cash_like", {"fd", "deposit", "deposits", "savings"}),
    ("gold", {"gold"}),
)


def classify_holding(asset_type: str | None, asset_class: str | None) -> str:
    """A broad class for a holding from INDmoney's asset class (first) and asset type; ``unknown`` if neither says."""
    text = str(asset_class or "").lower()
    kind = str(asset_type or "").lower()
    tokens = set(re.split(r"[^a-z0-9]+", kind))
    for name, pattern in _CLASS_RULES:
        if pattern.search(text):
            if name == "equity":
                return "equity_direct" if tokens & _TYPE_TOKENS[0][1] else "equity_fund"
            return name
    for name, words in _TYPE_TOKENS:
        if tokens & words:
            return name
    return "unknown"


def review_holding(asset_type: str | None, asset_class: str | None, suitability: Mapping[str, Any]) -> dict[str, Any]:
    """How a holding sits against a goal's suitability: ``allowed``, ``caution``, ``unsuitable`` or ``cannot_assess``."""
    broad = classify_holding(asset_type, asset_class)
    spec = HOLDING_CLASSES[broad]
    verdicts = {item["id"]: item["verdict"] for item in suitability["categories"]}
    in_range = [verdicts[name] for name in spec["categories"] if name in verdicts]
    if not in_range:
        verdict = "cannot_assess"
        reason = "CA Mode cannot judge this kind of holding against the goal's categories."
    elif all(item == "unsuitable" for item in in_range):
        verdict, reason = "unsuitable", "Every category this kind of holding can fall in is unsuitable for this goal."
    elif all(item == "allowed" for item in in_range):
        verdict, reason = "allowed", "Every category this kind of holding can fall in is allowed for this goal."
    else:
        verdict = "caution"
        reason = (
            "Depends on the scheme's category, which the INDmoney data CA Mode stores does not include."
        )
    if spec["note"] and verdict != "cannot_assess":
        reason += " " + spec["note"]
    return {"broadClass": broad, "classLabel": spec["label"], "verdict": verdict, "reason": reason}

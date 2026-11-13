"""Capital gains tax on mutual fund units: dated rules, tax lots, the year's position and sell-planning.

Pure ``Decimal`` apart from ``nav_2018`` (an offline lookup in ``casparser_isin``) and the
``tax_for`` loader at the end.

Rules are data (``MF_TAX_RULES``): each row says which tax class it covers, the sale and
purchase dates it applies to, when a holding becomes long-term, the short- and long-term
rates (``"slab"`` for the investor's slab rate), the equity long-term exemption, whether
cost is indexed, the sections in the act in force on the sale date and the citation.
Every gain carries the id of the rule that produced it. Read from the Finance (No. 2)
Bill, 2024 as introduced and its Memorandum, and the Finance Bill, 2026 rates for the
Income-tax Act, 2025, on 2026-11-13; see docs/superpowers/plans/2026-11-13-mf-returns-batch-3.md
for what could not be confirmed. Figures are estimates for information, never advice.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final, Literal

from . import benchmarks
from .goal_planning import add_months
from .mf_math import SELL_TYPES, Txn

SLAB: Final = "slab"
ACT_1961: Final = "Income-tax Act, 1961"
ACT_2025: Final = "Income-tax Act, 2025"
MIN_FY: Final = "2023-24"
TURNING_LONG_TERM_DAYS: Final = 90
ELSS_LOCK_MONTHS: Final = 36
GRANDFATHER_DATE: Final = date(2018, 1, 31)
CESS_RATE: Final = Decimal("0.04")

EXEMPTION_NOTE: Final = "Shares, equity ETFs or funds sold outside this statement also use this exemption."
CARRY_FORWARD_NOTE: Final = "Unused losses can be carried forward 8 years if your return is filed on time."
ESTIMATE_NOTE: Final = "An estimate: 4% cess included; surcharge, rebates and losses carried from earlier years are not."
SLAB_NOTE: Final = "Depends on your tax slab. Add it in your profile to include it in the estimate."
EXIT_LOAD_NOTE: Final = "Check the exit load before redeeming; the statement does not include it."
DEADLINE_NOTE: Final = "Redeem by about 25 March so the sale is dated in this financial year."
SET_OFF_NOTE: Final = (
    "Losses are set off against slab-taxed gains first, then other short-term gains, then long-term gains; your "
    "return may use another order the law allows."
)
FIGURES_NOTE: Final = "Figures and options for information, not advice or a recommendation to redeem or invest."

FY_FORMAT_ERROR: Final = "Use a financial year like 2026-27."
FY_FUTURE_ERROR: Final = "That financial year has not started."
FY_EARLY_ERROR: Final = f"Tax figures start from FY {MIN_FY}."


# ── Rules ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TaxRule:
    id: str
    tax_classes: tuple[str, ...]
    sold_from: date
    sold_to: date | None
    bought_from: date | None
    bought_to: date | None
    long_term_after_months: int | None  # None: always short-term.
    st_rate: Decimal | Literal["slab"]
    lt_rate: Decimal | None
    lt_exemption_inr: Decimal | None  # Per financial year, in aggregate across that year's equity rules.
    indexation: bool
    sections: dict[str, str | None]
    act: str
    source: str


_D = Decimal
_SPECIFIED_FROM = date(2023, 4, 1)  # s. 50AA applies to specified fund units acquired on or after this date.
_BEFORE_SPECIFIED = date(2023, 3, 31)
_FNO2_2024 = "Finance (No. 2) Act, 2024"

MF_TAX_RULES: Final[tuple[TaxRule, ...]] = (
    TaxRule("equity_fy2023_24", ("equity",), date(2023, 4, 1), date(2024, 3, 31), None, None, 12,
            _D("15"), _D("10"), _D("100000"), False, {"st": "111A", "lt": "112A"}, ACT_1961,
            "Income-tax Act, 1961 ss. 111A (15%), 112A (10% above Rs 1,00,000), 2(42A) first proviso (12 months); "
            "s. 55(2)(ac) for units acquired before 1 Feb 2018."),
    TaxRule("equity_2024_before_jul23", ("equity",), date(2024, 4, 1), date(2024, 7, 22), None, None, 12,
            _D("15"), _D("10"), _D("125000"), False, {"st": "111A", "lt": "112A"}, ACT_1961,
            "Income-tax Act, 1961 ss. 111A(1)(i)(a) and 112A(2)(i)(a), substituted by the Finance (No. 2) Act, 2024 "
            "ss. 29 and 31; the Rs 1,25,000 limit applies to the year's long-term gains in aggregate (proviso to "
            "s. 112A(2)(i))."),
    TaxRule("equity_from_2024_07_23", ("equity",), date(2024, 7, 23), date(2026, 3, 31), None, None, 12,
            _D("20"), _D("12.5"), _D("125000"), False, {"st": "111A", "lt": "112A"}, ACT_1961,
            "Income-tax Act, 1961 ss. 111A(1)(i)(b) (20%) and 112A(2)(i)(b) (12.5% above Rs 1,25,000), "
            "Finance (No. 2) Act, 2024 ss. 29 and 31."),
    TaxRule("equity_ita2025", ("equity",), date(2026, 4, 1), None, None, None, 12,
            _D("20"), _D("12.5"), _D("125000"), False, {"st": "196", "lt": "198"}, ACT_2025,
            "Income-tax Act, 2025 ss. 196 (20%) and 198 (12.5% above Rs 1,25,000), rates as in the Finance Act, 2026 "
            "(Bill No. 3 of 2026 as introduced)."),
    TaxRule("specified_debt", ("debt",), date(2023, 4, 1), date(2026, 3, 31), _SPECIFIED_FROM, None, None,
            SLAB, None, None, False, {"st": "50AA", "lt": None}, ACT_1961,
            "Income-tax Act, 1961 s. 50AA (Finance Act, 2023): gains on specified mutual fund units acquired on or "
            "after 1 Apr 2023 are short-term; Explanation (ii) substituted by the Finance (No. 2) Act, 2024 s. 21(b) "
            "from AY 2026-27 (more than 65% in debt and money market instruments)."),
    TaxRule("specified_debt_ita2025", ("debt",), date(2026, 4, 1), None, _SPECIFIED_FROM, None, None,
            SLAB, None, None, False, {"st": "76", "lt": None}, ACT_2025,
            "Income-tax Act, 2025 s. 76 (specified mutual fund units acquired on or after 1 Apr 2023)."),
    TaxRule("specified_other_old_definition", ("other",), date(2023, 4, 1), date(2025, 3, 31), _SPECIFIED_FROM, None,
            None, SLAB, None, None, False, {"st": "50AA", "lt": None}, ACT_1961,
            "Income-tax Act, 1961 s. 50AA, Explanation (ii) as inserted by the Finance Act, 2023: a fund with not "
            "more than 35% in equity shares of domestic companies was a specified mutual fund until AY 2025-26."),
    TaxRule("other_new_definition", ("other",), date(2025, 4, 1), date(2026, 3, 31), _SPECIFIED_FROM, None, 24,
            SLAB, _D("12.5"), None, False, {"st": None, "lt": "112"}, ACT_1961,
            "Income-tax Act, 1961 s. 50AA Explanation (ii) as substituted from AY 2026-27 (no longer a specified "
            "fund); ss. 2(42A) (24 months) and 112 (12.5%) as amended by the Finance (No. 2) Act, 2024 ss. 3(b) "
            "and 30."),
    TaxRule("other_new_definition_ita2025", ("other",), date(2026, 4, 1), None, _SPECIFIED_FROM, None, 24,
            SLAB, _D("12.5"), None, False, {"st": None, "lt": "197"}, ACT_2025,
            "Income-tax Act, 2025 ss. 76 (not a specified fund) and 197 (12.5%)."),
    TaxRule("non_equity_indexed", ("debt", "other"), date(2023, 4, 1), date(2024, 7, 22), None, _BEFORE_SPECIFIED, 36,
            SLAB, _D("20"), None, True, {"st": None, "lt": "112"}, ACT_1961,
            "Income-tax Act, 1961 s. 2(42A) (36 months for units other than of an equity-oriented fund) and "
            "s. 112(1)(a) (20% after indexation under the second proviso to s. 48)."),
    TaxRule("non_equity", ("debt", "other"), date(2024, 7, 23), date(2026, 3, 31), None, _BEFORE_SPECIFIED, 24,
            SLAB, _D("12.5"), None, False, {"st": None, "lt": "112"}, ACT_1961,
            "Income-tax Act, 1961 ss. 2(42A) (24 months) and 112 (12.5%, no indexation), Finance (No. 2) Act, 2024 "
            "ss. 3(b) and 30."),
    TaxRule("non_equity_ita2025", ("debt", "other"), date(2026, 4, 1), None, None, _BEFORE_SPECIFIED, 24,
            SLAB, _D("12.5"), None, False, {"st": None, "lt": "197"}, ACT_2025,
            "Income-tax Act, 2025 s. 197 (12.5%)."),
)

# The equity long-term exemption per financial year (the limit, not per rule row).
EQUITY_LT_EXEMPTION_INR: Final = {"2023-24": _D("100000")}
DEFAULT_EXEMPTION_INR: Final = _D("125000")

# Cost Inflation Index, 2001-02 series (Income Tax Department chart; FY 2024-25 by Notification 44/2024).
COST_INFLATION_INDEX: Final = {
    "2001-02": 100, "2002-03": 105, "2003-04": 109, "2004-05": 113, "2005-06": 117, "2006-07": 122,
    "2007-08": 129, "2008-09": 137, "2009-10": 148, "2010-11": 167, "2011-12": 184, "2012-13": 200,
    "2013-14": 220, "2014-15": 240, "2015-16": 254, "2016-17": 264, "2017-18": 272, "2018-19": 280,
    "2019-20": 289, "2020-21": 301, "2021-22": 317, "2022-23": 331, "2023-24": 348, "2024-25": 363,
}


def _applies(rule: TaxRule, tax_class: str, bought: date, sold: date) -> bool:
    return (tax_class in rule.tax_classes and rule.sold_from <= sold
            and (rule.sold_to is None or sold <= rule.sold_to)
            and (rule.bought_from is None or bought >= rule.bought_from)
            and (rule.bought_to is None or bought <= rule.bought_to))


def rule_for(tax_class: str, bought: date, sold: date) -> TaxRule | None:
    return next((rule for rule in MF_TAX_RULES if _applies(rule, tax_class, bought, sold)), None)


def long_term_on(rule: TaxRule, bought: date) -> date | None:
    """The first sale date on which units bought on ``bought`` are long-term ("held for more than N months")."""
    if rule.long_term_after_months is None:
        return None
    return add_months(bought, rule.long_term_after_months) + timedelta(days=1)


def is_long_term(rule: TaxRule, bought: date, sold: date) -> bool:
    first = long_term_on(rule, bought)
    return first is not None and sold >= first


def indexed_cost(cost: Decimal, bought: date, sold: date) -> tuple[Decimal, str | None]:
    """Cost x CII(sale year) / CII(purchase year); actual cost with ``no_index`` when either year has no index."""
    start, end = COST_INFLATION_INDEX.get(fy_of(bought)), COST_INFLATION_INDEX.get(fy_of(sold))
    if start is None or end is None:
        return cost, "no_index"
    return cost * end / start, None


# ── Financial years ────────────────────────────────────────────────────────────

_FY_PATTERN: Final = re.compile(r"^(\d{4})-(\d{2})$")


def fy_of(day: date) -> str:
    start = day.year if day.month >= 4 else day.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def _fy_start(fy: str) -> int:
    return int(fy[:4])


def fy_bounds(fy: str) -> tuple[date, date]:
    start = _fy_start(fy)
    return date(start, 4, 1), date(start + 1, 3, 31)


def parse_fy(text: str, today: date) -> str:
    """A valid, started financial year from ``MIN_FY`` on; ``ValueError`` with the message to show otherwise."""
    match = _FY_PATTERN.match((text or "").strip())
    if not match or int(match.group(2)) != (int(match.group(1)) + 1) % 100:
        raise ValueError(FY_FORMAT_ERROR)
    fy = match.group(0)
    if _fy_start(fy) > _fy_start(fy_of(today)):
        raise ValueError(FY_FUTURE_ERROR)
    if _fy_start(fy) < _fy_start(MIN_FY):
        raise ValueError(FY_EARLY_ERROR)
    return fy


def fy_options(today: date) -> list[str]:
    """Supported financial years, newest first."""
    return [fy_of(date(year, 4, 1)) for year in range(_fy_start(fy_of(today)), _fy_start(MIN_FY) - 1, -1)]


def exemption_for(fy: str) -> Decimal:
    return EQUITY_LT_EXEMPTION_INR.get(fy, DEFAULT_EXEMPTION_INR)


# ── Tax class ──────────────────────────────────────────────────────────────────

EQUITY_SUBS: Final = frozenset({"aggressive hybrid", "arbitrage", "equity savings"})
DEBT_INDEX_WORDS: Final = ("sdl", "g-sec", "gsec", "gilt", "bond", "crisil ibx", "debt", "t-bill", "money market",
                           "liquid", "target maturity")
STT_REASON: Final = "Classified as equity-oriented because its redemptions paid STT."
MIXED_REASON: Final = (
    "Balanced advantage, multi-asset, fund-of-funds and similar schemes can be taxed either way; no redemption paid "
    "STT, so tax is not estimated."
)
UNKNOWN_REASON: Final = "The fund's AMFI category is not known and no redemption paid STT, so tax is not estimated."


def _paid_stt(transactions: Sequence[Txn]) -> bool:
    sale_days = {t.date for t in transactions if t.type in SELL_TYPES}
    return any(t.type == "STT_TAX" and t.amount and t.date in sale_days for t in transactions)


def tax_class(amfi_category: str | None, scheme_name: str, transactions: Sequence[Txn]) -> tuple[str, str]:
    """``equity``, ``debt`` (a specified fund), ``other`` (gold, international, FoFs) or ``unclassified``, with why.

    The law's tests depend on a fund's actual holdings; the AMFI category is the proxy.
    """
    if not amfi_category:
        return ("equity", STT_REASON) if _paid_stt(transactions) else ("unclassified", UNKNOWN_REASON)
    lowered = re.sub(r"\s+", " ", amfi_category.lower())
    major = lowered.split(" - ", 1)[0]
    sub = benchmarks.sub_category(amfi_category)
    name = scheme_name.lower()
    if major.startswith("equity") or sub == "elss" or sub in EQUITY_SUBS or "equity etf" in lowered \
            or lowered == "index funds - equity funds":
        return "equity", "Equity-oriented AMFI category (over 65% in domestic equity)."
    if major.startswith(("debt", "income")) or lowered == "gilt" or lowered == "index funds - debt funds" \
            or "debt etf" in lowered or sub in {"conservative hybrid", "fixed term plan"}:
        return "debt", "Debt-oriented AMFI category: a specified mutual fund (over 65% in debt)."
    if any(word in lowered for word in ("fof overseas", "investing overseas", "gold etf", "silver etf")) \
            or (("fof domestic" in lowered or "fund of funds" in lowered) and ("gold" in name or "silver" in name)):
        return "other", "Gold, silver or international fund (not equity-oriented, not mainly debt)."
    if "index fund" in lowered and "hybrid" not in lowered:
        if any(word in name for word in DEBT_INDEX_WORDS):
            return "debt", "Index fund on a debt index: a specified mutual fund."
        if "gold" in name or "silver" in name:
            return "other", "Index fund on gold or silver (not equity-oriented, not mainly debt)."
        return "equity", "Index fund on an equity index."
    return ("equity", STT_REASON) if _paid_stt(transactions) else ("unclassified", MIXED_REASON)

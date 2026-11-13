"""Mutual fund capital gains tax: rules, lots, this year's position and sell-planning.

Every expected figure is worked by hand in a comment beside it.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from app import mf_tax
from app.mf_math import Txn


def day(text: str) -> date:
    return date.fromisoformat(text)


def txn(on: str, kind: str, amount: str | None, units: str | None = None, nav: str | None = None) -> Txn:
    return Txn(day(on), kind, None if amount is None else D(amount), None if units is None else D(units),
               None if nav is None else D(nav))


# ── Rules ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("tax_class", "bought", "sold", "rule_id"), [
    ("equity", "2020-01-01", "2024-03-31", "equity_fy2023_24"),
    ("equity", "2020-01-01", "2024-04-01", "equity_2024_before_jul23"),
    ("equity", "2020-01-01", "2024-07-22", "equity_2024_before_jul23"),
    ("equity", "2020-01-01", "2024-07-23", "equity_from_2024_07_23"),
    ("equity", "2020-01-01", "2026-03-31", "equity_from_2024_07_23"),
    ("equity", "2020-01-01", "2026-04-01", "equity_ita2025"),
    ("debt", "2023-03-31", "2025-06-01", "non_equity"),
    ("debt", "2023-04-01", "2025-06-01", "specified_debt"),
    ("debt", "2023-04-01", "2026-04-01", "specified_debt_ita2025"),
    ("other", "2023-04-01", "2025-03-31", "specified_other_old_definition"),
    ("other", "2023-04-01", "2025-04-01", "other_new_definition"),
    ("other", "2023-04-01", "2026-04-01", "other_new_definition_ita2025"),
    ("debt", "2023-03-31", "2024-07-22", "non_equity_indexed"),
    ("other", "2019-01-01", "2024-07-22", "non_equity_indexed"),
    ("debt", "2023-03-31", "2024-07-23", "non_equity"),
    ("debt", "2023-03-31", "2026-04-01", "non_equity_ita2025"),
])
def test_rule_boundaries(tax_class, bought, sold, rule_id):
    assert mf_tax.rule_for(tax_class, day(bought), day(sold)).id == rule_id


def test_no_rule_before_fy_2023_24_or_for_unclassified_funds():
    assert mf_tax.rule_for("equity", day("2020-01-01"), day("2023-03-31")) is None
    assert mf_tax.rule_for("unclassified", day("2020-01-01"), day("2025-01-01")) is None


def test_long_term_boundaries():
    equity = mf_tax.rule_for("equity", day("2024-01-15"), day("2025-01-15"))
    assert not mf_tax.is_long_term(equity, day("2024-01-15"), day("2025-01-15"))  # exactly 12 months
    assert mf_tax.is_long_term(equity, day("2024-01-15"), day("2025-01-16"))  # 12 months and a day
    other = mf_tax.rule_for("debt", day("2022-08-01"), day("2024-08-01"))
    assert other.id == "non_equity"
    assert not mf_tax.is_long_term(other, day("2022-08-01"), day("2024-08-01"))
    assert mf_tax.is_long_term(other, day("2022-08-01"), day("2024-08-02"))
    indexed = mf_tax.rule_for("debt", day("2020-07-10"), day("2023-07-10"))
    assert indexed.id == "non_equity_indexed"
    assert not mf_tax.is_long_term(indexed, day("2020-07-10"), day("2023-07-10"))
    assert mf_tax.is_long_term(indexed, day("2020-07-10"), day("2023-07-11"))
    assert mf_tax.long_term_on(equity, day("2024-02-29")) == day("2025-03-01")  # 28 Feb 2025 + 1 day
    specified = mf_tax.rule_for("debt", day("2023-05-01"), day("2030-01-01"))
    assert not mf_tax.is_long_term(specified, day("2023-05-01"), day("2030-01-01"))
    assert mf_tax.long_term_on(specified, day("2023-05-01")) is None


def test_every_rule_is_cited_and_rules_never_overlap():
    for rule in mf_tax.MF_TAX_RULES:
        assert rule.source and rule.sections
        expected_act = "Income-tax Act, 2025" if rule.sold_from >= day("2026-04-01") else "Income-tax Act, 1961"
        assert rule.act == expected_act
    for tax_class in ("equity", "debt", "other"):
        for bought in (day("2023-03-31"), day("2023-04-01")):
            sold = day("2023-04-01")
            while sold <= day("2027-03-31"):
                matches = [r for r in mf_tax.MF_TAX_RULES if mf_tax._applies(r, tax_class, bought, sold)]
                assert len(matches) == 1, (tax_class, bought, sold, [r.id for r in matches])
                sold += timedelta(days=1)


def test_indexed_cost():
    cost, flag = mf_tax.indexed_cost(D("100000"), day("2015-06-01"), day("2024-07-01"))
    assert cost == D("100000") * 363 / 254 and flag is None  # FY 2015-16 = 254, FY 2024-25 = 363
    cost, flag = mf_tax.indexed_cost(D("100000"), day("2000-05-01"), day("2024-07-01"))
    assert cost == D("100000") and flag == "no_index"


def test_financial_years():
    today = day("2026-11-13")
    assert mf_tax.fy_of(day("2027-03-31")) == "2026-27"
    assert mf_tax.fy_of(day("2026-04-01")) == "2026-27"
    assert mf_tax.fy_of(day("2026-03-31")) == "2025-26"
    assert mf_tax.parse_fy("2025-26", today) == "2025-26"
    assert mf_tax.fy_bounds("2025-26") == (day("2025-04-01"), day("2026-03-31"))
    for bad in ("2025-27", "2025", "FY25", "26-27", ""):
        with pytest.raises(ValueError, match="Use a financial year like 2026-27."):
            mf_tax.parse_fy(bad, today)
    with pytest.raises(ValueError, match="That financial year has not started."):
        mf_tax.parse_fy("2027-28", today)
    with pytest.raises(ValueError, match="Tax figures start from FY 2023-24."):
        mf_tax.parse_fy("2022-23", today)
    assert mf_tax.fy_options(today) == ["2026-27", "2025-26", "2024-25", "2023-24"]
    assert mf_tax.exemption_for("2023-24") == D("100000")
    assert mf_tax.exemption_for("2024-25") == D("125000")
    assert mf_tax.exemption_for("2026-27") == D("125000")


# ── Tax class ──────────────────────────────────────────────────────────────────

SOLD_WITH_STT = [txn("2025-01-10", "REDEMPTION", "-1000", "-10"), txn("2025-01-10", "STT_TAX", "0.01")]
SOLD_WITHOUT_STT = [txn("2025-01-10", "REDEMPTION", "-1000", "-10")]


@pytest.mark.parametrize(("category", "name", "expected"), [
    ("Equity Scheme - Large Cap Fund", "A Large Cap Fund", "equity"),
    ("Equity Schemes - Thematic Fund", "A Thematic Fund", "equity"),
    ("Equity Scheme - Sectoral/ Thematic", "A Banking Fund", "equity"),
    ("ELSS", "A Tax Saver Fund", "equity"),
    ("Equity Schemes - ELSS- Tax Saver Fund", "A Tax Saver Fund", "equity"),
    ("Hybrid Scheme - Aggressive Hybrid Fund", "An Aggressive Hybrid", "equity"),
    ("Hybrid Schemes - Arbitrage Fund", "An Arbitrage Fund", "equity"),
    ("Hybrid Scheme - Equity Savings", "An Equity Savings Fund", "equity"),
    ("Index Funds - Equity Funds", "A Nifty Next 50 Index Fund", "equity"),
    ("Exchange Traded Funds (ETFs) - Equity ETF", "A Nifty ETF", "equity"),
    ("Debt Scheme - Liquid Fund", "A Liquid Fund", "debt"),
    ("Income/Debt Oriented Schemes - Gilt Fund", "A Gilt Fund", "debt"),
    ("Index Funds - Debt Funds", "A Crisil IBX Index Fund", "debt"),
    ("Income", "An Income Plan", "debt"),
    ("Gilt", "A Gilt Plan", "debt"),
    ("Hybrid Scheme - Conservative Hybrid Fund", "A Conservative Hybrid", "debt"),
    ("Exchange Traded Funds (ETFs) - Debt ETF", "A Bharat Bond ETF", "debt"),
    ("Other Scheme - FoF Overseas", "A US Equity FoF", "other"),
    ("Overseas Fund of Funds - Fund of Funds investing overseas", "A Global FoF", "other"),
    ("Other Scheme - Gold ETF", "A Gold ETF", "other"),
    ("Exchange Traded Funds (ETFs) - Silver ETF", "A Silver ETF", "other"),
    ("Other Scheme - FoF Domestic", "SBI Gold Savings Fund - Direct Plan - Growth", "other"),
    ("Other Scheme - Index Funds", "Edelweiss Nifty SDL Apr 2027 Index Fund - Direct Plan - Growth", "debt"),
    ("Other Scheme - Index Funds", "UTI Nifty 50 Index Fund - Direct Plan - Growth", "equity"),
])
def test_tax_class_by_category(category, name, expected):
    tax_class, reason = mf_tax.tax_class(category, name, SOLD_WITHOUT_STT)
    assert tax_class == expected
    assert reason


@pytest.mark.parametrize("category", [
    "Hybrid Scheme - Dynamic Asset Allocation or Balanced Advantage",
    "Hybrid Schemes - Balanced Advantage Fund/ Dynamic Asset Allocation",
    "Hybrid Schemes - Multi Asset Allocation Fund",
    "Other Scheme - FoF Domestic",
    "Solution Oriented Scheme - Retirement Fund",
    "Life Cycle Funds - Life Cycle Fund with Maturity of 10 Years",
    "Growth",
    None,
])
def test_mixed_funds_are_equity_only_with_stt(category):
    tax_class, reason = mf_tax.tax_class(category, "A Fund", SOLD_WITH_STT)
    assert tax_class == "equity"
    assert reason == "Classified as equity-oriented because its redemptions paid STT."
    tax_class, reason = mf_tax.tax_class(category, "A Fund", SOLD_WITHOUT_STT)
    assert tax_class == "unclassified"
    assert reason


def test_stt_must_be_on_a_redemption_day():
    stray = [txn("2025-01-10", "PURCHASE", "1000", "10"), txn("2025-01-10", "STT_TAX", "0.01")]
    assert mf_tax.tax_class("Other Scheme - FoF Domestic", "A Fund", stray)[0] == "unclassified"

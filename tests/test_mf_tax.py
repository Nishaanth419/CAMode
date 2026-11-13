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


# ── Tax lots ───────────────────────────────────────────────────────────────────

from app.amfi import NavRow  # noqa: E402
from app.mf_ledger import LedgerScheme  # noqa: E402

EQUITY = "Equity Scheme - Large Cap Fund"
DEBT = "Debt Scheme - Corporate Bond Fund"


def scheme(isin: str, transactions: list[Txn], close: str, *, folio: str = "F", status: str = "ok",
           name: str | None = None, reason: str | None = None) -> LedgerScheme:
    return LedgerScheme(isin=isin, amfi=None, name=name or f"Fund {isin}", amc="AMC", rta="CAMS", folio_key=folio,
                        open=D(0), close=D(close), valuation_date=None, valuation_nav=None,
                        transactions=transactions, status=status, reason=reason)


def nav_row(isin: str, category: str, nav: str = "10", on: str = "2026-11-10") -> NavRow:
    return NavRow(isin, "1", f"Fund {isin}", category, D(nav), day(on))


def lots_for(schemes, categories: dict[str, str], fmv: dict[str, D] | None = None):
    navs = {isin: nav_row(isin, category) for isin, category in categories.items()}
    return mf_tax.tax_lots(schemes, navs, fmv=(fmv or {}).get)


@pytest.mark.parametrize(("fmv_nav", "sale", "tax_cost", "gain"), [
    ("150", "2000", "1500", "500"),  # max(1000, min(1500, 2000)) = 1500
    ("250", "2000", "2000", "0"),  # max(1000, min(2500, 2000)) = 2000
    ("150", "800", "1000", "-200"),  # max(1000, min(1500, 800)) = 1000: a sale below cost keeps the actual cost
])
def test_grandfathering_cases(fmv_nav, sale, tax_cost, gain):
    fund = scheme("INF1", [txn("2017-06-01", "PURCHASE", "1000", "10"),
                           txn("2025-01-10", "REDEMPTION", f"-{sale}", "-10")], "0")
    (result,) = lots_for([fund], {"INF1": EQUITY}, {"INF1": D(fmv_nav)})

    (row,) = result.gains
    assert (row.rule_id, row.term, row.tax_class) == ("equity_from_2024_07_23", "long", "equity")
    assert row.fmv_2018 == D(fmv_nav) * 10
    assert row.tax_cost == D(tax_cost) and row.gain == D(gain)


def test_grandfathering_date_and_missing_nav():
    on_the_day = scheme("INF1", [txn("2018-01-31", "PURCHASE", "1000", "10"),
                                 txn("2025-01-10", "REDEMPTION", "-2000", "-10")], "0")
    after = scheme("INF2", [txn("2018-02-01", "PURCHASE", "1000", "10"),
                            txn("2025-01-10", "REDEMPTION", "-2000", "-10")], "0")
    missing = scheme("INF3", [txn("2017-06-01", "PURCHASE", "1000", "10"),
                              txn("2025-01-10", "REDEMPTION", "-2000", "-10")], "0")
    results = lots_for([on_the_day, after, missing], {"INF1": EQUITY, "INF2": EQUITY, "INF3": EQUITY},
                       {"INF1": D("150"), "INF2": D("150")})

    assert results[0].gains[0].tax_cost == D("1500")
    assert results[1].gains[0].fmv_2018 is None and results[1].gains[0].tax_cost == D("1000")
    assert results[2].gains[0].tax_cost == D("1000") and "no_2018_nav" in results[2].gains[0].flags


def test_short_term_and_debt_sales_are_never_grandfathered():
    short = scheme("INF1", [txn("2017-06-01", "PURCHASE", "1000", "10"),
                            txn("2018-01-10", "REDEMPTION", "-2000", "-10")], "0")
    debt = scheme("INF2", [txn("2017-06-01", "PURCHASE", "1000", "10"),
                           txn("2024-08-01", "REDEMPTION", "-2000", "-10")], "0")
    results = lots_for([short, debt], {"INF1": EQUITY, "INF2": DEBT}, {"INF1": D("150"), "INF2": D("150")})

    assert results[0].gains[0].rule_id is None and results[0].gains[0].tax_cost == D("1000")  # sold before FY 2023-24
    assert "no_rule" in results[0].gains[0].flags
    assert results[1].gains[0].rule_id == "non_equity" and results[1].gains[0].tax_cost == D("1000")


def test_one_sale_spans_lots():
    fund = scheme("INF1", [txn("2017-06-01", "PURCHASE", "1000", "10"),
                           txn("2024-12-01", "PURCHASE", "2000", "10"),
                           txn("2025-06-02", "REDEMPTION", "-3750", "-15")], "5")
    (result,) = lots_for([fund], {"INF1": EQUITY}, {"INF1": D("150")})

    first, second = result.gains
    # 10 units: sale 3750 x 10/15 = 2500, grandfathered cost max(1000, min(1500, 2500)) = 1500, gain 1000.
    assert (first.units, first.sale_value, first.tax_cost, first.gain, first.term) == (D(10), D(2500), D(1500), D(1000), "long")
    # 5 units of the Dec 2024 lot: sale 1250, cost 1000, short-term.
    assert (second.units, second.sale_value, second.cost, second.gain, second.term) == (D(5), D(1250), D(1000), D(250), "short")
    assert second.rule_id == "equity_from_2024_07_23"
    (remaining,) = result.open_lots
    assert (remaining.units, remaining.cost) == (D(5), D(1000))


MERGED_OUT = scheme("INFA", [txn("2016-05-01", "PURCHASE", "1000", "100"),
                             txn("2018-05-18", "SWITCH_OUT_MERGER", "-1500", "-100")], "0")


def test_merger_is_carried_over():
    merged_in = scheme("INFB", [txn("2018-05-18", "SWITCH_IN_MERGER", "1500", "50"),
                                txn("2025-06-02", "REDEMPTION", "-5000", "-50")], "0")
    source, target = lots_for([MERGED_OUT, merged_in], {"INFA": EQUITY, "INFB": EQUITY}, {"INFA": D("14")})

    assert source.gains == []  # A merger is not a transfer.
    (row,) = target.gains
    # Bought 1 May 2016 at 1000; FMV = A's 31 Jan 2018 NAV 14 x 100 A units (50 B units x ratio 2) = 1400.
    assert (row.bought, row.cost, row.term, row.fmv_2018, row.tax_cost, row.gain) == (
        day("2016-05-01"), D(1000), "long", D(1400), D(1400), D(3600))


def test_merged_lots_keep_their_origin():
    merged_in = scheme("INFB", [txn("2018-05-18", "SWITCH_IN_MERGER", "1500", "50")], "50")
    _, target = lots_for([MERGED_OUT, merged_in], {"INFA": EQUITY, "INFB": EQUITY})

    (lot,) = target.open_lots
    assert (lot.buy_date, lot.units, lot.cost) == (day("2016-05-01"), D(50), D(1000))
    assert lot.origin.isin == "INFA" and lot.origin.ratio == D(2)


def test_unmatched_merger_is_flagged():
    merged_in = scheme("INFB", [txn("2018-05-18", "SWITCH_IN_MERGER", "1500", "50"),
                                txn("2025-06-02", "REDEMPTION", "-5000", "-50")], "0")
    (target,) = lots_for([merged_in], {"INFB": EQUITY})
    (source,) = lots_for([MERGED_OUT], {"INFA": EQUITY})

    assert "merged_in_without_source" in target.flags
    assert target.gains[0].bought == day("2018-05-18") and "merged_in_without_source" in target.gains[0].flags
    assert source.gains == [] and "merged_out_without_target" in source.flags
    assert mf_tax.FLAG_MESSAGES["merged_in_without_source"] == (
        "Merged in from a scheme not in this statement: the holding period and cost may start earlier.")


@pytest.mark.parametrize("status", ["incomplete_history", "unreconciled", "unsupported"])
def test_excluded_schemes_have_no_gains(status):
    fund = scheme("INF1", [txn("2024-06-01", "REDEMPTION", "-2000", "-10")], "0", status=status, reason="Why.")
    (result,) = lots_for([fund], {"INF1": EQUITY})

    assert (result.status, result.reason, result.gains) == (status, "Why.", [])


def test_units_that_do_not_add_up_are_excluded():
    fund = scheme("INF1", [txn("2024-06-01", "PURCHASE", "1000", "10")], "12")
    (result,) = lots_for([fund], {"INF1": EQUITY})

    assert result.status == "unreconciled" and result.gains == [] and "12" in result.reason


def test_indexation_applies_only_to_its_rule():
    old = scheme("INF1", [txn("2019-05-01", "PURCHASE", "10000", "100"),
                          txn("2023-06-01", "REDEMPTION", "-13000", "-100")], "0")
    new = scheme("INF2", [txn("2019-05-01", "PURCHASE", "10000", "100"),
                          txn("2024-08-01", "REDEMPTION", "-13000", "-100")], "0")
    first, second = lots_for([old, new], {"INF1": DEBT, "INF2": DEBT})

    assert first.gains[0].rule_id == "non_equity_indexed"
    # FY 2019-20 = 289, FY 2023-24 = 348 (computed at 34 digits, so compared at 6 places).
    assert first.gains[0].tax_cost.quantize(D("0.000001")) == (D("10000") * 348 / 289).quantize(D("0.000001"))
    assert second.gains[0].rule_id == "non_equity" and second.gains[0].tax_cost == D("10000")


# ── This year's position ───────────────────────────────────────────────────────

def g(rule_id: str, term: str, amount: str, sold: str = "2025-06-01", tax_class: str = "equity",
      name: str = "Fund A", flags: tuple[str, ...] = ()) -> mf_tax.Gain:
    return mf_tax.Gain(name, "INF1", "F", tax_class, rule_id, term, day("2020-01-01"), day(sold), D(1), D(0), D(0),
                       D(0), None, D(amount), flags)


def holding(*gains: mf_tax.Gain, name: str = "Fund A", tax_class: str = "equity", status: str = "ok",
            reason: str | None = None) -> mf_tax.SchemeLots:
    return mf_tax.SchemeLots(scheme(f"INF-{name}", [], "0", name=name), tax_class, "Why this class.", status, reason,
                             gains=list(gains))


def rows_by_rule(result):
    return {(row["ruleId"], row["term"]): row for row in result["rows"]}


def test_position_splits_by_class_and_term():
    lots = [holding(g("equity_from_2024_07_23", "short", "30000"), g("equity_from_2024_07_23", "long", "200000")),
            holding(g("specified_debt", "short", "10000", tax_class="debt"), name="Debt Fund", tax_class="debt")]

    result = mf_tax.position(lots, "2025-26", 30)

    rows = rows_by_rule(result)
    assert (rows[("equity_from_2024_07_23", "short")]["rate"], rows[("equity_from_2024_07_23", "short")]["section"]) == (20.0, "111A")
    assert rows[("specified_debt", "short")]["rate"] == "slab"
    assert result["exemption"] == {"limit": 125000.0, "used": 125000.0, "left": 0.0, "note": mf_tax.EXEMPTION_NOTE}
    tax = result["tax"]
    # 30,000 x 20% + (2,00,000 - 1,25,000) x 12.5% + 10,000 x 30% = 6,000 + 9,375 + 3,000 = 18,375; cess 735.
    assert (tax["status"], tax["beforeCess"], tax["cess"], tax["total"], tax["slabPct"]) == ("estimated", 18375.0, 735.0, 19110.0, 30)
    assert result["act"] == "Income-tax Act, 1961"
    assert mf_tax.ESTIMATE_NOTE in result["notes"] and mf_tax.FIGURES_NOTE in result["notes"]


def test_position_without_a_slab_is_partial():
    lots = [holding(g("equity_from_2024_07_23", "short", "30000"), g("equity_from_2024_07_23", "long", "200000")),
            holding(g("specified_debt", "short", "10000", tax_class="debt"), name="Debt Fund", tax_class="debt")]

    tax = mf_tax.position(lots, "2025-26", None)["tax"]

    # The fixed-rate part only: (6,000 + 9,375) x 1.04 = 15,990.
    assert (tax["status"], tax["slabTaxable"], tax["total"]) == ("partial", 10000.0, 15990.0)
    assert mf_tax.SLAB_NOTE in tax["notes"]


def test_fy_2024_25_exemption_is_one_for_the_year():
    lots = [holding(g("equity_2024_before_jul23", "long", "80000", sold="2024-06-10"),
                    g("equity_from_2024_07_23", "long", "100000", sold="2024-09-10"))]

    result = mf_tax.position(lots, "2024-25", None)

    # 1,25,000 absorbs the 12.5% gains first (1,00,000), then 25,000 of the 10% ones: 55,000 x 10% = 5,500.
    assert result["exemption"]["used"] == 125000.0 and result["exemption"]["left"] == 0.0
    items = {item["ruleId"]: item for item in result["tax"]["items"]}
    assert items["equity_2024_before_jul23"]["taxable"] == 55000.0 and items["equity_2024_before_jul23"]["tax"] == 5500.0
    assert "equity_from_2024_07_23" not in items
    assert result["tax"]["total"] == 5720.0  # 5,500 + 4% cess


def test_exemption_for_fy_2023_24_is_one_lakh():
    result = mf_tax.position([holding(g("equity_fy2023_24", "long", "150000", sold="2023-09-01"))], "2023-24", None)

    assert result["exemption"]["limit"] == 100000.0
    assert result["tax"]["items"][0]["tax"] == 5000.0  # 50,000 x 10%


def test_losses_set_off_in_order():
    lots = [holding(g("other_new_definition", "short", "-50000", tax_class="other"),
                    g("other_new_definition", "long", "-8000", tax_class="other"), name="Gold", tax_class="other"),
            holding(g("specified_debt", "short", "20000", tax_class="debt"), name="Debt", tax_class="debt"),
            holding(g("non_equity", "long", "15000", tax_class="debt"), name="Old Debt", tax_class="debt"),
            holding(g("equity_from_2024_07_23", "short", "10000"), g("equity_from_2024_07_23", "long", "30000"))]

    result = mf_tax.position(lots, "2025-26", None)

    assert [(s["loss"], s["against"], s["term"], s["amount"]) for s in result["setOff"]] == [
        ("short", "specified_debt", "short", 20000.0),
        ("short", "equity_from_2024_07_23", "short", 10000.0),
        ("long", "non_equity", "long", 8000.0),
        ("short", "non_equity", "long", 7000.0),
        ("short", "equity_from_2024_07_23", "long", 13000.0),
    ]
    assert result["exemption"]["used"] == 17000.0 and result["exemption"]["left"] == 108000.0
    assert result["tax"]["total"] == 0.0 and result["tax"]["status"] == "estimated"
    assert result["carryForward"] is None
    assert mf_tax.SET_OFF_NOTE in result["notes"]


def test_long_term_losses_never_offset_short_term_gains():
    lots = [holding(g("equity_from_2024_07_23", "long", "-40000"), g("equity_from_2024_07_23", "short", "10000"))]

    result = mf_tax.position(lots, "2025-26", None)

    assert result["setOff"] == []
    assert result["carryForward"] == {"short": 0.0, "long": 40000.0, "note": mf_tax.CARRY_FORWARD_NOTE}
    assert result["tax"]["total"] == 2080.0  # 10,000 x 20% + cess


def test_only_debt_without_slab_is_never_zero():
    result = mf_tax.position([holding(g("specified_debt", "short", "25000", tax_class="debt"), tax_class="debt")],
                             "2025-26", None)

    assert result["tax"]["status"] == "none"
    assert result["tax"]["total"] is None and result["tax"]["slabTaxable"] == 25000.0


def test_unclassified_gains_shown_not_taxed():
    mixed = holding(g(None, None, "12000", tax_class="unclassified"), name="Balanced", tax_class="unclassified")

    result = mf_tax.position([mixed, holding(g("equity_from_2024_07_23", "short", "-5000"))], "2025-26", 30)

    assert result["unclassified"] == {"gain": 12000.0, "schemes": [{"name": "Balanced", "reason": "Why this class."}]}
    assert result["setOff"] == []  # the equity loss is not set off against a gain with no rule
    assert result["carryForward"]["short"] == 5000.0
    assert result["tax"]["total"] == 0.0


def test_excluded_schemes_are_listed():
    result = mf_tax.position([holding(name="Old Fund", status="incomplete_history", reason="History is missing.")],
                             "2025-26", None)

    assert result["excluded"] == [{"name": "Old Fund", "status": "incomplete_history", "reason": "History is missing."}]
    assert "Gains from funds left out are not included." in result["notes"]


def test_other_years_gains_are_ignored():
    result = mf_tax.position([holding(g("equity_ita2025", "short", "10000", sold="2026-04-01"))], "2025-26", None)

    assert result["rows"] == [] and result["tax"]["total"] == 0.0 and result["schemes"] == []


def test_act_label_and_sections_follow_the_year():
    result = mf_tax.position([holding(g("equity_ita2025", "long", "200000", sold="2026-06-01"))], "2026-27", None)

    assert result["act"] == "Income-tax Act, 2025"
    assert result["rows"][0]["section"] == "198"
    assert (result["from"], result["to"]) == ("2026-04-01", "2027-03-31")


def test_scheme_rows_carry_flags_in_words():
    flagged = holding(g("equity_from_2024_07_23", "long", "1000", flags=("no_2018_nav",)))

    (row,) = mf_tax.position([flagged], "2025-26", None)["schemes"]

    assert (row["name"], row["taxClass"], row["shortTerm"], row["longTerm"]) == ("Fund A", "equity", 0.0, 1000.0)
    assert row["flags"] == [mf_tax.FLAG_MESSAGES["no_2018_nav"]]


# ── Sell-planning ──────────────────────────────────────────────────────────────

import re  # noqa: E402

TODAY = day("2027-01-15")  # FY 2026-27
NO_GAINS_YET = {"exemption": {"left": 125000.0}, "tax": {"items": []}}


def plan_for(schemes, categories: dict[str, str], *, nav: str = "500", nav_on: str = "2027-01-14",
             position: dict | None = None, slab: int | None = None, missing_nav: tuple[str, ...] = ()):
    navs = {isin: nav_row(isin, category, nav, nav_on) for isin, category in categories.items()
            if isin not in missing_nav}
    lots = mf_tax.tax_lots(schemes, {isin: nav_row(isin, c) for isin, c in categories.items()}, fmv={}.get)
    return mf_tax.planning(lots, navs, TODAY, position or NO_GAINS_YET, slab)


HARVESTABLE = scheme("INF1", [txn("2020-01-01", "PURCHASE", "10000", "100"),  # value 50,000: gain 40,000, long
                              txn("2021-01-01", "PURCHASE", "50000", "200"),  # value 1,00,000: gain 50,000, long
                              txn("2026-06-01", "PURCHASE", "4000", "10")], "310")  # short-term


def test_gain_harvest_stops_before_first_short_term_lot():
    (option,) = plan_for([HARVESTABLE], {"INF1": EQUITY})["gainHarvest"]

    assert (option["units"], option["value"], option["gain"], option["tax"], option["stoppedBy"]) == (
        300.0, 150000.0, 90000.0, 0.0, "short_term")
    assert (option["nav"], option["navDate"], option["navSource"]) == (500.0, "2027-01-14", "amfi")


def test_gain_harvest_stays_within_the_exemption_left():
    position = {"exemption": {"left": 60000.0}, "tax": {"items": []}}

    (option,) = plan_for([HARVESTABLE], {"INF1": EQUITY}, position=position)["gainHarvest"]

    # The first lot (40,000) and 200 x 20,000 / 50,000 = 80 units of the second: 180 units, gain 60,000.
    assert (option["units"], option["value"], option["gain"], option["stoppedBy"]) == (180.0, 90000.0, 60000.0, "exemption")


def test_gain_harvest_needs_a_long_term_equity_lot_first():
    young = scheme("INF1", [txn("2026-06-01", "PURCHASE", "4000", "10"),
                            txn("2026-07-01", "PURCHASE", "4000", "10")], "20")
    debt = scheme("INF2", [txn("2020-01-01", "PURCHASE", "10000", "100")], "100")

    result = plan_for([young, debt], {"INF1": EQUITY, "INF2": DEBT})

    assert result["gainHarvest"] == []


def test_loss_harvest_takes_the_most_loss_in_fifo_order():
    fund = scheme("INF1", [txn("2020-01-01", "PURCHASE", "55000", "100"),  # 50,000: -5,000 long
                           txn("2026-09-01", "PURCHASE", "8000", "10"),  # 5,000: -3,000 short
                           txn("2026-10-01", "PURCHASE", "40000", "100")], "210")  # 50,000: +10,000 short
    winner = scheme("INF2", [txn("2020-01-01", "PURCHASE", "1000", "10")], "10")
    position = {"exemption": {"left": 125000.0}, "tax": {"items": [{"term": "short", "taxable": 6000.0}]}}

    result = plan_for([fund, winner], {"INF1": EQUITY, "INF2": EQUITY}, position=position)

    (option,) = result["lossHarvest"]
    assert (option["name"], option["units"], option["value"], option["loss"]) == ("Fund INF1", 110.0, 55000.0, 8000.0)
    assert (option["shortTermResult"], option["longTermResult"]) == (-3000.0, -5000.0)
    # The long-term 5,000 finds no long-term gain; the short-term 3,000 offsets 3,000 of the 6,000 taxable.
    assert option["couldOffset"] == 3000.0


def test_turning_long_term_in_90_days():
    fund = scheme("INF1", [txn("2020-01-01", "PURCHASE", "10000", "100"),
                           txn("2026-03-19", "PURCHASE", "10000", "100"),  # long-term from 20 Mar 2027
                           txn("2026-04-20", "PURCHASE", "10000", "100")], "300")  # 21 Apr 2027: 96 days away
    gold = scheme("INF2", [txn("2025-03-20", "PURCHASE", "10000", "100")], "100")  # 24 months: 21 Mar 2027
    debt = scheme("INF3", [txn("2024-01-01", "PURCHASE", "10000", "100")], "100")

    result = plan_for([fund, gold, debt], {"INF1": EQUITY, "INF2": "Other Scheme - FoF Overseas", "INF3": DEBT}, nav="200")

    equity_option, gold_option = result["turningLongTerm"]
    assert (equity_option["longTermOn"], equity_option["units"], equity_option["unitsBefore"], equity_option["gain"]) == (
        "2027-03-20", 100.0, 100.0, 10000.0)
    # 10,000 x 20% now against 10,000 x 12.5% after 20 Mar 2027.
    assert (equity_option["taxNow"], equity_option["taxAfter"], equity_option["extraTax"]) == (2000.0, 1250.0, 750.0)
    assert equity_option["extraTaxNote"] == "If the gain also fits within the exemption, the saving is larger."
    assert (gold_option["name"], gold_option["longTermOn"], gold_option["taxNow"], gold_option["taxAfter"],
            gold_option["extraTax"], gold_option["extraTaxNote"]) == (
        "Fund INF2", "2027-03-21", None, 1250.0, None, mf_tax.SLAB_NOTE)


def test_turning_long_term_with_a_slab():
    gold = scheme("INF2", [txn("2025-03-20", "PURCHASE", "10000", "100")], "100")

    (option,) = plan_for([gold], {"INF2": "Other Scheme - FoF Overseas"}, nav="200", slab=30)["turningLongTerm"]

    assert (option["taxNow"], option["taxAfter"], option["extraTax"]) == (3000.0, 1250.0, 1750.0)


def test_elss_units_within_three_years_never_offered():
    fund = scheme("INF1", [txn("2023-12-01", "PURCHASE", "10000", "100"),  # free since 2 Dec 2026
                           txn("2024-02-01", "PURCHASE", "10000", "100")], "200")  # locked until 1 Feb 2027

    result = plan_for([fund], {"INF1": "ELSS"}, nav="200")

    (option,) = result["gainHarvest"]
    assert (option["units"], option["gain"], option["stoppedBy"]) == (100.0, 10000.0, "elss_lock")
    assert result["elssLocked"] == [{"name": "Fund INF1", "units": 100.0, "unlocksOn": "2027-02-02"}]


def test_planning_without_or_with_a_stale_nav():
    missing = scheme("INF1", [txn("2020-01-01", "PURCHASE", "10000", "100")], "100")
    stale = scheme("INF2", [txn("2020-01-01", "PURCHASE", "10000", "100")], "100")
    navs = {"INF2": nav_row("INF2", EQUITY, "500", "2026-12-01")}
    lots = mf_tax.tax_lots([missing, stale], {"INF1": nav_row("INF1", EQUITY), "INF2": nav_row("INF2", EQUITY)},
                           fmv={}.get)

    result = mf_tax.planning(lots, navs, TODAY, NO_GAINS_YET, None)

    assert result["left"] == [{"name": "Fund INF1",
                               "reason": "No NAV is available, so redemption figures cannot be worked out."}]
    (option,) = result["gainHarvest"]
    assert option["navDate"] == "2026-12-01" and option["navStale"] is True


def test_planning_notes_and_wording():
    result = plan_for([HARVESTABLE], {"INF1": EQUITY})

    for note in (mf_tax.DEADLINE_NOTE, mf_tax.EXIT_LOAD_NOTE, mf_tax.FIGURES_NOTE):
        assert note in result["notes"]
    assert result["asOf"] == "2027-01-15" and result["exemptionLeft"] == 125000.0

    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)

    for text in strings(result):
        assert not re.search(r"\b(you should|sell|buy|recommend)\b", text, re.IGNORECASE), text

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

from app import mf_math
from app.mf_math import Txn


def txn(day: str, kind: str, amount: str | None, units: str | None = None, nav: str | None = None) -> Txn:
    return Txn(date.fromisoformat(day), kind, None if amount is None else D(amount),
               None if units is None else D(units), None if nav is None else D(nav))


# Bought 100 units at 100 (stamp duty 0.50) and 40 at 125 (stamp 0.25); redeemed 120 at 110 (STT 0.13).
EXAMPLE = [
    txn("2024-01-10", "PURCHASE", "10000", "100", "100"),
    txn("2024-01-10", "STAMP_DUTY_TAX", "0.50"),
    txn("2024-06-10", "PURCHASE_SIP", "5000", "40", "125"),
    txn("2024-06-10", "STAMP_DUTY_TAX", "0.25"),
    txn("2025-03-10", "REDEMPTION", "-13200", "-120", "110"),
    txn("2025-03-10", "STT_TAX", "0.13"),
]


def test_fifo_with_stamp_duty():
    result = mf_math.fifo(EXAMPLE)

    first, second = result.disposals
    # First lot in full: 100 units, cost 10000 + 0.50 stamp, sale share 100/120 x 13200 = 11000.
    assert (first.buy_date, first.sell_date, first.units, first.cost, first.sale_value) == (
        date(2024, 1, 10), date(2025, 3, 10), D("100"), D("10000.50"), D("11000"))
    # 20 of the 40 SIP units: cost half of 5000.25, sale 20 x 110.
    assert (second.units, second.cost, second.sale_value) == (D("20"), D("2500.125"), D("2200"))
    assert first.stt + second.stt == D("0.13")
    (lot,) = result.open_lots
    assert (lot.buy_date, lot.units, lot.cost) == (date(2024, 6, 10), D("20"), D("2500.125"))
    # Realised gain: 11000 - 10000.50 + 2200 - 2500.125 = 699.375.
    assert sum(d.sale_value - d.cost for d in result.disposals) == D("699.375")
    # Cash flows from the investor's side: purchases with stamp duty out, the sale less STT in.
    assert result.cashflows == [
        (date(2024, 1, 10), D("-10000.50")), (date(2024, 6, 10), D("-5000.25")), (date(2025, 3, 10), D("13199.87")),
    ]
    assert result.problems == []


def test_same_day_purchases_share_the_stamp_duty():
    result = mf_math.fifo([
        txn("2025-01-01", "PURCHASE_SIP", "3000", "30", "100"),
        txn("2025-01-01", "PURCHASE", "1000", "10", "100"),
        txn("2025-01-01", "STAMP_DUTY_TAX", "0.20"),
        txn("2025-02-01", "STAMP_DUTY_TAX", "0.05"),
    ])

    # 0.20 split 3:1 by amount.
    assert [lot.cost for lot in result.open_lots] == [D("3000.15"), D("1000.05")]
    assert len(result.problems) == 1 and "2025-02-01" in result.problems[0]
    assert sum(amount for _, amount in result.cashflows) == D("-4000.25")


def test_switch_in_and_out_are_lots_and_disposals():
    result = mf_math.fifo([
        txn("2024-01-01", "SWITCH_IN", "5000", "50", "100"),
        txn("2024-03-01", "SWITCH_IN_MERGER", "1000", "10", "100"),
        txn("2025-01-01", "SWITCH_OUT", "-6600", "-55", "120"),
    ])

    assert [d.units for d in result.disposals] == [D("50"), D("5")]
    assert [(lot.units, lot.cost) for lot in result.open_lots] == [(D("5"), D("500"))]
    assert result.cashflows == [(date(2024, 1, 1), D("-5000")), (date(2024, 3, 1), D("-1000")), (date(2025, 1, 1), D("6600"))]


def test_dividend_reinvest_adds_a_lot_without_cash():
    result = mf_math.fifo([
        txn("2024-01-01", "PURCHASE", "1000", "10", "100"),
        txn("2024-06-01", "DIVIDEND_REINVEST", "50", "0.5", "100"),
    ])

    assert [(lot.units, lot.cost) for lot in result.open_lots] == [(D("10"), D("1000")), (D("0.5"), D("50"))]
    # The dividend never left the fund, so it is not a cash flow.
    assert result.cashflows == [(date(2024, 1, 1), D("-1000"))]


def test_dividend_payout_is_cash_not_a_disposal():
    result = mf_math.fifo([
        txn("2024-01-01", "PURCHASE", "1000", "10", "100"),
        txn("2024-06-01", "DIVIDEND_PAYOUT", "40"),
    ])

    assert result.disposals == [] and result.open_lots[0].units == D("10")
    assert result.cashflows[-1] == (date(2024, 6, 1), D("40"))


def test_negative_reversal_cancels_the_matching_lot():
    result = mf_math.fifo([
        txn("2024-01-01", "PURCHASE", "1000", "100", "10"),
        txn("2024-02-01", "PURCHASE", "500", "50", "10"),
        txn("2024-02-03", "REVERSAL", "-500", "-50", "10"),
    ])

    assert [(lot.buy_date, lot.units) for lot in result.open_lots] == [(date(2024, 1, 1), D("100"))]
    assert result.disposals == []
    assert sum(amount for _, amount in result.cashflows) == D("-1000")


def test_reversal_without_a_matching_purchase_is_a_problem():
    result = mf_math.fifo([
        txn("2024-01-01", "PURCHASE", "1000", "100", "10"),
        txn("2024-02-03", "REVERSAL", "-500", "-50", "10"),
    ])

    assert len(result.problems) == 1
    assert result.open_lots[0].units == D("100")


def test_overselling_is_a_problem_not_a_negative_lot():
    result = mf_math.fifo([
        txn("2024-01-01", "PURCHASE", "1000", "10", "100"),
        txn("2024-06-01", "REDEMPTION", "-1320", "-12", "110"),
    ])

    assert len(result.problems) == 1
    assert result.open_lots == []
    assert sum(d.units for d in result.disposals) == D("10")


def test_rounding_dust_is_not_overselling():
    result = mf_math.fifo([
        txn("2024-01-01", "PURCHASE", "1000", "10", "100"),
        txn("2024-06-01", "REDEMPTION", "-1100.05", "-10.0005", "110"),
    ])

    assert result.problems == [] and result.open_lots == []


SPREADSHEET = [
    (date(2008, 1, 1), D("-10000")), (date(2008, 3, 1), D("2750")), (date(2008, 10, 30), D("4250")),
    (date(2009, 2, 15), D("3250")), (date(2009, 4, 1), D("2750")),
]


def test_xirr_matches_the_spreadsheet_example():
    # Excel's documented XIRR example: 0.373362535 (37.34%).
    result = mf_math.xirr(SPREADSHEET)

    assert result.reason is None
    assert abs(result.rate - D("0.373362535")) < D("0.0001")


def test_xirr_two_flows_closed_form():
    # 1.21 ** (365 / 731) - 1 = 0.0998565877
    result = mf_math.xirr([(date(2024, 1, 1), D("-10000")), (date(2026, 1, 1), D("12100"))])

    assert abs(result.rate - D("0.0998565877")) < D("0.0001")


def test_xirr_negative_return():
    # 0.8 ** (365 / 731) - 1 = -0.1054362831
    result = mf_math.xirr([(date(2024, 1, 1), D("-10000")), (date(2026, 1, 1), D("8000"))])

    assert abs(result.rate - D("-0.1054362831")) < D("0.0001")


def test_xirr_sums_flows_on_the_same_date():
    result = mf_math.xirr([
        (date(2024, 1, 1), D("-6000")), (date(2024, 1, 1), D("-4000")), (date(2026, 1, 1), D("12100")),
    ])

    assert abs(result.rate - D("0.0998565877")) < D("0.0001")


def test_xirr_without_sign_change():
    assert mf_math.xirr([(date(2024, 1, 1), D("-1000")), (date(2025, 1, 1), D("0"))]) == mf_math.XirrResult(None, "no_sign_change")
    assert mf_math.xirr([(date(2024, 1, 1), D("-1000")), (date(2024, 1, 1), D("400"))]).reason == "no_sign_change"


def test_xirr_no_flows():
    assert mf_math.xirr([]) == mf_math.XirrResult(None, "no_flows")


def test_xirr_falls_back_to_bisection(monkeypatch):
    monkeypatch.setattr(mf_math, "NEWTON_ITERATIONS", 0)

    result = mf_math.xirr(SPREADSHEET)

    assert abs(result.rate - D("0.373362535")) < D("0.0001")


def test_xirr_without_a_root_in_range_gives_a_reason(monkeypatch):
    monkeypatch.setattr(mf_math, "NEWTON_ITERATIONS", 0)
    # A 1000-fold gain in one day is far beyond the bisection range.
    result = mf_math.xirr([(date(2024, 1, 1), D("-1")), (date(2024, 1, 2), D("1000"))])

    assert result == mf_math.XirrResult(None, "no_convergence")


def test_absolute_return():
    flows = [(date(2025, 1, 1), D("-10000")), (date(2025, 3, 1), D("500")), (date(2025, 6, 1), D("10200"))]

    assert mf_math.absolute_return_pct(flows) == D("7")
    assert mf_math.absolute_return_pct([(date(2025, 1, 1), D("500"))]) is None

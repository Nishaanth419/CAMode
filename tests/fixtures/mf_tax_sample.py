"""A synthetic mutual fund ledger for tax and returns cases (today 28 Oct 2026, FY 2026-27).

- Equity Large Cap: 1,000 units bought 10 Apr 2019 at 100, 250 on 10 May 2021 at 200, 100 on 20 Jan 2026 at 300;
  400 redeemed on 15 Jun 2026 at 300 (STT paid). The sale takes 400 of the 2019 lot: cost 40,000, sale 1,20,000,
  long-term gain 80,000, so 45,000 of the Rs 1,25,000 exemption is left. NAV 320: the rest of the 2019 lot
  (600 units, gain 1,32,000) is long-term; the Jan 2026 lot (gain 2,000) turns long-term on 21 Jan 2027.
- Corporate Bond (a specified fund): 10,000 units bought 1 Feb 2024 at 20; 5,000 redeemed 1 Aug 2026 at 22,
  a slab-taxed short-term gain of 10,000.
- Equity Small Cap: 1,000 units bought 10 Feb 2026 at 50, NAV 42: an unrealised short-term loss of 8,000.
No 31 Jan 2018 NAVs are needed (nothing was bought before 2018).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

from app.amfi import NavRow
from app.mf_ledger import LedgerScheme
from app.mf_math import Txn

TODAY = date(2026, 10, 28)
NAV_DATE = date(2026, 10, 27)
LARGE, BOND, SMALL = "INF000T01LC1", "INF000T01CB2", "INF000T01SC3"


def _txn(day: str, kind: str, amount: str, units: str | None = None) -> Txn:
    return Txn(date.fromisoformat(day), kind, D(amount), None if units is None else D(units), None)


def _scheme(isin: str, name: str, transactions: list[Txn], close: str) -> LedgerScheme:
    return LedgerScheme(isin=isin, amfi=None, name=name, amc="Sample AMC", rta="CAMS", folio_key="sample",
                        open=D(0), close=D(close), valuation_date=date(2026, 9, 30), valuation_nav=None,
                        transactions=transactions, period_from=date(2019, 4, 1), period_to=date(2026, 9, 30))


SCHEMES = [
    _scheme(LARGE, "Sample Large Cap Fund - Direct Growth", [
        _txn("2019-04-10", "PURCHASE", "100000", "1000"),
        _txn("2021-05-10", "PURCHASE", "50000", "250"),
        _txn("2026-01-20", "PURCHASE", "30000", "100"),
        _txn("2026-06-15", "REDEMPTION", "-120000", "-400"),
        _txn("2026-06-15", "STT_TAX", "1.20"),
    ], "950"),
    _scheme(BOND, "Sample Corporate Bond Fund - Direct Growth", [
        _txn("2024-02-01", "PURCHASE", "200000", "10000"),
        _txn("2026-08-01", "REDEMPTION", "-110000", "-5000"),
    ], "5000"),
    _scheme(SMALL, "Sample Small Cap Fund - Direct Growth", [
        _txn("2026-02-10", "PURCHASE", "50000", "1000"),
    ], "1000"),
]

NAVS = {
    LARGE: NavRow(LARGE, "900001", "Sample Large Cap Fund", "Equity Scheme - Large Cap Fund", D("320"), NAV_DATE),
    BOND: NavRow(BOND, "900002", "Sample Corporate Bond Fund", "Debt Scheme - Corporate Bond Fund", D("22.5"), NAV_DATE),
    SMALL: NavRow(SMALL, "900003", "Sample Small Cap Fund", "Equity Scheme - Small Cap Fund", D("42"), NAV_DATE),
}

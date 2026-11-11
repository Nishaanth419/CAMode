"""Detailed CAMS/KFintech statements built from casparser's own models, for the mutual fund ledger tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from casparser.enums import FileType
from casparser.types import CASData, Folio, InvestorInfo, Scheme, SchemeValuation, StatementPeriod, TransactionData

PERSONAL = ("Test Investor", "ABCDE1234F", "investor@example.com", "9876543210", "12 Example Road")
FOLIO_NUMBER = "91234567/89"
ADVISOR = "ARN-123456"
NOMINEE = "Nominee Person"
DESCRIPTION = "Purchase - via Internet - Ref 777001"


def t(day: str, kind: str, amount: str | None, units: str | None = None, nav: str | None = None) -> TransactionData:
    return TransactionData(
        date=date.fromisoformat(day), description=DESCRIPTION, type=kind,
        amount=None if amount is None else Decimal(amount), units=None if units is None else Decimal(units),
        nav=None if nav is None else Decimal(nav), balance=Decimal("0"),
    )


# Bought 100 units at 100 (stamp 0.50) and 40 at 125 (stamp 0.25); redeemed 120 at 110 (STT 0.13); 20 left.
EXAMPLE_TRANSACTIONS = [
    ("2024-01-10", "PURCHASE", "10000", "100", "100"),
    ("2024-01-10", "STAMP_DUTY_TAX", "0.50", None, None),
    ("2024-06-10", "PURCHASE_SIP", "5000", "40", "125"),
    ("2024-06-10", "STAMP_DUTY_TAX", "0.25", None, None),
    ("2025-03-10", "REDEMPTION", "-13200", "-120", "110"),
    ("2025-03-10", "STT_TAX", "0.13", None, None),
]


def scheme(
    name: str = "HDFC Index Fund Nifty 50 Plan - Direct", isin: str | None = "INF179K01VK6", *,
    amfi: str | None = "120716", open_units: str = "0", close: str = "20",
    transactions: list[tuple] | None = None, valuation: tuple[str, str] = ("30-Sep-2026", "125"),
) -> Scheme:
    rows = [t(*row) for row in (EXAMPLE_TRANSACTIONS if transactions is None else transactions)]
    nav = Decimal(valuation[1])
    return Scheme(
        scheme=name, advisor=ADVISOR, rta_code="H1", rta="CAMS", isin=isin, amfi=amfi, nominees=[NOMINEE],
        open=Decimal(open_units), close=Decimal(close), close_calculated=Decimal(close),
        valuation=SchemeValuation(date=valuation[0], nav=nav, value=nav * Decimal(close), cost=None),
        transactions=rows,
    )


def statement(
    *schemes: Scheme, period: tuple[str, str] = ("01-Jan-2020", "30-Sep-2026"), folio: str = FOLIO_NUMBER,
    cas_type: str = "DETAILED", file_type: FileType = FileType.CAMS,
) -> CASData:
    return CASData(
        statement_period=StatementPeriod(**{"from": period[0], "to": period[1]}),
        folios=[Folio(folio=folio, amc="HDFC Mutual Fund", PAN=PERSONAL[1], KYC="OK", PANKYC="OK",
                      name=PERSONAL[0], schemes=list(schemes or [scheme()]))],
        investor_info=InvestorInfo(name=PERSONAL[0], email=PERSONAL[2], address=PERSONAL[4], mobile=PERSONAL[3]),
        cas_type=cas_type, file_type=file_type,
    )

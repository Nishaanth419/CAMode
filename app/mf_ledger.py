"""The mutual fund transaction ledger kept from detailed CAMS and KFintech statements.

``extract()`` runs on upload (via ``cas.parse_statement``) and keeps, per scheme, only
what returns and tax need: ISIN, AMFI code, scheme and AMC names, RTA, opening and
closing units, the statement's valuation, and each transaction's date, type, amount,
units and NAV. The folio number is replaced by ``folioKey``, an HMAC-SHA256 under a
random per-profile salt (sealed in ``users.folio_salt``), so one profile's uploads agree
on a folio and two profiles never do. Never kept: names, PAN, KYC, nominees, advisor,
transaction descriptions and running balances.

``build()`` merges every stored statement (the newest statement period wins for each
folio and scheme) and gives each scheme a status with a reason:

- ``unsupported``: a gift, segregation, miscellaneous or unknown transaction moved units.
- ``unreconciled``: opening units plus the transactions differ from the closing units.
- ``incomplete_history``: units were held before the statement period began.
- ``ok`` otherwise.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

from .db import connect_db
from .mf_math import Txn
from .sealing import LockedError, ProfileKeys

UNSUPPORTED_TYPES: Final = ("GIFT_IN", "GIFT_OUT", "SEGREGATION", "MISC", "UNKNOWN")
UNIT_TOLERANCE: Final = Decimal("0.001")
LEDGER_SOURCES: Final = frozenset({"CAMS", "KFINTECH"})
HINT: Final = "For returns and tax, upload a detailed CAS from CAMS or KFintech covering since inception."
_DATE_FORMATS: Final = ("%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y")


@dataclass
class LedgerScheme:
    isin: str | None
    amfi: str | None
    name: str
    amc: str | None
    rta: str | None
    folio_key: str
    open: Decimal
    close: Decimal
    valuation_date: date | None
    valuation_nav: Decimal | None
    transactions: list[Txn] = field(default_factory=list)
    period_from: date | None = None
    period_to: date | None = None
    status: str = "ok"
    reason: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.folio_key, scheme_id(self.isin, self.amfi, self.name)


def scheme_id(isin: str | None, amfi: str | None, name: str) -> str:
    return isin or (f"amfi:{amfi}" if amfi else f"name:{name}")


def parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    for pattern in _DATE_FORMATS:
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


# ── Upload ─────────────────────────────────────────────────────────────────────

def folio_salt(user_id: str, keys: ProfileKeys) -> bytes:
    """The profile's random folio salt, created on first use. Raises ``LockedError`` when locked."""
    if not keys.is_unlocked:
        raise LockedError("The profile is locked.")
    with connect_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT folio_salt FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise LookupError("No such profile.")
        if row["folio_salt"]:
            return base64.b64decode(keys.open(row["folio_salt"]))
        salt = secrets.token_bytes(32)
        connection.execute("UPDATE users SET folio_salt = ? WHERE id = ?",
                           (keys.seal(base64.b64encode(salt).decode()), user_id))
    return salt


def folio_key_for(salt: bytes) -> Callable[[str], str]:
    def folio_key(folio: str) -> str:
        return hmac.new(salt, folio.strip().encode(), hashlib.sha256).hexdigest()

    return folio_key


def extract(data: Any, folio_key: Callable[[str], str]) -> dict[str, Any] | None:
    """The ledger of a detailed CAMS/KFintech ``CASData``; None for any other statement."""
    if not hasattr(data, "folios") or not hasattr(data, "cas_type"):
        return None  # A depository (NSDL/CDSL) statement: holdings only.
    source = str(getattr(data.file_type, "value", data.file_type))
    cas_type = str(getattr(data.cas_type, "value", data.cas_type))
    if source not in LEDGER_SOURCES or cas_type != "DETAILED":
        return None
    schemes = []
    for folio in data.folios:
        key = folio_key(folio.folio)
        for scheme in folio.schemes:
            valuation = scheme.valuation
            valuation_date = parse_date(valuation.date)
            schemes.append({
                "isin": scheme.isin, "amfi": scheme.amfi, "name": scheme.scheme, "amc": folio.amc, "rta": scheme.rta,
                "folioKey": key, "open": _text(scheme.open), "close": _text(scheme.close),
                "valuation": {"date": valuation_date.isoformat() if valuation_date else None,
                              "nav": _text(valuation.nav), "value": _text(valuation.value)},
                "transactions": [
                    {"date": day.isoformat(), "type": str(getattr(txn.type, "value", txn.type)),
                     "amount": _text(txn.amount), "units": _text(txn.units), "nav": _text(txn.nav)}
                    for txn in scheme.transactions if (day := parse_date(txn.date)) is not None
                ],
            })
    return {"schemes": schemes}


# ── Merging and status ─────────────────────────────────────────────────────────

def _status(item: LedgerScheme) -> tuple[str, str | None]:
    for txn in item.transactions:
        if txn.type in UNSUPPORTED_TYPES and txn.units:
            return "unsupported", f"Has a {txn.type} transaction, which is not supported yet."
    moved = sum((txn.units for txn in item.transactions if txn.units is not None), Decimal(0))
    if abs(item.open + moved - item.close) > UNIT_TOLERANCE:
        return "unreconciled", (
            f"Units do not add up: opening {item.open} plus transactions {moved} is not the closing {item.close}."
        )
    if item.open > 0:
        start = f"{item.period_from:%d %b %Y}" if item.period_from else "the statement period"
        return "incomplete_history", f"History before {start} is missing; upload a since-inception CAS."
    return "ok", None


def _scheme(raw: dict[str, Any], period_from: date | None, period_to: date | None) -> LedgerScheme:
    valuation = raw.get("valuation") or {}
    return LedgerScheme(
        isin=raw.get("isin"), amfi=raw.get("amfi"), name=raw.get("name") or "", amc=raw.get("amc"), rta=raw.get("rta"),
        folio_key=raw["folioKey"], open=Decimal(raw.get("open") or "0"), close=Decimal(raw.get("close") or "0"),
        valuation_date=parse_date(valuation.get("date")), valuation_nav=_decimal(valuation.get("nav")),
        transactions=[
            Txn(date.fromisoformat(t["date"]), t["type"], _decimal(t["amount"]), _decimal(t["units"]), _decimal(t["nav"]))
            for t in raw.get("transactions") or []
        ],
        period_from=period_from, period_to=period_to,
    )


def build(statements: list[dict[str, Any]]) -> list[LedgerScheme]:
    """One entry per (folio, scheme) across the stored statements; the newest statement period wins."""
    with_ledger = [s for s in statements if s.get("ledger")]
    with_ledger.sort(key=lambda s: (parse_date(s.get("period_to")) or date.min, s.get("uploadedAt") or 0))
    merged: dict[tuple[str, str], LedgerScheme] = {}
    for item in with_ledger:
        period_from, period_to = parse_date(item.get("period_from")), parse_date(item.get("period_to"))
        for raw in item["ledger"]["schemes"]:
            scheme = _scheme(raw, period_from, period_to)
            merged[scheme.key] = scheme
    for scheme in merged.values():
        scheme.status, scheme.reason = _status(scheme)
    return list(merged.values())


def ledger_for(user_id: str, keys: ProfileKeys) -> list[LedgerScheme]:
    """The merged ledger from the profile's stored statements. Raises ``LockedError`` when locked."""
    from . import cas  # cas imports this module for the upload

    return build(cas.all_statements(user_id, keys))


def has_ledger(user_id: str, keys: ProfileKeys) -> bool:
    try:
        return bool(ledger_for(user_id, keys))
    except LockedError:
        return False

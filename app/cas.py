"""Consolidated Account Statement (CAS) uploads: parse in memory, keep only holdings, seal them.

An NSDL or CDSL CAS lists every demat account the user has, at every broker, plus
mutual fund folios and NPS; a CAMS or KFintech CAS lists mutual fund folios. That
covers brokers INDmoney's MCP server does not. Parsing uses ``casparser`` offline.

Only holdings are kept: name, ISIN, broker (the demat account's depository
participant), units, price, value and cost where the statement gives one. The PDF,
its password, and the investor's name, PAN, address, contact details and DP/client
IDs are never stored. Holdings are sealed with the profile's keys like snapshots.

Rows use INDmoney's holdings layout so ``portfolio_math.compute_metrics`` computes
the same figures for both sources.
"""

from __future__ import annotations

import io
import json
import logging
from decimal import Decimal
from typing import Any, Final

import casparser
from casparser.exceptions import IncorrectPasswordError, ParserException

from . import users
from .db import connect_db
from .portfolio_math import HOLDINGS_PREFIX, compute_metrics
from .sealing import ProfileKeys

logger = logging.getLogger("ca_mode.cas")

MAX_PDF_BYTES: Final = 15 * 1024 * 1024
KEEP_STATEMENTS: Final = 12
MF_FOLIOS: Final = "Mutual fund folios"


class CASError(Exception):
    """The statement could not be read; the message is safe to show the user."""


def _row(
    name: str | None, code: str | None, asset_type: str, broker: str,
    units: Decimal | None, price: Decimal | None, value: Decimal, invested: Decimal | None = None,
) -> dict[str, Any]:
    """One holding in INDmoney's ``networth_holdings`` row layout (amounts as exact strings)."""
    as_text = lambda amount: None if amount is None else str(amount)  # noqa: E731 - local one-liner
    return {
        "investment": name or code, "investment_code": code, "asset_type": asset_type, "broker": broker,
        "total_units": as_text(units), "unit_price": as_text(price), "market_value": as_text(value),
        "invested_amount": as_text(invested) if invested and invested > 0 else "0",
    }


def _demat_rows(data: Any) -> list[dict[str, Any]]:
    rows = []
    for account in data.accounts:
        broker = MF_FOLIOS if account.type == "Mutual Fund Folios" else (account.name or "Demat account")
        for equity in account.equities:
            rows.append(_row(equity.name or equity.symbol, equity.isin, "IND_STOCK", broker,
                             equity.num_shares, equity.price, equity.value))
        for fund in account.mutual_funds:
            rows.append(_row(fund.name, fund.isin, "MF", broker, fund.balance, fund.nav, fund.value, fund.total_cost))
        for bond in account.bonds:
            rows.append(_row(bond.name, bond.isin, "BOND", broker, bond.num_bonds, bond.market_price, bond.value))
    if data.nps:
        for scheme in data.nps.schemes:
            rows.append(_row(scheme.scheme, None, "NPS", "NPS", scheme.units, scheme.nav, scheme.value))
    return rows


def _folio_rows(data: Any) -> list[dict[str, Any]]:
    return [
        _row(scheme.scheme, scheme.isin or scheme.amfi, "MF", MF_FOLIOS, scheme.close, scheme.valuation.nav,
             scheme.valuation.value, scheme.valuation.cost)
        for folio in data.folios for scheme in folio.schemes
    ]


def parse_statement(pdf: bytes, password: str) -> dict[str, Any]:
    """Parse a CAS PDF into holdings rows and statement metadata. Raises ``CASError``."""
    if len(pdf) > MAX_PDF_BYTES:
        raise CASError("That file is larger than 15 MB; a CAS is usually much smaller.")
    if not pdf.startswith(b"%PDF"):
        raise CASError("That file is not a PDF. Upload the CAS PDF emailed by NSDL, CDSL, CAMS or KFintech.")
    try:
        data = casparser.read_cas_pdf(io.BytesIO(pdf), password)
    except IncorrectPasswordError as exc:
        raise CASError("Wrong password. A CAS is usually locked with your PAN in capital letters.") from exc
    except ParserException as exc:
        raise CASError(
            "Could not read this statement. Supported: NSDL or CDSL consolidated statements, and CAMS or KFintech "
            "mutual fund statements."
        ) from exc
    except Exception as exc:  # pypdfium2 raises its own errors for damaged files.
        logger.warning("cas_parse_failed: %s", type(exc).__name__)
        raise CASError("This PDF could not be opened. Check the file and the password.") from exc
    rows = _demat_rows(data) if hasattr(data, "accounts") else _folio_rows(data)
    if not rows:
        raise CASError("No holdings were found in this statement.")
    return {
        # casparser's models store enums by value, so this is usually already a string ("CDSL").
        "source": str(getattr(data.file_type, "value", data.file_type)),
        "period_from": data.statement_period.from_,
        "period_to": data.statement_period.to,
        "rows": rows,
        "warnings": [str(warning)[:200] for warning in getattr(data, "parse_warnings", [])][:10],
    }


def statement_metrics(statement: dict[str, Any], *, include_all_holdings: bool = False) -> dict[str, Any]:
    """The portfolio figures for a stored statement, computed like INDmoney snapshots."""
    by_type: dict[str, list[dict[str, Any]]] = {}
    for row in statement["rows"]:
        by_type.setdefault(row["asset_type"], []).append(row)
    results = {HOLDINGS_PREFIX + asset_type: json.dumps({"holdings": rows}) for asset_type, rows in by_type.items()}
    return compute_metrics(results, include_all_holdings=include_all_holdings)


def _summary(statement: dict[str, Any]) -> dict[str, Any]:
    figures = statement_metrics(statement)
    return {
        "source": statement["source"],
        "periodTo": statement["period_to"],
        "holdings": figures["concentration"]["holdings_count"],
        "valueInr": round(sum((row["current_value_inr"] or 0) for row in figures["by_broker"]), 2),
        "brokers": [{"broker": row["broker"], "valueInr": row["current_value_inr"]} for row in figures["by_broker"]],
    }


def store_statement(user_id: str, statement: dict[str, Any], keys: ProfileKeys) -> dict[str, Any]:
    """Seal and save a parsed statement, keeping the newest ``KEEP_STATEMENTS``; return its summary."""
    statement_id, now = users.random_token(), users.now_ms()
    with connect_db() as connection:
        connection.execute(
            "INSERT INTO cas_statements(id, user_id, uploaded_at, data) VALUES (?, ?, ?, ?)",
            (statement_id, user_id, now, keys.seal(json.dumps(statement, ensure_ascii=False))),
        )
        connection.execute(
            "DELETE FROM cas_statements WHERE user_id = ? AND id NOT IN "
            "(SELECT id FROM cas_statements WHERE user_id = ? ORDER BY uploaded_at DESC LIMIT ?)",
            (user_id, user_id, KEEP_STATEMENTS),
        )
    return {"id": statement_id, "uploadedAt": now, **_summary(statement)}


def _rows(user_id: str, limit: int | None = None) -> list[Any]:
    with connect_db() as connection:
        return connection.execute(
            "SELECT id, uploaded_at, data FROM cas_statements WHERE user_id = ? ORDER BY uploaded_at DESC LIMIT ?",
            (user_id, -1 if limit is None else limit),
        ).fetchall()


def has_statement(user_id: str) -> bool:
    with connect_db() as connection:
        row = connection.execute("SELECT 1 FROM cas_statements WHERE user_id = ? LIMIT 1", (user_id,)).fetchone()
    return row is not None


def list_statements(user_id: str, keys: ProfileKeys) -> list[dict[str, Any]]:
    """Summaries of stored statements, newest first. Raises ``sealing.LockedError`` when locked."""
    return [
        {"id": row["id"], "uploadedAt": row["uploaded_at"], **_summary(json.loads(keys.open(row["data"])))}
        for row in _rows(user_id)
    ]


def latest_statement(user_id: str, keys: ProfileKeys) -> dict[str, Any] | None:
    rows = _rows(user_id, limit=1)
    if not rows:
        return None
    return {"id": rows[0]["id"], "uploaded_at": rows[0]["uploaded_at"], **json.loads(keys.open(rows[0]["data"]))}


def all_statements(user_id: str, keys: ProfileKeys) -> list[dict[str, Any]]:
    """Every stored statement with its holdings (for export)."""
    return [{"uploadedAt": row["uploaded_at"], **json.loads(keys.open(row["data"]))} for row in _rows(user_id)]


def delete_statement(user_id: str, statement_id: str) -> bool:
    with connect_db() as connection:
        deleted = connection.execute(
            "DELETE FROM cas_statements WHERE user_id = ? AND id = ?", (user_id, statement_id),
        ).rowcount
    return deleted > 0

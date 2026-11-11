from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from casparser.enums import FileType

from app import cas, mf_ledger, sealing, unlock, users
from app.db import connect_db
from mf_fixtures import ADVISOR, DESCRIPTION, FOLIO_NUMBER, NOMINEE, PERSONAL, scheme, statement

PDF = b"%PDF-1.7 synthetic"


def keys_of(client, user_id):
    user = users.get_user(user_id)
    return unlock.keys_for_session(user, client.cookies.get("indmoney_session"))


def stored(data, *, uploaded_at: int = 1, folio_key=lambda folio: "key-" + folio[-2:]) -> dict:
    """A statement as cas.all_statements() returns it."""
    return {
        "uploadedAt": uploaded_at, "source": "CAMS", "period_from": data.statement_period.from_,
        "period_to": data.statement_period.to, "rows": [], "warnings": [],
        "ledger": mf_ledger.extract(data, folio_key),
    }


def test_extract_keeps_transactions_and_nothing_personal():
    ledger = mf_ledger.extract(statement(), mf_ledger.folio_key_for(b"s" * 32))

    (item,) = ledger["schemes"]
    assert item["isin"] == "INF179K01VK6" and item["amfi"] == "120716" and item["amc"] == "HDFC Mutual Fund"
    assert item["rta"] == "CAMS" and item["open"] == "0" and item["close"] == "20"
    assert item["valuation"] == {"date": "2026-09-30", "nav": "125", "value": "2500"}
    assert item["transactions"][0] == {"date": "2024-01-10", "type": "PURCHASE", "amount": "10000", "units": "100", "nav": "100"}
    assert item["transactions"][1] == {"date": "2024-01-10", "type": "STAMP_DUTY_TAX", "amount": "0.50", "units": None, "nav": None}
    assert len(item["folioKey"]) == 64
    text = json.dumps(ledger)
    for value in (*PERSONAL, FOLIO_NUMBER, ADVISOR, NOMINEE, DESCRIPTION, "balance"):
        assert value not in text


def test_summary_and_demat_statements_have_no_ledger():
    key = mf_ledger.folio_key_for(b"s" * 32)
    assert mf_ledger.extract(statement(cas_type="SUMMARY"), key) is None
    assert mf_ledger.extract(statement(file_type=FileType.KFINTECH), key) is not None


def test_parse_statement_adds_the_ledger_only_with_a_folio_key(monkeypatch):
    monkeypatch.setattr(cas.casparser, "read_cas_pdf", lambda file, password: statement())

    assert "ledger" not in cas.parse_statement(PDF, "x")
    assert cas.parse_statement(PDF, "x", folio_key=mf_ledger.folio_key_for(b"s" * 32))["ledger"]["schemes"]


def test_folio_keys_are_stable_per_profile_and_differ_across_profiles(client):
    from conftest import sign_up

    first = sign_up(client, "Asha")
    first_keys = keys_of(client, first["id"])
    salt = mf_ledger.folio_salt(first["id"], first_keys)
    assert mf_ledger.folio_salt(first["id"], first_keys) == salt and len(salt) == 32
    with connect_db() as connection:
        column = connection.execute("SELECT folio_salt FROM users WHERE id = ?", (first["id"],)).fetchone()[0]
    assert sealing.is_sealed(column)

    second = sign_up(client, "Ravi")
    other = mf_ledger.folio_salt(second["id"], keys_of(client, second["id"]))
    assert mf_ledger.folio_key_for(salt)(FOLIO_NUMBER) == mf_ledger.folio_key_for(salt)(FOLIO_NUMBER)
    assert mf_ledger.folio_key_for(salt)(FOLIO_NUMBER) != mf_ledger.folio_key_for(other)(FOLIO_NUMBER)


def test_folio_salt_needs_the_unlocked_profile(client, signed_in):
    with pytest.raises(sealing.LockedError):
        mf_ledger.folio_salt(signed_in["id"], sealing.ProfileKeys())


def test_newest_statement_wins_per_folio_and_isin():
    two = [("2024-01-10", "PURCHASE", "1000", "10", "100"), ("2024-02-10", "PURCHASE", "1000", "10", "100")]
    three = [*two, ("2026-08-10", "PURCHASE", "1000", "10", "100")]
    older = statement(scheme(close="20", transactions=two), scheme("Axis Liquid", "INF846K01CH7", close="0", transactions=[]),
                      period=("01-Jan-2020", "31-Mar-2026"))
    newer = statement(scheme(close="30", transactions=three), period=("01-Jan-2020", "30-Sep-2026"))

    # Uploaded in the other order: the statement period decides, not the upload time.
    ledger = mf_ledger.build([stored(newer, uploaded_at=1), stored(older, uploaded_at=2)])

    by_isin = {item.isin: item for item in ledger}
    assert len(by_isin["INF179K01VK6"].transactions) == 3
    assert by_isin["INF179K01VK6"].period_to == date(2026, 9, 30)
    assert "INF846K01CH7" in by_isin


def test_scheme_without_isin_merges_by_amfi_code():
    older = statement(scheme(isin=None, close="10", transactions=[("2024-01-10", "PURCHASE", "1000", "10", "100")]),
                      period=("01-Jan-2020", "31-Mar-2026"))
    newer = statement(scheme(isin=None, close="20", transactions=[
        ("2024-01-10", "PURCHASE", "1000", "10", "100"), ("2026-05-10", "PURCHASE", "1000", "10", "100"),
    ]))

    (item,) = mf_ledger.build([stored(older), stored(newer)])

    assert item.isin is None and item.amfi == "120716" and len(item.transactions) == 2


def test_open_units_mark_incomplete_history():
    (item,) = mf_ledger.build([stored(statement(
        scheme(open_units="50", close="70", transactions=[("2024-06-10", "PURCHASE", "2000", "20", "100")]),
        period=("01-Apr-2024", "30-Sep-2026"),
    ))])

    assert item.status == "incomplete_history"
    assert item.reason == "History before 01 Apr 2024 is missing; upload a since-inception CAS."


def test_units_that_do_not_add_up_are_unreconciled():
    (item,) = mf_ledger.build([stored(statement(scheme(close="25")))])

    assert item.status == "unreconciled"
    assert "do not add up" in item.reason


def test_gift_transactions_are_unsupported():
    (item,) = mf_ledger.build([stored(statement(scheme(close="30", transactions=[
        ("2024-01-10", "PURCHASE", "1000", "10", "100"), ("2024-03-10", "GIFT_IN", "2000", "20", "100"),
    ])))])

    assert item.status == "unsupported" and "GIFT_IN" in item.reason


def test_clean_scheme_is_ok():
    (item,) = mf_ledger.build([stored(statement())])

    assert (item.status, item.reason) == ("ok", None)
    assert item.close == Decimal("20") and item.valuation_nav == Decimal("125") and item.valuation_date == date(2026, 9, 30)
    assert item.transactions[0].amount == Decimal("10000") and item.transactions[0].date == date(2024, 1, 10)
    assert item.name == "HDFC Index Fund Nifty 50 Plan - Direct"


def test_statements_without_a_ledger_are_skipped():
    plain = {"uploadedAt": 1, "source": "CDSL", "period_from": "01-Sep-2026", "period_to": "30-Sep-2026", "rows": []}

    assert mf_ledger.build([plain]) == []


def test_ledger_for_reads_the_stored_statements(client, signed_in, monkeypatch):
    keys = keys_of(client, signed_in["id"])
    assert mf_ledger.has_ledger(signed_in["id"], keys) is False
    monkeypatch.setattr(cas.casparser, "read_cas_pdf", lambda file, password: statement())
    key = mf_ledger.folio_key_for(mf_ledger.folio_salt(signed_in["id"], keys))
    cas.store_statement(signed_in["id"], cas.parse_statement(PDF, "x", folio_key=key), keys)

    assert [item.isin for item in mf_ledger.ledger_for(signed_in["id"], keys)] == ["INF179K01VK6"]
    assert mf_ledger.has_ledger(signed_in["id"], keys) is True
    assert mf_ledger.has_ledger(signed_in["id"], sealing.ProfileKeys()) is False

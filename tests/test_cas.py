from __future__ import annotations

import asyncio
import base64
import json
from decimal import Decimal

import pytest
from casparser.enums import FileType
from casparser.exceptions import CASParseError, IncorrectPasswordError
from casparser.types import (
    CASData, DematAccount, DematOwner, Equity, Folio, InvestorInfo, MutualFund, NSDLCASData, Scheme,
    SchemeValuation, StatementPeriod,
)

from app import cas, privacy, sealing, snapshots, unlock, users

PDF = b"%PDF-1.7 synthetic"
PERSONAL = ("Test Investor", "ABCDE1234F", "investor@example.com", "9876543210", "12 Example Road", "IN300000", "10000001")


def investor() -> InvestorInfo:
    return InvestorInfo(name=PERSONAL[0], email=PERSONAL[2], address=PERSONAL[4], mobile=PERSONAL[3])


def nsdl_statement() -> NSDLCASData:
    owners = [DematOwner(name=PERSONAL[0], PAN=PERSONAL[1])]
    zerodha = DematAccount(
        name="ZERODHA BROKING LIMITED", type="CDSL Demat Account", dp_id=PERSONAL[5], client_id=PERSONAL[6],
        folios=2, balance=Decimal("230000"), owners=owners,
        equities=[
            Equity(name="HDFC BANK LIMITED", isin="INE040A01034", num_shares=Decimal("80"), price=Decimal("1625"), value=Decimal("130000")),
            Equity(name="INFOSYS LIMITED", isin="INE009A01021", num_shares=Decimal("50"), price=Decimal("2000"), value=Decimal("100000")),
        ],
        mutual_funds=[],
    )
    groww = DematAccount(
        name="NEXTBILLION TECHNOLOGY PRIVATE LIMITED", type="CDSL Demat Account", folios=1, balance=Decimal("70000"),
        owners=owners, mutual_funds=[],
        equities=[Equity(name="TCS LIMITED", isin="INE467B01029", num_shares=Decimal("20"), price=Decimal("3500"), value=Decimal("70000"))],
    )
    folios = DematAccount(
        name="Mutual Fund Folios", type="Mutual Fund Folios", folios=1, balance=Decimal("150000"), owners=owners,
        equities=[],
        mutual_funds=[MutualFund(name="PARAG PARIKH FLEXI CAP FUND - DIRECT", isin="INF879O01027", balance=Decimal("2000"),
                                 nav=Decimal("75"), value=Decimal("150000"), total_cost=Decimal("120000"))],
    )
    return NSDLCASData(
        accounts=[zerodha, groww, folios], statement_period=StatementPeriod(**{"from": "01-Sep-2026", "to": "30-Sep-2026"}),
        investor_info=investor(), file_type=FileType.CDSL,
    )


def cams_statement() -> CASData:
    scheme = Scheme(
        scheme="HDFC Index Fund Nifty 50 Plan - Direct", rta_code="H1", rta="CAMS", isin="INF179K01VK6",
        open=Decimal("0"), close=Decimal("500"), close_calculated=Decimal("500"),
        valuation=SchemeValuation(date="30-Sep-2026", nav=Decimal("250"), value=Decimal("125000"), cost=Decimal("100000")),
        transactions=[],
    )
    return CASData(
        statement_period=StatementPeriod(**{"from": "01-Apr-2026", "to": "30-Sep-2026"}),
        folios=[Folio(folio="12345/67", amc="HDFC Mutual Fund", PAN=PERSONAL[1], schemes=[scheme])],
        investor_info=investor(), cas_type="DETAILED", file_type=FileType.CAMS,
    )


@pytest.fixture
def parsed_pdf(monkeypatch):
    def use(statement=None, error=None):
        def fake_read(file, password):
            assert file.read() == PDF
            if error:
                raise error
            return statement or nsdl_statement()

        monkeypatch.setattr(cas.casparser, "read_cas_pdf", fake_read)

    use()
    return use


def upload(client, password: str = "ABCDE1234F", pdf: bytes = PDF):
    return client.post("/api/cas", json={"password": password, "pdfBase64": base64.b64encode(pdf).decode()})


def test_demat_statement_keeps_holdings_by_broker_and_nothing_personal(parsed_pdf):
    statement = cas.parse_statement(PDF, "ABCDE1234F")

    assert statement["source"] == "CDSL" and statement["period_to"] == "30-Sep-2026"
    assert [(row["investment"], row["asset_type"], row["broker"], row["market_value"]) for row in statement["rows"]] == [
        ("HDFC BANK LIMITED", "IND_STOCK", "ZERODHA BROKING LIMITED", "130000"),
        ("INFOSYS LIMITED", "IND_STOCK", "ZERODHA BROKING LIMITED", "100000"),
        ("TCS LIMITED", "IND_STOCK", "NEXTBILLION TECHNOLOGY PRIVATE LIMITED", "70000"),
        ("PARAG PARIKH FLEXI CAP FUND - DIRECT", "MF", cas.MF_FOLIOS, "150000"),
    ]
    stored = json.dumps(statement)
    for value in PERSONAL:
        assert value not in stored


def test_statement_figures_reuse_the_portfolio_calculations(parsed_pdf):
    figures = cas.statement_metrics(cas.parse_statement(PDF, "x"))

    assert [(row["broker"], row["current_value_inr"]) for row in figures["by_broker"]] == [
        ("ZERODHA BROKING LIMITED", 230000.0), (cas.MF_FOLIOS, 150000.0), ("NEXTBILLION TECHNOLOGY PRIVATE LIMITED", 70000.0),
    ]
    # Demat equities carry no cost in a CAS, so P&L covers only the fund that has one.
    assert figures["pnl"]["pnl_inr"] == 30000.0 and figures["pnl"]["holdings_excluded"] == 3


def test_mutual_fund_statement(parsed_pdf):
    parsed_pdf(cams_statement())

    statement = cas.parse_statement(PDF, "x")

    assert statement["source"] == "CAMS"
    assert statement["rows"] == [{
        "investment": "HDFC Index Fund Nifty 50 Plan - Direct", "investment_code": "INF179K01VK6", "asset_type": "MF",
        "broker": cas.MF_FOLIOS, "total_units": "500", "unit_price": "250", "market_value": "125000",
        "invested_amount": "100000",
    }]


@pytest.mark.parametrize(("pdf", "error", "message"), [
    (b"not a pdf", None, "not a PDF"),
    (PDF, IncorrectPasswordError("bad"), "Wrong password"),
    (PDF, CASParseError("unknown"), "Supported: NSDL or CDSL"),
    (PDF, RuntimeError("pdfium"), "could not be opened"),
])
def test_unreadable_statements_give_clear_errors(parsed_pdf, pdf, error, message):
    parsed_pdf(error=error)
    with pytest.raises(cas.CASError, match=message):
        cas.parse_statement(pdf, "x")


def test_oversized_file_is_refused_before_parsing(parsed_pdf):
    with pytest.raises(cas.CASError, match="larger than 15 MB"):
        cas.parse_statement(PDF + b"0" * cas.MAX_PDF_BYTES, "x")


def test_upload_seals_holdings_and_lists_them(client, signed_in, parsed_pdf):
    response = upload(client)

    assert response.status_code == 201
    summary = response.json()
    assert summary["valueInr"] == 450000.0 and summary["holdings"] == 4 and summary["periodTo"] == "30-Sep-2026"
    (stored,) = [row["data"] for row in cas._rows(signed_in["id"])]
    assert sealing.is_sealed(stored) and "ZERODHA" not in stored
    listed = client.get("/api/cas").json()["statements"]
    assert [item["id"] for item in listed] == [summary["id"]]

    assert client.delete(f"/api/cas/{summary['id']}").json() == {"deleted": True}
    assert client.get("/api/cas").json()["statements"] == []
    assert client.delete(f"/api/cas/{summary['id']}").status_code == 404


def test_upload_errors_and_gates(client, signed_in, parsed_pdf):
    parsed_pdf(error=IncorrectPasswordError("bad"))
    assert "Wrong password" in upload(client).json()["error"]
    bad = client.post("/api/cas", json={"password": "x", "pdfBase64": "!!!not base64"})
    assert bad.status_code == 400

    unlock._unlocked.clear()
    assert upload(client).json()["unlockRequired"] is True


def test_private_mode_cannot_upload(client, parsed_pdf):
    from conftest import sign_up

    sign_up(client, storage_mode="private")
    assert upload(client).status_code == 409


def test_only_the_newest_statements_are_kept(client, signed_in, parsed_pdf, monkeypatch):
    monkeypatch.setattr(cas, "KEEP_STATEMENTS", 2)
    for _ in range(3):
        upload(client)
    assert len(client.get("/api/cas").json()["statements"]) == 2


def test_portfolio_agent_gets_the_cas_tool_only_after_an_upload(client, signed_in, parsed_pdf):
    def tool_names():
        user = users.get_user(signed_in["id"])
        user["keys"] = unlock.keys_for_session(user, client.cookies.get("indmoney_session"))
        return [tool.name for tool in snapshots.portfolio_context(user).local_tools], user

    names, _ = tool_names()
    assert "get_cas_holdings" not in names
    upload(client)
    names, user = tool_names()
    assert "get_cas_holdings" in names

    tool = next(tool for tool in snapshots.portfolio_context(user).local_tools if tool.name == "get_cas_holdings")
    payload = json.loads(asyncio.run(tool.handler({"include_all_holdings": True})))
    assert payload["statementPeriod"]["to"] == "30-Sep-2026"
    assert len(payload["figures"]["all_holdings"]) == 4


def test_clearing_history_and_private_mode_delete_statements(client, signed_in, parsed_pdf):
    upload(client)
    client.delete("/api/users/me/history")
    assert not cas.has_statement(signed_in["id"])

    upload(client)
    client.post("/api/privacy/consent", json={"policyVersion": privacy.POLICY_VERSION, "storageMode": "private"})
    assert not cas.has_statement(signed_in["id"])


def test_export_includes_statement_holdings(client, signed_in, parsed_pdf):
    upload(client)

    body = client.post("/api/users/me/export", json={"pin": "2468"}).json()

    assert body["casStatements"][0]["rows"][0]["investment"] == "HDFC BANK LIMITED"

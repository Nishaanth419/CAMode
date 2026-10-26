from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

import pytest

from app import options_math as om
from tests.fixtures import indmoney_market as market


def chain(**kwargs) -> om.Chain:
    return om.parse_chain(json.dumps(market.chain(**kwargs)))


def test_monthly_expiry_is_the_last_listed_expiry_of_its_month_including_holiday_moves():
    listed = [om.parse_expiry(d) for d in ["20261027", "20261103", "20261109", "20261117", "20261123", "20261201", "20261229"]]
    assert om.monthly_expiries(listed) == [date(2026, 10, 27), date(2026, 11, 23), date(2026, 12, 29)]
    assert om.next_monthly_expiry(listed, date(2026, 10, 26)) == date(2026, 10, 27)
    # On expiry day itself the next monthly is the following month's.
    assert om.next_monthly_expiry(listed, date(2026, 10, 27)) == date(2026, 11, 23)
    assert om.next_monthly_expiry(listed, date(2026, 12, 30)) is None


@pytest.mark.parametrize("raw, expected", [
    ("20261123", date(2026, 11, 23)), ("2026-11-23", date(2026, 11, 23)), ("2026-11-23 14:00:00", date(2026, 11, 23)),
    ("23 Nov", None), (None, None), ("", None),
])
def test_expiry_formats(raw, expected):
    assert om.parse_expiry(raw) == expected


def test_parse_chain_reads_the_recorded_layout_and_keeps_absent_quotes_absent():
    parsed = chain()
    assert parsed.underlying_key == "INDI00012" and parsed.expiry == date(2026, 11, 23)
    assert parsed.spot == Decimal("22659.25") and parsed.lot_sizes() == {65}
    assert len(parsed.strikes) == 61 and parsed.strikes[1] - parsed.strikes[0] == 50
    deepest = parsed.calls[0]
    assert deepest.delta is None and deepest.bid is None and deepest.premium is None
    assert "no bid/ask or traded price" in deepest.problems()
    broken = parsed.calls[3]
    assert broken.delta == -1 and "call delta -1 is outside 0 to 1" in broken.problems()


def test_parse_chain_reads_bp_sp_and_volume_aliases():
    data = market.chain()
    leg = data["option_chain_data"][40]["put_data"]
    leg["bp"], leg["sp"] = leg.pop("buy_price"), leg.pop("sell_price")
    quote = om.parse_chain(json.dumps(data)).quote("put", Decimal(data["option_chain_data"][40]["strike_price"]))
    assert quote.bid == Decimal(str(leg["bp"])) and quote.premium_source == "mid of bid/ask"


def test_a_crossed_quote_falls_back_to_last_price_and_is_flagged():
    quote = om.Quote(Decimal(100), "put", None, Decimal(10), Decimal(8), Decimal(9), Decimal("-0.2"), Decimal(-1), 65)
    assert quote.premium == Decimal(9) and quote.premium_source == "last traded price"
    assert "bid 10 is above ask 8" in quote.problems()


def test_parse_chain_errors_and_rejections():
    with pytest.raises(om.ChainError):
        om.parse_chain("not json")
    rejected = json.dumps(market.expiry_rejected("20261124"))
    with pytest.raises(om.ChainError, match="not an available expiry"):
        om.parse_chain(rejected)
    assert om.tool_error(rejected).startswith("expiry_date '20261124'")
    assert om.expiries_in(rejected)[3] == date(2026, 11, 23)
    # A response with data beside an "error" key is data (get_mf_by_category carries one).
    assert om.tool_error(json.dumps({"error": "", "data": [1]})) is None
    assert om.tool_error(json.dumps({"error": "x", "data": [1]})) is None


def test_nearest_delta_uses_only_out_of_the_money_legs_with_valid_deltas():
    parsed = chain()
    call, call_notes = om.nearest_delta(parsed, "call", Decimal("0.15"))
    put, _ = om.nearest_delta(parsed, "put", Decimal("0.15"))
    assert (call.strike, call.delta) == (Decimal(23850), Decimal("0.15"))
    assert (put.strike, put.delta) == (Decimal(21600), Decimal("-0.15"))
    assert call.strike > parsed.spot > put.strike
    assert call_notes == []


def test_nearest_delta_refuses_to_pick_without_deltas():
    data = market.chain()
    for row in data["option_chain_data"]:
        row["call_data"].pop("delta", None)
    leg, notes = om.nearest_delta(om.parse_chain(json.dumps(data)), "call", Decimal("0.15"))
    assert leg is None and any("no delta" in note for note in notes)


def test_wing_prefers_the_range_and_says_when_it_cannot():
    parsed = chain()
    wing, notes = om.pick_wing(parsed, "call", Decimal(23850))
    assert wing.strike == Decimal(24100) and notes == []
    wing, notes = om.pick_wing(parsed, "put", Decimal(21600), (Decimal(400), Decimal(500)))
    assert wing.strike == Decimal(21150) and notes == []  # the lowest strike in a 30-strike chain, 450 points away
    wing, notes = om.pick_wing(parsed, "call", Decimal(23850), (Decimal(5000), Decimal(6000)))
    assert wing.strike == parsed.strikes[-1] and "used" in notes[0]


def _quote(side, strike, premium, theta="-5"):
    return om.Quote(Decimal(strike), side, None, Decimal(premium), Decimal(premium), None, None, Decimal(theta), 65)


def test_iron_condor_figures_and_breakevens():
    result = om.iron_condor(_quote("put", 21350, "54.7"), _quote("put", 21600, "89.075"), _quote("call", 23850, "81.9"),
                            _quote("call", 24100, "52.95"), lot_size=65, lots=2)
    assert result.problems == []
    figures = result.figures
    assert figures["netCreditPoints"] == 63.33 and figures["maxLossPoints"] == 186.68
    assert (figures["lowerBreakeven"], figures["upperBreakeven"]) == (21536.68, 23913.33)
    # 63.325 × 65 × 2 and 186.675 × 65 × 2, exactly.
    assert figures["netCreditRupees"] == 8232.25 and figures["maxLossRupees"] == 24267.75


def test_iron_condor_with_uneven_wings_loses_the_wider_width():
    result = om.iron_condor(_quote("put", 21400, "50"), _quote("put", 21600, "90"), _quote("call", 23850, "80"),
                            _quote("call", 24150, "40"), lot_size=None)
    assert result.figures["maxLossPoints"] == 220.0 and "points" in result.notes[0]
    assert "netCreditRupees" not in result.figures


@pytest.mark.parametrize("legs, problem", [
    ((("put", 21600, 50), ("put", 21350, 90), ("call", 23850, 80), ("call", 24100, 50)), "not in the order"),
    ((("put", 21350, 90), ("put", 21600, 50), ("call", 23850, 50), ("call", 24100, 80)), "no net credit"),
])
def test_iron_condor_rejects_bad_structures(legs, problem):
    result = om.iron_condor(*(_quote(side, strike, premium) for side, strike, premium in legs), lot_size=65)
    assert any(problem in item for item in result.problems) and result.figures == {}


def test_theta_estimate_signs_and_units():
    legs = [(_quote("put", 21350, 1, "-3.76"), 1), (_quote("put", 21600, 1, "-4.96"), -1),
            (_quote("call", 23850, 1, "-4.85"), -1), (_quote("call", 24100, 1, "-3.78"), 1)]
    result = om.theta_estimate(legs, 5, lot_size=65)
    assert result.figures["positionThetaPerDayPoints"] == 2.27
    assert result.figures["estimatedThetaPoints"] == 11.35 and result.figures["estimatedThetaRupees"] == 737.75
    assert "Estimate only" in result.notes[0]
    missing = om.theta_estimate([(om.Quote(Decimal(1), "put", None, None, None, None, None, None, 65), 1)], 5, lot_size=65)
    assert missing.problems and not missing.figures


def test_put_spread_payoff_and_scenario():
    result = om.put_spread(_quote("put", 22650, "450"), _quote("put", 21550, "101.6"), spot=Decimal("22659.25"),
                           move_pct=Decimal(5), lot_size=65)
    assert result.problems == []
    f = result.figures
    assert (f["netDebitPoints"], f["breakeven"], f["maxPayoffPoints"], f["maxProfitPoints"]) == (348.4, 22301.6, 1100.0, 751.6)
    assert f["scenarioLevel"] == 21526.29 and f["grossPayoffAtScenarioPoints"] == 1100.0
    assert f["netDebitRupees"] == 22646.0 and f["maxPayoffRupees"] == 71500.0


def test_put_spread_rejects_inverted_or_unpriced_legs():
    assert om.put_spread(_quote("put", 21550, "100"), _quote("put", 22650, "400"), spot=Decimal(22659), move_pct=Decimal(5), lot_size=65).problems
    unpriced = om.Quote(Decimal(21550), "put", None, None, None, None, None, None, 65)
    assert om.put_spread(_quote("put", 22650, "400"), unpriced, spot=Decimal(22659), move_pct=Decimal(5), lot_size=65).problems == ["a leg has no premium"]
    debit_too_big = om.put_spread(_quote("put", 22650, "2000"), _quote("put", 21550, "100"), spot=Decimal(22659), move_pct=Decimal(5), lot_size=65)
    assert "not between 0" in debit_too_big.problems[0]


def test_hedge_sizing_rounds_down_and_shows_each_beta():
    result = om.hedge_sizing(exposure=Decimal(5_000_000), spot=Decimal("22659.25"), lot_size=65, move_pct=Decimal(5),
                             debit=Decimal("348.4"), gross_at_scenario=Decimal(1100), betas=[Decimal("0.8"), Decimal(1), Decimal("1.2")])
    rows = result.figures["byBeta"]
    assert [row["lots"] for row in rows] == [2, 3, 4] and rows[1]["lotsExact"] == 3.39
    assert rows[1]["grossProtectionRupees"] == 214500.0 and rows[1]["hedgeCostRupees"] == 67938.0
    assert rows[1]["netProtectionRupees"] == 146562.0 and rows[1]["shareOfLossOffsetPct"] == 58.62
    assert om.hedge_sizing(exposure=Decimal(1), spot=Decimal(1), lot_size=None, move_pct=Decimal(5), debit=None,
                           gross_at_scenario=None, betas=[Decimal(1)]).problems
    small = om.hedge_sizing(exposure=Decimal(100_000), spot=Decimal(22659), lot_size=65, move_pct=Decimal(5), debit=Decimal(1),
                            gross_at_scenario=Decimal(1), betas=[Decimal(1)])
    assert small.figures["byBeta"][0]["lots"] == 0 and "smaller than one lot" in small.notes[0]


def test_leg_details_and_greeks_history_layouts():
    data = market.chain()
    key = data["option_chain_data"][20]["put_data"]["derivative_id"]
    details = om.parse_leg_details(json.dumps(market.leg_details(data, [key])))
    leg = details[key]
    assert leg.expiry == date(2026, 11, 23) and leg.underlying_key == "INDI00012" and leg.premium is not None
    assert leg.lot_size is None  # INDmoney's option details carry no lot size; the chain does.
    history = om.parse_greeks_history(json.dumps(market.greeks_history(key, -5.0)))
    described = history.describe()
    assert described["samples"] == 15 and described["sessions"] == 2 and described["latestTheta"] == -4.99


def test_quote_age():
    assert om.quote_age_minutes(datetime(2026, 10, 28, 10, 0), datetime(2026, 10, 28, 10, 45)) == 45
    assert om.quote_age_minutes(None, datetime(2026, 10, 28)) is None

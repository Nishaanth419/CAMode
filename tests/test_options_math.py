from __future__ import annotations

from decimal import Decimal

import pytest

from app import options_math as om


def legs(*items):
    return om.parse_legs([{"type": t, "strike": k, "premium": p, "quantity": q, **({"theta": th} if th is not None else {})}
                          for t, k, p, q, th in items])


def test_iron_condor_figures_match_hand_computed_values():
    # Credit 89.075 + 81.9 - 54.7 - 52.95 = 63.325; max loss 250 - 63.325 = 186.675.
    result = om.analyze(legs(("put", 21350, 54.7, 1, -3.76), ("put", 21600, 89.075, -1, -4.96),
                             ("call", 23850, 81.9, -1, -4.85), ("call", 24100, 52.95, 1, -3.78)),
                        lot_size=Decimal(65), days=Decimal(5))
    assert (result["netPremiumType"], result["netPremiumPoints"]) == ("credit", 63.33)
    assert (result["maxProfitPoints"], result["maxLossPoints"]) == (63.33, 186.68)
    assert result["breakevens"] == [21536.68, 23913.33]
    assert (result["netPremiumRupees"], result["maxLossRupees"]) == (4116.13, 12133.88)  # 186.675 × 65, exactly
    assert result["theta"]["positionThetaPerDayPoints"] == 2.27 and result["theta"]["estimatedPoints"] == 11.35
    assert result["theta"]["estimatedRupees"] == 737.75 and "Estimate only" in result["theta"]["note"]


def test_bear_put_spread_and_a_scenario():
    result = om.analyze(legs(("put", 22650, 429.55, 1, None), ("put", 21550, 81.15, -1, None)),
                        lot_size=Decimal(65), lots=Decimal(3), scenarios=[Decimal("21526.29")])
    assert (result["netPremiumType"], result["netPremiumPoints"]) == ("debit", 348.4)
    assert (result["maxProfitPoints"], result["maxLossPoints"], result["breakevens"]) == (751.6, 348.4, [22301.6])
    assert result["scenarios"] == [{"price": 21526.29, "pnlPoints": 751.6, "pnlRupees": 146562.0}]
    assert result["units"] == 195.0


def test_unlimited_risk_and_reward_are_named_not_numbered():
    short_call = om.analyze(legs(("call", 100, 5, -1, None)))
    assert short_call["maxLossPoints"] == "unlimited" and short_call["maxProfitPoints"] == 5.0
    assert short_call["breakevens"] == [105.0]
    straddle = om.analyze(legs(("call", 100, 6, 1, None), ("put", 100, 4, 1, None)))
    assert straddle["maxProfitPoints"] == "unlimited" and straddle["maxLossPoints"] == 10.0
    assert straddle["breakevens"] == [90.0, 110.0]


def test_a_breakeven_exactly_on_a_strike_is_reported_once():
    # Bought a 100 call for 10, sold a 110 call for 0: break-even exactly at the 110 strike.
    result = om.analyze(legs(("call", 100, 10, 1, None), ("call", 110, 0, -1, None)))
    assert result["breakevens"] == [110.0] and result["maxLossPoints"] == 10.0 and result["maxProfitPoints"] == 0.0


def test_theta_needs_every_leg():
    result = om.analyze(legs(("put", 100, 2, 1, -1.5), ("put", 90, 1, -1, None)), days=Decimal(5))
    assert result["theta"] == {"available": False, "reason": "every leg needs its theta for a theta estimate"}


@pytest.mark.parametrize("raw, problem", [
    ([], "non-empty"), ([{"type": "swap", "strike": 1, "premium": 1, "quantity": 1}], "call or put"),
    ([{"type": "call", "strike": 0, "premium": 1, "quantity": 1}], "strike"),
    ([{"type": "call", "strike": 100, "premium": None, "quantity": 1}], "premium"),
    ([{"type": "call", "strike": 100, "premium": 1, "quantity": 0}], "quantity"),
])
def test_bad_legs_are_refused_with_the_reason(raw, problem):
    with pytest.raises(om.StrategyError, match=problem):
        om.parse_legs(raw)


def test_ce_and_pe_are_read_as_call_and_put():
    assert [leg.side for leg in om.parse_legs([{"type": "CE", "strike": 1, "premium": 0, "quantity": 1},
                                               {"side": "pe", "strike": 1, "premium": 0, "quantity": 1}])] == ["call", "put"]

"""Fitting leg sizes to available margin.

The case that motivated this: swapping SOL-USD for ETH-USD and leaving
`size: 2.0` turns a $200 leg into a $5,200 one, because sizes are in the base
coin. The accounts held $24, so every order would have been rejected with
nothing explaining why.
"""

import pytest

from bulkdn.config import LegConfig
from bulkdn.marketdata import MarketSpec
from bulkdn.sizing import InsufficientMargin, plan_sizes

BTC = "BTC-USD"
ETH = "ETH-USD"

SPECS = {
    BTC: MarketSpec(BTC, tick_size=0.001, lot_size=1e-06, min_notional=1.0),
    ETH: MarketSpec(ETH, tick_size=0.001, lot_size=0.0001, min_notional=50.0),
}
PRICES = {BTC: 80_000.0, ETH: 2_600.0}


def leg(symbol, size, leverage=10.0):
    return LegConfig(
        symbol=symbol,
        size=size,
        offset_bps=2.0,
        max_distance_bps=5.0,
        max_order_size=size,
        leverage=leverage,
    )


def plan(legs, margin, fraction=0.25):
    return plan_sizes(
        legs=legs,
        specs=SPECS,
        prices=PRICES,
        available_margin=margin,
        max_margin_fraction=fraction,
    )


# -- fits ------------------------------------------------------------------


def test_sizes_that_fit_are_used_exactly_as_written():
    """The fraction is a fallback. An operator who sized a cycle that the
    accounts can carry should get that cycle, not a quarter of it."""
    legs = [leg(BTC, 0.001), leg(ETH, 0.03)]  # $8 + $7.80 of margin
    result = plan(legs, {"master": 1_000.0, "sub1": 1_000.0})

    assert not result.scaled
    assert result.scale == 1.0
    assert [x.actual for x in result.legs] == [0.001, 0.03]


# -- scales ----------------------------------------------------------------


def test_oversized_legs_are_scaled_into_the_fraction():
    """Balanced legs, too big for the accounts, scale down and fit."""
    legs = [leg(BTC, 0.1), leg(ETH, 3.0)]  # $1,580 of margin needed
    result = plan(legs, {"master": 900.0, "sub1": 800.0})

    assert result.scaled
    # Budget is a quarter of the smaller account.
    assert result.budget_usd == pytest.approx(800.0 * 0.25, rel=1e-6)
    assert result.required_usd <= result.budget_usd * 1.01


def test_lopsided_legs_refuse_and_say_why():
    """The real case: 0.001 BTC next to 2.0 ETH is $79 against $5,200.

    Proportional scaling keeps that ratio, so the BTC leg reaches its $1 floor
    long before the pair fits. Rebalancing it automatically would be making a
    trading decision, so this refuses and names the cause.
    """
    legs = [leg(BTC, 0.001), leg(ETH, 2.0)]
    with pytest.raises(InsufficientMargin, match="apart in dollar terms"):
        plan(legs, {"master": 24.51, "sub1": 23.94})


def test_scaling_keeps_the_ratio_between_legs():
    """Shrinking one leg and not the other would silently change the balance
    the operator chose."""
    legs = [leg(BTC, 0.05), leg(ETH, 1.5)]
    before = legs[0].size / legs[1].size

    result = plan(legs, {"master": 400.0, "sub1": 400.0})
    after = result.legs[0].actual / result.legs[1].actual

    assert result.scaled
    assert after == pytest.approx(before, rel=0.02)


def test_the_smaller_account_decides():
    """Each account carries one side of both legs, so sizing for the richer
    one would strand the poorer mid-entry."""
    legs = [leg(BTC, 0.1), leg(ETH, 3.0)]
    rich = plan(legs, {"master": 10_000.0, "sub1": 900.0})

    assert rich.budget_usd == pytest.approx(900.0 * 0.25, rel=1e-6)


def test_the_fraction_is_configurable():
    legs = [leg(BTC, 0.5), leg(ETH, 15.0)]
    half = plan(legs, {"master": 1_000.0, "sub1": 1_000.0}, fraction=0.5)
    assert half.budget_usd == pytest.approx(500.0, rel=1e-6)


def test_leverage_reduces_the_margin_a_leg_needs():
    cheap = plan([leg(BTC, 0.01, leverage=50.0)], {"m": 1_000.0})
    dear = plan([leg(BTC, 0.01, leverage=1.0)], {"m": 1_000.0})
    assert cheap.required_usd < dear.required_usd


# -- refuses ---------------------------------------------------------------


def test_a_size_under_the_minimum_notional_refuses_rather_than_trades():
    """ETH will not accept an order under $50. Sizing one anyway just moves
    the failure to the first order, with a worse message."""
    legs = [leg(ETH, 1.0)]
    with pytest.raises(InsufficientMargin, match="minimum"):
        plan(legs, {"master": 20.0, "sub1": 20.0})


def test_a_size_under_one_lot_refuses():
    legs = [leg(BTC, 1.0)]
    with pytest.raises(InsufficientMargin, match="one lot"):
        plan(legs, {"master": 0.0001, "sub1": 0.0001})


def test_an_empty_account_says_what_to_do():
    with pytest.raises(InsufficientMargin, match="Balance Subaccounts"):
        plan([leg(BTC, 0.001)], {"master": 10.0, "sub1": 0.0})


def test_no_margin_readings_at_all_is_an_error():
    with pytest.raises(InsufficientMargin, match="no account margin"):
        plan([leg(BTC, 0.001)], {})


def test_an_unpriced_leg_refuses_rather_than_sizing_on_zero():
    """A zero price would otherwise look like a leg that needs no margin."""
    with pytest.raises(InsufficientMargin, match="market data"):
        plan_sizes(
            legs=[leg(BTC, 0.001)],
            specs=SPECS,
            prices={BTC: 0.0},
            available_margin={"master": 100.0},
        )


# -- reporting -------------------------------------------------------------


def test_describe_names_what_changed():
    legs = [leg(BTC, 0.1), leg(ETH, 3.0)]
    text = plan(legs, {"master": 900.0, "sub1": 800.0}).describe()
    assert "from 0.1" in text
    assert "margin" in text


def test_comparable_legs_blame_the_budget_not_the_ratio():
    """Two nearly equal legs that still will not fit.

    An earlier message blamed the ratio here, which sent you to even up sizes
    that were already even. The cause is the budget against ETH's $50 floor.
    """
    legs = [leg(BTC, 0.01), leg(ETH, 0.3)]  # $800 vs $780 -- comparable
    with pytest.raises(InsufficientMargin, match="already comparable"):
        plan(legs, {"master": 24.51, "sub1": 23.94})


def test_an_affordable_leg_under_the_floor_does_not_blame_the_budget():
    """Nothing was scaled, so money is not the problem and saying so misleads.

    $40 a leg needs $8 of margin against $240 available -- the sizes are used
    exactly as written. The refusal is the market's own $50 floor, and telling
    the operator to deposit funds sends them to fix something that is not wrong.
    """
    legs = [leg(BTC, 40 / PRICES[BTC]), leg(ETH, 40 / PRICES[ETH])]
    with pytest.raises(InsufficientMargin) as caught:
        plan(legs, {"master": 240.0, "sub1": 240.0})

    message = str(caught.value)
    assert "Nothing was scaled down" in message
    assert f"at least ${SPECS[ETH].min_notional:g}" in message
    # The two explanations that would be wrong here.
    assert "Fund the accounts" not in message
    assert "apart in dollar terms" not in message


# -- the per-order cap ------------------------------------------------------
#
# `max_order_notional_usd` caps a single resting order, which is what bounds
# the exposure the pair carries between a fill and the hedge that answers it.
# `LegConfig.validate` documents that leaving it unset means the whole leg goes
# in one order. It did not: the chaser places `min(remaining, max_order_size)`,
# and an unset cap left that at zero, so the leg placed nothing at all -- with
# no error anywhere, because nothing about it looks like one.


def usd_leg(**kw):
    from bulkdn.sizing import resolve_notionals

    leg = LegConfig(symbol="ETH-USD", notional_usd=2500.0, **kw)
    leg.validate("sub_account")
    resolve_notionals(
        legs=[leg], specs=SPECS, prices={"ETH-USD": 2500.0, "BTC-USD": 50_000.0}
    )
    return leg


def test_an_unset_cap_means_the_whole_leg():
    leg = usd_leg()
    assert leg.max_order_size == leg.size
    assert leg.max_order_size > 0, "the leg would place nothing at all"


def test_a_cap_equal_to_the_leg_is_the_whole_leg():
    leg = usd_leg(max_order_notional_usd=2500.0)
    assert leg.max_order_size == pytest.approx(leg.size)


def test_a_smaller_cap_still_slices():
    """The fix must not quietly remove the cap for everyone who set one."""
    leg = usd_leg(max_order_notional_usd=750.0)
    assert leg.max_order_size < leg.size
    assert leg.max_order_size * 2500.0 == pytest.approx(750.0, rel=0.01)


def test_a_cap_larger_than_the_leg_does_not_inflate_it():
    leg = usd_leg(max_order_notional_usd=10_000.0)
    assert leg.max_order_size >= leg.size
    # cli.py brings it back down to the sized leg; the leg itself is unchanged.
    assert leg.size == pytest.approx(1.0)


def test_scaling_the_leg_brings_the_cap_down_with_it():
    """Margin can shrink a leg after the cap was computed. A cap left at the
    original size would stop capping anything."""
    leg = usd_leg(max_order_notional_usd=750.0)
    scaled = leg.size / 4
    assert min(leg.max_order_size, scaled) == pytest.approx(scaled)

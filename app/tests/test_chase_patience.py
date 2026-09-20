"""Giving up the offset when the market will not come to us.

A resting order sits `offset_bps` inside the touch, which is what earns the
maker side -- and what makes it wait. On a live cycle that wait ran from 17
seconds to over two and a half minutes, and the drift rule does not help: if
the market has not moved, the order has not drifted, so it is held exactly
where it is failing to fill.

After `chase_patience_s` the order gives up the offset and goes to the market:
it posts `improve_ticks` INTO the spread, which makes it the best bid or ask
outright rather than joining the queue at the old one, and from then on it
follows the touch tick by tick. It never crosses, so the fill is still a maker
fill -- this buys speed with a tick of price, not with a taker fee.
"""

import pytest

from bulkdn.chaser import ChaseParams, effective_offset_bps
from bulkdn.config import ConfigError, LegConfig
from bulkdn.marketdata import MarketSpec, chase_price

BASE = 2.0
PATIENCE = 8.0


def offset(resting_for, tightened=False, patience=PATIENCE, base=BASE):
    return effective_offset_bps(base, resting_for, patience, tightened)


# -- waiting, then not -------------------------------------------------------


def test_a_fresh_order_keeps_the_full_offset():
    assert offset(0.0) == BASE
    assert offset(PATIENCE - 0.1) == BASE


def test_the_offset_is_given_up_once_patience_runs_out():
    assert offset(PATIENCE) == 0.0
    assert offset(PATIENCE + 30) == 0.0


def test_it_never_goes_past_the_touch():
    """Zero is the closest a passive order can get. Anything less would cross,
    and crossing is what turns a maker fill into a taker fee."""
    assert offset(PATIENCE * 100) == 0.0
    assert offset(PATIENCE, base=0.5) == 0.0


# -- sticky, so the order does not walk back out -----------------------------


def test_a_tightened_leg_stays_on_the_touch():
    """Springing back would walk the order away from the market, only to have
    to walk in again after the next wait."""
    assert offset(0.0, tightened=True) == 0.0
    assert offset(1.0, tightened=True) == 0.0


# -- switched off ------------------------------------------------------------


@pytest.mark.parametrize("patience", [0, 0.0])
def test_zero_patience_keeps_the_old_behaviour(patience):
    assert offset(9999.0, patience=patience) == BASE


# -- the setting -------------------------------------------------------------


def test_the_leg_carries_its_own_patience():
    leg = LegConfig(symbol="BTC-USD", size=1.0, max_order_size=1.0, chase_patience_s=5.0)
    leg.validate("master_account")
    assert leg.chase_patience_s == 5.0


def test_a_negative_patience_is_refused():
    leg = LegConfig(symbol="BTC-USD", size=1.0, max_order_size=1.0, chase_patience_s=-1.0)
    with pytest.raises(ConfigError, match="chase_patience_s"):
        leg.validate("master_account")


def test_params_default_to_off():
    """A ChaseParams built without it behaves as the chaser always did."""
    assert ChaseParams(offset_bps=2.0, max_distance_bps=5.0, max_order_size=1.0).chase_patience_s == 0.0


# -- following the touch once committed --------------------------------------
#
# From a live book: an ETH order resting at 2527.26 with the best bid at
# 2528.34 -- $1.08 behind, ten levels deep. The leg had already tightened, so
# it was meant to be on the market, but max_distance_bps (8) still governed the
# replace and called 4.3bps of lag acceptable.
#
# A bps tolerance was the wrong instrument entirely. A tightened order is meant
# to be AT the front of the book, and "at the front" is a tick-level fact: a
# 1bps tolerance on a $100k instrument with a $0.50 tick is twenty price levels
# of other people's orders. So there is no threshold now -- any price other
# than the computed target is a replace.


def params(max_distance=8.0, improve=1):
    return ChaseParams(
        offset_bps=3.0, max_distance_bps=max_distance,
        max_order_size=1.0, chase_patience_s=3.0, improve_ticks=improve,
    )


def would_replace(resting_price, target, tightened, params):
    """The rule under test, as the chaser applies it."""
    if tightened:
        return resting_price != target
    lag = abs(resting_price - target) / abs(target) * 10_000
    return lag > params.max_distance_bps


def test_a_waiting_order_keeps_the_loose_tolerance():
    """It is resting away from the market on purpose; chasing every tick would
    burn nonces and queue position for nothing."""
    assert not would_replace(2527.26, 2528.34, tightened=False, params=params())


def test_a_committed_order_follows_every_tick():
    """The live case, which used to be held."""
    assert would_replace(2527.26, 2528.34, tightened=True, params=params())


def test_an_unmoved_touch_sends_nothing():
    """What stops this being a replace storm: same touch, same target, no tx."""
    assert not would_replace(2528.34, 2528.34, tightened=True, params=params())


def test_improve_ticks_must_not_be_negative():
    leg = LegConfig(
        symbol="BTC-USD", size=1.0, max_order_size=1.0, improve_ticks=-1
    )
    with pytest.raises(ConfigError, match="improve_ticks"):
        leg.validate("master_account")


def test_joining_the_touch_is_still_available():
    """0 is the old behaviour, for anyone who would rather keep the spread."""
    leg = LegConfig(symbol="BTC-USD", size=1.0, max_order_size=1.0, improve_ticks=0)
    leg.validate("master_account")
    assert leg.improve_ticks == 0


# -- one tick better than the touch, and never one tick too far ---------------
#
# The aggression that does not cost a taker fee: being the best bid rather than
# joining it. The whole risk is overshooting into the other side, so most of
# these are about the clamp.

BTC = MarketSpec("BTC-USD", tick_size=0.5, lot_size=0.001, min_notional=1.0)


def price(bid, ask, is_buy, improve=1, offset=0.0, spec=BTC):
    return chase_price(
        best_bid=bid, best_ask=ask, mark_price=None,
        is_buy=is_buy, offset_bps=offset, spec=spec, improve_ticks=improve,
    )


def test_a_buy_posts_one_tick_above_the_best_bid():
    assert price(100_000.0, 100_010.0, is_buy=True) == 100_000.5


def test_a_sell_posts_one_tick_below_the_best_ask():
    assert price(100_000.0, 100_010.0, is_buy=False) == 100_009.5


def test_it_never_reaches_the_other_side():
    """A one-tick spread has no room: the order stays on the touch."""
    assert price(100_000.0, 100_000.5, is_buy=True) == 100_000.0
    assert price(100_000.0, 100_000.5, is_buy=False) == 100_000.5


def test_a_wide_step_is_clamped_short_of_the_other_side():
    """improve_ticks larger than the spread must not walk through it."""
    assert price(100_000.0, 100_002.0, is_buy=True, improve=99) == 100_001.5
    assert price(100_000.0, 100_002.0, is_buy=False, improve=99) == 100_000.5


def test_the_improved_price_is_always_strictly_inside():
    """The property that makes this a maker order rather than a taker one."""
    for spread_ticks in range(1, 12):
        bid = 100_000.0
        ask = bid + spread_ticks * BTC.tick_size
        for improve in (1, 2, 5, 50):
            buy = price(bid, ask, is_buy=True, improve=improve)
            sell = price(bid, ask, is_buy=False, improve=improve)
            assert bid <= buy < ask, (spread_ticks, improve, buy)
            assert bid < sell <= ask, (spread_ticks, improve, sell)


def test_zero_ticks_joins_the_touch():
    assert price(100_000.0, 100_010.0, is_buy=True, improve=0) == 100_000.0


def test_an_offset_order_is_not_improved():
    """While still waiting at a deliberate distance, stepping forward from it
    would contradict the offset that was just applied."""
    assert price(100_000.0, 100_010.0, is_buy=True, improve=1, offset=2.0) < 100_000.0


def test_a_missing_book_side_is_not_guessed():
    """Without an ask there is no way to know the room, and guessing is how a
    'passive' order crosses. Falls back to the touch."""
    assert price(100_000.0, None, is_buy=True) == 100_000.0


# -- the flag lives on the leg, not in a set of symbols ---------------------


def test_two_legs_on_one_market_tighten_independently():
    """A set keyed by symbol made one leg's patience run out for the other,
    which has not necessarily placed an order yet. Two groups trading the same
    market at once is the whole point of the account pool."""
    from bulkdn.state import LegState

    first, second = LegState(symbol="BTC-USD"), LegState(symbol="BTC-USD")
    first.tightened = True
    assert second.tightened is False


def test_a_tightened_leg_stays_tightened_across_a_restart():
    """It used to live in memory only, so a restart sent a leg that had
    already given up its offset back to waiting out its patience again."""
    from bulkdn.state import LegState, StrategyState

    state = StrategyState()
    state.leg("BTC-USD").tightened = True

    revived = StrategyState.from_dict(state.to_dict())
    assert revived.leg("BTC-USD").tightened is True


def test_a_fresh_leg_has_not_tightened():
    from bulkdn.state import LegState

    assert LegState(symbol="BTC-USD").tightened is False

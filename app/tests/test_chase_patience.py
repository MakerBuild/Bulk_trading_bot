"""Giving up the offset when the market will not come to us.

A resting order sits `offset_bps` inside the touch, which is what earns the
maker side -- and what makes it wait. On a live cycle that wait ran from 17
seconds to over two and a half minutes, and the drift rule does not help: if
the market has not moved, the order has not drifted, so it is held exactly
where it is failing to fill.

After `chase_patience_s` the order gives up the offset and moves onto the
touch. It never crosses, so the fill is still a maker fill and the rebate is
untouched -- this buys speed with queue position, not with fees.
"""

import pytest

from bulkdn.chaser import ChaseParams, effective_offset_bps
from bulkdn.config import ConfigError, LegConfig

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


def replace_threshold(params, tightened):
    """The rule under test, as the chaser applies it."""
    return params.tight_distance_bps if tightened else params.max_distance_bps


def params(max_distance=8.0, tight=1.0):
    return ChaseParams(
        offset_bps=3.0, max_distance_bps=max_distance,
        max_order_size=1.0, chase_patience_s=8.0, tight_distance_bps=tight,
    )


def test_a_waiting_order_keeps_the_loose_tolerance():
    """It is resting away from the market on purpose; chasing every tick would
    burn nonces and queue position for nothing."""
    assert replace_threshold(params(), tightened=False) == 8.0


def test_a_committed_order_follows_the_price():
    assert replace_threshold(params(), tightened=True) == 1.0


def test_the_live_lag_would_now_trigger_a_replace():
    order, bid = 2527.26, 2528.34
    lag_bps = (bid - order) / bid * 10_000

    assert lag_bps > replace_threshold(params(), tightened=True), "still held"
    assert lag_bps < replace_threshold(params(), tightened=False), (
        "this is the case that used to be tolerated"
    )


def test_the_threshold_must_be_positive():
    leg = LegConfig(
        symbol="BTC-USD", size=1.0, max_order_size=1.0, tight_distance_bps=0.0
    )
    with pytest.raises(ConfigError, match="tight_distance_bps"):
        leg.validate("master_account")

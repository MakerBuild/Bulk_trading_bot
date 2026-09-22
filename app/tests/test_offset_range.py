"""The offset written as a range, drawn once per cycle.

`offset_bps` was the last of the three numbers that says what a cycle looks
like to still be a constant. `notional_usd`, `max_order_notional_usd` and
`hold_minutes` were already ranges; this one was not, and it is the one the
resting price is computed from.

That mattered as soon as more than one group ran at a time. `ChaseParams` is
built per symbol, so both groups read one offset, worked it against one book,
and landed on one tick -- queued behind each other, in public, every cycle.
Rotating the accounts does not separate two orders at the same price.
"""


from bulkdn.config import Config, RiskConfig, _leg_from_dict
from bulkdn.strategy import Strategy

BTC = "BTC-USD"


def leg(raw):
    return _leg_from_dict({"symbol": BTC, **raw}, "btc")


def strategy(offset):
    obj = object.__new__(Strategy)
    obj.config = Config(
        markets=[leg({"offset_bps": offset, "max_distance_bps": 5.0})],
        risk=RiskConfig(),
        private_key="x",
    )
    return obj


# -- reading it -------------------------------------------------------------


def test_a_range_is_read_as_one():
    parsed = leg({"offset_bps": "1.5-2.5"})
    assert (parsed.offset_span.low, parsed.offset_span.high) == (1.5, 2.5)


def test_a_plain_number_still_reads_as_a_plain_number():
    parsed = leg({"offset_bps": 2.0})
    assert parsed.offset_bps == 2.0
    assert parsed.offset_span.is_range is False


def test_the_scalar_is_the_low_end_of_a_range():
    """It is only read by a leg that was not drawn from a group, and the
    least passive end is the safer fallback: an order resting further inside
    the touch waits longer, and a phase that never fills ends the run."""
    assert leg({"offset_bps": "1.5-2.5"}).offset_bps == 1.5


def test_a_leg_that_never_mentions_it_keeps_the_old_default():
    parsed = leg({})
    assert parsed.offset_bps == 0.0
    assert parsed.offset_span is None


# -- drawing it -------------------------------------------------------------


def test_a_draw_stays_inside_the_range():
    obj = strategy("1.5-2.5")
    draws = [obj._offset_for_cycle(BTC) for _ in range(200)]
    assert all(1.5 <= d <= 2.5 for d in draws)


def test_a_range_actually_varies():
    obj = strategy("1.5-2.5")
    draws = {obj._offset_for_cycle(BTC) for _ in range(50)}
    assert len(draws) > 1, "a range that never varies is a fixed number"


def test_a_fixed_offset_draws_itself_every_time():
    obj = strategy(2.0)
    assert {obj._offset_for_cycle(BTC) for _ in range(20)} == {2.0}


def test_two_cycles_on_one_market_do_not_share_an_offset():
    """The property the whole change exists for."""
    obj = strategy("1.5-2.5")
    first = obj._offset_for_cycle(BTC)
    rest = [obj._offset_for_cycle(BTC) for _ in range(20)]
    assert any(other != first for other in rest)


def test_a_market_that_is_not_configured_draws_nothing():
    """A leg keyed by a market this run does not trade must not raise in the
    middle of registering a group."""
    assert strategy(2.0)._offset_for_cycle("DOGE-USD") == 0.0

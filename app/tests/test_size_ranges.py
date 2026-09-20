"""Leg sizes written as a range, drawn fresh each cycle.

A fixed size repeats exactly, and an exact repeat is a shape in the fill
history -- the same notional, cycle after cycle, between the same two
accounts. `hold_minutes` already accepted a range for that reason; this is the
same idea applied to the two settings that say how much.

The care is all in what must NOT change: a leg written as a plain number has to
keep behaving exactly as it did, and a draw must never exceed what the margin
plan allowed at startup.
"""

import pytest

from bulkdn.config import ConfigError, HoldTime, LegConfig, Span
from bulkdn.sizing import draw_sizes

BTC = "BTC-USD"


# -- reading the three spellings --------------------------------------------


@pytest.mark.parametrize("written,low,high", [
    (4000, 4000, 4000),
    ("2000-6000", 2000, 6000),
    ([2000, 6000], 2000, 6000),
    ("4000", 4000, 4000),
])
def test_every_spelling_a_range_can_have(written, low, high):
    span = Span.parse(written, "notional_usd")
    assert (span.low, span.high) == (low, high)


def test_a_plain_number_is_not_a_range():
    assert Span.parse(4000, "notional_usd").is_range is False
    assert Span.parse("2000-6000", "notional_usd").is_range is True


def test_a_draw_stays_inside_its_range():
    span = Span.parse("2000-6000", "notional_usd")
    draws = [span.pick() for _ in range(200)]
    assert all(2000 <= d <= 6000 for d in draws)
    assert len(set(draws)) > 1, "a range that never varies is a fixed number"


def test_a_fixed_value_draws_itself_every_time():
    span = Span.parse(4000, "notional_usd")
    assert {span.pick() for _ in range(20)} == {4000}


def test_the_message_names_the_setting_that_was_wrong():
    """A complaint about hold_minutes sends the operator to the wrong line."""
    with pytest.raises(ConfigError, match="notional_usd"):
        Span.parse("6000-2000", "notional_usd")
    with pytest.raises(ConfigError, match="hold_minutes"):
        HoldTime.parse("5-1")


@pytest.mark.parametrize("bad", [True, [1, 2, 3], "banana", -5])
def test_what_is_refused(bad):
    with pytest.raises(ConfigError):
        Span.parse(bad, "notional_usd")


# -- what a leg carries -----------------------------------------------------


def leg(notional, cap=None):
    from bulkdn.config import _leg_from_dict

    raw = {"symbol": BTC, "notional_usd": notional, "offset_bps": 1.0,
           "max_distance_bps": 5.0}
    if cap is not None:
        raw["max_order_notional_usd"] = cap
    return _leg_from_dict(raw, "master_account")


def test_a_range_is_carried_at_its_high_end_until_the_first_draw():
    """The margin plan is built from this number at startup. Planning from the
    low end would let a later draw ask for margin nobody checked was there."""
    assert leg("2000-6000").notional_usd == 6000


def test_a_fixed_leg_keeps_its_number():
    assert leg(4000).notional_usd == 4000
    assert leg(4000).notional_span.is_range is False


def test_an_uncapped_leg_follows_its_own_size_when_drawn():
    """The cap defaults to the whole leg. If it stayed pinned to the high end
    while the size was drawn lower, every order would be the whole leg again."""
    one = leg("2000-6000")
    assert one.max_order_span is one.notional_span


# -- drawing ----------------------------------------------------------------


def test_drawing_moves_a_range_and_leaves_a_number_alone():
    varying, fixed = leg("2000-6000"), leg(4000)
    seen = set()
    for _ in range(30):
        draw_sizes([varying, fixed])
        seen.add(round(varying.notional_usd))
        assert fixed.notional_usd == 4000, "a fixed leg was redrawn"
    assert len(seen) > 1
    assert all(2000 <= value <= 6000 for value in seen)


def test_the_cap_is_drawn_independently_of_the_size():
    one = leg("2000-6000", cap="250-750")
    caps = set()
    for _ in range(30):
        draw_sizes([one])
        caps.add(round(one.max_order_notional_usd))
    assert len(caps) > 1
    assert all(250 <= value <= 750 for value in caps)


def test_drawing_a_leg_with_no_spans_at_all_does_nothing():
    """`size:`-style legs have no dollar spans. They must survive the call."""
    plain = LegConfig(symbol=BTC, size=1.0, offset_bps=1.0, max_distance_bps=5.0)
    draw_sizes([plain])
    assert plain.size == 1.0

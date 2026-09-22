"""Which way round a group trades.

`maker_is_buy` was the constant `not exiting`, so every maker in every group
bought on the way in and sold on the way out, for the life of the run. A dry
run showed both live groups resting BUY at the same price, tick for tick:

    g1:BTC-USD OPEN maker=m1s4 ... LimitOrder(BUY 0.00348 @ 85814.47)
    g2:BTC-USD OPEN maker=m1s2 ... LimitOrder(BUY 0.00348 @ 85814.47)

That is the one shape a rotating pool does not remove. The accounts move, the
sizes move, the holds move -- and the side of the book never does.

The care is in the exit. The side is drawn once and kept on the group, so the
close is the mirror of whatever that cycle opened. A side decided per phase,
or guessed after a restart, would "close" a short by selling more of it.
"""

import random

from bulkdn.pairing import Group, Pairing
from bulkdn.state import Phase, StrategyState
from bulkdn.strategy import Strategy

BTC = "BTC-USD"
POOL = [f"acct{n}" for n in range(12)]


def roles_for(group, phase):
    """The roles the strategy derives for a group sitting in `phase`."""
    obj = object.__new__(Strategy)
    obj._groups = {}
    obj.state = StrategyState()
    key = "g1:" + group.symbol
    obj._groups[key] = group
    obj.state.leg(key, group.symbol).phase = phase
    return Strategy._roles_for_key(obj, key)


def group(maker_is_buy):
    return Group(
        BTC, maker="m", takers=("t",), shares=(1.0,), maker_is_buy=maker_is_buy
    )


# -- the side is drawn ------------------------------------------------------


def test_both_sides_come_up_over_a_run():
    book = Pairing(pool=list(POOL), max_groups=5, rng=random.Random(7))
    sides = set()
    for _ in range(40):
        group_id, drawn = book.draw(BTC)
        sides.add(drawn.maker_is_buy)
        book.release(group_id)
    assert sides == {True, False}, "the maker always took the same side"


def test_two_groups_drawn_together_are_not_forced_onto_one_side():
    """The live symptom: both groups resting BUY at the same price."""
    book = Pairing(pool=list(POOL), max_groups=5, rng=random.Random(7))
    seen = set()
    for _ in range(20):
        first = book.draw(BTC)
        second = book.draw(BTC)
        seen.add((first[1].maker_is_buy, second[1].maker_is_buy))
        book.release(first[0])
        book.release(second[0])
    assert any(a != b for a, b in seen), "two live groups never differed"


# -- and the exit mirrors it ------------------------------------------------


def test_entry_takes_the_side_the_group_was_drawn_with():
    assert roles_for(group(True), Phase.OPEN).maker_is_buy is True
    assert roles_for(group(False), Phase.OPEN).maker_is_buy is False


def test_exit_is_the_mirror_of_whatever_opened():
    """A group that opened short buys its way out, rather than selling
    further into the position it is supposed to be closing."""
    assert roles_for(group(True), Phase.EXIT).maker_is_buy is False
    assert roles_for(group(False), Phase.EXIT).maker_is_buy is True


def test_the_exit_is_reduce_only_whichever_way_it_opened():
    for side in (True, False):
        assert roles_for(group(side), Phase.EXIT).reduce_only is True
        assert roles_for(group(side), Phase.OPEN).reduce_only is False


# -- and it is visible ------------------------------------------------------


def test_the_log_line_says_which_way_the_group_trades():
    assert " BUY -> " in str(group(True))
    assert " SELL -> " in str(group(False))

"""Drawing who trades against whom.

The rule that carries the safety is that an account is in at most one group at
a time. The hedge is derived from positions -- `net = position[maker] +
position[taker]` -- so an account in two groups has one position serving two
sums, and both of them are wrong. Everything else here is about not being
readable from the outside.
"""

import random

import pytest

from bulkdn.pairing import Group, Pairing, split_shares

POOL = [f"acct{n}" for n in range(12)]


def pairing(pool=None, **kwargs):
    kwargs.setdefault("rng", random.Random(1234))
    return Pairing(pool=list(pool if pool is not None else POOL), **kwargs)


# -- an account is never in two groups --------------------------------------


def test_drawn_accounts_are_taken_out_of_circulation():
    book = pairing()
    _id, group = book.draw("BTC-USD")
    assert all(account not in book.free for account in group.accounts)


def test_no_account_appears_in_two_live_groups():
    book = pairing(max_groups=5)
    drawn = [book.draw("BTC-USD") for _ in range(5)]
    seen = [a for _id, g in drawn for a in g.accounts]
    assert len(seen) == len(set(seen))


def test_finishing_a_group_returns_its_accounts():
    book = pairing()
    group_id, group = book.draw("BTC-USD")
    book.release(group_id)
    assert all(account in book.free for account in group.accounts)


def test_releasing_the_same_group_twice_is_harmless():
    book = pairing()
    group_id, _group = book.draw("BTC-USD")
    assert book.release(group_id) is not None
    assert book.release(group_id) is None


def test_a_group_never_holds_one_account_twice():
    """It would net to nothing, trade nothing, and pay two taker fees."""
    book = pairing(max_takers=3)
    for _ in range(20):
        result = book.draw("BTC-USD")
        if result is None:
            break
        _id, group = result
        assert len(set(group.accounts)) == len(group.accounts)


# -- and the cap holds ------------------------------------------------------


def test_the_cap_is_the_cap():
    book = pairing(max_groups=3)
    assert len([g for g in (book.draw("BTC-USD") for _ in range(10)) if g]) == 3


def test_a_full_book_says_no_rather_than_raising():
    """A busy run is full most of the time; that is not a fault."""
    book = pairing(max_groups=1)
    assert book.draw("BTC-USD") is not None
    assert book.draw("BTC-USD") is None


def test_room_reappears_when_a_group_finishes():
    book = pairing(max_groups=1)
    group_id, _ = book.draw("BTC-USD")
    assert book.draw("BTC-USD") is None
    book.release(group_id)
    assert book.draw("BTC-USD") is not None


def test_a_pool_too_thin_for_a_pair_draws_nothing():
    assert pairing(pool=["only-one"]).draw("BTC-USD") is None
    assert pairing(pool=[]).draw("BTC-USD") is None


def test_two_accounts_are_exactly_enough_for_one_group():
    book = pairing(pool=["a", "b"])
    assert book.draw("BTC-USD") is not None
    assert book.draw("BTC-USD") is None


def test_a_thin_pool_takes_fewer_takers_rather_than_nothing():
    """Three accounts cannot make a maker and three takers, but they can make
    a maker and two, and a cycle traded is better than a cycle skipped."""
    book = pairing(pool=["a", "b", "c"], max_takers=3)
    result = book.draw("BTC-USD")
    assert result is not None
    _id, group = result
    assert len(group.takers) == 2


# -- the pairing actually moves ---------------------------------------------


def test_the_same_pair_does_not_come_up_every_time():
    """The whole point. A fixed pairing is the signal this exists to remove."""
    book = pairing(max_groups=1)
    seen = set()
    for _ in range(40):
        group_id, group = book.draw("BTC-USD")
        seen.add((group.maker, group.takers))
        book.release(group_id)
    assert len(seen) > 5


def test_the_maker_is_not_always_the_same_account():
    book = pairing(max_groups=1)
    makers = set()
    for _ in range(40):
        group_id, group = book.draw("BTC-USD")
        makers.add(group.maker)
        book.release(group_id)
    assert len(makers) > 3


def test_a_seed_makes_it_repeatable():
    """So a run can be reproduced when something goes wrong in one."""
    first = pairing(rng=random.Random(7)).draw("BTC-USD")[1]
    second = pairing(rng=random.Random(7)).draw("BTC-USD")[1]
    assert first == second


# -- splitting the hedge ----------------------------------------------------


def test_one_taker_takes_all_of_it():
    assert split_shares(1, random.Random(1)) == (1.0,)


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5])
def test_shares_always_add_up(count):
    shares = split_shares(count, random.Random(count))
    assert sum(shares) == pytest.approx(1.0)
    assert len(shares) == count


@pytest.mark.parametrize("count", [2, 3, 4, 5])
def test_no_share_is_too_small_to_send(count):
    """A share under the market's minimum order cannot be sent at all, and the
    group would carry exactly that much unhedged delta with no way to clear
    it. BTC-USD admits $1 and ETH-USD $50."""
    for seed in range(50):
        shares = split_shares(count, random.Random(seed))
        assert min(shares) >= 0.15 - 1e-9, shares


def test_an_impossible_floor_splits_evenly_rather_than_failing():
    """Ten ways cannot all be a seventh. Even is the honest answer."""
    shares = split_shares(10, random.Random(1))
    assert sum(shares) == pytest.approx(1.0)
    assert len({round(s, 9) for s in shares}) == 1


def test_the_split_is_not_the_same_every_time():
    seen = {split_shares(3, random.Random(seed)) for seed in range(20)}
    assert len(seen) > 10


def test_a_group_can_say_what_each_taker_owes():
    group = Group("BTC-USD", "maker", ("t1", "t2"), (0.6, 0.4))
    assert group.share_for("t1") == 0.6
    assert group.share_for("t2") == 0.4
    assert group.share_for("stranger") == 0.0

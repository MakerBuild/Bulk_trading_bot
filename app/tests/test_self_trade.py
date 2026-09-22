"""Not trading against our own accounts.

A hedge covers its maker by trading the other way, so a maker that bought is
covered by a market SELL -- and that sell sweeps the bid, where the unfilled
remainder of that same maker's order is resting, at the touch, first in the
queue. That is not an edge case; it is exactly where the chaser puts it.

The exchange refuses to cross ONE account with itself (`CANCELLED_SELFCROSSING`
in its own status enum) but matches two accounts of one master like strangers,
and the fee documentation then excludes that volume from the tier. Measured on
a live run: 365 of 1465 trades.

Two mechanisms, and only one of them is load-bearing:

* `_clear_hedge_path` pulls our resting orders off the side the hedge is about
  to sweep. This is what makes the result correct.
* `_least_crowded_side` keeps the groups on opposite sides, so the only order
  in the way is the filling leg's own. This only makes it cheap -- it cannot
  be a guarantee, because a group's side flips when it unwinds and two groups
  whose phases have drifted can end up on one side with neither able to move.
"""

import asyncio
import random

from bulkdn.pairing import Group, Pairing
from bulkdn.state import Phase, StrategyState
from bulkdn.strategy import Strategy

BTC = "BTC-USD"


class FakeSession:
    def __init__(self, name, pubkey, fails=False):
        self.name = name
        self.pubkey = pubkey
        self.fails = fails
        self.cancelled = []

    async def cancel(self, symbol, oid):
        if self.fails:
            raise RuntimeError("no answer from the exchange")
        self.cancelled.append((symbol, oid))


class FakeHedger:
    def __init__(self, actionable=1.0):
        self.actionable = actionable
        self.hedged = []

    def actionable_hedge(self, roles, price):
        return self.actionable

    async def hedge(self, roles, mark_price=None):
        self.hedged.append(roles.key)


class FakeFeed:
    def reference_price(self, symbol):
        return 85_000.0


def strategy(*, actionable=1.0):
    obj = object.__new__(Strategy)
    obj.state = StrategyState()
    obj._groups = {}
    obj.sessions = {}
    obj.feed = FakeFeed()
    obj.hedger = FakeHedger(actionable)
    obj._rng = random.Random(4)
    obj._stop = asyncio.Event()
    obj._book_suspect = False
    obj._liquidation_seen = asyncio.Event()
    obj._hedge_queue = asyncio.Queue()
    return obj


def register(obj, key, maker, *, maker_is_buy, phase=Phase.OPEN, oid="oid-1",
             fails=False):
    """Put a live group on the book, the way `_run_group` would."""
    obj._groups[key] = Group(
        BTC, maker=maker, takers=("t-" + maker,), shares=(1.0,),
        maker_is_buy=maker_is_buy,
    )
    leg = obj.state.leg(key, BTC)
    leg.phase = phase
    leg.oid = oid
    obj.sessions[maker] = FakeSession(maker, maker, fails=fails)
    return obj._roles_for_key(key)


# -- clearing the path ------------------------------------------------------


async def test_our_own_remainder_is_pulled_before_the_hedge_sweeps_it():
    obj = strategy()
    roles = register(obj, "g1:" + BTC, "maker-a", maker_is_buy=True)

    await obj._clear_hedge_path(roles)

    assert obj.sessions["maker-a"].cancelled == [(BTC, "oid-1")]
    assert obj.state.leg("g1:" + BTC).oid is None, "the chaser must re-place"


async def test_another_groups_order_on_that_side_is_pulled_too():
    """The one `_least_crowded_side` is there to make rare, not impossible:
    two groups whose phases have drifted can share a side."""
    obj = strategy()
    roles = register(obj, "g1:" + BTC, "maker-a", maker_is_buy=True)
    register(obj, "g2:" + BTC, "maker-b", maker_is_buy=True, oid="oid-2")

    await obj._clear_hedge_path(roles)

    assert obj.sessions["maker-b"].cancelled == [(BTC, "oid-2")]


async def test_the_other_side_of_the_book_is_left_alone():
    """A sell resting on the ask is not in the path of a sell hedge, and
    cancelling it would cost that group its queue position for nothing."""
    obj = strategy()
    roles = register(obj, "g1:" + BTC, "maker-a", maker_is_buy=True)
    register(obj, "g2:" + BTC, "maker-b", maker_is_buy=False, oid="oid-2")

    await obj._clear_hedge_path(roles)

    assert obj.sessions["maker-b"].cancelled == []
    assert obj.state.leg("g2:" + BTC).oid == "oid-2"


async def test_nothing_is_cancelled_when_no_hedge_is_going_to_be_sent():
    """A cancel costs a request against an exchange that has answered 429 to
    two accounts polling every five seconds."""
    obj = strategy(actionable=0.0)
    roles = register(obj, "g1:" + BTC, "maker-a", maker_is_buy=True)

    await obj._clear_hedge_path(roles)

    assert obj.sessions["maker-a"].cancelled == []
    assert obj.state.leg("g1:" + BTC).oid == "oid-1"


async def test_a_failed_cancel_keeps_the_order_id():
    """It may still be resting, and forgetting the id is how an order stops
    being cancellable at all."""
    obj = strategy()
    roles = register(obj, "g1:" + BTC, "maker-a", maker_is_buy=True, fails=True)

    await obj._clear_hedge_path(roles)

    assert obj.state.leg("g1:" + BTC).oid == "oid-1"


# -- but the hedge always goes --------------------------------------------


async def _run_worker_once(obj, key):
    obj._hedge_queue.put_nowait(key)
    task = asyncio.create_task(obj._hedge_worker())
    for _ in range(500):
        if obj.hedger.hedged:
            break
        await asyncio.sleep(0)
    obj._stop.set()
    await asyncio.wait_for(task, timeout=5)


async def test_a_cancel_that_fails_does_not_stop_the_hedge():
    """Unhedged exposure has no bounded cost and a self-trade costs a fee.
    When only one of the two can be avoided, it is never the hedge."""
    obj = strategy()
    key = "g1:" + BTC
    register(obj, key, "maker-a", maker_is_buy=True, fails=True)

    await _run_worker_once(obj, key)

    assert obj.hedger.hedged == [key]


async def test_the_path_is_cleared_before_the_hedge_not_after():
    """Ordering is the whole point: afterwards would be a log entry about a
    trade that already happened."""
    obj = strategy()
    key = "g1:" + BTC
    register(obj, key, "maker-a", maker_is_buy=True)
    order = []

    real_clear = obj._clear_hedge_path

    async def clear(roles):
        order.append("clear")
        await real_clear(roles)

    async def hedge(roles, mark_price=None):
        order.append("hedge")
        obj.hedger.hedged.append(roles.key)

    obj._clear_hedge_path = clear
    obj.hedger.hedge = hedge

    await _run_worker_once(obj, key)

    assert order == ["clear", "hedge"]


# -- keeping the groups apart ----------------------------------------------


def test_the_next_group_takes_the_side_the_others_are_not_on():
    obj = strategy()
    register(obj, "g1:" + BTC, "maker-a", maker_is_buy=True)
    assert obj._least_crowded_side(BTC) is False

    obj2 = strategy()
    register(obj2, "g1:" + BTC, "maker-a", maker_is_buy=False)
    assert obj2._least_crowded_side(BTC) is True


def test_a_tie_is_drawn_rather_than_always_opening_the_same_way():
    obj = strategy()
    assert {obj._least_crowded_side(BTC) for _ in range(40)} == {True, False}


def test_a_balanced_book_of_groups_is_a_tie_again():
    """Two groups, one a side: the third is free to go either way."""
    obj = strategy()
    register(obj, "g1:" + BTC, "maker-a", maker_is_buy=True)
    register(obj, "g2:" + BTC, "maker-b", maker_is_buy=False, oid="oid-2")
    assert {obj._least_crowded_side(BTC) for _ in range(40)} == {True, False}


def test_another_market_does_not_count_toward_this_one():
    obj = strategy()
    register(obj, "g1:" + BTC, "maker-a", maker_is_buy=True)
    assert {obj._least_crowded_side("ETH-USD") for _ in range(40)} == {True, False}


def test_a_drawn_group_takes_the_side_it_was_given():
    book = Pairing(pool=[f"acct{n}" for n in range(8)], max_groups=4,
                   rng=random.Random(1))
    for side in (True, False):
        _id, group = book.draw(BTC, maker_is_buy=side)
        assert group.maker_is_buy is side


def test_without_a_side_the_draw_still_flips_a_coin():
    book = Pairing(pool=[f"acct{n}" for n in range(8)], max_groups=4,
                   rng=random.Random(1))
    sides = set()
    for _ in range(40):
        group_id, group = book.draw(BTC)
        sides.add(group.maker_is_buy)
        book.release(group_id)
    assert sides == {True, False}

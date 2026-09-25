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

import pytest

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

    async def hedge(self, roles, mark_price=None, suspended=None):
        self.hedged.append(roles.key)


class FakeFeed:
    def reference_price(self, symbol):
        return 85_000.0


class FakeChaser:
    """Every order is on the book unless named as filled."""

    def __init__(self):
        self.filled = set()

    def may_be_resting(self, session, oid):
        return bool(oid) and oid not in self.filled


def strategy(*, actionable=1.0):
    obj = object.__new__(Strategy)
    obj.state = StrategyState()
    obj._groups = {}
    obj.sessions = {}
    obj.feed = FakeFeed()
    obj.hedger = FakeHedger(actionable)
    obj.chaser = FakeChaser()
    obj._rng = random.Random(4)
    obj._stop = asyncio.Event()
    obj._book_suspect = False
    obj._liquidation_seen = asyncio.Event()
    obj._hedge_queue = asyncio.Queue()
    # The side count reads the pairing, because that is the record which is
    # current at the moment of a draw -- `_groups` is filled by a task that
    # has not run yet.
    obj.pairing = Pairing(
        pool=[f"acct{n}" for n in range(12)], max_groups=6,
        rng=random.Random(9),
    )
    return obj


def register(obj, key, maker, *, maker_is_buy, phase=Phase.OPEN, oid="oid-1",
             fails=False):
    """Put a live group on the book, the way a draw plus `_run_group` would."""
    group = Group(
        BTC, maker=maker, takers=("t-" + maker,), shares=(1.0,),
        maker_is_buy=maker_is_buy,
    )
    obj.pairing.reserve(int(key.split(":")[0][1:]), group)
    obj._groups[key] = group
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

    async def hedge(roles, mark_price=None, suspended=None):
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


# -- a finished group must stop counting ------------------------------------
#
# A released group leaves its leg in `state.legs` on purpose: the state file
# is what a restart reads to find positions. But it is dropped from `_groups`,
# and `_roles_for_key` then falls through to the CONFIGURED pair, whose side
# is the constant True. Walking `state.legs` therefore counted every group
# that had ever finished, as a buy.
#
# Live symptom, two draws in a row on a six-account pool:
#
#     === group 3 drawn: BTC-USD CCCCCC..CCC3 SELL -> ... at 1.65bps ===
#     === group 4 drawn: BTC-USD DDDDDD..DDD4 SELL -> ... at 1.67bps ===
#     ... DDDDDD..DDD4: LimitOrder(SELL 0.003407 @ 86315.009999999995 ...)
#     ... CCCCCC..CCC3: LimitOrder(SELL 0.003407 @ 86315.009999999995 ...)
#
# Two makers, one side, one price, to the cent.


def release(obj, key):
    """What `_run_group` does in its finally: the group goes, the leg stays."""
    obj.pairing.release(int(key.split(":")[0][1:]))
    del obj._groups[key]


def test_a_released_groups_leg_does_not_vote_on_the_side():
    obj = strategy()
    register(obj, "g1:" + BTC, "maker-a", maker_is_buy=True)
    release(obj, "g1:" + BTC)

    # Nothing is live, so the side is free and must be drawn, not inherited
    # from a leg that finished.
    assert {obj._least_crowded_side(BTC) for _ in range(40)} == {True, False}


def test_two_finished_groups_do_not_push_the_next_one_onto_their_side():
    """The live case exactly: two finished groups and one live SELL. The
    next group must go BUY, and used to go SELL."""
    obj = strategy()
    for n, side in ((1, True), (2, False)):
        key = f"g{n}:{BTC}"
        register(obj, key, f"done-{n}", maker_is_buy=side, oid=f"old-{n}")
        release(obj, key)
    register(obj, "g3:" + BTC, "maker-c", maker_is_buy=False, oid="oid-3")

    assert obj._least_crowded_side(BTC) is True


async def test_a_released_groups_order_is_left_alone():
    obj = strategy()
    roles = register(obj, "g1:" + BTC, "maker-a", maker_is_buy=True)
    stale = "g2:" + BTC
    register(obj, stale, "maker-b", maker_is_buy=True, oid="oid-2")
    release(obj, stale)

    await obj._clear_hedge_path(roles)

    assert obj.sessions["maker-a"].cancelled == [(BTC, "oid-1")]
    assert obj.sessions["maker-b"].cancelled == [], "that group is gone"


# -- drawn, not yet running -------------------------------------------------


def test_two_groups_drawn_in_one_pass_do_not_take_the_same_side():
    """The dispatcher draws, spawns `_run_group` as a task, and comes straight
    back round without yielding -- so the second draw happens before the first
    group has registered itself anywhere except in the pairing.

    A live run opened exactly this way:

        === group 1 drawn: BTC-USD AAAAAA..AAA1 BUY ... at 2.20bps ===
        === group 2 drawn: BTC-USD BBBBBB..BBB2 BUY ... at 2.19bps ===
    """
    obj = strategy()
    obj._groups = {}  # nothing has run yet, which is the whole point

    first = obj.pairing.draw(BTC, maker_is_buy=obj._least_crowded_side(BTC))
    second = obj.pairing.draw(BTC, maker_is_buy=obj._least_crowded_side(BTC))

    assert first[1].maker_is_buy != second[1].maker_is_buy


def test_the_side_is_still_drawn_when_the_pairing_is_empty():
    obj = strategy()
    assert {obj._least_crowded_side(BTC) for _ in range(40)} == {True, False}


def test_a_group_that_has_turned_around_counts_on_the_side_it_is_now_resting():
    """Its leg says EXIT, so it is selling what it bought and the next group
    should take the side it has left, not the one it opened on."""
    obj = strategy()
    register(obj, "g1:" + BTC, "maker-a", maker_is_buy=True, phase=Phase.EXIT)
    # Opened BUY, now closing, so it rests a SELL -- the bid is free.
    assert obj._least_crowded_side(BTC) is True


# -- not waiting on a cancel that has nothing to cancel ----------------------
#
# A live run put the first hedge ~730ms behind its maker fill, and the hedge's
# price got worse with every hundred of them. The cancel went first every time,
# although 70% of maker fills had taken the whole order: nothing left to pull.


async def test_an_order_already_filled_is_not_cancelled():
    obj = strategy()
    roles = register(obj, "g1:" + BTC, "maker-a", maker_is_buy=True)
    obj.chaser.filled.add("oid-1")

    await obj._clear_hedge_path(roles)

    assert obj.sessions["maker-a"].cancelled == []


async def test_a_remainder_still_resting_is_pulled_as_before():
    obj = strategy()
    roles = register(obj, "g1:" + BTC, "maker-a", maker_is_buy=True)
    register(obj, "g2:" + BTC, "maker-b", maker_is_buy=True, oid="oid-2")
    obj.chaser.filled.add("oid-1")

    await obj._clear_hedge_path(roles)

    assert obj.sessions["maker-a"].cancelled == []
    assert obj.sessions["maker-b"].cancelled == [(BTC, "oid-2")]


# -- no hedge while positions are being taken down ---------------------------


async def test_the_worker_sends_nothing_while_closing_out():
    obj = strategy()
    register(obj, "g1:" + BTC, "maker-a", maker_is_buy=True)
    obj._closing_out = True
    obj._hedge_queue.put_nowait("g1:" + BTC)

    worker = asyncio.create_task(obj._hedge_worker())
    await asyncio.sleep(0.05)
    obj._stop.set()
    await asyncio.wait_for(worker, 2)

    assert obj.hedger.hedged == [], "a close was answered with a new position"


async def test_the_worker_sends_nothing_once_halted():
    """The emergency stop flattens every account while the worker still runs."""
    obj = strategy()
    register(obj, "g1:" + BTC, "maker-a", maker_is_buy=True)
    obj._halt_reason = "hedge limit exceeded"
    obj._hedge_queue.put_nowait("g1:" + BTC)

    worker = asyncio.create_task(obj._hedge_worker())
    await asyncio.sleep(0.05)
    obj._stop.set()
    await asyncio.wait_for(worker, 2)

    assert obj.hedger.hedged == []


async def test_the_worker_still_hedges_normally():
    obj = strategy()
    register(obj, "g1:" + BTC, "maker-a", maker_is_buy=True)
    obj._hedge_queue.put_nowait("g1:" + BTC)

    worker = asyncio.create_task(obj._hedge_worker())
    await asyncio.sleep(0.05)
    obj._stop.set()
    await asyncio.wait_for(worker, 2)

    assert obj.hedger.hedged == ["g1:" + BTC]


# -- a replacement placed during the cancel is not forgotten -----------------


async def test_an_order_replaced_mid_cancel_keeps_its_new_id():
    """The chaser runs as its own task and can replace the order while the
    cancel is in flight. Clearing `leg.oid` blindly afterwards dropped the
    replacement's id: an order left resting that nothing tracked."""
    obj = strategy()
    roles = register(obj, "g1:" + BTC, "maker-a", maker_is_buy=True)
    leg = obj.state.leg("g1:" + BTC)
    session = obj.sessions["maker-a"]
    real_cancel = session.cancel

    async def cancel_while_chaser_replaces(symbol, oid):
        await real_cancel(symbol, oid)
        leg.oid = "oid-replacement"            # the chaser, meanwhile

    session.cancel = cancel_while_chaser_replaces

    await obj._clear_hedge_path(roles)

    assert session.cancelled == [(BTC, "oid-1")]
    assert leg.oid == "oid-replacement", "the replacement was forgotten"


# -- one unanswered slice does not stop the pool ------------------------------


async def test_an_unanswered_slice_does_not_suspect_the_whole_book():
    """Marking the book suspect stopped every hedge in the pool until a
    full read finished, while every other group's makers filled unhedged."""
    from bulkdn.hedger import HedgeInDoubt

    obj = strategy()
    roles = register(obj, "g1:" + BTC, "maker-a", maker_is_buy=True)
    reads = []

    async def read(max_age_s=0.0):
        reads.append(max_age_s)

    async def unanswered(roles, mark_price=None, suspended=None):
        raise HedgeInDoubt("no answer")

    obj._sync_positions = read
    obj.hedger.hedge = unanswered

    with pytest.raises(HedgeInDoubt):
        await obj._hedge_leg(roles)
    assert obj._book_suspect is False, "every other leg would now wait"

    await asyncio.sleep(0.01)
    assert reads, "nothing settled the unanswered slice"
    assert obj._hedge_queue.get_nowait() == "g1:" + BTC, "the leg was not re-evaluated"


# -- a signal for a leg whose hedge just finished is not dropped -------------


async def test_a_signal_during_the_read_is_not_lost():
    """Finished tasks were pruned before the read; a task that ended during
    it was still listed, and the new signal was folded into it -- dropped."""
    obj = strategy()
    key = "g1:" + BTC
    register(obj, key, "maker-a", maker_is_buy=True)
    release = asyncio.Event()
    calls = []

    async def hedge(roles, mark_price=None, suspended=None):
        calls.append(roles.key)
        if len(calls) == 1:
            await release.wait()

    async def refreshed():
        release.set()                 # the running hedge finishes meanwhile
        await asyncio.sleep(0.01)
        obj._book_suspect = False
        return True

    obj.hedger.hedge = hedge
    obj._refreshed = refreshed

    worker = asyncio.create_task(obj._hedge_worker())
    obj._hedge_queue.put_nowait(key)
    await asyncio.sleep(0.02)                      # first hedge is running
    obj._book_suspect = True
    obj._hedge_queue.put_nowait(key)               # new fill, read pending
    await asyncio.sleep(0.1)
    obj._stop.set()
    await asyncio.wait_for(worker, 2)

    assert calls == [key, key], "the second signal was dropped"

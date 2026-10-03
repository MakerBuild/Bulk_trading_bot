"""Chasing: replace only on real drift, and never leave two orders working."""

import asyncio
from dataclasses import dataclass

import pytest

from bulkdn.chaser import ChaseParams, Chaser
from bulkdn.feed import Quote
from bulkdn.hedger import LegRoles
from bulkdn.marketdata import MarketSpec
from bulkdn.positions import PositionBook
from bulkdn.state import LegState

MASTER = "master-pubkey"
SUB1 = "sub1-pubkey"
BTC = "BTC-USD"
SPEC = MarketSpec(symbol=BTC, tick_size=0.5, lot_size=0.001, min_notional=10.0)

OPEN_BTC = LegRoles(BTC, maker=MASTER, taker=SUB1, maker_is_buy=True, reduce_only=False)
EXIT_BTC = LegRoles(BTC, maker=SUB1, taker=MASTER, maker_is_buy=True, reduce_only=True)


@dataclass
class FakeOrder:
    price: float
    size: float


class FakeClient:
    def __init__(self):
        self.order_map = {}

    def get_order_map(self):
        return self.order_map


class FakeSession:
    def __init__(self, name, pubkey):
        self.name = name
        self.pubkey = pubkey
        self.client = FakeClient()
        self.placed = []
        self.cancelled = []
        self._counter = 0

    async def place_limit(self, symbol, is_buy, price, size, reduce_only=False, cancel_oid=None):
        self._counter += 1
        oid = f"oid-{self._counter}"
        self.placed.append(
            {
                "symbol": symbol, "is_buy": is_buy, "price": price, "size": size,
                "reduce_only": reduce_only, "cancel_oid": cancel_oid, "oid": oid,
            }
        )
        if cancel_oid:
            self.client.order_map.pop(cancel_oid, None)
        self.client.order_map[oid] = FakeOrder(price=price, size=size)
        return oid, []

    async def cancel(self, symbol, oid):
        self.cancelled.append(oid)
        self.client.order_map.pop(oid, None)
        return []


class FakeFeed:
    def __init__(self, quote):
        self.specs = {BTC: SPEC}
        self._quote = quote

    def quote(self, symbol):
        return self._quote

    def reference_price(self, symbol):
        return self._quote.reference_price


def build(quote=None, max_order_size=1.0, max_distance_bps=5.0, offset_bps=0.0):
    quote = quote or Quote(BTC, best_bid=100_000.0, best_ask=100_010.0, mark_price=100_005.0, age_s=0.0)
    book = PositionBook(overlay_ttl_ms=5000)
    master = FakeSession("master", MASTER)
    sub1 = FakeSession("sub1", SUB1)
    feed = FakeFeed(quote)
    chaser = Chaser(
        sessions={MASTER: master, SUB1: sub1},
        feed=feed,
        book=book,
        params={BTC: ChaseParams(offset_bps=offset_bps, max_distance_bps=max_distance_bps, max_order_size=max_order_size)},
    )
    return chaser, book, master, sub1


# -- sizing ----------------------------------------------------------------


def test_entry_remaining_is_the_shortfall_to_target():
    chaser, book, _m, _s = build()
    book.set_authoritative(MASTER, BTC, 0.4)
    leg = LegState(symbol=BTC, target_size=1.0)
    assert chaser.remaining_size(OPEN_BTC, leg) == 0.6


def test_exit_remaining_is_whatever_is_still_held():
    chaser, book, _m, _s = build()
    book.set_authoritative(SUB1, BTC, -0.75)
    leg = LegState(symbol=BTC, target_size=1.0)
    # Direction is irrelevant: a short of 0.75 still needs 0.75 bought back.
    assert chaser.remaining_size(EXIT_BTC, leg) == 0.75


def test_entry_remaining_never_goes_negative():
    chaser, book, _m, _s = build()
    book.set_authoritative(MASTER, BTC, 1.5)
    leg = LegState(symbol=BTC, target_size=1.0)
    assert chaser.remaining_size(OPEN_BTC, leg) == 0.0


# -- placing / replacing ---------------------------------------------------


async def test_places_when_nothing_is_resting():
    chaser, book, master, _s = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    leg = LegState(symbol=BTC, target_size=1.0)

    outcome = await chaser.step(OPEN_BTC, leg)

    assert outcome.action == "placed"
    assert master.placed[0]["cancel_oid"] is None
    assert master.placed[0]["is_buy"] is True
    assert leg.oid == "oid-1"


async def test_holds_while_within_the_drift_threshold():
    chaser, book, master, _s = build(max_distance_bps=50.0)
    book.set_authoritative(MASTER, BTC, 0.0)
    leg = LegState(symbol=BTC, target_size=1.0)

    await chaser.step(OPEN_BTC, leg)
    outcome = await chaser.step(OPEN_BTC, leg)

    assert outcome.action == "held"
    assert len(master.placed) == 1  # not replaced


async def test_replaces_atomically_once_drift_exceeds_the_threshold():
    quote = Quote(BTC, best_bid=100_000.0, best_ask=100_010.0, mark_price=100_005.0, age_s=0.0)
    chaser, book, master, _s = build(quote=quote, max_distance_bps=5.0)
    book.set_authoritative(MASTER, BTC, 0.0)
    leg = LegState(symbol=BTC, target_size=1.0)

    await chaser.step(OPEN_BTC, leg)
    first_oid = leg.oid

    # Market moves 100 bps away.
    chaser.feed._quote = Quote(BTC, best_bid=101_000.0, best_ask=101_010.0, mark_price=101_005.0, age_s=0.0)
    outcome = await chaser.step(OPEN_BTC, leg)

    assert outcome.action == "replaced"
    # The cancel rides in the same transaction as the replacement -- there is
    # no window with no order working.
    assert master.placed[1]["cancel_oid"] == first_oid
    assert leg.oid != first_oid


async def test_leg_completes_and_cancels_the_remainder_when_target_is_met():
    chaser, book, master, _s = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    leg = LegState(symbol=BTC, target_size=1.0)
    await chaser.step(OPEN_BTC, leg)

    # Target reached.
    book.set_authoritative(MASTER, BTC, 1.0)
    outcome = await chaser.step(OPEN_BTC, leg)

    assert outcome.action == "complete"
    assert leg.complete is True
    assert leg.oid is None
    assert master.cancelled  # leftover order was pulled


async def test_order_size_is_capped():
    chaser, book, master, _s = build(max_order_size=0.25)
    book.set_authoritative(MASTER, BTC, 0.0)
    leg = LegState(symbol=BTC, target_size=1.0)

    await chaser.step(OPEN_BTC, leg)
    assert master.placed[0]["size"] == 0.25


async def test_exit_orders_are_reduce_only():
    chaser, book, _m, sub1 = build()
    book.set_authoritative(SUB1, BTC, -1.0)
    leg = LegState(symbol=BTC, target_size=1.0)

    await chaser.step(EXIT_BTC, leg)
    assert sub1.placed[0]["reduce_only"] is True


async def test_stale_prices_are_not_chased():
    quote = Quote(BTC, best_bid=100_000.0, best_ask=100_010.0, mark_price=100_005.0, age_s=999.0)
    chaser, book, master, _s = build(quote=quote)
    book.set_authoritative(MASTER, BTC, 0.0)

    outcome = await chaser.step(OPEN_BTC, LegState(symbol=BTC, target_size=1.0))
    assert outcome.action == "skipped"
    assert master.placed == []


async def test_superseded_order_still_resting_is_swept():
    """If the cancel half of a replace silently failed, two orders would work."""
    chaser, book, master, _s = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    leg = LegState(symbol=BTC, target_size=1.0, oid="new-oid")
    leg.stale_oids = ["ghost-oid"]
    master.client.order_map["ghost-oid"] = FakeOrder(price=99_000.0, size=1.0)
    master.client.order_map["new-oid"] = FakeOrder(price=100_000.0, size=1.0)

    await chaser.step(OPEN_BTC, leg)

    assert "ghost-oid" in master.cancelled
    assert leg.stale_oids == []


async def test_sweep_forgets_orders_that_are_already_gone():
    chaser, book, master, _s = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    leg = LegState(symbol=BTC, target_size=1.0, oid="new-oid")
    leg.stale_oids = ["already-filled"]
    master.client.order_map["new-oid"] = FakeOrder(price=100_000.0, size=1.0)

    await chaser.step(OPEN_BTC, leg)

    assert master.cancelled == []
    assert leg.stale_oids == []


# -- whose offset the order rests at ----------------------------------------
#
# `ChaseParams` is built once per symbol, so every group trading that market
# reads one offset. The resting price is a pure function of the book and that
# number, so two groups computed the same tick and queued behind each other --
# a live run showed both resting BUY at 85814.47. A group may now carry its
# own, drawn per cycle, and it takes precedence.


async def _resting_price(*, params_offset, roles_offset):
    chaser, book, master, _sub1 = build(offset_bps=params_offset)
    roles = LegRoles(
        BTC, maker=MASTER, taker=SUB1, maker_is_buy=True, reduce_only=False,
        offset_bps=roles_offset,
    )
    await chaser.step(roles, LegState(symbol=BTC, target_size=1.0))
    return master.placed[-1]["price"]


async def test_a_leg_without_one_still_rests_at_its_markets_offset():
    """Every leg that is not drawn from a pool, and every run before this."""
    bid = 100_000.0
    assert await _resting_price(params_offset=2.0, roles_offset=None) == bid - 20.0


async def test_the_cycles_own_offset_is_the_one_used():
    bid = 100_000.0
    assert await _resting_price(params_offset=2.0, roles_offset=4.0) == bid - 40.0


async def test_two_cycles_on_one_market_rest_at_different_prices():
    """The symptom, stated as the property that fixes it."""
    first = await _resting_price(params_offset=2.0, roles_offset=1.6)
    second = await _resting_price(params_offset=2.0, roles_offset=2.4)
    assert first != second


async def test_an_offset_of_zero_is_used_rather_than_read_as_absent():
    """0.0 is falsy and means "rest on the touch", which is not the same as
    "this leg was never given one" -- a truthiness test here would silently
    hand the market's offset to a cycle that drew zero."""
    on_the_touch = await _resting_price(params_offset=2.0, roles_offset=0.0)
    assert on_the_touch != await _resting_price(params_offset=2.0, roles_offset=None)


# The cap on one resting order had the same problem as the offset. A dry run
# with three groups open placed 600 orders and every one of them was 0.003176
# BTC, the last cap any group drew. A group now carries its own.


async def test_the_cycles_own_order_cap_is_the_one_used():
    chaser, book, master, _s = build(max_order_size=0.25)
    book.set_authoritative(MASTER, BTC, 0.0)
    roles = LegRoles(
        BTC, maker=MASTER, taker=SUB1, maker_is_buy=True, reduce_only=False,
        max_order_size=0.4,
    )

    await chaser.step(roles, LegState(symbol=BTC, target_size=1.0))
    assert master.placed[0]["size"] == 0.4


# -- whether an order can still be in a hedge's way -------------------------


async def test_an_order_in_the_map_may_be_resting():
    chaser, book, master, _s = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    leg = LegState(symbol=BTC, target_size=1.0)
    await chaser.step(OPEN_BTC, leg)

    assert chaser.may_be_resting(master, leg.oid)


async def test_a_filled_order_is_not_resting():
    chaser, book, master, _s = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    leg = LegState(symbol=BTC, target_size=1.0)
    await chaser.step(OPEN_BTC, leg)
    await chaser.step(OPEN_BTC, leg)                     # acknowledged
    master.client.order_map.pop(leg.oid)                 # and filled

    assert not chaser.may_be_resting(master, leg.oid)


async def test_an_order_not_yet_acknowledged_is_assumed_resting():
    """It may be on the book with nothing here to say so yet."""
    chaser, book, master, _s = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    leg = LegState(symbol=BTC, target_size=1.0)
    await chaser.step(OPEN_BTC, leg)
    master.client.order_map.pop(leg.oid)

    assert chaser.may_be_resting(master, leg.oid)


async def test_an_order_seen_and_then_gone_is_not_resting_however_young():
    """Filled a moment after it showed up: nothing is left to cancel.

    Judging by age alone kept 49% of a live run's maker fills inside the
    acknowledgement grace, and each of them cancelled an order that was
    already gone before the hedge could go out.
    """
    chaser, book, master, _s = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    leg = LegState(symbol=BTC, target_size=1.0)
    await chaser.step(OPEN_BTC, leg)
    assert chaser.may_be_resting(master, leg.oid)      # shown in the map
    master.client.order_map.pop(leg.oid)                 # and filled, at once

    assert not chaser.may_be_resting(master, leg.oid)


async def test_a_chase_step_records_the_order_as_acknowledged():
    """The step that sees an order resting is what marks it as seen."""
    chaser, book, master, _s = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    leg = LegState(symbol=BTC, target_size=1.0)
    await chaser.step(OPEN_BTC, leg)
    await chaser.step(OPEN_BTC, leg)                     # held: sees it resting
    master.client.order_map.pop(leg.oid)

    assert not chaser.may_be_resting(master, leg.oid)


# -- the order cap is floored at the price the order goes out at ------------


async def test_a_cap_drawn_at_a_higher_price_is_lifted_to_the_minimum():
    """The cap was floored at $50 when it was drawn, at $143.37: 0.35 SOL.
    At $142 that is $49.70, refused -- and five refusals in a row halt."""
    sol = MarketSpec(symbol=BTC, tick_size=0.01, lot_size=0.01, min_notional=50.0)
    quote = Quote(BTC, best_bid=142.0, best_ask=142.1, mark_price=142.05, age_s=0.0)
    chaser, book, master, _s = build(quote=quote, max_order_size=0.35)
    chaser.feed.specs[BTC] = sol
    book.set_authoritative(MASTER, BTC, 0.0)

    await chaser.step(OPEN_BTC, LegState(symbol=BTC, target_size=2.0))

    placed = master.placed[0]
    assert placed["size"] * placed["price"] >= 50.0, "an order under the minimum went out"
    assert placed["size"] == pytest.approx(0.36)


# -- two of our own groups on one side ----------------------------------------
#
# The "already the touch" check knew only the leg's own order. With another of
# our groups at the touch, each saw a stranger ahead and stepped past it, once
# a chase interval each, walking toward the far side a tick at a time.

G1 = LegRoles(BTC, maker=MASTER, taker=SUB1, maker_is_buy=True, reduce_only=False, id="g1")
G2 = LegRoles(BTC, maker=SUB1, taker=MASTER, maker_is_buy=True, reduce_only=False, id="g2")


def touch_of(*sessions, stranger=100_000.0):
    """The best bid: the highest of our resting bids and the stranger's."""
    ours = [o.price for s in sessions for o in s.client.order_map.values()]
    return max([stranger, *ours])


async def test_two_of_our_groups_on_one_side_do_not_outbid_each_other():
    chaser, book, master, sub1 = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    book.set_authoritative(SUB1, BTC, 0.0)
    leg1 = LegState(symbol=BTC, target_size=1.0, tightened=True)
    leg2 = LegState(symbol=BTC, target_size=1.0, tightened=True)

    for _ in range(6):
        for roles, leg in ((G1, leg1), (G2, leg2)):
            bid = touch_of(master, sub1)
            chaser.feed._quote = Quote(
                BTC, best_bid=bid, best_ask=100_010.0, mark_price=100_005.0, age_s=0.0
            )
            await chaser.step(roles, leg)

    prices = {o.price for s in (master, sub1) for o in s.client.order_map.values()}
    assert prices == {100_000.5}, f"our own groups walked the bid up to {max(prices)}"
    assert len(master.placed) + len(sub1.placed) == 2, "each step was a replace"


async def test_a_strangers_touch_is_still_stepped_past():
    chaser, book, _master, sub1 = build()
    book.set_authoritative(SUB1, BTC, 0.0)
    leg = LegState(symbol=BTC, target_size=1.0, tightened=True)

    await chaser.step(G2, leg)

    assert sub1.placed[0]["price"] == 100_000.5


async def test_a_level_our_order_has_left_is_a_strangers_again():
    """Filled or cancelled, it no longer holds the level."""
    chaser, book, master, sub1 = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    book.set_authoritative(SUB1, BTC, 0.0)
    leg1 = LegState(symbol=BTC, target_size=1.0, tightened=True)
    leg2 = LegState(symbol=BTC, target_size=1.0, tightened=True)
    await chaser.step(G1, leg1)                          # ours at 100_000.5
    chaser._orders[leg1.oid].seen = True
    master.client.order_map.clear()                      # ... and filled

    chaser.feed._quote = Quote(                          # a stranger joined there
        BTC, best_bid=100_000.5, best_ask=100_010.0, mark_price=100_005.0, age_s=0.0
    )
    await chaser.step(G2, leg2)

    assert sub1.placed[0]["price"] == 100_001.0


# -- an order the map has not shown yet is unknown, not gone -----------------
#
# The order map is filled from orderUpdate alone, and orderUpdate has run
# seven to thirty seconds behind the answer that accepted the order. The step
# used to treat an order missing from the map for three seconds as gone: it
# forgot the id and placed a second order beside the first, and both could
# fill.


def _later(monkeypatch, seconds):
    """Move the chaser's clock forward without touching the event loop's."""
    import time as real_time
    import types

    from bulkdn import chaser as chaser_module

    monkeypatch.setattr(
        chaser_module, "time",
        types.SimpleNamespace(monotonic=lambda: real_time.monotonic() + seconds),
    )


async def test_an_order_the_map_never_showed_is_replaced_not_forgotten(monkeypatch):
    chaser, book, master, _s = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    leg = LegState(symbol=BTC, target_size=1.0)
    await chaser.step(OPEN_BTC, leg)
    first = leg.oid
    master.client.order_map.pop(first)                   # its orderUpdate is late

    _later(monkeypatch, 10.0)
    outcome = await chaser.step(OPEN_BTC, leg)

    assert outcome.action == "replaced"
    assert master.placed[-1]["cancel_oid"] == first, (
        "a second order went out beside one that may still rest"
    )


async def test_an_order_not_yet_shown_is_waited_for_inside_the_grace():
    chaser, book, master, _s = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    leg = LegState(symbol=BTC, target_size=1.0)
    await chaser.step(OPEN_BTC, leg)
    master.client.order_map.pop(leg.oid)

    outcome = await chaser.step(OPEN_BTC, leg)

    assert outcome.action == "waiting"
    assert len(master.placed) == 1


async def test_an_order_shown_and_then_gone_is_re_placed_at_once():
    """Seen, then gone, means gone -- filled or cancelled. Nothing is left to
    cancel, so the next order goes out without waiting out the grace."""
    chaser, book, master, _s = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    leg = LegState(symbol=BTC, target_size=1.0)
    await chaser.step(OPEN_BTC, leg)
    await chaser.step(OPEN_BTC, leg)                     # held: seen resting
    master.client.order_map.pop(leg.oid)                 # and gone

    outcome = await chaser.step(OPEN_BTC, leg)

    assert outcome.action == "placed"
    assert master.placed[-1]["cancel_oid"] is None


async def test_an_order_never_shown_may_still_be_resting_after_the_grace(monkeypatch):
    """A hedge pulls our orders out of its way, and one the map has not
    caught up with is as much in the way as one it shows."""
    chaser, book, master, _s = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    leg = LegState(symbol=BTC, target_size=1.0)
    await chaser.step(OPEN_BTC, leg)
    master.client.order_map.pop(leg.oid)

    _later(monkeypatch, 10.0)
    assert chaser.may_be_resting(master, leg.oid)


async def test_a_cancel_that_got_no_answer_is_kept_for_the_sweep():
    chaser, book, master, _s = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    leg = LegState(symbol=BTC, target_size=1.0)
    await chaser.step(OPEN_BTC, leg)
    oid = leg.oid

    async def unanswered(symbol, oid):
        raise TimeoutError("no answer")

    master.cancel = unanswered
    await chaser.cancel_leg(OPEN_BTC, leg)

    assert leg.oid is None
    assert leg.stale_oids == [oid], "an order that may still rest was forgotten"


async def test_the_sweep_waits_for_an_order_the_map_has_not_shown_yet():
    """A superseded order the map has never shown cannot be judged gone by
    its absence. It is kept, and cancelled the moment it surfaces."""
    chaser, book, master, _s = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    leg = LegState(symbol=BTC, target_size=1.0)
    await chaser.step(OPEN_BTC, leg)
    ghost = leg.oid
    master.client.order_map.pop(ghost)

    async def unanswered(symbol, oid):
        raise TimeoutError("no answer")

    real_cancel = master.cancel
    master.cancel = unanswered
    await chaser.cancel_leg(OPEN_BTC, leg)
    master.cancel = real_cancel

    await chaser.step(OPEN_BTC, leg)                     # not in the map yet
    assert ghost in leg.stale_oids

    master.client.order_map[ghost] = FakeOrder(price=100_000.0, size=1.0)
    await chaser.step(OPEN_BTC, leg)                     # its update arrives
    assert ghost in master.cancelled


async def test_the_order_an_adopted_one_replaced_goes_on_the_sweep_list():
    """The cancel half of the replace was refused, so the old order's fate is
    the exchange's word against nothing; it is watched until it is gone."""
    from bulkdn.accounts import OrderRejected

    chaser, book, master, _s = build(max_distance_bps=5.0)
    book.set_authoritative(MASTER, BTC, 0.0)
    leg = LegState(symbol=BTC, target_size=1.0)
    await chaser.step(OPEN_BTC, leg)
    old = leg.oid

    async def cancel_half_refused(**kwargs):
        exc = OrderRejected("cancel refused")
        exc.order_id, exc.placed = "adopted-oid", True
        raise exc

    master.place_limit = cancel_half_refused
    chaser.feed._quote = Quote(
        BTC, best_bid=101_000.0, best_ask=101_010.0, mark_price=101_005.0, age_s=0.0
    )
    await chaser.step(OPEN_BTC, leg)

    assert leg.oid == "adopted-oid"
    assert leg.stale_oids == [old]


async def test_a_long_unseen_order_is_still_replaced_rather_than_doubled(monkeypatch):
    """However long the leg was not stepped -- a stale price holds it -- its
    order is not forgotten for having never shown up."""
    chaser, book, master, _s = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    leg = LegState(symbol=BTC, target_size=1.0)
    await chaser.step(OPEN_BTC, leg)
    first = leg.oid
    master.client.order_map.pop(first)

    _later(monkeypatch, 600.0)
    await chaser.step(OPEN_BTC, leg)

    assert master.placed[-1]["cancel_oid"] == first



async def test_a_remainder_under_the_minimum_at_the_order_price_completes_the_leg():
    """The minimum was measured at the mark while the order goes out at the
    bid. $50.75 at the mark is $49.70 at the bid: refused, and five refusals
    in a row halt the run."""
    sol = MarketSpec(symbol=BTC, tick_size=0.01, lot_size=0.01, min_notional=50.0)
    quote = Quote(BTC, best_bid=142.0, best_ask=142.1, mark_price=145.0, age_s=0.0)
    chaser, book, master, _s = build(quote=quote, max_order_size=1.0)
    chaser.feed.specs[BTC] = sol
    book.set_authoritative(MASTER, BTC, 1.65)
    leg = LegState(symbol=BTC, target_size=2.0)          # 0.35 to go

    outcome = await chaser.step(OPEN_BTC, leg)

    assert master.placed == [], "an order under the minimum went out"
    assert outcome.action == "complete"


@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_an_unanswered_placement_stays_tracked(failure):
    """A placement that timed out may be resting. `place_limit` carries its
    id on the exception; dropping it left an order nothing tracked, and the
    next pass placed a second one beside it."""
    chaser, book, master, _s = build(max_distance_bps=5.0)
    book.set_authoritative(MASTER, BTC, 0.0)
    leg = LegState(symbol=BTC, target_size=1.0)
    await chaser.step(OPEN_BTC, leg)
    old = leg.oid

    async def no_answer(**kwargs):
        exc = failure()
        exc.order_id, exc.placed = "in-doubt-oid", None
        raise exc

    master.place_limit = no_answer
    chaser.feed._quote = Quote(
        BTC, best_bid=101_000.0, best_ask=101_010.0, mark_price=101_005.0, age_s=0.0
    )
    with pytest.raises(failure):
        await chaser.step(OPEN_BTC, leg)

    assert leg.oid == "in-doubt-oid", "the possibly-resting order was forgotten"
    assert leg.stale_oids == [old], "the order it replaced may still rest"
    assert chaser.may_be_resting(master, "in-doubt-oid")


async def test_a_placement_that_never_left_is_not_tracked():
    from bulkdn.accounts import NotConnected

    chaser, book, master, _s = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    leg = LegState(symbol=BTC, target_size=1.0)

    async def down(**kwargs):
        raise NotConnected("socket down")

    master.place_limit = down
    with pytest.raises(NotConnected):
        await chaser.step(OPEN_BTC, leg)
    assert leg.oid is None

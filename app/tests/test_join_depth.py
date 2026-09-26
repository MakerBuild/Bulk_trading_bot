"""`join_depth_usd`: queue behind a level others hold, never stand alone.

From live fills out of Tokyo: when our maker order was the only thing at its
price, the hedge met a touch ~1.6bps worse, because taking us emptied the
level. With more than 1 BTC resting beside us it met our own price. The
default `improve_ticks: 1` is what put us alone -- one tick ahead of the book.
"""

import pytest

from bulkdn.chaser import ChaseParams, Chaser
from bulkdn.config import ConfigError, LegConfig, _leg_from_dict
from bulkdn.feed import Quote
from bulkdn.positions import PositionBook
from bulkdn.state import LegState

from test_chaser import BTC, MASTER, OPEN_BTC, SPEC, SUB1, FakeOrder, FakeSession

BID, ASK = 100_000.0, 100_010.0


class LevelFeed:
    def __init__(self, bids):
        self.specs = {BTC: SPEC}
        self.bids = bids

    def quote(self, symbol):
        return Quote(BTC, best_bid=self.bids[0][0], best_ask=ASK,
                     mark_price=BID + 5, age_s=0.0)

    def reference_price(self, symbol):
        return BID + 5

    def levels(self, symbol, is_buy, n):
        assert is_buy, "these tests only rest bids"
        return list(self.bids[:n])


def build(bids, *, join=2_000.0, improve=1):
    master = FakeSession("master", MASTER)
    sub1 = FakeSession("sub1", SUB1)
    feed = LevelFeed(bids)
    chaser = Chaser(
        sessions={MASTER: master, SUB1: sub1},
        feed=feed,
        book=PositionBook(overlay_ttl_ms=5000),
        params={BTC: ChaseParams(
            offset_bps=0.0, max_distance_bps=5.0, max_order_size=0.01,
            improve_ticks=improve, join_depth_usd=join,
        )},
    )
    leg = LegState(symbol=BTC, target_size=0.01)
    return chaser, master, feed, leg


async def test_without_it_the_order_steps_ahead_of_the_book():
    chaser, master, _feed, leg = build([(BID, 0.05)], join=0.0)
    await chaser.step(OPEN_BTC, leg)
    assert master.placed[-1]["price"] == BID + 0.5


async def test_it_joins_the_touch_instead_of_beating_it():
    chaser, master, _feed, leg = build([(BID, 0.05)])  # $5,000 at the touch
    await chaser.step(OPEN_BTC, leg)
    assert master.placed[-1]["price"] == BID


async def test_dust_at_the_touch_is_skipped_for_a_level_with_depth():
    chaser, master, _feed, leg = build([(BID, 0.001), (BID - 0.5, 0.05)])
    await chaser.step(OPEN_BTC, leg)
    assert master.placed[-1]["price"] == BID - 0.5


async def test_nothing_deep_nearby_joins_the_touch_rather_than_resting_back():
    """Resting 1bps back measured worse, so depth further away than
    JOIN_MAX_BPS does not pull the order back to it."""
    chaser, master, _feed, leg = build([(BID, 0.001), (BID - 20.0, 5.0)])
    await chaser.step(OPEN_BTC, leg)
    assert master.placed[-1]["price"] == BID


async def test_our_own_orders_do_not_count_as_depth():
    chaser, master, _feed, leg = build([(BID, 0.05), (BID - 0.5, 0.05)])
    # Another group of ours holds the whole touch.
    master.client.order_map["other-group"] = FakeOrder(price=BID, size=0.05)
    chaser._ours["other-group"] = (BTC, True, MASTER)

    await chaser.step(OPEN_BTC, leg)

    assert master.placed[-1]["price"] == BID - 0.5


async def test_an_order_left_alone_at_its_level_moves_to_one_with_depth():
    chaser, master, feed, leg = build([(BID, 0.05), (BID - 0.5, 0.05)])
    leg.tightened = True
    await chaser.step(OPEN_BTC, leg)
    assert master.placed[-1]["price"] == BID

    # Everyone else at our price has gone: the level is just us now.
    feed.bids = [(BID, 0.01), (BID - 0.5, 0.05)]
    await chaser.step(OPEN_BTC, leg)

    assert master.placed[-1]["price"] == BID - 0.5
    assert master.placed[-1]["cancel_oid"] == "oid-1"


async def test_a_thinner_queue_is_kept_rather_than_hopping():
    """Each hop goes to the back of a new queue, so a level that is still
    half as deep as asked for is kept."""
    chaser, master, feed, leg = build([(BID, 0.05), (BID - 0.5, 0.05)])
    leg.tightened = True
    await chaser.step(OPEN_BTC, leg)

    feed.bids = [(BID, 0.01 + 0.012), (BID - 0.5, 0.05)]  # others: $1,200
    await chaser.step(OPEN_BTC, leg)

    assert len(master.placed) == 1, "the order moved for a queue still worth keeping"


async def test_the_offset_phase_is_left_alone():
    """Resting deliberately behind the touch is a different mode; joining only
    applies once the order has gone to the touch."""
    chaser, master, _feed, leg = build([(BID, 0.001), (BID - 0.5, 0.05)])
    chaser.params[BTC].offset_bps = 2.0
    chaser.params[BTC].chase_patience_s = 60.0
    await chaser.step(OPEN_BTC, leg)
    assert master.placed[-1]["price"] == pytest.approx(BID * (1 - 2 / 10_000), abs=0.5)


# -- the setting ---------------------------------------------------------------


def test_it_is_off_unless_asked_for():
    leg = _leg_from_dict({"symbol": BTC, "notional_usd": 100}, "btc")
    assert leg.join_depth_usd == 0.0


def test_it_is_read_from_the_settings():
    leg = _leg_from_dict(
        {"symbol": BTC, "notional_usd": 100, "join_depth_usd": 2000}, "btc"
    )
    assert leg.join_depth_usd == 2000.0


def test_a_negative_is_refused():
    leg = LegConfig(symbol=BTC, size=1.0, max_order_size=1.0, join_depth_usd=-1)
    with pytest.raises(ConfigError, match="join_depth_usd"):
        leg.validate("btc")

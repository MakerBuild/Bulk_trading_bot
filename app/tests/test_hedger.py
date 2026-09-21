"""Hedging is the correctness core: net exposure must return to zero.

These tests exercise the property that matters -- the hedge is derived from
position state, so it is idempotent and self-correcting rather than dependent
on seeing exactly one event per fill.
"""

import pytest

from bulkdn.hedger import Hedger, HedgeLimitExceeded, InFlight, LegRoles
from bulkdn.marketdata import MarketSpec
from bulkdn.positions import PositionBook

MASTER = "master-pubkey"
SUB1 = "sub1-pubkey"
BTC = "BTC-USD"

SPEC = MarketSpec(symbol=BTC, tick_size=0.5, lot_size=0.001, min_notional=10.0)
PRICE = 100_000.0


class FakeSession:
    def __init__(self, name, pubkey):
        self.name = name
        self.pubkey = pubkey
        self.orders = []

    async def market(self, symbol, is_buy, size, reduce_only=False):
        self.orders.append(
            {"symbol": symbol, "is_buy": is_buy, "size": size, "reduce_only": reduce_only}
        )
        return []


def build(tolerance_lots=1.0, ceilings=None, ttl_ms=5000):
    book = PositionBook(overlay_ttl_ms=ttl_ms)
    master = FakeSession("master", MASTER)
    sub1 = FakeSession("sub1", SUB1)
    hedger = Hedger(
        book=book,
        sessions={MASTER: master, SUB1: sub1},
        specs={BTC: SPEC},
        tolerance_lots=tolerance_lots,
        max_hedge_size=ceilings,
        in_flight_ttl_ms=ttl_ms,
    )
    return book, hedger, master, sub1


OPEN_BTC = LegRoles(BTC, maker=MASTER, taker=SUB1, maker_is_buy=True, reduce_only=False)
EXIT_BTC = LegRoles(BTC, maker=SUB1, taker=MASTER, maker_is_buy=True, reduce_only=True)


async def test_partial_fill_is_hedged_for_exactly_its_size():
    book, hedger, _master, sub1 = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    book.set_authoritative(SUB1, BTC, 0.0)

    # Master's BTC limit fills 0.10.
    book.apply_fill(MASTER, BTC, is_buy=True, size=0.10)
    result = await hedger.hedge(OPEN_BTC, mark_price=PRICE)

    assert result.hedged_size == 0.10
    assert sub1.orders == [
        {"symbol": BTC, "is_buy": False, "size": 0.10, "reduce_only": False}
    ]


async def test_each_partial_fill_hedges_independently():
    book, hedger, _master, sub1 = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    book.set_authoritative(SUB1, BTC, 0.0)

    book.apply_fill(MASTER, BTC, is_buy=True, size=0.10)
    await hedger.hedge(OPEN_BTC, mark_price=PRICE)
    # The hedge fill lands on sub1 and retires its reservation.
    book.apply_fill(SUB1, BTC, is_buy=False, size=0.10)
    hedger.note_taker_fill(BTC, -0.10)

    # A second partial fill arrives.
    book.apply_fill(MASTER, BTC, is_buy=True, size=0.05)
    await hedger.hedge(OPEN_BTC, mark_price=PRICE)

    assert [o["size"] for o in sub1.orders] == [0.10, 0.05]


async def test_hedging_twice_without_a_new_fill_is_a_no_op():
    """The property that makes replayed fills harmless."""
    book, hedger, _master, sub1 = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    book.set_authoritative(SUB1, BTC, 0.0)

    book.apply_fill(MASTER, BTC, is_buy=True, size=0.10)
    await hedger.hedge(OPEN_BTC, mark_price=PRICE)
    second = await hedger.hedge(OPEN_BTC, mark_price=PRICE)

    assert second.hedged_size == 0.0
    assert len(sub1.orders) == 1


async def test_already_neutral_does_nothing():
    book, hedger, _master, sub1 = build()
    book.set_authoritative(MASTER, BTC, 1.0)
    book.set_authoritative(SUB1, BTC, -1.0)

    result = await hedger.hedge(OPEN_BTC, mark_price=PRICE)
    assert result.hedged_size == 0.0
    assert result.skipped_reason == "within tolerance"
    assert sub1.orders == []


async def test_net_short_is_hedged_by_buying():
    book, hedger, _master, sub1 = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    book.set_authoritative(SUB1, BTC, -0.2)

    result = await hedger.hedge(OPEN_BTC, mark_price=PRICE)
    assert result.is_buy is True
    assert sub1.orders[0]["is_buy"] is True
    assert sub1.orders[0]["size"] == 0.2


async def test_exit_hedge_closes_the_counter_position_reduce_only():
    """Sub1 buys back 0.25 of its short; master must sell 0.25 of its long."""
    book, hedger, master, _sub1 = build()
    book.set_authoritative(MASTER, BTC, 1.0)
    book.set_authoritative(SUB1, BTC, -1.0)

    book.apply_fill(SUB1, BTC, is_buy=True, size=0.25)
    result = await hedger.hedge(EXIT_BTC, mark_price=PRICE)

    assert result.hedged_size == 0.25
    assert master.orders == [
        {"symbol": BTC, "is_buy": False, "size": 0.25, "reduce_only": True}
    ]


async def test_residual_below_lot_size_is_left_alone():
    book, hedger, _master, sub1 = build()
    book.set_authoritative(MASTER, BTC, 0.0005)
    book.set_authoritative(SUB1, BTC, 0.0)

    result = await hedger.hedge(OPEN_BTC, mark_price=PRICE)
    assert result.hedged_size == 0.0
    assert sub1.orders == []


async def test_residual_below_min_notional_is_left_alone():
    book, hedger, _master, sub1 = build()
    # 0.001 BTC at $100 is $0.10, well under the $10 minimum notional.
    book.set_authoritative(MASTER, BTC, 0.001)
    book.set_authoritative(SUB1, BTC, 0.0)

    result = await hedger.hedge(OPEN_BTC, mark_price=100.0)
    assert result.hedged_size == 0.0
    assert result.skipped_reason == "below min notional"


async def test_in_flight_hedge_prevents_double_firing():
    """A second fill arriving before the hedge fills must not re-hedge it all."""
    book, hedger, _master, sub1 = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    book.set_authoritative(SUB1, BTC, 0.0)

    book.apply_fill(MASTER, BTC, is_buy=True, size=0.10)
    await hedger.hedge(OPEN_BTC, mark_price=PRICE)

    # Another 0.05 fills while the first hedge is still in flight.
    book.apply_fill(MASTER, BTC, is_buy=True, size=0.05)
    await hedger.hedge(OPEN_BTC, mark_price=PRICE)

    # Only the incremental 0.05 is hedged, not the full 0.15.
    assert [o["size"] for o in sub1.orders] == [0.10, 0.05]


async def test_hedge_beyond_the_ceiling_raises_rather_than_trading():
    book, hedger, _master, sub1 = build(ceilings={BTC: 0.05})
    book.set_authoritative(MASTER, BTC, 1.0)
    book.set_authoritative(SUB1, BTC, 0.0)

    with pytest.raises(HedgeLimitExceeded):
        await hedger.hedge(OPEN_BTC, mark_price=PRICE)
    assert sub1.orders == []


async def test_failed_hedge_releases_its_reservation_so_it_retries():
    book, hedger, _master, sub1 = build()
    book.set_authoritative(MASTER, BTC, 0.0)
    book.set_authoritative(SUB1, BTC, 0.0)
    book.apply_fill(MASTER, BTC, is_buy=True, size=0.10)

    async def boom(*args, **kwargs):
        raise RuntimeError("rejected")

    sub1.market = boom
    with pytest.raises(RuntimeError):
        await hedger.hedge(OPEN_BTC, mark_price=PRICE)

    # Exposure is still real, so the next attempt must see it.
    assert hedger.in_flight.total(BTC) == 0.0
    assert hedger.effective_net(OPEN_BTC) == pytest.approx(0.10)


def test_in_flight_only_consumes_same_direction_fills():
    in_flight = InFlight(ttl_ms=5000)
    in_flight.add(BTC, -0.10)  # pending sell

    # A buy fill on the maker side must not cancel a pending sell hedge.
    in_flight.consume(BTC, 0.10)
    assert in_flight.total(BTC) == -0.10

    in_flight.consume(BTC, -0.10)
    assert in_flight.total(BTC) == 0.0


def test_in_flight_expires():
    in_flight = InFlight(ttl_ms=0)
    in_flight.add(BTC, -0.10)
    assert in_flight.total(BTC) == 0.0


# -- what the book looked like before the order went out --------------------
#
# The cost of a hedge splits three ways: the spread it crossed, the depth it
# ate past the touch, and the price moving while it was in flight. Only the
# middle one is slippage, and only a reading taken BEFORE the order can
# measure it -- afterwards the depth is already gone. The exchange publishes
# no impact curve for these markets, so there is nothing to predict it from
# either.


class FakeQuoteFeed:
    def __init__(self, bid=99_999.0, ask=100_001.0, explode=False):
        self.bid, self.ask, self.explode = bid, ask, explode
        self.asked_at = []

    def quote(self, symbol):
        if self.explode:
            raise RuntimeError("book not ready")
        self.asked_at.append(symbol)
        return type("Q", (), {"best_bid": self.bid, "best_ask": self.ask})()


async def test_a_hedge_records_the_book_before_it_trades(caplog):
    book, hedger, _master, sub1 = build()
    hedger.feed = FakeQuoteFeed()
    book.apply_fill(MASTER, BTC, is_buy=True, size=0.01)

    with caplog.at_level("INFO", logger="bulkdn.hedger"):
        await hedger.hedge(OPEN_BTC, mark_price=PRICE)

    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("hedge "))
    assert "bid=99999.00000000" in line
    assert "ask=100001.00000000" in line
    assert sub1.orders, "the hedge itself still went out"


async def test_the_touch_is_read_before_the_order_not_after():
    """Read afterwards it would describe a book this very order has already
    eaten into, which is the result rather than the reference."""
    book, hedger, _master, sub1 = build()
    feed = FakeQuoteFeed()
    hedger.feed = feed

    async def market(symbol, is_buy, size, reduce_only=False):
        assert feed.asked_at, "the order went out before the book was read"
        sub1.orders.append({"symbol": symbol, "size": size})
        return []

    sub1.market = market
    book.apply_fill(MASTER, BTC, is_buy=True, size=0.01)
    await hedger.hedge(OPEN_BTC, mark_price=PRICE)

    assert sub1.orders


async def test_a_hedger_without_a_feed_still_hedges(caplog):
    """`flatten` and the reconciler build one without a feed. Losing a hedge
    over a log line would be an absurd trade."""
    book, hedger, _master, sub1 = build()

    with caplog.at_level("INFO", logger="bulkdn.hedger"):
        book.apply_fill(MASTER, BTC, is_buy=True, size=0.01)
        await hedger.hedge(OPEN_BTC, mark_price=PRICE)

    assert sub1.orders
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("hedge "))
    assert "bid=" not in line, "it invented a book it does not have"


async def test_a_book_that_throws_does_not_stop_the_hedge(caplog):
    book, hedger, _master, sub1 = build()
    hedger.feed = FakeQuoteFeed(explode=True)

    with caplog.at_level("INFO", logger="bulkdn.hedger"):
        book.apply_fill(MASTER, BTC, is_buy=True, size=0.01)
        await hedger.hedge(OPEN_BTC, mark_price=PRICE)

    assert sub1.orders, "an unhedged position, to save a log line"
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("hedge "))
    assert "bid=? ask=?" in line


# -- how much was resting at that price ------------------------------------
#
# Knowing a hedge filled past the touch says it slipped; it does not say
# whether the touch held nine tenths of the order or almost none of it. Those
# point at opposite conclusions about capping the hedge price, because what a
# price-bounded order leaves unfilled stops being slippage and becomes
# exposure. The size is in the book the bot already holds; it was simply never
# written down.


class FakeDepthFeed:
    def __init__(self, bid=99_999.0, ask=100_001.0, bid_size=0.5, ask_size=0.25):
        self.args = (bid, ask, bid_size, ask_size)

    def quote(self, symbol):
        bid, ask, bid_size, ask_size = self.args
        return type("Q", (), {
            "best_bid": bid, "best_ask": ask,
            "bid_size": bid_size, "ask_size": ask_size,
        })()


async def test_a_hedge_records_the_size_resting_at_the_touch(caplog):
    book, hedger, _master, sub1 = build()
    hedger.feed = FakeDepthFeed()
    book.apply_fill(MASTER, BTC, is_buy=True, size=0.01)

    with caplog.at_level("INFO", logger="bulkdn.hedger"):
        await hedger.hedge(OPEN_BTC, mark_price=PRICE)

    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("hedge "))
    assert "bidsz=0.50000000" in line
    assert "asksz=0.25000000" in line
    assert sub1.orders


async def test_a_book_without_sizes_still_logs_its_prices(caplog):
    """The size is a nicety; the price is the reference the cost is measured
    against, and losing it would cost the measurement entirely."""
    book, hedger, _master, sub1 = build()
    hedger.feed = FakeDepthFeed(bid_size=None, ask_size=None)
    book.apply_fill(MASTER, BTC, is_buy=True, size=0.01)

    with caplog.at_level("INFO", logger="bulkdn.hedger"):
        await hedger.hedge(OPEN_BTC, mark_price=PRICE)

    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("hedge "))
    assert "bid=99999.00000000" in line and "ask=100001.00000000" in line
    assert "sz=" not in line, "an absent size was printed as a number"


async def test_an_empty_level_is_left_out_rather_than_printed_as_zero(caplog):
    """`bidsz=0` would read as a price level with nothing on it, which is a
    different claim from not knowing."""
    book, hedger, _master, sub1 = build()
    hedger.feed = FakeDepthFeed(bid_size=0.0, ask_size=0.0)
    book.apply_fill(MASTER, BTC, is_buy=True, size=0.01)

    with caplog.at_level("INFO", logger="bulkdn.hedger"):
        await hedger.hedge(OPEN_BTC, mark_price=PRICE)

    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("hedge "))
    assert "sz=" not in line


# -- reservations belong to the leg, not to the market ----------------------
#
# In-flight hedges were keyed by symbol. Two groups trading BTC-USD at once
# would have retired each other's reservations and concluded they were already
# neutral -- which leaves a real position unhedged while the book says it is
# covered. Two groups on one market is the point of the account pool.


def other_pair():
    """A second group on the same market, sharing no accounts with the first."""
    return LegRoles(
        BTC, maker="third", taker="fourth", maker_is_buy=True,
        reduce_only=False, id="g2",
    )


def test_a_leg_defaults_to_being_keyed_by_its_market():
    """Which is what every leg was before groups existed."""
    assert OPEN_BTC.key == BTC


def test_two_groups_on_one_market_do_not_share_reservations():
    _book, hedger, _master, _sub1 = build()
    first = LegRoles(BTC, maker=MASTER, taker=SUB1, maker_is_buy=True,
                     reduce_only=False, id="g1")

    hedger.in_flight.add(first.key, -0.01)
    assert hedger.in_flight.total(other_pair().key) == 0.0
    assert hedger.in_flight.total(first.key) == -0.01


def test_one_groups_fill_does_not_retire_anothers_hedge():
    """The failure this prevents is silent: the second group reads itself as
    neutral and never sends the hedge it owes."""
    _book, hedger, _master, _sub1 = build()
    first = LegRoles(BTC, maker=MASTER, taker=SUB1, maker_is_buy=True,
                     reduce_only=False, id="g1")
    second = other_pair()

    hedger.in_flight.add(first.key, -0.01)
    hedger.in_flight.add(second.key, -0.02)
    hedger.note_taker_fill(first.key, -0.01)

    assert hedger.in_flight.total(first.key) == 0.0
    assert hedger.in_flight.total(second.key) == -0.02, "the other group's hedge was retired"


async def test_two_groups_on_one_market_hedge_independently():
    book, hedger, master, sub1 = build()
    first = LegRoles(BTC, maker=MASTER, taker=SUB1, maker_is_buy=True,
                     reduce_only=False, id="g1")

    book.apply_fill(MASTER, BTC, is_buy=True, size=0.01)
    result = await hedger.hedge(first, mark_price=PRICE)

    assert result.hedged_size == pytest.approx(0.01)
    assert sub1.orders, "the first group hedged"
    assert not master.orders, "the second group's accounts were not touched"


# -- one hedge covered by several accounts ----------------------------------
#
# Splitting is what keeps a pool of accounts unreadable even as the pairing
# moves: one maker of $4,000 answered by one taker of $4,000 is a line anyone
# can draw, and the same $4,000 answered by $1,800, $1,400 and $800 is not.


def split_roles(shares=(0.5, 0.3, 0.2), takers=("t1", "t2", "t3")):
    return LegRoles(
        BTC, maker=MASTER, taker=takers[0], maker_is_buy=True, reduce_only=False,
        id="g1", takers=takers, shares=shares,
    )


def test_a_leg_without_a_split_still_has_one_hedger():
    """Every leg outside a pool. It must behave exactly as it always has."""
    assert OPEN_BTC.hedgers == (SUB1,)
    assert OPEN_BTC.weights == (1.0,)


def test_the_slices_add_up_to_the_hedge():
    book, hedger, _master, _sub1 = build()
    pieces = hedger._slice(split_roles(), 0.1, SPEC)
    assert sum(size for _pubkey, size in pieces) == pytest.approx(0.1)


def test_each_slice_goes_to_its_own_account():
    _book, hedger, _master, _sub1 = build()
    pieces = hedger._slice(split_roles(), 0.1, SPEC)
    assert [pubkey for pubkey, _size in pieces] == ["t1", "t2", "t3"]
    assert len({pubkey for pubkey, _ in pieces}) == 3


def test_rounding_goes_to_the_last_slice_rather_than_being_lost():
    """Under-hedging leaves the group directional by the difference, and
    nothing notices until the reconciler runs."""
    _book, hedger, _master, _sub1 = build()
    pieces = hedger._slice(split_roles(shares=(1 / 3, 1 / 3, 1 / 3)), 0.01, SPEC)
    assert sum(size for _p, size in pieces) == pytest.approx(0.01)


def test_a_slice_below_one_lot_is_not_sent_as_a_smaller_one():
    """An order the exchange will not accept is a missing hedge, not a small
    one. Its weight goes to the accounts that can carry it."""
    _book, hedger, _master, _sub1 = build()
    pieces = hedger._slice(split_roles(shares=(0.98, 0.01, 0.01)), 0.01, SPEC)
    assert all(size >= SPEC.lot_size for _p, size in pieces)
    assert sum(size for _p, size in pieces) == pytest.approx(0.01)


def test_a_hedge_too_small_to_split_goes_to_one_account_whole():
    _book, hedger, _master, _sub1 = build()
    pieces = hedger._slice(split_roles(), SPEC.lot_size, SPEC)
    assert len(pieces) == 1
    assert pieces[0][1] == pytest.approx(SPEC.lot_size)


def test_the_net_counts_every_account_in_the_leg():
    """Reading only the first hedger reports the others' coverage as missing,
    and hedges it a second time."""
    book, hedger, _master, _sub1 = build()
    roles = split_roles()
    book.set_authoritative(MASTER, BTC, 0.09)
    book.set_authoritative("t1", BTC, -0.05)
    book.set_authoritative("t2", BTC, -0.03)
    book.set_authoritative("t3", BTC, -0.01)

    assert hedger.effective_net(roles) == pytest.approx(0.0)


# -- and the pieces have to be ones the exchange will take -------------------
#
# The lot is not the floor that bites. ETH-USD takes a lot of 0.0001 -- about
# forty cents -- and refuses any order under $50. Measuring a slice against
# the lot alone therefore produced pieces the exchange rejected, one order at
# a time, until the reject streak halted the run. A split that cannot clear
# the market's own minimum should yield FEWER pieces, which is what the
# settings file has always said `max_takers` does.


def test_a_slice_under_the_markets_minimum_notional_is_not_sent():
    """Every piece has to be an order the exchange would accept."""
    _book, hedger, _master, _sub1 = build()
    price = 1000.0  # so SPEC's $10 minimum is 0.01 -- ten lots
    pieces = hedger._slice(split_roles(), 0.03, SPEC, price)

    assert pieces, "the hedge has to go somewhere"
    for _pubkey, size in pieces:
        assert size * price >= SPEC.min_notional, "the exchange would reject this"


def test_the_hedge_is_still_covered_in_full():
    """Dropping a piece must not drop its exposure with it."""
    _book, hedger, _master, _sub1 = build()
    pieces = hedger._slice(split_roles(), 0.03, SPEC, 1000.0)

    assert sum(size for _p, size in pieces) == pytest.approx(0.03)


def test_asking_for_more_pieces_than_the_size_carries_yields_fewer():
    """Not rejected ones. This is the promise `max_takers` makes."""
    _book, hedger, _master, _sub1 = build()
    price = 1000.0

    roomy = hedger._slice(split_roles(), 0.09, SPEC, price)
    tight = hedger._slice(split_roles(), 0.021, SPEC, price)

    assert len(roomy) == 3
    assert len(tight) < 3, "a size this small cannot carry three orders"
    assert sum(size for _p, size in tight) == pytest.approx(0.021)


def test_a_hedge_that_cannot_be_split_at_all_goes_whole():
    """One account, one order, which is what a pair does anyway."""
    _book, hedger, _master, _sub1 = build()
    pieces = hedger._slice(split_roles(), 0.011, SPEC, 1000.0)

    assert len(pieces) == 1
    assert pieces[0][1] == pytest.approx(0.011)


def test_without_a_price_the_lot_is_still_the_floor():
    """The old behaviour, and all that is left when the feed has no quote."""
    _book, hedger, _master, _sub1 = build()
    pieces = hedger._slice(split_roles(), 0.03, SPEC, None)

    assert len(pieces) == 3, "nothing should have been dropped"


# -- and the line that announces it says where it went ----------------------


def test_a_single_hedge_reads_as_it_always_did():
    """By the session's short name, which is what a log is read with."""
    _book, hedger, _master, sub1 = build()

    assert hedger._destination([(SUB1, 0.001)]) == f"on {sub1.name}"


def test_a_split_hedge_names_every_account_and_its_share():
    """The three fills under this line should need no adding up."""
    _book, hedger, _master, _sub1 = build()

    said = hedger._destination([("t1", 0.000083), ("t2", 0.000229), ("t3", 0.000126)])

    assert said.startswith("across ")
    for name, piece in (("t1", "0.00008300"), ("t2", "0.00022900"), ("t3", "0.00012600")):
        assert f"{name} {piece}" in said, said


def test_an_account_with_no_session_is_named_by_its_pubkey():
    """A message about an account is worth printing even when the session
    map has nothing to call it."""
    _book, hedger, _master, _sub1 = build()

    said = hedger._destination([("STRANGER-PUBKEY-1234", 0.001)])

    assert said.startswith("on ")
    assert "STRANG" in said and "1234" in said, said

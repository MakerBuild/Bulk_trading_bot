"""Making a trade between the two accounts impossible, not merely unlikely.

The hedge is a market order, so it sweeps as deep as it needs to -- straight
through this bot's own resting order on the other account. BULK's self-trade
prevention does not help: it is per account, and the documentation says orders
from different sub-accounts under one main account may trade against each other.

Widening `offset_bps` only lowers the odds. How far a hedge sweeps depends on
the book at that instant, and it was measured reaching $28.74 past the touch on
BTC. So the hedge is bounded by price instead: one tick short of wherever our
own order rests.
"""

import asyncio

from bulkdn.hedger import Hedger
from bulkdn.marketdata import MarketSpec

BTC = "BTC-USD"
SPEC = MarketSpec(BTC, tick_size=0.5, lot_size=0.001, min_notional=1.0)


def bound(avoid, is_buy):
    return Hedger._bounded_price(avoid, is_buy, SPEC)


# -- the bound itself --------------------------------------------------------


def test_a_hedging_sell_stays_above_our_resting_buy():
    """A sell walks DOWN the book. Our buy is below it, so the floor goes
    above ours."""
    assert bound(100_000.0, is_buy=False) == 100_000.5


def test_a_hedging_buy_stays_below_our_resting_sell():
    """A buy walks UP. Our sell is above, so the ceiling goes below ours."""
    assert bound(100_000.0, is_buy=True) == 99_999.5


def test_the_gap_is_exactly_one_tick():
    """Wider gives up liquidity for no extra safety -- one tick is the
    smallest step the book has, so nothing can sit between."""
    for is_buy in (True, False):
        assert abs(bound(100_000.0, is_buy) - 100_000.0) == SPEC.tick_size


def test_nothing_to_avoid_means_no_bound():
    """Then a plain market order is right: nothing of ours is on the book."""
    assert bound(None, is_buy=True) is None


def test_a_price_that_cannot_carry_a_tick_is_refused():
    """Rather than silently producing a zero or negative limit."""
    assert bound(0.5, is_buy=True) is None
    assert bound(0.0, is_buy=False) is None


# -- what the hedger sends ---------------------------------------------------


class FakeSession:
    def __init__(self):
        self.name = "sub1"
        self.markets = []
        self.limits = []

    async def market(self, symbol, is_buy, size, reduce_only=False):
        self.markets.append((symbol, is_buy, size, reduce_only))
        return []

    async def aggressive_limit(self, symbol, is_buy, price, size, reduce_only=False):
        self.limits.append((symbol, is_buy, price, size, reduce_only))
        return []


def hedger_with(net):
    from bulkdn.positions import PositionBook

    book = PositionBook()
    book.set_authoritative("MAKER", BTC, net)
    session = FakeSession()
    h = Hedger(
        book=book,
        sessions={"MAKER": session, "TAKER": session},
        specs={BTC: SPEC},
        tolerance_lots=1.0,
    )
    return h, session


def roles_for():
    from bulkdn.hedger import LegRoles

    return LegRoles(
        symbol=BTC, maker="MAKER", taker="TAKER",
        maker_is_buy=True, reduce_only=False,
    )


def hedge(net, avoid_price):
    h, session = hedger_with(net)
    asyncio.run(h.hedge(roles_for(), mark_price=100_000.0, avoid_price=avoid_price))
    return session


def test_without_a_price_to_avoid_it_is_a_market_order():
    session = hedge(0.01, avoid_price=None)
    assert session.markets and not session.limits


def test_with_one_it_is_a_bounded_limit_instead():
    """The whole point: a market order cannot be told where to stop."""
    session = hedge(0.01, avoid_price=99_000.0)
    assert session.limits and not session.markets, "it still swept the book"


def test_the_limit_stops_short_of_our_own_order():
    session = hedge(0.01, avoid_price=99_000.0)      # net long -> hedge sells
    _symbol, is_buy, price, _size, _ro = session.limits[0]
    assert is_buy is False
    assert price > 99_000.0, "the hedge could reach our own buy"
    assert price == 99_000.5


def test_the_other_direction_too():
    session = hedge(-0.01, avoid_price=101_000.0)    # net short -> hedge buys
    _symbol, is_buy, price, _size, _ro = session.limits[0]
    assert is_buy is True
    assert price < 101_000.0, "the hedge could reach our own sell"


def test_an_unboundable_price_holds_the_hedge_rather_than_sweeping():
    """Falling back to a market order there would be the one thing this
    exists to prevent. Staying exposed is visible to the risk limit; a
    self-trade is not."""
    # Net short, so the hedge buys: the bound goes BELOW our resting sell, and
    # 0.4 minus a 0.5 tick is not a price any order can carry.
    h, session = hedger_with(-0.01)
    result = asyncio.run(
        h.hedge(roles_for(), mark_price=100_000.0, avoid_price=0.4)
    )
    assert not session.markets and not session.limits
    assert result.acted is False
    assert result.skipped_reason == "unboundable"


def test_a_held_hedge_leaves_no_reservation_behind():
    """An in-flight reservation for an order that was never sent would make
    the next trigger think the exposure was already being dealt with."""
    h, _session = hedger_with(-0.01)
    asyncio.run(h.hedge(roles_for(), mark_price=100_000.0, avoid_price=0.4))
    assert h.in_flight.total(BTC) == 0.0


# -- and only while the order is really there --------------------------------


class LegLike:
    def __init__(self, oid, price):
        self.oid = oid
        self.price = price


class StateLike:
    def __init__(self, leg):
        self._leg = leg

    def leg(self, symbol):
        return self._leg


class BotLike:
    def __init__(self, oid, price):
        from bulkdn.strategy import Strategy

        self.state = StateLike(LegLike(oid, price))
        self._resting_price = Strategy._resting_price.__get__(self)


def test_the_price_is_used_while_an_order_is_resting():
    assert BotLike("abc123", 100_000.0)._resting_price(BTC) == 100_000.0


def test_a_cleared_order_id_means_nothing_is_resting():
    """`oid` and `price` are set together but a few paths clear only the id.
    Steering hedges around an order that is no longer on the book would hold
    corrections for nothing -- and, when the bound is unreachable, refuse them
    outright."""
    assert BotLike(None, 100_000.0)._resting_price(BTC) is None


def test_no_order_at_all_means_no_bound():
    assert BotLike(None, None)._resting_price(BTC) is None

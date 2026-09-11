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

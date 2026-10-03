"""A hedge of ours shrinking a hedger is not a liquidation.

A live run halted and closed every account at market over its own correction.
A fill that crossed zero on one hedger was briefly overwritten in the book, the
hedge rule bought again, and then sold the excess back -- which shrank both
hedgers it had bought on, in the exact proportion of their shares. The guard,
which assumed the bot never reduces a position while opening, called that a
position closed from outside; the exchange confirmed no liquidation, and the
run halted anyway.
"""

import pytest

from bulkdn import liquidation
from bulkdn.accounts import OrderRejected
from bulkdn.hedger import Hedger, LegRoles
from bulkdn.liquidation import LiquidationGuard
from bulkdn.marketdata import MarketSpec
from bulkdn.positions import PositionBook
from bulkdn.state import Phase

BTC = "BTC-USD"
SPEC = MarketSpec(BTC, tick_size=0.01, lot_size=0.000001, min_notional=1.0)
PRICE = 84_657.0
MAKER, H1, H2 = "MAKER-PUB", "HEDGER1-PUB", "HEDGER2-PUB"
OPEN = {(MAKER, BTC): Phase.OPEN, (H1, BTC): Phase.OPEN, (H2, BTC): Phase.OPEN}


def guard_over(book, held):
    guard = LiquidationGuard(specs={BTC: SPEC})
    for account, size in held.items():
        book.set_authoritative(account, BTC, size)
    guard.check(book, OPEN, list(held))          # the peaks, as they stood
    return guard


# -- the guard ------------------------------------------------------------------


def test_a_shrink_our_own_hedge_explains_is_not_reported():
    book = PositionBook()
    guard = guard_over(book, {H1: 0.001913, H2: 0.003517})

    guard.note_own_order(H1, BTC, -0.000752)
    guard.note_own_order(H2, BTC, -0.001381)
    book.set_authoritative(H1, BTC, 0.001161)
    book.set_authoritative(H2, BTC, 0.002136)

    assert guard.check(book, OPEN, [H1, H2]) == []


def test_after_our_hedge_the_next_shrink_is_measured_from_what_is_left():
    """Re-baselined, so a real liquidation right after is still seen."""
    book = PositionBook()
    guard = guard_over(book, {H1: 0.003517})
    guard.note_own_order(H1, BTC, -0.001381)
    book.set_authoritative(H1, BTC, 0.002136)
    guard.check(book, OPEN, [H1])

    book.set_authoritative(H1, BTC, 0.0)
    (event,) = guard.check(book, OPEN, [H1])
    assert event.previous == pytest.approx(0.002136)


def test_a_shrink_with_no_hedge_of_ours_behind_it_is_still_reported():
    book = PositionBook()
    guard = guard_over(book, {H1: 0.003517})
    book.set_authoritative(H1, BTC, 0.0)

    (event,) = guard.check(book, OPEN, [H1])
    assert event.fully_closed


def test_a_liquidation_larger_than_our_hedge_is_reported_whole():
    book = PositionBook()
    guard = guard_over(book, {H1: 0.003517})
    guard.note_own_order(H1, BTC, -0.001000)
    book.set_authoritative(H1, BTC, 0.0)

    (event,) = guard.check(book, OPEN, [H1])
    assert event.previous == pytest.approx(0.003517)


def test_a_hedge_that_adds_to_the_position_explains_no_shrink():
    book = PositionBook()
    guard = guard_over(book, {H1: 0.003517})
    guard.note_own_order(H1, BTC, +0.002000)      # a buy, on a long
    book.set_authoritative(H1, BTC, 0.001517)

    assert len(guard.check(book, OPEN, [H1])) == 1


def test_a_refused_hedge_explains_nothing():
    book = PositionBook()
    guard = guard_over(book, {H1: 0.003517})
    guard.note_own_order(H1, BTC, -0.002000)
    guard.withdraw_own_order(H1, BTC, -0.002000)
    book.set_authoritative(H1, BTC, 0.001517)

    assert len(guard.check(book, OPEN, [H1])) == 1


def test_an_old_hedge_stops_explaining(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(liquidation.time, "monotonic", lambda: clock[0])
    book = PositionBook()
    guard = guard_over(book, {H1: 0.003517})
    guard.note_own_order(H1, BTC, -0.002000)

    clock[0] += liquidation.OWN_ORDER_HOLD_S + 1
    book.set_authoritative(H1, BTC, 0.001517)
    assert len(guard.check(book, OPEN, [H1])) == 1


# -- the hedger tells the guard --------------------------------------------------


class Session:
    def __init__(self, pubkey, refuses=False):
        self.name = pubkey
        self.pubkey = pubkey
        self.refuses = refuses

    async def hedge_market(self, symbol, is_buy, size, reduce_only=False):
        if self.refuses:
            raise OrderRejected("rejected")
        return False


def build(refuses=False):
    book = PositionBook()
    hedger = Hedger(
        book=book,
        sessions={MAKER: Session(MAKER), H1: Session(H1, refuses), H2: Session(H2, refuses)},
        specs={BTC: SPEC},
    )
    guard = LiquidationGuard(specs={BTC: SPEC})
    hedger.own_orders = guard
    return book, hedger, guard


ROLES = LegRoles(
    BTC, maker=MAKER, taker=H2, maker_is_buy=False, reduce_only=False,
    takers=(H2, H1), shares=(0.65, 0.35),
)


async def test_the_correction_of_an_over_hedge_does_not_read_as_a_liquidation():
    """The live sequence: hedgers over-bought, the excess sold back."""
    book, hedger, guard = build()
    book.set_authoritative(MAKER, BTC, -0.003296)
    book.set_authoritative(H1, BTC, 0.001913)
    book.set_authoritative(H2, BTC, 0.003517)
    guard.check(book, OPEN, [MAKER, H1, H2])

    result = await hedger.hedge(ROLES, mark_price=PRICE)
    assert not result.is_buy and result.hedged_size == pytest.approx(0.002134)

    # The sells land; the exchange reports the hedgers smaller.
    book.set_authoritative(H1, BTC, 0.001913 - 0.000747)
    book.set_authoritative(H2, BTC, 0.003517 - 0.001387)

    assert guard.check(book, OPEN, [MAKER, H1, H2]) == []


async def test_a_refused_slice_is_withdrawn_from_the_guard():
    book, hedger, guard = build(refuses=True)
    book.set_authoritative(MAKER, BTC, -0.003296)
    book.set_authoritative(H1, BTC, 0.001913)
    book.set_authoritative(H2, BTC, 0.003517)
    guard.check(book, OPEN, [MAKER, H1, H2])

    with pytest.raises(OrderRejected):
        await hedger.hedge(ROLES, mark_price=PRICE)

    # Something else takes them down: nothing of ours explains it.
    book.set_authoritative(H1, BTC, 0.0)
    assert [e.account for e in guard.check(book, OPEN, [MAKER, H1, H2])] == [H1]

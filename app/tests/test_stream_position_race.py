"""A streamed position that leaves out a fill we already have.

A hedger holding -0.000001 bought 0.002135. The fill arrived, and then a
position update saying 0 -- the account partway through crossing zero, or an
update that simply did not include the fill. Written outright, it dropped the
fill: the book read the hedger as flat, the hedge rule bought the same
exposure a second time, sold the excess back, and the liquidation guard took
that for a position closed from outside. The run halted.
"""

import time

import pytest

from bulkdn import positions
from bulkdn.hedger import Hedger, LegRoles
from bulkdn.marketdata import MarketSpec
from bulkdn.positions import PositionBook

BTC = "BTC-USD"
SPEC = MarketSpec(BTC, tick_size=0.01, lot_size=0.000001, min_notional=1.0)
PRICE = 84_657.0
MAKER, H1, H2 = "MAKER-PUB", "HEDGER1-PUB", "HEDGER2-PUB"


# -- the book --------------------------------------------------------------------


def test_an_update_that_leaves_out_a_fill_we_hold_does_not_erase_it():
    book = PositionBook()
    book.set_authoritative(H2, BTC, -0.000001)
    book.apply_fill(H2, BTC, is_buy=True, size=0.002135)

    assert book.apply_stream_position(H2, BTC, 0.0) is False
    assert book.effective(H2, BTC) == pytest.approx(0.002134)
    assert book.awaiting_read(), "nothing asked for a read to settle it"


def test_an_update_that_includes_the_fill_replaces_it():
    """The ordinary order: the fill, then the position that accounts for it."""
    book = PositionBook()
    book.set_authoritative(H2, BTC, -0.000001)
    book.apply_fill(H2, BTC, is_buy=True, size=0.002135)

    assert book.apply_stream_position(H2, BTC, 0.002134) is True
    assert book.effective(H2, BTC) == pytest.approx(0.002134)
    assert not book.awaiting_read()


def test_an_update_from_before_the_fill_keeps_it():
    book = PositionBook()
    book.set_authoritative(H2, BTC, -0.000001)
    book.apply_fill(H2, BTC, is_buy=True, size=0.002135)

    assert book.apply_stream_position(H2, BTC, -0.000001) is True
    assert book.effective(H2, BTC) == pytest.approx(0.002134)


def test_an_update_that_accounts_for_some_fills_keeps_the_rest():
    """A market order filled in pieces, its second fill here before the
    update for the first."""
    book = PositionBook()
    book.set_authoritative(H1, BTC, 0.001913)
    book.apply_fill(H1, BTC, is_buy=False, size=0.000142)
    book.apply_fill(H1, BTC, is_buy=False, size=0.000610)

    assert book.apply_stream_position(H1, BTC, 0.001771) is True
    assert book.effective(H1, BTC) == pytest.approx(0.001161)


def test_with_no_fill_held_an_update_is_written_as_before():
    """A liquidation, a manual close or a fill we never saw still lands."""
    book = PositionBook()
    book.set_authoritative(H2, BTC, 0.003517)

    assert book.apply_stream_position(H2, BTC, 0.0) is True
    assert book.effective(H2, BTC) == 0.0


def test_a_contradicted_fill_outlives_its_ordinary_clock(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(positions.time, "monotonic", lambda: clock[0])
    book = PositionBook(overlay_ttl_ms=2000)
    book.set_authoritative(H2, BTC, -0.000001)
    book.apply_fill(H2, BTC, is_buy=True, size=0.002135)
    book.apply_stream_position(H2, BTC, 0.0)

    clock[0] += 5.0                            # well past the two seconds
    assert book.effective(H2, BTC) == pytest.approx(0.002134)


def test_a_read_sent_after_the_fill_settles_it():
    book = PositionBook()
    book.set_authoritative(H2, BTC, -0.000001)
    book.apply_fill(H2, BTC, is_buy=True, size=0.002135)
    book.apply_stream_position(H2, BTC, 0.0)

    sent = time.monotonic() + positions.READ_LAG_S + 0.1
    book.apply_read(
        H2, [type("P", (), {"symbol": BTC, "size": 0.002134})()], requested_at=sent,
    )
    assert book.effective(H2, BTC) == pytest.approx(0.002134)
    assert not book.awaiting_read()


def test_a_snapshot_that_omits_a_position_we_just_filled_does_not_zero_it():
    book = PositionBook()
    book.set_authoritative(H2, BTC, -0.000001)
    book.apply_fill(H2, BTC, is_buy=True, size=0.002135)

    assert book.apply_snapshot(H2, []) == [BTC]
    assert book.effective(H2, BTC) == pytest.approx(0.002134)


# -- the hedge that followed -------------------------------------------------------


class Session:
    def __init__(self, pubkey):
        self.name = pubkey
        self.pubkey = pubkey
        self.orders = []

    async def hedge_market(self, symbol, is_buy, size, reduce_only=False):
        self.orders.append((is_buy, size))
        return False


ROLES = LegRoles(
    BTC, maker=MAKER, taker=H2, maker_is_buy=False, reduce_only=False,
    takers=(H2, H1), shares=(0.65, 0.35),
)


async def test_the_live_sequence_sends_one_hedge_not_two():
    book = PositionBook()
    sessions = {MAKER: Session(MAKER), H1: Session(H1), H2: Session(H2)}
    hedger = Hedger(book=book, sessions=sessions, specs={BTC: SPEC})
    book.set_authoritative(MAKER, BTC, 0.0)
    book.set_authoritative(H1, BTC, 0.0)
    book.set_authoritative(H2, BTC, -0.000001)

    # The maker sells; the hedge buys it back across both hedgers.
    book.apply_fill(MAKER, BTC, is_buy=False, size=0.003296)
    await hedger.hedge(ROLES, mark_price=PRICE)
    for pubkey in (H1, H2):
        for is_buy, size in sessions[pubkey].orders:
            book.apply_fill(pubkey, BTC, is_buy=is_buy, size=size)
            hedger.note_taker_fill(ROLES.key, size if is_buy else -size)

    # The update that said the crossing hedger was flat.
    book.apply_stream_position(H2, BTC, 0.0)
    again = await hedger.hedge(ROLES, mark_price=PRICE)

    assert again.hedged_size == 0.0, "the same exposure was hedged twice"
    assert sum(len(s.orders) for s in sessions.values()) == 2

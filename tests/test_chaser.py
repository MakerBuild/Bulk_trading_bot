"""Chasing: replace only on real drift, and never leave two orders working."""

from dataclasses import dataclass

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


def build(quote=None, max_order_size=1.0, max_distance_bps=5.0):
    quote = quote or Quote(BTC, best_bid=100_000.0, best_ask=100_010.0, mark_price=100_005.0, age_s=0.0)
    book = PositionBook(overlay_ttl_ms=5000)
    master = FakeSession("master", MASTER)
    sub1 = FakeSession("sub1", SUB1)
    feed = FakeFeed(quote)
    chaser = Chaser(
        sessions={MASTER: master, SUB1: sub1},
        feed=feed,
        book=book,
        params={BTC: ChaseParams(offset_bps=0.0, max_distance_bps=max_distance_bps, max_order_size=max_order_size)},
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

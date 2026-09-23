"""Core fixes from the September audit: hedging, the book, the chaser, sizing.

Each test names the failure it pins. The ones that were seen live say so.
"""

import asyncio
import time
import types

import pytest

from bulkdn.feed import MarketFeed
from bulkdn.hedger import LegRoles
from bulkdn.marketdata import MarketSpec, epoch_seconds, min_order_size
from bulkdn.positions import PositionBook
from bulkdn.risk import RiskMonitor
from bulkdn.config import RiskConfig
from bulkdn.strategy import Strategy

BTC = "BTC-USD"
SOL = "SOL-USD"


class P:
    def __init__(self, symbol, size):
        self.symbol = symbol
        self.size = size


# -- the minimum order is rounded up -----------------------------------------


def test_the_minimum_order_size_clears_the_minimum_notional():
    """SOL at $143.37 with a 0.01 lot: rounding down gave 0.34 = $48.75."""
    spec = MarketSpec(SOL, tick_size=0.001, lot_size=0.01, min_notional=50.0)
    size = min_order_size(spec, 143.37)
    assert size == pytest.approx(0.35)
    assert size * 143.37 >= 50.0


def test_without_a_price_the_lot_stands_alone():
    spec = MarketSpec(SOL, tick_size=0.001, lot_size=0.01, min_notional=50.0)
    assert min_order_size(spec, None) == 0.01


# -- an HTTP read does not overwrite what the stream said since ---------------


def test_a_read_sent_before_a_fill_does_not_erase_it():
    """The reconciler hedged the same exposure twice this way."""
    book = PositionBook(overlay_ttl_ms=60_000)
    book.set_authoritative("m", BTC, 0.0)
    sent = time.monotonic() - 0.001
    book.apply_fill("m", BTC, is_buy=True, size=0.1)  # lands while the read is out

    skipped = book.apply_read("m", [P(BTC, 0.0)], requested_at=sent)

    assert skipped == [BTC]
    assert book.effective("m", BTC) == pytest.approx(0.1)


def test_a_read_newer_than_the_stream_is_applied():
    book = PositionBook()
    book.set_authoritative("m", BTC, 0.2)
    book.apply_read("m", [P(BTC, 0.3)], requested_at=time.monotonic() + 1)
    assert book.authoritative("m", BTC) == pytest.approx(0.3)


def test_a_snapshot_never_shows_the_account_flat_in_between():
    """It zeroed every position first and wrote the real ones after."""
    book = PositionBook()
    book.set_authoritative("m", BTC, 0.2)
    writes = []
    real = book.set_authoritative

    def spy(account, symbol, size):
        writes.append((symbol, size))
        real(account, symbol, size)

    book.set_authoritative = spy
    book.apply_snapshot("m", [P(BTC, 0.2)])
    assert writes == [(BTC, 0.2)]


# -- risk -----------------------------------------------------------------


def _risk(book, price=100.0):
    feed = types.SimpleNamespace(reference_price=lambda symbol: price)
    sessions = {
        name: types.SimpleNamespace(
            pubkey=name, name=name, reject_streak=0, dry_run=True, last_reject=""
        )
        for name in ("a1", "a2", "b1", "b2")
    }
    return RiskMonitor(
        config=RiskConfig(max_net_exposure_usd=100.0, max_position_usd=1e9),
        book=book, feed=feed, sessions=sessions, symbols=[BTC],
    )


def test_two_groups_cannot_hide_each_other_s_exposure():
    """+$300 and -$300 summed to $0 while each group sat directional."""
    book = PositionBook()
    book.set_authoritative("a1", BTC, 3.0)
    book.set_authoritative("b1", BTC, -3.0)
    risk = _risk(book)
    assert not [v for v in risk.check() if v.kind == "net_exposure"]

    risk.groups = lambda: [("group 1", BTC, ("a1", "a2")), ("group 2", BTC, ("b1", "b2"))]
    assert len([v for v in risk.check() if v.kind == "net_exposure"]) == 2


def test_a_missing_price_uses_the_last_one_not_zero():
    book = PositionBook()
    book.set_authoritative("a1", BTC, 3.0)
    prices = {"now": 100.0}
    risk = _risk(book)
    risk.feed = types.SimpleNamespace(reference_price=lambda symbol: prices["now"])
    assert risk.check()
    prices["now"] = None
    assert risk.check(), "the limit passed while the price was missing"


# -- the feed -------------------------------------------------------------


def test_timestamps_are_read_in_any_unit():
    now = 1_790_000_000.0
    for stamp in (now, now * 1e3, now * 1e6, now * 1e9):
        assert epoch_seconds(stamp) == pytest.approx(now)


def test_a_frozen_book_is_dropped_for_the_mark():
    level = lambda price: types.SimpleNamespace(price=price, size=1.0)  # noqa: E731
    book = types.SimpleNamespace(
        get_best_bid=lambda: level(100.0),
        get_best_ask=lambda: level(100.1),
        last_update_time=(time.time() - 60) * 1000,
    )
    ticker = types.SimpleNamespace(mark_price=110.0, timestamp=time.time() * 1000)
    client = types.SimpleNamespace(get_book=lambda s: book, get_ticker=lambda s: ticker)
    feed = MarketFeed(types.SimpleNamespace(client=client), [BTC])

    quote = feed.quote(BTC)
    assert quote.best_bid is None and quote.best_ask is None
    assert quote.reference_price == 110.0


def test_a_quiet_book_the_mark_agrees_with_is_kept():
    level = lambda price: types.SimpleNamespace(price=price, size=1.0)  # noqa: E731
    book = types.SimpleNamespace(
        get_best_bid=lambda: level(100.0),
        get_best_ask=lambda: level(100.1),
        last_update_time=(time.time() - 60) * 1000,
    )
    ticker = types.SimpleNamespace(mark_price=100.05, timestamp=time.time() * 1000)
    client = types.SimpleNamespace(get_book=lambda s: book, get_ticker=lambda s: ticker)
    quote = MarketFeed(types.SimpleNamespace(client=client), [BTC]).quote(BTC)
    assert quote.best_bid == 100.0


# -- the hedge worker -------------------------------------------------------


def _worker_strategy(legs):
    obj = object.__new__(Strategy)
    obj._stop = asyncio.Event()
    obj._liquidation_seen = asyncio.Event()
    obj._hedge_queue = asyncio.Queue()
    obj._closing_out = False
    obj._halt_reason = None
    obj._book_suspect = False
    obj.pairing = None
    obj._groups = {}
    obj.symbols = [BTC]
    obj._roles_for_key = lambda key: LegRoles(
        symbol=BTC, maker="m", taker="t", maker_is_buy=True, reduce_only=False, id=key
    ) if key in legs else None
    return obj


async def test_two_legs_are_hedged_at_the_same_time():
    """Group B's round trips used to sit in front of group A's hedge."""
    obj = _worker_strategy({"g1", "g2"})
    inside = 0
    most = 0

    async def hedge(roles):
        nonlocal inside, most
        inside += 1
        most = max(most, inside)
        await asyncio.sleep(0.05)
        inside -= 1

    obj._hedge_leg = hedge
    worker = asyncio.create_task(obj._hedge_worker())
    obj._hedge_queue.put_nowait("g1")
    obj._hedge_queue.put_nowait("g2")
    await asyncio.sleep(0.2)
    obj._stop.set()
    await worker
    assert most == 2


async def test_one_leg_is_never_hedged_twice_at_once():
    obj = _worker_strategy({"g1"})
    inside = 0
    most = 0
    calls = 0

    async def hedge(roles):
        nonlocal inside, most, calls
        calls += 1
        inside += 1
        most = max(most, inside)
        await asyncio.sleep(0.05)
        inside -= 1

    obj._hedge_leg = hedge
    worker = asyncio.create_task(obj._hedge_worker())
    obj._hedge_queue.put_nowait("g1")
    await asyncio.sleep(0.02)  # its hedge is under way
    for _ in range(3):
        obj._hedge_queue.put_nowait("g1")
    await asyncio.sleep(0.3)
    obj._stop.set()
    await worker
    assert most == 1
    # The signals that arrived mid-hedge folded into one more pass.
    assert calls == 2


# -- the liquidation guard halts whatever happens mid-close -------------------


async def test_a_close_that_raises_still_halts():
    """A market with no spec was a KeyError that skipped the halt."""
    obj = object.__new__(Strategy)
    obj._halt_reason = None
    obj._stop = asyncio.Event()
    obj._closing_out = False
    obj.guard = types.SimpleNamespace(check=lambda *a: [types.SimpleNamespace(
        symbol="GONE-USD", describe=lambda: "m GONE-USD 1 -> 0", account="m",
    )])
    obj.sessions = {}
    obj.book = PositionBook()
    obj.feed = types.SimpleNamespace(specs={})
    obj.notifier = types.SimpleNamespace(
        send_soon=lambda coro: coro.close(), halted=lambda reason: asyncio.sleep(0)
    )
    obj.title = types.SimpleNamespace(halted=lambda reason: None)

    async def no(*a, **k):
        return False

    obj._deferred_to_our_own_orders = no
    obj._settled_by_a_fresh_read = no
    obj._liquidation_confirmed = no

    import bulkdn.strategy as strategy_module

    async def cancel_all(sessions, symbols):
        return None

    original = strategy_module.cancel_all_orders
    strategy_module.cancel_all_orders = cancel_all
    try:
        assert await obj._guard_liquidation({}) is True
    finally:
        strategy_module.cancel_all_orders = original
    assert obj._halt_reason is not None
    assert obj._stop.is_set()


def test_a_fill_the_read_could_not_see_outlives_its_ordinary_ttl():
    """Skipped and left to its two-second clock, the fill lapsed and the book
    fell back to the position from before it -- a close sized from that went
    out a second time."""
    book = PositionBook(overlay_ttl_ms=20)
    book.set_authoritative("m", BTC, 0.5)
    time.sleep(0.02)                 # Windows' clock moves in ~15ms steps
    sent = time.monotonic()
    time.sleep(0.02)
    book.apply_fill("m", BTC, is_buy=False, size=0.5)   # the close fills

    book.apply_read("m", [P(BTC, 0.5)], requested_at=sent)
    time.sleep(0.05)                                      # past its ordinary ttl

    assert book.effective("m", BTC) == pytest.approx(0.0)
    assert book.awaiting_read(), "nothing asked for a read to confirm it"


def test_the_confirming_read_settles_it_without_counting_twice():
    """A read sent after the fill can only include it, so it is written and
    the overlay goes -- the fill is not added on top of an answer that has it."""
    book = PositionBook(overlay_ttl_ms=20)
    book.set_authoritative("m", BTC, 0.5)
    time.sleep(0.02)                 # Windows' clock moves in ~15ms steps
    sent = time.monotonic()
    time.sleep(0.02)
    book.apply_fill("m", BTC, is_buy=False, size=0.5)
    book.apply_read("m", [P(BTC, 0.5)], requested_at=sent)

    book.apply_read("m", [P(BTC, 0.0)], requested_at=time.monotonic())

    assert book.effective("m", BTC) == pytest.approx(0.0)
    assert book.authoritative("m", BTC) == pytest.approx(0.0)
    assert not book.awaiting_read()


def test_a_read_sent_after_every_fill_is_simply_applied():
    book = PositionBook()
    book.apply_fill("m", BTC, is_buy=True, size=0.1)
    book.apply_read("m", [P(BTC, 0.1)], requested_at=time.monotonic())
    assert book.effective("m", BTC) == pytest.approx(0.1), "the fill was counted twice"

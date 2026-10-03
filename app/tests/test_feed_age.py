"""How old a price is, judged by when it arrived here.

The age used to be this machine's wall clock minus the exchange's stamp. A
clock a few seconds out made every price look stale, or none; a ticker that
never arrived read as zero seconds old; and a book without an update time
could never be found frozen. Missing data is now stale, not fresh.
"""

import time
import types

import pytest
from bulk_api.common import Topic

from bulkdn import feed as feed_module
from bulkdn.feed import MarketFeed

BTC = "BTC-USD"


def level(price):
    return types.SimpleNamespace(price=price, size=1.0)


def ticker(mark, stamp_s):
    return types.SimpleNamespace(symbol=BTC, mark_price=mark, timestamp=stamp_s * 1000)


def book_update(stamp_s):
    return types.SimpleNamespace(symbol=BTC, timestamp=stamp_s * 1000)


def build(mark=100.05, bid=100.0, ask=100.1):
    """A feed over a fake client, and a way to deliver messages to it."""
    handlers = {}
    book = types.SimpleNamespace(
        get_best_bid=lambda: level(bid), get_best_ask=lambda: level(ask)
    )
    state = {"ticker": None}
    client = types.SimpleNamespace(
        get_book=lambda s: book, get_ticker=lambda s: state["ticker"]
    )
    session = types.SimpleNamespace(
        client=client, on=lambda topic, handler: handlers.setdefault(topic, []).append(handler)
    )
    feed = MarketFeed(session, [BTC])
    feed.listen()

    def deliver_ticker(stamp_s, mark=mark):
        state["ticker"] = ticker(mark, stamp_s)
        for handler in handlers.get(Topic.TICKER, ()):
            handler(state["ticker"])

    def deliver_book(stamp_s):
        for handler in handlers.get(Topic.L2DELTA, ()):
            handler(book_update(stamp_s))

    return feed, deliver_ticker, deliver_book


def test_a_ticker_that_never_arrived_is_stale():
    feed, _ticker, _book = build()
    assert feed.quote(BTC).age_s == float("inf")


def test_a_ticker_just_received_is_fresh_whatever_the_clocks_say():
    """The exchange's clock a minute behind this one read every price as a
    minute old, and the chaser skipped every pass as stale."""
    feed, deliver_ticker, _book = build()
    deliver_ticker(time.time() - 60)

    assert feed.quote(BTC).age_s < 1.0


def test_age_counts_from_when_the_ticker_arrived(monkeypatch):
    feed, deliver_ticker, _book = build()
    deliver_ticker(time.time())
    later = time.monotonic() + 20
    monkeypatch.setattr(feed_module.time, "monotonic", lambda: later)

    assert feed.quote(BTC).age_s == pytest.approx(20, abs=1)


def test_a_stream_running_behind_the_exchange_reads_old():
    """Received just now, but sent thirty seconds after the one before it was:
    the socket is that far behind, and the price is that old. A lagging
    market-data socket priced maker orders off a book half a minute stale."""
    feed, deliver_ticker, _book = build()
    now = time.time()
    deliver_ticker(now - 1)            # the usual delay, whatever the skew
    deliver_ticker(now - 31)           # thirty seconds more than that

    assert feed.quote(BTC).age_s == pytest.approx(30, abs=1)


def test_a_book_that_never_updated_can_be_found_frozen():
    """No update time read as zero seconds old, so the frozen-book check
    never fired on it."""
    feed, deliver_ticker, _book = build(mark=110.0)
    deliver_ticker(time.time())

    quote = feed.quote(BTC)
    assert quote.best_bid is None and quote.reference_price == 110.0


def test_a_book_just_updated_is_kept_even_off_the_mark():
    """Away from the mark but moving: a fast market, not a frozen book."""
    feed, deliver_ticker, deliver_book = build(mark=110.0)
    deliver_ticker(time.time())
    deliver_book(time.time())

    assert feed.quote(BTC).best_bid == 100.0

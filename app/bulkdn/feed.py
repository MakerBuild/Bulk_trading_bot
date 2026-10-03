"""Market data access, read off a single WS client.

Every account shares one price view -- prices are public and identical for all
of them, so subscribing more than once would only multiply the bandwidth.
Market data comes from a socket of its own; see `market_data_session`.
"""

from __future__ import annotations

import logging
import time
from collections import deque

import requests
from bulk_api.common import Topic
from dataclasses import dataclass
from collections.abc import Sequence

from .accounts import AccountSession
from .marketdata import MarketSpec, epoch_seconds
from .retry import describe

log = logging.getLogger(__name__)

# How far outside the book's own touch the mark may sit before the book is
# taken to be frozen rather than merely quiet, and how old the book must be.
# See `MarketFeed.quote`.
_FROZEN_BOOK_BPS = 10.0
_FROZEN_BOOK_AGE_S = 5.0
# How far back `_Arrivals` looks for the shortest delay between the
# exchange's stamp and our receipt. Long enough to hold through a lagging
# spell, short enough that a clock correction is absorbed within minutes.
_SKEW_WINDOW_S = 600.0


class _Arrivals:
    """When one stream's messages for a symbol arrived, and how far behind.

    Age is measured on this machine's monotonic clock, from when the last
    message ARRIVED. It used to be the wall clock minus the exchange's stamp,
    which reads any difference between the two clocks as age: a few seconds
    out and every price was stale, or none was.

    Arrival alone cannot see a socket running behind, though -- messages keep
    arriving, every one of them old, and a live market-data socket fell 7 to
    32 seconds behind that way. So the stamps are still read, but only
    against each other: the shortest stamp-to-arrival delay in the last
    `_SKEW_WINDOW_S` is the clocks' difference plus the network's floor, and
    anything a message took beyond it is how far behind the stream is running.
    """

    def __init__(self) -> None:
        self.received_at: float | None = None
        self._behind_s = 0.0
        # (arrived, delay), with the delays increasing from the left: the
        # window's minimum is always the first entry.
        self._floor: deque[tuple[float, float]] = deque()

    def heard(self, stamp) -> None:
        now = time.monotonic()
        self.received_at = now
        if not stamp:
            self._behind_s = 0.0
            return
        delay = time.time() - epoch_seconds(stamp)
        while self._floor and self._floor[-1][1] >= delay:
            self._floor.pop()
        self._floor.append((now, delay))
        while now - self._floor[0][0] > _SKEW_WINDOW_S:
            self._floor.popleft()
        self._behind_s = delay - self._floor[0][1]

    def age_s(self) -> float:
        """How old the last message's data is now. Never heard: infinitely."""
        if self.received_at is None:
            return float("inf")
        return time.monotonic() - self.received_at + self._behind_s


@dataclass(frozen=True)
class Quote:
    symbol: str
    best_bid: float | None
    best_ask: float | None
    mark_price: float | None
    age_s: float
    # How much is resting AT the touch, on each side. Optional and defaulted so
    # every existing construction of a Quote keeps working -- nothing decides
    # anything on these, they exist to be written into the log.
    #
    # Without them a hedge that filled past the touch is known to have slipped
    # but not why: it cannot be told whether the touch held nine tenths of the
    # order and the rest walked, or held almost none of it. Those two point at
    # opposite conclusions about capping the hedge price, because what an
    # IOC leaves unfilled becomes exposure rather than saving.
    bid_size: float | None = None
    ask_size: float | None = None

    @property
    def reference_price(self) -> float | None:
        """Best available price for notional maths."""
        if self.mark_price:
            return self.mark_price
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return self.best_bid or self.best_ask


class MarketFeed:
    """Quotes and market specs for the symbols the strategy trades."""

    def __init__(self, session: AccountSession, symbols: Sequence[str]):
        self.session = session
        self.symbols = list(symbols)
        self.specs: dict[str, MarketSpec] = {}
        self._frozen_warned: set[str] = set()
        # When each symbol's ticker and book messages arrived. See `listen`.
        self._tickers: dict[str, _Arrivals] = {}
        self._books: dict[str, _Arrivals] = {}
        self._listening = False

    def listen(self) -> None:
        """Start timing ticker and book messages as they arrive. Idempotent.

        Until a symbol's first ticker is heard its price reads as stale, so
        this goes in before anything trades on the feed -- `subscribe` does
        it first thing. A ticker that arrived before it is only a second's
        delay: the next one is timed.
        """
        if self._listening:
            return
        self._listening = True
        self.session.on(Topic.TICKER, self._heard_ticker)
        self.session.on(Topic.L2SNAPSHOT, self._heard_book)
        self.session.on(Topic.L2DELTA, self._heard_book)

    def _heard_ticker(self, ticker) -> None:
        self._tickers.setdefault(ticker.symbol, _Arrivals()).heard(ticker.timestamp)

    def _heard_book(self, update) -> None:
        self._books.setdefault(update.symbol, _Arrivals()).heard(update.timestamp)

    def _age_s(self, arrivals: dict[str, _Arrivals], symbol: str) -> float:
        heard = arrivals.get(symbol)
        return heard.age_s() if heard is not None else float("inf")

    def load_specs(self, strict: bool = True) -> dict[str, MarketSpec]:
        """Fetch tick size, lot size, and min notional over HTTP.

        These are needed before the first order can be rounded correctly, so
        this is a blocking startup step rather than something driven off the
        stream.

        `strict=False` drops a market the exchange does not list, with a
        warning, instead of refusing. That is for closing and for status:
        they cover every market in the settings, switched off or not, so one
        delisted or misspelled market nobody trades stopped the panic button
        before it sent a single cancel. A run that trades keeps the refusal --
        it cannot trade a market it has no spec for.
        """
        info = self.session.http.get_exchange_info()
        markets = info if isinstance(info, list) else info.get("symbols", [])
        by_symbol = {m["symbol"]: m for m in markets if "symbol" in m}

        unlisted = [symbol for symbol in self.symbols if symbol not in by_symbol]
        if unlisted and strict:
            raise RuntimeError(
                f"{unlisted[0]} is not listed on this exchange. Available: "
                f"{sorted(by_symbol)[:20]}"
            )
        for symbol in unlisted:
            log.warning(
                "%s is not listed on this exchange -- skipped here; a position "
                "in it cannot be read or closed by the bot", symbol,
            )
        self.symbols = [symbol for symbol in self.symbols if symbol in by_symbol]

        for symbol in self.symbols:
            spec = MarketSpec.from_api(by_symbol[symbol])
            self.specs[symbol] = spec
            log.info(
                "%s spec: tick=%s lot=%s min_notional=%s",
                symbol,
                spec.tick_size,
                spec.lot_size,
                spec.min_notional,
            )
        return self.specs

    async def subscribe(self) -> None:
        """Subscribe to the L2 book for each traded symbol.

        The snapshot must come first: deltas alone have nothing to apply to, so
        the SDK discards them and logs "delta for uninitialized book" for every
        message until a snapshot arrives. Subscribing to both gives the book a
        starting state and then keeps it current.

        Tickers are already auto-subscribed on connect; the book is what the
        chaser needs, since resting inside the touch requires knowing the touch.
        """
        self.listen()
        for symbol in self.symbols:
            try:
                await self.session.client.subscribe_orderbook_snapshot(symbol)
            except Exception as exc:
                log.warning(
                    "could not subscribe to %s book snapshot (%s); "
                    "chasing will fall back to mark price",
                    symbol,
                    describe(exc),
                )
                continue
            try:
                await self.session.client.subscribe_orderbook_delta(symbol)
            except Exception as exc:
                # The snapshot alone still yields a usable, if staler, book.
                log.warning("could not subscribe to %s book deltas: %s", symbol, describe(exc))

    def quote(self, symbol: str) -> Quote:
        client = self.session.client
        best_bid = best_ask = None
        bid_size = ask_size = None

        book = client.get_book(symbol)
        if book is not None:
            bid_level = book.get_best_bid()
            ask_level = book.get_best_ask()
            best_bid = bid_level.price if bid_level else None
            best_ask = ask_level.price if ask_level else None
            bid_size = bid_level.size if bid_level else None
            ask_size = ask_level.size if ask_level else None

        ticker = client.get_ticker(symbol)
        mark_price = ticker.mark_price if ticker else None
        # A ticker never received is stale, not fresh. It read as zero
        # seconds old, so a chaser with no ticker at all never skipped.
        age_s = self._age_s(self._tickers, symbol)

        # A book that stopped updating -- its delta subscription failed, which
        # is tolerated, or the stream for it went quiet -- still answers with
        # a touch, and the chaser priced maker-only orders off it: rejected as
        # crossing, or resting where the market left long ago. Age alone
        # cannot tell a frozen book from a quiet one, so it is judged against
        # the ticker: a book whose touch the mark has left by more than
        # `_FROZEN_BOOK_BPS` is not describing this market. It is dropped, and
        # the price falls back to the mark, as with no book at all.
        if book is not None and mark_price and best_bid and best_ask:
            # A book with no update heard is as old as can be. Read from the
            # book's own stamp, a missing one read as zero seconds old and
            # the book could never be found frozen.
            book_age = self._age_s(self._books, symbol)
            band = _FROZEN_BOOK_BPS / 10_000
            outside = mark_price < best_bid * (1 - band) or mark_price > best_ask * (1 + band)
            if outside and book_age > _FROZEN_BOOK_AGE_S:
                if symbol not in self._frozen_warned:
                    self._frozen_warned.add(symbol)
                    log.warning(
                        "%s: book looks frozen (%s, touch %g/%g, mark %g) "
                        "-- pricing off the mark until it moves",
                        symbol,
                        "no update heard" if book_age == float("inf")
                        else f"{book_age:.0f}s since an update",
                        best_bid, best_ask, mark_price,
                    )
                best_bid = best_ask = bid_size = ask_size = None
            elif symbol in self._frozen_warned and not outside:
                self._frozen_warned.discard(symbol)
                log.info("%s: book is updating again", symbol)

        return Quote(
            symbol=symbol,
            best_bid=best_bid,
            best_ask=best_ask,
            mark_price=mark_price,
            age_s=age_s,
            bid_size=bid_size,
            ask_size=ask_size,
        )

    def levels(self, symbol: str, is_buy: bool, n: int) -> list[tuple[float, float]]:
        """The first `n` price levels on one side, best first, as (price, size).

        Empty when there is no book. Only for a caller that has already had a
        usable touch from `quote`: this does not repeat its frozen-book check.
        """
        book = self.session.client.get_book(symbol)
        if book is None:
            return []
        side = book.get_bids(n) if is_buy else book.get_asks(n)
        return [(level.price, level.size) for level in side if level.price and level.size]

    def reference_price(self, symbol: str) -> float | None:
        return self.quote(symbol).reference_price

    def http_price(self, symbol: str) -> float:
        """Mark price over HTTP, for decisions made before the socket is up.

        The streamed quote is the one to use once running; this exists for
        startup, where the ticker cache is still empty and a zero price would
        be read as "could not price" rather than "not connected yet".
        """
        try:
            body = requests.get(
                f"{self.session.http.base_url}/ticker/{symbol}", timeout=20
            )
            body.raise_for_status()
            data = body.json()
        except Exception as exc:
            log.warning("could not read a price for %s over HTTP: %s", symbol, describe(exc))
            return 0.0
        return float(data.get("markPrice") or data.get("lastPrice") or 0.0)

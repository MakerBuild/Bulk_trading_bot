"""Market data access, read off a single WS client.

Both accounts share one price view -- prices are public and identical for both,
so subscribing twice would only double the bandwidth. Market data is taken from
the master session's client.
"""

from __future__ import annotations

import logging
import time

import requests
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

    def load_specs(self) -> dict[str, MarketSpec]:
        """Fetch tick size, lot size, and min notional over HTTP.

        These are needed before the first order can be rounded correctly, so
        this is a blocking startup step rather than something driven off the
        stream.
        """
        info = self.session.http.get_exchange_info()
        markets = info if isinstance(info, list) else info.get("symbols", [])
        by_symbol = {m["symbol"]: m for m in markets if "symbol" in m}

        for symbol in self.symbols:
            if symbol not in by_symbol:
                raise RuntimeError(
                    f"{symbol} is not listed on this exchange. Available: "
                    f"{sorted(by_symbol)[:20]}"
                )
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
        age_s = 0.0
        if ticker and ticker.timestamp:
            age_s = max(0.0, time.time() - epoch_seconds(ticker.timestamp))

        # A book that stopped updating -- its delta subscription failed, which
        # is tolerated, or the stream for it went quiet -- still answers with
        # a touch, and the chaser priced maker-only orders off it: rejected as
        # crossing, or resting where the market left long ago. Age alone
        # cannot tell a frozen book from a quiet one, so it is judged against
        # the ticker: a book whose touch the mark has left by more than
        # `_FROZEN_BOOK_BPS` is not describing this market. It is dropped, and
        # the price falls back to the mark, as with no book at all.
        if book is not None and mark_price and best_bid and best_ask:
            updated = getattr(book, "last_update_time", None)
            book_age = time.time() - epoch_seconds(updated) if updated else 0.0
            band = _FROZEN_BOOK_BPS / 10_000
            outside = mark_price < best_bid * (1 - band) or mark_price > best_ask * (1 + band)
            if outside and book_age > _FROZEN_BOOK_AGE_S:
                if symbol not in self._frozen_warned:
                    self._frozen_warned.add(symbol)
                    log.warning(
                        "%s: book looks frozen (%.0fs old, touch %g/%g, mark %g) "
                        "-- pricing off the mark until it moves",
                        symbol, book_age, best_bid, best_ask, mark_price,
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

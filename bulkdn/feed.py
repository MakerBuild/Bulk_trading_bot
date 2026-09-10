"""Market data access, read off a single WS client.

Both accounts share one price view -- prices are public and identical for both,
so subscribing twice would only double the bandwidth. Market data is taken from
the master session's client.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional, Sequence

from .accounts import AccountSession
from .marketdata import MarketSpec

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Quote:
    symbol: str
    best_bid: Optional[float]
    best_ask: Optional[float]
    mark_price: Optional[float]
    age_s: float

    @property
    def usable(self) -> bool:
        return bool(self.mark_price or self.best_bid or self.best_ask)

    @property
    def reference_price(self) -> Optional[float]:
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
        self.specs: Dict[str, MarketSpec] = {}

    def load_specs(self) -> Dict[str, MarketSpec]:
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
                    exc,
                )
                continue
            try:
                await self.session.client.subscribe_orderbook_delta(symbol)
            except Exception as exc:
                # The snapshot alone still yields a usable, if staler, book.
                log.warning("could not subscribe to %s book deltas: %s", symbol, exc)

    def quote(self, symbol: str) -> Quote:
        client = self.session.client
        best_bid = best_ask = None

        book = client.get_book(symbol)
        if book is not None:
            bid_level = book.get_best_bid()
            ask_level = book.get_best_ask()
            best_bid = bid_level.price if bid_level else None
            best_ask = ask_level.price if ask_level else None

        ticker = client.get_ticker(symbol)
        mark_price = ticker.mark_price if ticker else None
        age_s = 0.0
        if ticker and ticker.timestamp:
            # Ticker timestamps are epoch milliseconds.
            age_s = max(0.0, time.time() - (ticker.timestamp / 1000.0))

        return Quote(
            symbol=symbol,
            best_bid=best_bid,
            best_ask=best_ask,
            mark_price=mark_price,
            age_s=age_s,
        )

    def reference_price(self, symbol: str) -> Optional[float]:
        return self.quote(symbol).reference_price

"""The delta-neutral hedge rule.

The strategy's invariant is that the two accounts' positions in a symbol sum to
zero. Rather than mapping each fill event to one hedge order, the hedge size is
derived from that invariant:

    net  = position[maker] + position[taker]
    if |net| >= tolerance:
        market order of -net on the taker account

Fill events are only a trigger to re-evaluate. Deriving the size from state
makes the operation idempotent: running it twice in a row is a no-op, a dropped
fill is picked up by the next trigger or by the reconciler, and crash recovery
needs no fill journal at all.

API v1.0.17 added a lossless `tradeId` to fill updates, so replayed fills can
now also be recognised directly -- `SeenTrades` uses it to keep duplicates out
of the position book. That is a useful second line of defence, but it does not
replace this rule: a trade id only helps with fills that arrive, and says
nothing about fills that are missed, arrive out of order, or happen while the
process is dead. Position-derived sizing covers all of those.

On the happy path the behaviour is exactly what the strategy calls for: a
0.10 BTC partial fill moves net to 0.10 and immediately produces a 0.10 BTC
market hedge on the other account.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from .accounts import AccountSession
from .marketdata import MarketSpec, round_notional, round_size
from .positions import PositionBook

log = logging.getLogger(__name__)


class HedgeLimitExceeded(Exception):
    """Raised when the required hedge is larger than the configured ceiling.

    This means the book disagrees with reality by more than the strategy can
    explain, so the correct response is to halt and flatten rather than fire a
    very large market order.
    """


@dataclass(frozen=True)
class LegRoles:
    """Which account rests the limit order and which one hedges it.

    The roles swap between phases, and that swap is the whole reason a single
    rule covers both entry and exit:

        OPEN  BTC: maker=master (buying long),  taker=sub1
        OPEN  SOL: maker=sub1   (buying long),  taker=master
        EXIT  BTC: maker=sub1   (closing short), taker=master
        EXIT  SOL: maker=master (closing short), taker=sub1
    """

    symbol: str
    maker: str
    taker: str
    maker_is_buy: bool
    reduce_only: bool


@dataclass
class HedgeResult:
    symbol: str
    net_before: float
    hedged_size: float
    is_buy: bool
    skipped_reason: str | None = None

    @property
    def acted(self) -> bool:
        return self.hedged_size > 0


class InFlight:
    """Hedge orders submitted but not yet seen as fills.

    Needed because a market order is not instantaneous: between submitting it
    and its fill arriving on the stream, the position book still shows the
    exposure the order was sent to neutralise. Counting the in-flight size
    against net exposure stops a second fill in that window from triggering a
    duplicate hedge.

    Entries expire. If a fill is never seen -- a dropped frame, a rejected
    order -- the reservation must not pin net exposure at a false zero forever.
    """

    def __init__(self, ttl_ms: int = 2000):
        self.ttl_s = ttl_ms / 1000.0
        self._value: dict[str, float] = {}
        self._expires: dict[str, float] = {}

    def add(self, symbol: str, delta: float) -> None:
        self._value[symbol] = self.total(symbol) + delta
        self._expires[symbol] = time.monotonic() + self.ttl_s

    def consume(self, symbol: str, delta: float) -> None:
        """Retire part of a reservation as its fill arrives.

        Only reduces toward zero, and only for fills in the same direction as
        the outstanding reservation -- a maker fill on the other side must not
        cancel out a pending hedge.
        """
        current = self.total(symbol)
        if current == 0 or delta == 0:
            return
        if current > 0 and delta > 0:
            self._value[symbol] = max(0.0, current - delta)
        elif current < 0 and delta < 0:
            self._value[symbol] = min(0.0, current - delta)

    def total(self, symbol: str) -> float:
        if time.monotonic() > self._expires.get(symbol, 0.0):
            self._value.pop(symbol, None)
            self._expires.pop(symbol, None)
            return 0.0
        return self._value.get(symbol, 0.0)

    def clear(self, symbol: str | None = None) -> None:
        if symbol is None:
            self._value.clear()
            self._expires.clear()
        else:
            self._value.pop(symbol, None)
            self._expires.pop(symbol, None)


class Hedger:
    """Restores delta neutrality for one symbol at a time."""

    def __init__(
        self,
        *,
        book: PositionBook,
        sessions: dict[str, AccountSession],
        specs: dict[str, MarketSpec],
        tolerance_lots: float = 1.0,
        max_hedge_size: dict[str, float] | None = None,
        in_flight_ttl_ms: int = 2000,
    ):
        self.book = book
        self.sessions = sessions
        self.specs = specs
        self.tolerance_lots = tolerance_lots
        self.max_hedge_size = max_hedge_size or {}
        self.in_flight = InFlight(in_flight_ttl_ms)
        # One lock per symbol. Fills arrive faster than orders round-trip, so
        # without this two triggers could both read the same non-zero net and
        # each fire a full-size hedge, overshooting into the opposite exposure.
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, symbol: str) -> asyncio.Lock:
        if symbol not in self._locks:
            self._locks[symbol] = asyncio.Lock()
        return self._locks[symbol]

    def tolerance(self, symbol: str) -> float:
        return self.specs[symbol].lot_size * self.tolerance_lots

    def effective_net(self, roles: LegRoles) -> float:
        """Net exposure including hedges that are already on their way."""
        return (
            self.book.net(roles.maker, roles.taker, roles.symbol)
            + self.in_flight.total(roles.symbol)
        )

    def note_taker_fill(self, symbol: str, signed_size: float) -> None:
        """Retire an in-flight reservation once its hedge fill lands."""
        self.in_flight.consume(symbol, signed_size)

    def actionable_hedge(self, roles: LegRoles, mark_price: float | None = None) -> float:
        """Hedge size that could actually be submitted right now.

        Returns 0 when the imbalance exists but cannot be traded -- below one
        lot, or below the market's minimum notional. Distinguishing "neutral"
        from "imbalanced but untradeable" matters: treating the latter as
        outstanding work makes the strategy wait forever for a hedge that can
        never be placed.
        """
        symbol = roles.symbol
        spec = self.specs[symbol]
        net = self.effective_net(roles)

        if abs(net) < self.tolerance(symbol):
            return 0.0
        size = round_size(abs(net), spec)
        if size < spec.lot_size:
            return 0.0
        if mark_price and mark_price > 0 and size * mark_price < spec.min_notional:
            return 0.0
        return size

    def untradeable_residual(self, roles: LegRoles, mark_price: float | None = None) -> float:
        """Signed imbalance that exists but cannot be hedged. 0 when neutral."""
        net = self.effective_net(roles)
        if abs(net) < self.tolerance(roles.symbol):
            return 0.0
        if self.actionable_hedge(roles, mark_price) > 0:
            return 0.0
        return net

    async def hedge(self, roles: LegRoles, mark_price: float | None = None) -> HedgeResult:
        """Bring `roles.symbol` back to neutral, if it has drifted.

        Safe to call redundantly -- it is a no-op when already neutral.
        """
        symbol = roles.symbol
        spec = self.specs[symbol]

        async with self._lock(symbol):
            net = self.effective_net(roles)
            tolerance = self.tolerance(symbol)

            if abs(net) < tolerance:
                return HedgeResult(symbol, net, 0.0, False, "within tolerance")

            ceiling = self.max_hedge_size.get(symbol)
            if ceiling is not None and abs(net) > ceiling:
                raise HedgeLimitExceeded(
                    f"{symbol}: required hedge {abs(net):.8f} exceeds ceiling "
                    f"{ceiling:.8f} (maker={self.book.effective(roles.maker, symbol):.8f}, "
                    f"taker={self.book.effective(roles.taker, symbol):.8f})"
                )

            # net > 0 means the pair is net long, so the taker sells.
            is_buy = net < 0
            size = round_size(abs(net), spec)

            if size < spec.lot_size:
                return HedgeResult(symbol, net, 0.0, is_buy, "below lot size")

            price = mark_price or 0.0
            if price > 0 and size * price < spec.min_notional:
                # Too small to trade. The residual is left for the reconciler,
                # which will clear it once it grows past the floor or the
                # position is closed outright.
                return HedgeResult(symbol, net, 0.0, is_buy, "below min notional")

            session = self.sessions[roles.taker]
            log.info(
                "hedge %s: net=%+.8f -> %s %.8f on %s%s",
                symbol,
                net,
                "BUY" if is_buy else "SELL",
                size,
                session.name,
                " (reduce-only)" if roles.reduce_only else "",
            )

            signed = size if is_buy else -size
            # Reserve before sending. The fill for this order may arrive before
            # `market()` returns, and the reservation has to already be there
            # for the fill handler to retire it.
            self.in_flight.add(symbol, signed)
            try:
                await session.market(symbol, is_buy, size, reduce_only=roles.reduce_only)
            except Exception:
                # The order never made it, so the exposure is still real.
                # Releasing the reservation lets the next trigger retry.
                self.in_flight.consume(symbol, signed)
                raise

            if price > 0:
                log.debug(
                    "hedged %s notional=$%s", symbol, round_notional(size * price)
                )
            return HedgeResult(symbol, net, size, is_buy)

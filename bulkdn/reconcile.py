"""Startup synchronisation, periodic reconciliation, and flattening.

Recovery is built on a simple premise: after a crash, the only trustworthy
record of what the bot owns is the exchange. State on disk says what the bot was
*trying* to do; positions and open orders say what actually happened. Startup
reads the latter over HTTP -- not the WS stream, which has not delivered a
snapshot yet -- and reconciles the difference.

Because the hedge rule derives size from position rather than from a fill
journal, recovery needs no replay: read both accounts, compute net, correct it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from .accounts import AccountSession, OrderRejected
from .feed import MarketFeed
from .hedger import Hedger, LegRoles
from .marketdata import round_size
from .positions import PositionBook

log = logging.getLogger(__name__)


async def sync_positions(sessions: Sequence[AccountSession], book: PositionBook) -> None:
    """Async wrapper around the HTTP position sync.

    The SDK's HTTP client is synchronous, so this is offloaded to a thread --
    calling it directly from the event loop would stall the WebSocket receive
    loop and delay every fill and hedge behind a network round-trip.
    """
    await asyncio.to_thread(sync_positions_http, sessions, book)


def sync_positions_http(sessions: Sequence[AccountSession], book: PositionBook) -> None:
    """Load authoritative positions for every account over HTTP.

    Called before the strategy makes any decision, so the first hedge
    evaluation sees real positions rather than an empty book.
    """
    for session in sessions:
        data = session.full_account()
        positions = data.get("positions") or []
        parsed = [
            _SimplePosition(symbol=p.get("symbol", ""), size=float(p.get("size", 0.0)))
            for p in positions
            if p.get("symbol")
        ]
        book.apply_snapshot(session.pubkey, parsed)
        if parsed:
            summary = " ".join(f"{p.symbol}={p.size:+.8f}" for p in parsed)
            log.info("%s positions: %s", session.name, summary)
        else:
            log.info("%s has no open positions", session.name)


class _SimplePosition:
    """Minimal position shape accepted by `PositionBook.apply_snapshot`."""

    __slots__ = ("symbol", "size")

    def __init__(self, symbol: str, size: float):
        self.symbol = symbol
        self.size = size


async def cancel_all_orders(
    sessions: Sequence[AccountSession], symbols: Sequence[str]
) -> None:
    """Cancel every order both accounts have working in the strategy's symbols.

    Used on startup and on halt. Orders left over from a previous run cannot be
    matched to the current plan with confidence, and an unknown resting order is
    an unhedged fill waiting to happen.
    """
    for session in sessions:
        try:
            await session.cancel_all(symbols)
            log.info("%s: cancelled all orders in %s", session.name, ", ".join(symbols))
        except OrderRejected as exc:
            # Nothing to cancel is the common case and is not an error.
            log.debug("%s: cancel-all returned %s", session.name, exc)
        except Exception as exc:
            log.warning("%s: cancel-all failed: %s", session.name, exc)


async def reconcile_net(
    hedger: Hedger,
    roles: Sequence[LegRoles],
    feed: MarketFeed,
) -> list[str]:
    """Re-run the hedge rule for each leg, correcting any drift.

    This is the same operation the fill handler performs. Running it on a timer
    catches whatever the event path missed: a dropped frame, a hedge that was
    rejected, or exposure inherited from a previous process.
    """
    corrections: list[str] = []
    for leg in roles:
        result = await hedger.hedge(leg, mark_price=feed.reference_price(leg.symbol))
        if result.acted:
            corrections.append(
                f"{leg.symbol} {'BUY' if result.is_buy else 'SELL'} {result.hedged_size:.8f} "
                f"(net was {result.net_before:+.8f})"
            )
    return corrections


async def flatten(
    sessions: dict[str, AccountSession],
    book: PositionBook,
    feed: MarketFeed,
    symbols: Sequence[str],
    max_passes: int = 3,
) -> None:
    """Market-close every strategy position on both accounts.

    Reduce-only throughout, so a stale position reading can never flip an
    account into a new position in the opposite direction. Runs a few passes
    because a close can partially fill; positions are re-read from HTTP between
    passes rather than trusted from the local book.
    """
    for attempt in range(1, max_passes + 1):
        sync_positions_http(list(sessions.values()), book)

        outstanding = []
        for session in sessions.values():
            for symbol in symbols:
                size = book.authoritative(session.pubkey, symbol)
                spec = feed.specs.get(symbol)
                if spec is None:
                    continue
                rounded = round_size(abs(size), spec)
                if rounded >= spec.lot_size:
                    outstanding.append((session, symbol, size, rounded))

        if not outstanding:
            log.info("flatten: all strategy positions are closed")
            return

        log.info(
            "flatten pass %d/%d: closing %d position(s)",
            attempt,
            max_passes,
            len(outstanding),
        )
        for session, symbol, size, rounded in outstanding:
            # A long is closed by selling, a short by buying.
            is_buy = size < 0
            try:
                await session.market(symbol, is_buy, rounded, reduce_only=True)
                log.info(
                    "flatten: %s %s %.8f on %s",
                    "BUY" if is_buy else "SELL",
                    symbol,
                    rounded,
                    session.name,
                )
            except Exception as exc:
                log.error(
                    "flatten: failed to close %s on %s: %s", symbol, session.name, exc
                )

        # Give the exchange a moment to settle before re-reading.
        await asyncio.sleep(1.0)

    sync_positions_http(list(sessions.values()), book)
    leftovers = [
        f"{session.name} {symbol}={book.authoritative(session.pubkey, symbol):+.8f}"
        for session in sessions.values()
        for symbol in symbols
        if abs(book.authoritative(session.pubkey, symbol))
        >= feed.specs[symbol].lot_size
    ]
    if leftovers:
        log.error(
            "flatten did not fully close after %d passes -- MANUAL ACTION REQUIRED: %s",
            max_passes,
            ", ".join(leftovers),
        )

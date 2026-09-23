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
import time
from collections.abc import Sequence

from .accounts import AccountSession, OrderRejected
from .feed import MarketFeed
from .hedger import Hedger, LegRoles
from .marketdata import round_size
from .positions import PositionBook
from .retry import describe

log = logging.getLogger(__name__)


async def sync_positions(sessions: Sequence[AccountSession], book: PositionBook) -> None:
    """Read every account's positions over HTTP, and apply them on the loop.

    The SDK's HTTP client is synchronous, so the READS go to a thread --
    calling them from the event loop would stall the WebSocket receive loop
    and delay every fill and hedge behind a round trip per account.

    The WRITES do not. The book is the event loop's: fills and position
    updates land in it from there, and the hedger reads it from there. This
    used to apply each account's answer from the worker thread, racing both.
    """
    reads, failures = await asyncio.to_thread(_read_all, list(sessions))
    for session, parsed, requested_at in reads:
        _apply(session, parsed, requested_at, book)
    _raise_first(failures)


def sync_positions_http(sessions: Sequence[AccountSession], book: PositionBook) -> None:
    """Load authoritative positions for every account over HTTP, blocking.

    Only for callers with no event loop to protect -- the command line
    before anything is connected, and tests. Anything running beside a live
    socket uses `sync_positions`.
    """
    reads, failures = _read_all(list(sessions))
    for session, parsed, requested_at in reads:
        _apply(session, parsed, requested_at, book)
    _raise_first(failures)


def _raise_first(failures: list[tuple[AccountSession, BaseException]]) -> None:
    """Report a partial read as the failure it is, after keeping what worked."""
    if not failures:
        return
    session, exc = failures[0]
    if len(failures) > 1:
        log.error(
            "position read failed on %d account(s): %s",
            len(failures), ", ".join(s.name for s, _ in failures),
        )
    raise exc


def _read_all(
    sessions: Sequence[AccountSession],
) -> tuple[list[tuple[AccountSession, list, float]], list[tuple[AccountSession, BaseException]]]:
    """Each account's positions, with when its request was sent, and what failed.

    One account's failure does not throw the rest away. It used to: a single
    429 on one account of a hundred discarded ninety-nine good answers, and
    the caller -- waiting on a fresh book to hedge -- waited for all of them
    again.
    """
    reads = []
    failures = []
    for session in sessions:
        requested_at = time.monotonic()
        try:
            data = session.full_account()
        except Exception as exc:  # noqa: BLE001 - reported once the rest are in
            failures.append((session, exc))
            continue
        positions = data.get("positions") or []
        parsed = [
            _SimplePosition(symbol=p.get("symbol", ""), size=float(p.get("size", 0.0)))
            for p in positions
            if p.get("symbol")
        ]
        reads.append((session, parsed, requested_at))
    return reads, failures


def _apply(session: AccountSession, parsed: list, requested_at: float, book: PositionBook) -> None:
    # `apply_read`, not `apply_snapshot`: the answer is ~320ms old by the time
    # it lands, and a fill that reached us over the stream meanwhile is newer
    # than it. Overwriting that fill is how the reconciler came to hedge the
    # same exposure twice.
    skipped = book.apply_read(session.pubkey, parsed, requested_at)
    if parsed:
        summary = " ".join(f"{p.symbol}={p.size:+.8f}" for p in parsed)
        log.info("%s positions: %s", session.name, summary)
    else:
        log.info("%s has no open positions", session.name)
    if skipped:
        log.debug(
            "%s: kept the stream's newer %s over the HTTP read",
            session.name, ", ".join(sorted(skipped)),
        )


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
            log.warning("%s: cancel-all failed: %s", session.name, describe(exc))


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
        # Off the loop: this runs inside a live strategy (a group's residual
        # sweep, the emergency stop), and a blocking read here froze every
        # other group's hedges for a round trip per account.
        await sync_positions(list(sessions.values()), book)

        outstanding = []
        for session in sessions.values():
            for symbol in symbols:
                # Effective, not authoritative: a close that filled while the
                # read above was in flight is an overlay the read could not
                # see. Sized off the read alone, the same close went out
                # again -- refused as reduce-only, and counted toward the
                # reject streak, in the middle of an emergency stop.
                size = book.effective(session.pubkey, symbol)
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
            except Exception as exc:  # noqa: BLE001 - the next close still goes
                log.error(
                    "flatten: failed to close %s on %s: %s",
                    symbol, session.name, describe(exc),
                )

        # Give the exchange a moment to settle before re-reading.
        await asyncio.sleep(1.0)

    await sync_positions(list(sessions.values()), book)
    leftovers = [
        f"{session.name} {symbol}={book.effective(session.pubkey, symbol):+.8f}"
        for session in sessions.values()
        for symbol in symbols
        if symbol in feed.specs
        and abs(book.effective(session.pubkey, symbol)) >= feed.specs[symbol].lot_size
    ]
    if leftovers:
        log.error(
            "flatten did not fully close after %d passes -- MANUAL ACTION REQUIRED: %s",
            max_passes,
            ", ".join(leftovers),
        )


async def flatten_limit(
    sessions: dict[str, AccountSession],
    book: PositionBook,
    feed: MarketFeed,
    symbols: Sequence[str],
    *,
    improve_ticks: int = 1,
    timeout_s: float = 300.0,
    interval_s: float = 1.0,
) -> bool:
    """Close every strategy position with resting limit orders. True if flat.

    The patient half of `flatten`. A market close pays the spread and the taker
    fee on every unit; a limit close pays neither, at the cost of waiting and of
    possibly not finishing at all. Which of those an operator wants is not
    something this can decide, so both are offered and this one reports honestly
    when it runs out of time.

    The order is posted at the front of the book -- `improve_ticks` inside the
    touch -- and re-priced whenever the touch moves, which is the same rule the
    chaser uses once it has committed to filling. It never crosses, so the fill
    stays on the maker side.

    Reduce-only throughout, like `flatten`: a stale position reading can then
    never flip an account into a new position facing the other way.

    Positions are re-read from HTTP each pass rather than trusted from the local
    book, and the read is offloaded to a thread -- this loops for minutes with a
    live WebSocket behind it, and blocking the loop would stop the very book
    updates the re-pricing depends on.
    """
    from .marketdata import chase_price

    deadline = asyncio.get_running_loop().time() + timeout_s
    resting: dict[tuple[str, str], str] = {}

    async def pull_all() -> None:
        for (pubkey, symbol), oid in list(resting.items()):
            session = sessions.get(pubkey)
            if session is None:
                continue
            try:
                await session.cancel(symbol, oid)
            except Exception as exc:  # noqa: BLE001 - usually already filled
                log.debug("limit close: cancel of %s failed: %s", oid[:8], describe(exc))
        resting.clear()

    try:
        while True:
            await sync_positions(list(sessions.values()), book)

            outstanding = []
            for session in sessions.values():
                for symbol in symbols:
                    spec = feed.specs.get(symbol)
                    if spec is None:
                        continue
                    size = book.effective(session.pubkey, symbol)
                    rounded = round_size(abs(size), spec)
                    if rounded >= spec.lot_size:
                        outstanding.append((session, symbol, size, rounded, spec))

            if not outstanding:
                log.info("limit close: all strategy positions are closed")
                return True

            if asyncio.get_running_loop().time() >= deadline:
                break

            for session, symbol, size, rounded, spec in outstanding:
                # A long is closed by selling, a short by buying.
                is_buy = size < 0
                quote = feed.quote(symbol)
                target = chase_price(
                    best_bid=quote.best_bid,
                    best_ask=quote.best_ask,
                    mark_price=quote.mark_price,
                    is_buy=is_buy,
                    offset_bps=0.0,
                    spec=spec,
                    improve_ticks=improve_ticks,
                )
                if target is None:
                    log.warning("limit close: no price for %s yet", symbol)
                    continue

                key = (session.pubkey, symbol)
                current = resting.get(key)
                if current is not None:
                    order = session.client.get_order_map().get(current)
                    # Already the touch counts as in place: `target` steps past
                    # the best price on our side, and when that best price is
                    # this order, chasing it walked the close across the spread
                    # one tick per pass.
                    own_touch = quote.best_bid if is_buy else quote.best_ask
                    if (
                        order is not None
                        and order.price in (target, own_touch)
                        and order.size >= rounded
                    ):
                        continue

                try:
                    oid, _ = await session.place_limit(
                        symbol=symbol,
                        is_buy=is_buy,
                        price=target,
                        size=rounded,
                        reduce_only=True,
                        cancel_oid=current,
                    )
                except Exception as exc:  # noqa: BLE001 - retried on the next pass
                    log.warning(
                        "limit close: could not place %s on %s: %s",
                        symbol, session.name, describe(exc),
                    )
                    continue

                resting[key] = oid
                log.info(
                    "limit close: %s %s %.8f @ %.8f on %s",
                    "BUY" if is_buy else "SELL", symbol, rounded, target, session.name,
                )

            await asyncio.sleep(interval_s)
    finally:
        # Reached on every exit, including Ctrl+C. An interrupted limit close
        # that left its orders resting would be the worst of both worlds:
        # reduce-only orders on the book, with nothing left watching to re-price
        # them or hedge what they fill. The menu promises Ctrl+C pulls them;
        # this is what makes that true.
        await pull_all()

    leftovers = [
        f"{session.name} {symbol}={book.effective(session.pubkey, symbol):+.8f}"
        for session in sessions.values()
        for symbol in symbols
        if symbol in feed.specs
        and abs(book.effective(session.pubkey, symbol)) >= feed.specs[symbol].lot_size
    ]
    log.warning(
        "limit close: gave up after %.0f minutes with %s still open. Orders are "
        "cancelled; close at market if you need it done now.",
        timeout_s / 60, ", ".join(leftovers) or "nothing",
    )
    return False

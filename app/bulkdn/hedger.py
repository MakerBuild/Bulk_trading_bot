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

from .accounts import AccountSession, short_pubkey
from .marketdata import MarketSpec, round_notional, round_size, touch_text
from .impact import ImpactBook
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
    # What this leg is filed under. Empty means the market, which is
    # what every leg was until an account pool made two legs on one
    # market possible. Reservations and the hedge lock hang off it:
    # keyed by market, two groups trading BTC-USD would retire each
    # other's in-flight hedges and conclude they were already neutral.
    id: str = ""
    # Every account covering this leg, and each one's share of the hedge.
    # Empty means `taker` alone covers all of it, which is every leg that is
    # not drawn from an account pool.
    #
    # Splitting is what keeps a pool unreadable even as the pairing moves: one
    # maker of $4,000 answered by one taker of $4,000 is a line anyone can
    # draw, and the same $4,000 answered by $1,800, $1,400 and $800 is not.
    takers: tuple[str, ...] = ()
    shares: tuple[float, ...] = ()
    # This cycle's offset, when it was drawn from a range. None leaves
    # the chaser on the market's own number, which is every leg that is
    # not drawn from a pool.
    offset_bps: float | None = None
    # This cycle's cap on one resting order, for the same reason. None
    # leaves the chaser on the market's shared number.
    max_order_size: float | None = None

    @property
    def key(self) -> str:
        return self.id or self.symbol

    @property
    def hedgers(self) -> tuple[str, ...]:
        """The accounts that cover this leg, in share order."""
        return self.takers or (self.taker,)

    @property
    def weights(self) -> tuple[float, ...]:
        """Each hedger's fraction of the hedge. Sums to one."""
        if self.takers and self.shares:
            return self.shares
        return (1.0,) * len(self.hedgers)

    @property
    def accounts(self) -> tuple[str, ...]:
        return (self.maker, *self.hedgers)


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

    def _expected_impact_bps(
        self, symbol: str, size: float, is_buy: bool
    ) -> float | None:
        """Slippage the published curve predicts for this hedge, if any.

        Returns None when no curve has been published for the market, which is
        the case for every mainnet market at the time of writing. A guard with
        no data does not fire.
        """
        if self.impact is None:
            return None
        return self.impact.bps_for(symbol, size, is_buy)

    def __init__(
        self,
        *,
        book: PositionBook,
        sessions: dict[str, AccountSession],
        specs: dict[str, MarketSpec],
        tolerance_lots: float = 1.0,
        max_hedge_size: dict[str, float] | None = None,
        in_flight_ttl_ms: int = 2000,
        impact: ImpactBook | None = None,
        max_impact_bps: float = 0.0,
        feed=None,
    ):
        self.book = book
        self.sessions = sessions
        # Only ever read for the touch written into the hedge's log line, which
        # is why it is optional: a Hedger built without one still hedges, it
        # just cannot say what the book looked like beforehand.
        self.feed = feed
        self.impact = impact
        self.max_impact_bps = max_impact_bps
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
        """Net exposure including hedges that are already on their way.

        Summed over every account in the leg, not just two. A hedge split
        three ways is still one position that has to add up to zero, and
        reading only the first hedger would report the other two's coverage as
        missing -- and hedge it again.
        """
        total = self.book.effective(roles.maker, roles.symbol)
        for pubkey in roles.hedgers:
            total += self.book.effective(pubkey, roles.symbol)
        return total + self.in_flight.total(roles.key)

    def note_taker_fill(self, key: str, signed_size: float) -> None:
        """Retire an in-flight reservation once its hedge fill lands."""
        self.in_flight.consume(key, signed_size)

    def _floor(self, spec, price: float | None) -> float:
        """The smallest order this market accepts, in base units.

        Two floors, and the notional one is usually the binding one. BTC-USD
        admits a lot of 0.000001 and a notional of $1; ETH-USD admits a lot of
        0.0001 -- about forty cents -- and a notional of $50. Measuring a
        slice against the lot alone therefore passes pieces the exchange
        refuses, and it refuses them one order at a time until the reject
        streak halts the run.

        Without a price the notional floor cannot be expressed in base units,
        so the lot stands alone. That is the old behaviour, and it is only
        reached when the feed has no reference price at all.
        """
        if not price or price <= 0 or spec.min_notional <= 0:
            return spec.lot_size
        return max(spec.lot_size, round_size(spec.min_notional / price, spec))

    def _destination(self, slices: list[tuple[str, float]]) -> str:
        """Where a hedge is going, named so the fills under it can be matched.

        One slice reads as it always did. Several name each account and its
        own size, because that is what the next three log lines will say and
        a reader should not have to add them up to believe the first one.
        """
        if len(slices) == 1:
            return f"on {self._name(slices[0][0])}"
        return "across " + ", ".join(
            f"{self._name(pubkey)} {piece:.8f}" for pubkey, piece in slices
        )

    def _name(self, pubkey: str) -> str:
        """An account's short name for a message, or its pubkey."""
        session = self.sessions.get(pubkey)
        return session.name if session is not None else short_pubkey(pubkey)

    def _slice(
        self, roles: LegRoles, size: float, spec, price: float | None = None
    ) -> list[tuple[str, float]]:
        """Split one hedge across the leg's hedgers, by their shares.

        Rounding is settled by giving the remainder to the last slice, so the
        pieces add up to exactly the hedge rather than to a lot less or a lot
        more -- an under-hedge leaves the group directional by the difference,
        and there is nothing to notice it until the reconciler runs.

        A slice below what the market accepts is dropped and its weight handed
        on: an order the exchange will not take is not a smaller hedge, it is
        a missing one. Asking for more pieces than the size can carry
        therefore yields fewer pieces, not rejected ones -- which is what the
        settings file has always promised `max_takers` does.

        With one hedger, or when the split cannot survive that floor, this
        returns the whole hedge to the first account. The hedge itself was
        already checked against the same floor before we got here, so the
        undivided order is one the exchange will take.
        """
        hedgers, weights = roles.hedgers, roles.weights
        if len(hedgers) == 1:
            return [(hedgers[0], size)]

        floor = self._floor(spec, price)
        pieces: list[tuple[str, float]] = []
        placed = 0.0
        for pubkey, weight in zip(hedgers[:-1], weights[:-1], strict=False):
            piece = round_size(size * weight, spec)
            if piece < floor:
                continue
            pieces.append((pubkey, piece))
            placed += piece

        remainder = round_size(size - placed, spec)
        if remainder >= floor:
            pieces.append((hedgers[-1], remainder))
        elif pieces:
            # Too small to send on its own: fold it into the last real slice
            # rather than leaving the hedge short by it.
            pubkey, piece = pieces[-1]
            pieces[-1] = (pubkey, round_size(piece + remainder, spec))
        else:
            return [(hedgers[0], size)]
        return pieces

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
        # Deliberately not a shared "is this tradeable" helper. With no price
        # the hedge still goes: refusing to correct a known imbalance because a
        # ticker is missing leaves the pair directional, which is the worse of
        # the two failures. A helper that folded the two checks together would
        # have to pick one answer for both callers.
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

        async with self._lock(roles.key):
            net = self.effective_net(roles)
            tolerance = self.tolerance(symbol)

            if abs(net) < tolerance:
                return HedgeResult(symbol, net, 0.0, False, "within tolerance")

            ceiling = self.max_hedge_size.get(symbol)
            if ceiling is not None and abs(net) > ceiling:
                # Every account in the leg, because this message is the one an
                # operator reads at a halt and it has to add up to `net`. It
                # used to print `roles.taker`, the FIRST hedger, so a leg with
                # three of them reported a maker and a taker whose sum was not
                # the number in the same sentence.
                held = " ".join(
                    f"{self._name(pubkey)}={self.book.effective(pubkey, symbol):+.8f}"
                    for pubkey in roles.accounts
                )
                raise HedgeLimitExceeded(
                    f"{symbol}: required hedge {abs(net):.8f} exceeds ceiling "
                    f"{ceiling:.8f} ({held})"
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

            expected_bps = self._expected_impact_bps(symbol, size, is_buy)
            if (
                self.max_impact_bps > 0
                and expected_bps is not None
                and expected_bps > self.max_impact_bps
            ):
                # A market this thin is a reason to stop, not to trade through:
                # the hedge cannot be shrunk without leaving the pair
                # directional, so the choice is to pay the slippage or halt.
                raise HedgeLimitExceeded(
                    f"{symbol}: hedging {size:.8f} would cost about "
                    f"{expected_bps:.1f} bps, over the "
                    f"{self.max_impact_bps:.1f} bps ceiling"
                )

            slices = self._slice(roles, size, spec, price)
            # The book as it stands BEFORE the order goes out. Read here and
            # not from the fill that comes back, because by then this order has
            # eaten the depth it is about to eat: a touch taken afterwards is
            # the result, not the reference it has to be compared against. The
            # difference between this ask and the price the fill returns is the
            # slippage, and nothing else in the system records it -- the
            # exchange publishes no impact curve for these markets (404 on
            # every one), so it cannot be predicted either.
            touch = touch_text(self.feed, symbol) if self.feed is not None else ""
            log.info(
                "hedge %s: net=%+.8f -> %s %.8f %s%s%s",
                symbol,
                net,
                "BUY" if is_buy else "SELL",
                size,
                # Every account it is going to, with its share. This used to
                # name `slices[0]` alone, so a hedge split three ways was
                # announced as one order on one account and the line under it
                # showed three fills the reader had to reconcile by hand.
                self._destination(slices),
                " (reduce-only)" if roles.reduce_only else "",
                f" {touch}" if touch else "",
            )

            # Every slice at once. They go to different accounts, so nothing
            # orders them, and sending them in turn made each wait out the one
            # before: on a live run the second slice landed ~350ms after the
            # first, every time, while the price it was chasing moved on.
            #
            # Reserve before sending. The fill for an order may arrive before
            # `market()` returns, and the reservation has to already be there
            # for the fill handler to retire it.
            for _pubkey, piece in slices:
                self.in_flight.add(roles.key, piece if is_buy else -piece)
            results = await asyncio.gather(
                *(
                    self.sessions[pubkey].market(
                        symbol, is_buy, piece, reduce_only=roles.reduce_only
                    )
                    for pubkey, piece in slices
                ),
                return_exceptions=True,
            )
            sent = 0.0
            failures: list[BaseException] = []
            for (_pubkey, piece), result in zip(slices, results, strict=True):
                if isinstance(result, BaseException):
                    # This slice never made it, so its exposure is still real.
                    # Releasing only its reservation lets the next trigger
                    # retry exactly the part that failed -- the slices that did
                    # go out are covered and must not be hedged twice.
                    self.in_flight.consume(roles.key, piece if is_buy else -piece)
                    failures.append(result)
                else:
                    sent += piece
            if failures:
                if sent > 0:
                    # Some of it is covered. The reconciler re-derives the
                    # remainder from positions, so the partial cover is not
                    # lost; raising here still reports the failure.
                    log.warning(
                        "hedge %s: %d of %d slices sent, %d failed",
                        symbol, len(slices) - len(failures), len(slices),
                        len(failures),
                    )
                raise failures[0]
            size = sent

            if price > 0:
                log.debug(
                    "hedged %s notional=$%s%s",
                    symbol,
                    round_notional(size * price),
                    f" impact~{expected_bps:.1f}bps" if expected_bps is not None else "",
                )
            return HedgeResult(symbol, net, size, is_buy)

"""Limit order chasing.

BULK does not allow the price of a resting order to be modified, so following
the market means cancelling and re-placing. Both actions go into a single
transaction, which the exchange executes atomically -- there is no window where
the leg has no order working.

An order is only replaced once it has drifted further than `max_distance_bps`
from its target. Replacing on every tick would burn through nonces, invite rate
limiting, and lose queue position for no benefit.

That rule alone leaves one gap: if the market does not move, the order does not
drift, so an order failing to fill is held exactly where it is failing. So a
second trigger watches the clock -- after `chase_patience_s` unfilled, the order
gives up its offset and moves onto the touch. It still never crosses, so the
fill stays on the maker side; what is spent is queue position, not fees.

From then on the leg is in a different mode, and a much more aggressive one.

  * It does not join the touch, it beats it. `improve_ticks` posts the order
    one tick INTO the spread, which makes it the best bid or the best ask
    outright. Joining the touch means queueing behind everyone already at that
    price and filling after all of them; one tick better is alone at the front
    and fills first. Still passive -- inside the spread, never across it -- so
    this buys priority with a tick of price, not with a taker fee.

  * It follows tick by tick, not within a tolerance. Any price other than the
    computed target means the touch has moved, so the order is replaced. A bps
    tolerance was the wrong unit for "am I still at the front": at 8bps an ETH
    order sat $1.08 below the best bid, ten levels deep, with the drift rule
    calling that fine.

This costs a transaction whenever the book moves, which is the trade being
made. It is bounded by `chase_interval_s`, and when the touch has not moved the
target rounds to the same tick and nothing is sent.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .accounts import AccountSession, OrderRejected
from .feed import MarketFeed
from .hedger import LegRoles
from .marketdata import chase_price, distance_bps, round_size
from .retry import describe
from .positions import PositionBook
from .state import LegState

log = logging.getLogger(__name__)

# How long to wait for a placement to show up in the order map before treating
# the order as gone. Covers the gap between the transaction being accepted and
# the orderUpdate arriving.
ACK_GRACE_S = 3.0


@dataclass
class ChaseParams:
    offset_bps: float
    max_distance_bps: float
    max_order_size: float
    chase_patience_s: float = 0.0
    improve_ticks: int = 1


def effective_offset_bps(
    base_bps: float, resting_for_s: float, patience_s: float, tightened: bool
) -> float:
    """How far inside the touch to rest, given how long we have been waiting.

    Sitting `offset_bps` inside the touch is what makes the fill a maker fill,
    but it also means waiting for the market to come to us -- observed taking
    anywhere from 17 seconds to over two and a half minutes on a live cycle.
    Once an order has rested longer than `patience_s`, it gives up the offset
    and moves onto the touch: the best price that is still passive.

    It never crosses. The order joins the bid rather than lifting the ask, so
    the fill is still on the maker side and the rebate is unaffected.

    Sticky once tightened, for the rest of the leg. Letting it spring back to
    the full offset would walk the order away from the market again, and it
    would only have to walk back after the next wait.
    """
    if tightened:
        return 0.0
    if patience_s <= 0 or resting_for_s < patience_s:
        return base_bps
    return 0.0


@dataclass
class ChaseOutcome:
    symbol: str
    action: str  # placed | replaced | held | complete | waiting | skipped
    detail: str = ""
    price: float | None = None
    size: float | None = None


class Chaser:
    """Keeps one leg's resting limit order near the market."""

    def __init__(
        self,
        *,
        sessions: dict[str, AccountSession],
        feed: MarketFeed,
        book: PositionBook,
        params: dict[str, ChaseParams],
        price_stale_timeout_s: float = 15.0,
    ):
        self.sessions = sessions
        self.feed = feed
        self.book = book
        self.params = params
        self.price_stale_timeout_s = price_stale_timeout_s
        self._placed_at: dict[str, float] = {}
        # Orders the order map has shown at least once. Once seen, an order's
        # absence means it is gone -- filled or cancelled -- rather than not
        # yet acknowledged. See `may_be_resting`.
        self._seen: set[str] = set()
        # When each leg, in its current direction, first put an order on the
        # book. Patience is measured from here, not from the current order:
        # every drift replace reset the old clock, so in a market that kept
        # moving away the order was re-placed at its offset forever and never
        # moved onto the touch -- the opposite of "unfilled for this long".
        self._chasing_since: dict[tuple[str, bool], float] = {}
        # Orders kept after a replace was refused. See `_place`.
        self._adopted: set[str] = set()

    # -- sizing ------------------------------------------------------------

    def remaining_size(self, roles: LegRoles, leg: LegState) -> float:
        """How much of this leg is still to be executed.

        Entry accumulates toward a target, so what remains is the shortfall.
        Exit unwinds toward zero, so what remains is whatever position is left.
        Both are read from the position book rather than from a running total,
        which is what makes this correct after a restart.
        """
        position = abs(self.book.effective(roles.maker, roles.symbol))
        if roles.reduce_only:
            return position
        return max(0.0, leg.target_size - position)

    # -- main step ---------------------------------------------------------

    async def step(self, roles: LegRoles, leg: LegState) -> ChaseOutcome:
        """Evaluate one leg once and place, replace, or hold accordingly."""
        symbol = roles.symbol
        spec = self.feed.specs[symbol]
        params = self.params[symbol]
        session = self.sessions[roles.maker]

        await self._sweep_stale(session, leg)

        remaining = round_size(self.remaining_size(roles, leg), spec)
        quote = self.feed.quote(symbol)
        reference = quote.reference_price

        # Leg finished: nothing left worth trading. Any resting remainder is
        # pulled so it can't fill after the leg is considered done.
        if remaining < spec.lot_size or (
            reference and remaining * reference < spec.min_notional
        ):
            if leg.oid:
                await self._cancel(session, leg, symbol)
            leg.complete = True
            leg.tightened = False
            self._chasing_since.pop(self._chase_key(roles), None)
            return ChaseOutcome(symbol, "complete", f"remaining={remaining:.8f}")

        if quote.age_s > self.price_stale_timeout_s:
            return ChaseOutcome(
                symbol, "skipped", f"stale price ({quote.age_s:.1f}s old)"
            )

        since = self._chasing_since.get(self._chase_key(roles))
        resting_for = time.monotonic() - since if leg.oid and since else 0.0
        was_tightened = leg.tightened
        # The cycle's own offset when one was drawn, the market's otherwise.
        # `params` is built once per symbol, so every group on that market
        # shares it -- and two groups resting off one offset compute one price
        # from one book and sit at the same tick, in the same queue.
        base_offset = (
            params.offset_bps if roles.offset_bps is None else roles.offset_bps
        )
        offset = effective_offset_bps(
            base_offset, resting_for, params.chase_patience_s, was_tightened
        )
        tightening_now = offset < base_offset and not was_tightened

        target = chase_price(
            best_bid=quote.best_bid,
            best_ask=quote.best_ask,
            mark_price=quote.mark_price,
            is_buy=roles.maker_is_buy,
            offset_bps=offset,
            spec=spec,
            improve_ticks=params.improve_ticks,
        )
        if target is None:
            return ChaseOutcome(symbol, "skipped", "no reference price")

        cap = (
            params.max_order_size if roles.max_order_size is None
            else roles.max_order_size
        )
        desired = round_size(min(remaining, cap), spec)
        if desired < spec.lot_size:
            return ChaseOutcome(symbol, "skipped", "desired size below lot")

        resting = self._resting_order(session, leg)

        if resting is None:
            if leg.oid and time.monotonic() - self._placed_at.get(leg.oid, 0.0) < ACK_GRACE_S:
                return ChaseOutcome(symbol, "waiting", "awaiting order ack")
            if leg.oid:
                # Gone from the book: filled, or cancelled behind our back.
                # Either way the next pass re-derives from position.
                log.debug("%s: order %s no longer resting", symbol, leg.oid[:8])
                leg.oid = None
            return await self._place(session, roles, leg, target, desired, replace_oid=None)

        drift = distance_bps(resting.price, target)
        undersized = resting.size < desired - spec.lot_size
        # More on the book than the leg still needs -- an order adopted after a
        # replace whose cancel half failed because the old one had filled, or
        # a position that moved some other way. Left alone it fills the leg
        # past its size; nothing else caps what a leg can put on.
        #
        # Only for an adopted order. For any other, a fill can reach the book
        # before the order map shrinks the order, and the check would fire on
        # every such fill.
        oversized = (
            leg.oid in self._adopted and resting.size > remaining + spec.lot_size / 2
        )
        if oversized:
            return await self._place(
                session, roles, leg, target, desired, replace_oid=leg.oid,
                reason=f"resting {resting.size:.8f} > remaining {remaining:.8f}",
            )
        # Our own order is the touch. `chase_price` steps `improve_ticks` past
        # the best price on our side, and that best price IS this order, so
        # following it stepped past ourselves: one replace per tick, walking
        # the order across the spread and giving it away. Being the touch is
        # the goal; the price only has to move when someone else is ahead.
        own_touch = quote.best_bid if roles.maker_is_buy else quote.best_ask
        at_the_touch = own_touch is not None and resting.price == own_touch

        if tightening_now:
            # Below max_distance_bps, so the drift rule would have held this
            # order where it was. That is the whole point: the market never
            # came to it, so it goes to the market instead.
            leg.tightened = True
            return await self._place(
                session, roles, leg, target, desired, replace_oid=leg.oid,
                reason=(
                    f"unfilled for {resting_for:.0f}s -- moving onto the touch "
                    f"from {base_offset:g}bps inside"
                ),
            )

        # A tightened leg is meant to be AT the front of the book, so the test
        # is not a tolerance but an identity: any price other than the target
        # means the touch moved and this order is no longer where it was put.
        # No threshold is right here -- bps is the wrong unit for "am I still
        # the best bid". At $100k a 1bps tolerance is twenty ticks, which is
        # twenty price levels of other people's orders ahead of this one, and
        # that is what a live ETH leg sat behind for two and a half minutes.
        #
        # Self-limiting rather than a replace storm: when the touch has not
        # moved the target rounds to the same tick and nothing is sent, so a
        # transaction costs only when the book actually changed.
        if was_tightened:
            if resting.price != target and not at_the_touch:
                return await self._place(
                    session, roles, leg, target, desired, replace_oid=leg.oid,
                    reason=f"following the touch {resting.price:g} -> {target:g}",
                )
        elif drift > params.max_distance_bps:
            # Still resting deliberately away from the market, where a bps
            # tolerance is the right unit: re-pricing on every tick would burn
            # nonces to keep a distance that was chosen loosely in the first
            # place.
            return await self._place(
                session, roles, leg, target, desired, replace_oid=leg.oid,
                reason=f"drift {drift:.1f}bps > {params.max_distance_bps:.1f}bps",
            )
        if undersized:
            # The cap or a target change left the book short of what the leg
            # still needs; top it up by replacing at the same target.
            return await self._place(
                session, roles, leg, target, desired, replace_oid=leg.oid,
                reason=f"resting {resting.size:.8f} < desired {desired:.8f}",
            )

        return ChaseOutcome(
            symbol, "held", f"drift {drift:.1f}bps", resting.price, resting.size
        )

    # -- helpers -----------------------------------------------------------

    def may_be_resting(self, session: AccountSession, oid: str | None) -> bool:
        """Whether this order could still be on the book.

        Yes if the order map has it. No if the map has shown it before and no
        longer does: it was filled or cancelled, and filled is the usual reason
        a hedge is running. The acknowledgement grace is only for an order the
        map has never shown -- one placed so recently it may already be resting
        with nothing here to say so.

        It used to apply the grace by age alone, so any order younger than
        ACK_GRACE_S counted as possibly resting even after it had shown up and
        gone. On the touch an order is re-placed about every second, and 49%
        of maker fills on a live run landed inside that window: each one still
        sent a cancel for an order that no longer existed, and the hedge waited
        a second round trip behind it -- the median went from one (~330ms) to
        two (~650ms).
        """
        if not oid:
            return False
        if oid in session.client.get_order_map():
            self._seen.add(oid)
            return True
        if oid in self._seen:
            return False
        return time.monotonic() - self._placed_at.get(oid, 0.0) < ACK_GRACE_S

    def _resting_order(self, session: AccountSession, leg: LegState):
        if not leg.oid:
            return None
        order = session.client.get_order_map().get(leg.oid)
        if order is not None:
            # Every chase step passes through here, so an order that rests
            # for a step is recorded as acknowledged well before it fills.
            self._seen.add(leg.oid)
        return order

    async def _place(
        self,
        session: AccountSession,
        roles: LegRoles,
        leg: LegState,
        price: float,
        size: float,
        replace_oid: str | None,
        reason: str = "",
    ) -> ChaseOutcome:
        symbol = roles.symbol
        try:
            oid, _ = await session.place_limit(
                symbol=symbol,
                is_buy=roles.maker_is_buy,
                price=price,
                size=size,
                reduce_only=roles.reduce_only,
                cancel_oid=replace_oid,
            )
        except OrderRejected as exc:
            # A rejected replace can leave the original resting, so it stays on
            # the sweep list until it is confirmed gone.
            #
            # With ALO orders the common rejection is `rejectedCrossing`: the
            # book reached the target between reading it and the order landing.
            # The cancel in the same transaction may still have applied, so the
            # leg can be left with nothing resting -- the next pass sees no
            # order and places fresh, which bounds the gap at one chase
            # interval. `mod` would avoid it by changing size in place, but the
            # Python SDK has no message type for that action.
            if replace_oid:
                leg.remember_stale(replace_oid)
            placed_oid = getattr(exc, "order_id", None)
            if getattr(exc, "placed", False) and placed_oid:
                # The batch was refused as a whole because its CANCEL half
                # failed -- typically the old order had just filled -- but the
                # new order was accepted and is resting. Dropping its id left
                # an order nothing tracked: the next pass placed another, and
                # the leg filled past its size. Adopted instead; the next pass
                # resizes it if the fill left it too big.
                log.warning(
                    "%s: replace refused but the new order rests (%s) -- keeping it",
                    symbol, describe(exc),
                )
                self._track(roles, leg, placed_oid, price, size)
                self._adopted.add(placed_oid)
                return ChaseOutcome(symbol, "placed", "adopted after a refused cancel")
            log.warning("%s: chase order rejected: %s", symbol, describe(exc))
            return ChaseOutcome(symbol, "skipped", f"rejected: {exc}")

        if replace_oid:
            # The cancel and the placement were atomic, but the cancel can still
            # fail on its own terms (already filled, unknown id). Verifying on a
            # later pass is cheaper than trusting it.
            leg.remember_stale(replace_oid)

        self._track(roles, leg, oid, price, size)

        action = "replaced" if replace_oid else "placed"
        log.info(
            "%s %s %s %.8f @ %.8f on %s%s%s",
            symbol,
            action,
            "BUY" if roles.maker_is_buy else "SELL",
            size,
            price,
            session.name,
            " (reduce-only)" if roles.reduce_only else "",
            f" [{reason}]" if reason else "",
        )
        return ChaseOutcome(symbol, action, reason, price, size)

    @staticmethod
    def _chase_key(roles: LegRoles) -> tuple[str, bool]:
        # The direction is part of it: the same leg chases one way to open and
        # the other to close, and patience spent opening says nothing about
        # how long the close has waited.
        return (roles.key, roles.reduce_only)

    def _track(
        self, roles: LegRoles, leg: LegState, oid: str, price: float, size: float
    ) -> None:
        """Record `oid` as the leg's resting order."""
        previous = leg.oid
        if previous and previous != oid:
            # Replaced. The stale list sweeps it if it is somehow still there;
            # these two only ever grew, by one entry per replace for the life
            # of the process.
            self._placed_at.pop(previous, None)
            self._seen.discard(previous)
            self._adopted.discard(previous)
        leg.oid = oid
        leg.price = price
        leg.size = size
        now = time.monotonic()
        self._placed_at[oid] = now
        self._chasing_since.setdefault(self._chase_key(roles), now)

    async def _cancel(self, session: AccountSession, leg: LegState, symbol: str) -> None:
        oid = leg.oid
        if not oid:
            return
        try:
            await session.cancel(symbol, oid)
            log.info("%s: cancelled %s on %s", symbol, oid[:8], session.name)
        except OrderRejected as exc:
            # Usually means it already filled or was already cancelled.
            log.debug("%s: cancel of %s not accepted: %s", symbol, oid[:8], exc)
        except Exception as exc:
            log.warning("%s: cancel of %s failed: %s", symbol, oid[:8], describe(exc))
            leg.remember_stale(oid)
        leg.oid = None
        leg.price = None
        leg.size = None
        self._placed_at.pop(oid, None)
        self._seen.discard(oid)

    async def _sweep_stale(self, session: AccountSession, leg: LegState) -> None:
        """Cancel any superseded order that is somehow still on the book.

        Guards the case where the cancel half of a cancel+replace did not take
        effect, which would otherwise leave two orders working on the same leg
        and double the intended exposure.
        """
        if not leg.stale_oids:
            return
        order_map = session.client.get_order_map()
        still_resting = [oid for oid in leg.stale_oids if oid in order_map]

        for oid in still_resting:
            log.warning(
                "%s: superseded order %s is still resting -- cancelling",
                leg.symbol,
                oid[:8],
            )
            try:
                await session.cancel(leg.symbol, oid)
            except Exception as exc:
                log.warning("%s: sweep cancel of %s failed: %s", leg.symbol, oid[:8], describe(exc))

        # Anything no longer on the book needs no further attention.
        leg.stale_oids = [oid for oid in leg.stale_oids if oid in order_map]

    async def cancel_leg(self, roles: LegRoles, leg: LegState) -> None:
        """Public cancel, used on phase transitions and shutdown."""
        await self._cancel(self.sessions[roles.maker], leg, roles.symbol)
        # The next phase gets its own patience: entry and exit are different
        # sides of the book and one having been slow says nothing about the
        # other.
        leg.tightened = False

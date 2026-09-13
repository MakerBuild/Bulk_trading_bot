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
        # Legs that waited out their patience and now rest on the touch. By
        # symbol, not by order id: the decision belongs to the leg, and a
        # replacement should not start the wait over.
        self._tightened: set[str] = set()

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
            self._tightened.discard(symbol)
            return ChaseOutcome(symbol, "complete", f"remaining={remaining:.8f}")

        if quote.age_s > self.price_stale_timeout_s:
            return ChaseOutcome(
                symbol, "skipped", f"stale price ({quote.age_s:.1f}s old)"
            )

        resting_for = (
            time.monotonic() - self._placed_at[leg.oid]
            if leg.oid and leg.oid in self._placed_at
            else 0.0
        )
        was_tightened = symbol in self._tightened
        offset = effective_offset_bps(
            params.offset_bps, resting_for, params.chase_patience_s, was_tightened
        )
        tightening_now = offset < params.offset_bps and not was_tightened

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

        desired = round_size(min(remaining, params.max_order_size), spec)
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

        if tightening_now:
            # Below max_distance_bps, so the drift rule would have held this
            # order where it was. That is the whole point: the market never
            # came to it, so it goes to the market instead.
            self._tightened.add(symbol)
            return await self._place(
                session, roles, leg, target, desired, replace_oid=leg.oid,
                reason=(
                    f"unfilled for {resting_for:.0f}s -- moving onto the touch "
                    f"from {params.offset_bps:g}bps inside"
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
            if resting.price != target:
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

    def _resting_order(self, session: AccountSession, leg: LegState):
        if not leg.oid:
            return None
        return session.client.get_order_map().get(leg.oid)

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
            log.warning("%s: chase order rejected: %s", symbol, exc)
            return ChaseOutcome(symbol, "skipped", f"rejected: {exc}")

        if replace_oid:
            # The cancel and the placement were atomic, but the cancel can still
            # fail on its own terms (already filled, unknown id). Verifying on a
            # later pass is cheaper than trusting it.
            leg.remember_stale(replace_oid)

        leg.oid = oid
        leg.price = price
        leg.size = size
        self._placed_at[oid] = time.monotonic()

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
            log.warning("%s: cancel of %s failed: %s", symbol, oid[:8], exc)
            leg.remember_stale(oid)
        leg.oid = None
        leg.price = None
        leg.size = None
        self._placed_at.pop(oid, None)

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
                log.warning("%s: sweep cancel of %s failed: %s", leg.symbol, oid[:8], exc)

        # Anything no longer on the book needs no further attention.
        leg.stale_oids = [oid for oid in leg.stale_oids if oid in order_map]

    async def cancel_leg(self, roles: LegRoles, leg: LegState) -> None:
        """Public cancel, used on phase transitions and shutdown."""
        await self._cancel(self.sessions[roles.maker], leg, roles.symbol)
        # The next phase gets its own patience: entry and exit are different
        # sides of the book and one having been slow says nothing about the
        # other.
        self._tightened.discard(roles.symbol)

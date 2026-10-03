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
from .marketdata import (
    chase_price, distance_bps, is_tradeable, min_order_size, round_size,
)
from .retry import describe
from .positions import PositionBook
from .state import LegState

log = logging.getLogger(__name__)

# How long to wait for a placement to show up in the order map before acting
# on it without that confirmation. Covers the usual gap between the
# transaction being accepted and the orderUpdate arriving.
ACK_GRACE_S = 3.0
# How long an order the order map has never shown is still treated as one
# that may be resting. The map is filled from orderUpdate alone, which has run
# seven to thirty seconds behind the answer that accepted the order, so the
# grace above is not long enough to conclude anything from absence. Past this
# an order nobody is chasing any more stops being watched: nothing still on
# its way is that late.
UNSEEN_WATCH_S = 60.0

# `join_depth_usd`: how far behind the touch a level may be and still be
# joined. Further back is the deliberate resting offset under another name,
# and resting 1bps back measured +27% on the non-fee cost -- so past this the
# order joins the touch, whatever rests there.
JOIN_MAX_BPS = 1.0
# How many levels to look through for one deep enough.
JOIN_SCAN_LEVELS = 20
# A level we already rest at is kept while others there hold this share of
# `join_depth_usd`. Without it an order would hop every time the queue around
# it crossed the line, and each hop goes to the back of the new queue.
JOIN_KEEP_FRACTION = 0.5


@dataclass
class _Placed:
    """What the chaser knows about one order it placed."""

    symbol: str
    is_buy: bool
    maker: str
    placed_at: float
    # Shown by the order map at least once. Once seen, an order's absence
    # means it is gone -- filled or cancelled. Never seen, absence means
    # nothing yet: the orderUpdate may simply not have arrived.
    seen: bool = False
    # Kept after a replace was refused. See `_place`.
    adopted: bool = False
    # No longer any leg's order: replaced, or a cancel of it went unanswered.
    # Still watched until it is known gone, but it holds no level for
    # `_ours_at` -- its own leg has already moved on.
    superseded: bool = False


@dataclass
class ChaseParams:
    offset_bps: float
    max_distance_bps: float
    max_order_size: float
    chase_patience_s: float = 0.0
    improve_ticks: int = 1
    join_depth_usd: float = 0.0


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
        # Every order this chaser has placed that may still rest, across all
        # legs. One record per order, rather than four dicts kept in step by
        # hand at every place an order came or went. One Chaser serves every
        # group, so this is also how a leg tells another group's order at the
        # touch from a stranger's. See `_ours_at`. Pruned on every step by
        # `_forget_gone`.
        self._orders: dict[str, _Placed] = {}
        # When each leg, in its current direction, first put an order on the
        # book. Patience is measured from here, not from the current order:
        # every drift replace reset the old clock, so in a market that kept
        # moving away the order was re-placed at its offset forever and never
        # moved onto the touch -- the opposite of "unfilled for this long".
        #
        # Keyed by leg and direction, not by order, so it is not part of the
        # per-order record above: it outlives every order the leg places.
        self._chasing_since: dict[tuple[str, bool], float] = {}

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

        self._forget_gone()
        await self._sweep_stale(session, leg)

        remaining = round_size(self.remaining_size(roles, leg), spec)
        if remaining < spec.lot_size:
            return await self._complete(session, roles, leg, remaining)

        quote = self.feed.quote(symbol)
        if quote.age_s > self.price_stale_timeout_s:
            return ChaseOutcome(
                symbol, "skipped",
                "no price heard yet" if quote.age_s == float("inf")
                else f"stale price ({quote.age_s:.1f}s old)",
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

        joining = params.join_depth_usd > 0 and offset <= 0
        if joining:
            joined = self._join_target(roles, leg, quote, params.join_depth_usd)
            if joined is not None:
                target = joined

        # The best price on our side may be another of our own groups. Stepping
        # past it -- which `chase_price` does to any best price -- starts a
        # walk: that group sees a stranger ahead, steps past this one, and the
        # two climb toward the far side a tick at a time, each step a replace
        # and a tick given away. Joining the level ends it; only a stranger's
        # price is worth stepping past.
        own_touch = quote.best_bid if roles.maker_is_buy else quote.best_ask
        if own_touch is not None and self._ours_at(
            symbol, roles.maker_is_buy, own_touch, except_oid=leg.oid
        ):
            beyond = target > own_touch if roles.maker_is_buy else target < own_touch
            if beyond:
                target = own_touch

        # Also finished when what is left is under the market's minimum at the
        # price the order goes out at. It used to be measured at the mark: a
        # buy rests below it, so a remainder that cleared the minimum there
        # went out under it at the bid, was refused, and was sent again on
        # every pass until the reject streak halted the run.
        if not is_tradeable(spec, remaining, target):
            return await self._complete(session, roles, leg, remaining)

        cap = (
            params.max_order_size if roles.max_order_size is None
            else roles.max_order_size
        )
        # The cap was floored at the market minimum when it was drawn, at
        # that moment's price. A cap sitting one lot above $50 is under it
        # after a fraction of a percent of price move, and every order sized
        # at it is refused -- five in a row is the kill switch. So it is
        # floored again here, at the price this order goes out at.
        cap = max(cap, min_order_size(spec, target))
        desired = round_size(min(remaining, cap), spec)

        resting = self._resting_order(session, leg)

        if resting is None:
            record = self._orders.get(leg.oid) if leg.oid else None
            if record is not None and not record.seen:
                # Accepted, and never shown by the order map: unknown, not
                # gone. orderUpdate has run up to thirty seconds behind, and
                # this used to forget the id three seconds in and place a
                # second order beside the first -- both resting, both free to
                # fill, the leg past its size.
                age = time.monotonic() - record.placed_at
                if age < ACK_GRACE_S:
                    return ChaseOutcome(symbol, "waiting", "awaiting order ack")
                # So it is replaced like any resting order: the cancel rides
                # in the same transaction, and whichever it turns out to be --
                # resting or already filled -- there is never a second order.
                # A cancel refused because it had filled is the adopted case
                # in `_place`.
                return await self._place(
                    session, roles, leg, target, desired, replace_oid=leg.oid,
                    reason=f"not in the order map after {age:.0f}s",
                )
            if leg.oid:
                # Shown, then gone: filled, or cancelled behind our back.
                # Either way there is nothing left to cancel, so the next
                # order goes out now and the next pass re-derives from
                # position.
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
        record = self._orders.get(leg.oid)
        oversized = (
            record is not None and record.adopted
            and resting.size > remaining + spec.lot_size / 2
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
            # A joining order never steps past anyone, itself included -- its
            # target is a level others already hold -- so it moves whenever
            # that level does. That includes leaving the touch once everyone
            # else there has gone, which is the point of it.
            if resting.price != target and (joining or not at_the_touch):
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

    async def _complete(
        self, session: AccountSession, roles: LegRoles, leg: LegState, remaining: float
    ) -> ChaseOutcome:
        """Nothing left worth trading: finish the leg.

        Any resting remainder is pulled so it can't fill after the leg is
        considered done.
        """
        if leg.oid:
            await self._cancel(session, leg, roles.symbol)
        leg.complete = True
        leg.tightened = False
        self._chasing_since.pop(self._chase_key(roles), None)
        return ChaseOutcome(roles.symbol, "complete", f"remaining={remaining:.8f}")

    def may_be_resting(self, session: AccountSession, oid: str | None) -> bool:
        """Whether this order could still be on the book.

        Yes if the order map has it. No if the map has shown it before and no
        longer does: it was filled or cancelled, and filled is the usual reason
        a hedge is running. An order the map has never shown may be resting
        with nothing here to say so yet -- orderUpdate has run up to thirty
        seconds late -- so it counts as resting for `UNSEEN_WATCH_S`.

        It used to apply a three-second grace by age alone, both ways. Any
        order younger than that counted as possibly resting even after it had
        shown up and gone: on the touch an order is re-placed about every
        second, and 49% of maker fills on a live run landed inside that
        window, each one sending a cancel for an order that no longer existed
        while the hedge waited a second round trip behind it -- the median
        went from one (~330ms) to two (~650ms). And any order older than that
        counted as gone even if the map had simply not caught up with it,
        which left it in the hedge's path.
        """
        if not oid:
            return False
        if oid in session.client.get_order_map():
            self._mark_seen(oid)
            return True
        record = self._orders.get(oid)
        if record is None or record.seen:
            return False
        return time.monotonic() - record.placed_at < UNSEEN_WATCH_S

    def _mark_seen(self, oid: str) -> None:
        record = self._orders.get(oid)
        if record is not None:
            record.seen = True

    def _resting_order(self, session: AccountSession, leg: LegState):
        if not leg.oid:
            return None
        order = session.client.get_order_map().get(leg.oid)
        if order is not None:
            # Every chase step passes through here, so an order that rests
            # for a step is recorded as acknowledged well before it fills.
            self._mark_seen(leg.oid)
        return order

    def _forget_gone(self) -> None:
        """Drop the records of orders known to be gone, across every leg.

        Seen and then absent is gone. A superseded order never seen is kept
        for `UNSEEN_WATCH_S`, for the reason in `may_be_resting`. A leg's
        current order is never dropped unseen: without its record the next
        step would take it for gone and place a second one beside it, which is
        the failure the record exists to prevent. Without any of this the
        records would only ever grow, by one per replace for the life of the
        process.
        """
        now = time.monotonic()
        for oid, record in list(self._orders.items()):
            session = self.sessions.get(record.maker)
            if session is not None and oid in session.client.get_order_map():
                record.seen = True
            elif record.seen or (
                record.superseded and now - record.placed_at >= UNSEEN_WATCH_S
            ):
                del self._orders[oid]

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
            # A rejected replace leaves `leg.oid` on the original, which is
            # still the leg's order -- resting, or gone and found so by the
            # next pass.
            #
            # With ALO orders the common rejection is `rejectedCrossing`: the
            # book reached the target between reading it and the order landing.
            # The cancel in the same transaction may still have applied, so the
            # leg can be left with nothing resting -- the next pass sees no
            # order and places fresh, which bounds the gap at one chase
            # interval. `mod` would avoid it by changing size in place, but the
            # Python SDK has no message type for that action.
            placed_oid = getattr(exc, "order_id", None)
            if getattr(exc, "placed", False) and placed_oid:
                # The batch was refused as a whole because its CANCEL half
                # failed -- typically the old order had just filled -- but the
                # new order was accepted and is resting. Dropping its id left
                # an order nothing tracked: the next pass placed another, and
                # the leg filled past its size. Adopted instead; the next pass
                # resizes it if the fill left it too big. The order it
                # replaced goes on the sweep list: its cancel was refused, so
                # it is gone only once the order map says so.
                log.warning(
                    "%s: replace refused but the new order rests (%s) -- keeping it",
                    symbol, describe(exc),
                )
                self._track(roles, leg, placed_oid, price, size, uncertain=True)
                self._orders[placed_oid].adopted = True
                return ChaseOutcome(symbol, "placed", "adopted after a refused cancel")
            log.warning("%s: chase order rejected: %s", symbol, describe(exc))
            return ChaseOutcome(symbol, "skipped", f"rejected: {exc}")

        # The cancel half was answered as done -- a refused one raises above
        # -- so the order it replaced is not put on the sweep list. It used to
        # be, by a call that never took effect: `remember_stale` skips the
        # leg's current order, and the old id was still current at that point.
        # Making it take effect would send a second cancel for every replace
        # whose orderUpdate is late, which on the touch is most of them.
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
        self,
        roles: LegRoles,
        leg: LegState,
        oid: str,
        price: float,
        size: float,
        uncertain: bool = False,
    ) -> None:
        """Record `oid` as the leg's resting order.

        `uncertain` says the order it replaces may not have been cancelled,
        which puts that one on the leg's sweep list.
        """
        previous = leg.oid
        now = time.monotonic()
        self._orders[oid] = _Placed(
            symbol=roles.symbol, is_buy=roles.maker_is_buy, maker=roles.maker,
            placed_at=now,
        )
        leg.oid = oid
        leg.price = price
        leg.size = size
        if previous and previous != oid:
            superseded = self._orders.get(previous)
            if superseded is not None:
                superseded.superseded = True
            if uncertain:
                # Only once `leg.oid` has moved on: `remember_stale` skips
                # the leg's current order.
                leg.remember_stale(previous)
        self._chasing_since.setdefault(self._chase_key(roles), now)

    def _join_target(self, roles: LegRoles, leg: LegState, quote, depth_usd: float):
        """The best price on our side where others already rest `depth_usd`.

        Our own orders at a level are taken off its size: joining ourselves is
        standing alone with extra steps. The level this leg already rests at
        is kept while others there hold `JOIN_KEEP_FRACTION` of it and nothing
        better has filled up. Nothing deep enough within `JOIN_MAX_BPS` of the
        touch: join the touch itself rather than step ahead of it.

        None without a book, which leaves the ordinary target in place.
        """
        symbol = roles.symbol
        is_buy = roles.maker_is_buy
        touch = quote.best_bid if is_buy else quote.best_ask
        if not touch or not quote.best_bid or not quote.best_ask:
            return None
        levels = self.feed.levels(symbol, is_buy, JOIN_SCAN_LEVELS)
        if not levels:
            return None
        resting_at = leg.price if leg.oid else None
        # Best first, so reaching our own level means nothing ahead of it has
        # filled up -- and then it is kept on the lower bar.
        for price, size in levels:
            if distance_bps(price, touch) > JOIN_MAX_BPS:
                break
            others = (size - self._our_size_at(symbol, is_buy, price)) * price
            if others >= depth_usd:
                return price
            if price == resting_at and others >= depth_usd * JOIN_KEEP_FRACTION:
                return price
        return touch

    def _ours_resting(self, symbol: str, is_buy: bool, except_oid: str | None = None):
        """Every leg's current order of ours on this side that the map shows."""
        for oid, record in list(self._orders.items()):
            if (
                oid == except_oid or record.superseded
                or record.symbol != symbol or record.is_buy != is_buy
            ):
                continue
            session = self.sessions.get(record.maker)
            order = session.client.get_order_map().get(oid) if session else None
            if order is not None:
                yield order

    def _our_size_at(self, symbol: str, is_buy: bool, price: float) -> float:
        """How much of ours rests at `price` on this side, every leg together."""
        return sum(
            order.size for order in self._ours_resting(symbol, is_buy)
            if order.price == price
        )

    def _ours_at(
        self, symbol: str, is_buy: bool, price: float, except_oid: str | None
    ) -> bool:
        """Whether another leg's order of ours rests at `price` on this side.

        Read from the order maps, not from what was placed: an order that
        filled or was cancelled no longer holds the level.
        """
        return any(
            order.price == price
            for order in self._ours_resting(symbol, is_buy, except_oid)
        )

    async def _cancel(self, session: AccountSession, leg: LegState, symbol: str) -> None:
        oid = leg.oid
        if not oid:
            return
        answered = True
        try:
            await session.cancel(symbol, oid)
            log.info("%s: cancelled %s on %s", symbol, oid[:8], session.name)
        except OrderRejected as exc:
            # Usually means it already filled or was already cancelled.
            log.debug("%s: cancel of %s not accepted: %s", symbol, oid[:8], exc)
        except Exception as exc:
            log.warning("%s: cancel of %s failed: %s", symbol, oid[:8], describe(exc))
            answered = False
        leg.oid = None
        leg.price = None
        leg.size = None
        if answered:
            self._orders.pop(oid, None)
            return
        # It may still rest. Remembered only once `leg.oid` is cleared: it
        # used to be remembered before, `remember_stale` skipped it as the
        # leg's current order, and the order was forgotten instead of swept.
        leg.remember_stale(oid)
        record = self._orders.get(oid)
        if record is not None:
            record.superseded = True

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
        # One the map has never shown is not gone for being absent -- its
        # orderUpdate may be late -- so it is kept until it surfaces and is
        # cancelled, or until `_forget_gone` stops watching for it.
        unseen = {
            oid for oid in leg.stale_oids
            if oid in self._orders and not self._orders[oid].seen
        }

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

        # Anything known to be off the book needs no further attention.
        leg.stale_oids = [
            oid for oid in leg.stale_oids if oid in order_map or oid in unseen
        ]

    async def cancel_leg(self, roles: LegRoles, leg: LegState) -> None:
        """Public cancel, used on phase transitions and shutdown."""
        await self._cancel(self.sessions[roles.maker], leg, roles.symbol)
        # The next phase gets its own patience: entry and exit are different
        # sides of the book and one having been slow says nothing about the
        # other.
        leg.tightened = False

"""Taker-only execution, for `mode: delta-neutral`.

In the other modes a group's opener rests a post-only order and waits for the
market to come to it. Here nothing rests: the opener TAKES liquidity with an
IOC limit order, and every fill it gets is hedged at once on its partner with
a market order the other way -- through the same position-derived hedge path
every other mode uses (`Strategy._hedge_leg`). Opening and closing both work
this way; the close is reduce-only on both accounts.

Taking liquidity means paying the spread and whatever depth an order walks
through, twice: once on the opener's side of the book and once on the
hedge's. So every slice is planned against the book first, on BOTH sides:

  * The spread must be no wider than `max_spread_bps`. A wide spread is paid
    in full on every slice; waiting for it to close costs nothing but time.
  * Each side's depth is counted from its touch out to `max_slippage_bps`
    past it. The slice is at most `depth_fraction` of the thinner of the two
    -- the opener's side, because that is what it walks through, and the
    hedge's side, because the hedge is a market order that will walk through
    whatever it finds there a few milliseconds later.
  * The IOC's limit is the price of the deepest level the slice needs, never
    past the band. If the book moved between the read and the order landing,
    the order fills what is still there at that price or better and the rest
    is cancelled -- it never chases.

Nothing is sent while the previous slice is still being hedged, or while its
fill has not reached the position book yet, so exposure never stacks: at any
moment the group is off neutral by at most one slice.

The executor stands where the `Chaser` stands for the other modes, with the
same three calls `Strategy._drive_leg` makes -- `step`, `remaining_size` and
`cancel_leg` -- so the phase machine, the hedger, recovery, the liquidation
guard and the wind-down are one code path for every mode.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from bulk_api.common import OrderStatus

from .accounts import AccountSession, OrderRejected
from .chaser import ChaseOutcome, ChaseParams
from .config import TakerConfig
from .feed import MarketFeed
from .hedger import LegRoles
from .marketdata import MarketSpec, min_order_size, round_size
from .positions import PositionBook
from .retry import describe
from .state import LegState

log = logging.getLogger(__name__)

# How often an unchanged reason for waiting is written to the log again.
SAY_AGAIN_S = 30.0
# How long to wait for the fill of a slice the exchange answered without
# saying it filled. `cancelledIOC` may still carry a partial fill, so not at
# all would size the next slice off a position about to change; the full
# `fill_wait_s` would stall the leg after every slice that found nothing.
NO_FILL_GRACE_S = 0.5
# What a slice leaves behind, when it leaves anything, is at least this many
# minimum orders: one, and a little more, because the minimum is a notional
# and the price moves between this slice and the next.
REMAINDER_MARGIN = 1.05

_FILLED = frozenset({OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED})


# -- reading the book -------------------------------------------------------


@dataclass(frozen=True)
class SideDepth:
    """One side of the book as a taker sees it, out to a price band."""

    touch: float
    # The furthest price a taker on this side accepts: the touch plus the
    # band for a buy (it takes the asks), minus it for a sell.
    limit: float
    # Base units resting from the touch out to `limit`, inclusive.
    size: float
    levels: tuple[tuple[float, float], ...]


def side_depth(
    levels: list[tuple[float, float]], *, taker_buys: bool, band_bps: float
) -> SideDepth | None:
    """What a taker can fill on one side within `band_bps` of its touch.

    `levels` are that side's (price, size), best first: the asks for a buyer,
    the bids for a seller. None when the side is empty.
    """
    levels = [(p, s) for p, s in levels if p and p > 0 and s and s > 0]
    if not levels:
        return None
    touch = levels[0][0]
    if taker_buys:
        limit = touch * (1 + band_bps / 10_000)
        inside = [(p, s) for p, s in levels if p <= limit]
    else:
        limit = touch * (1 - band_bps / 10_000)
        inside = [(p, s) for p, s in levels if p >= limit]
    return SideDepth(
        touch=touch, limit=limit, size=sum(s for _p, s in inside),
        levels=tuple(inside),
    )


def walk(levels: tuple[tuple[float, float], ...], size: float) -> tuple[float, float]:
    """Filling `size` through `levels`: (average price, last price reached).

    Assumes the levels hold at least `size`; what does not fit is priced at
    the last level, which only `plan_slice`'s own sizing could get wrong.
    """
    left = size
    cost = 0.0
    last = levels[0][0]
    for price, available in levels:
        if left <= 0:
            break
        taken = min(left, available)
        cost += taken * price
        left -= taken
        last = price
    if left > 0:
        cost += left * last
    return cost / size, last


@dataclass(frozen=True)
class SlicePlan:
    """One IOC the book says is worth sending now."""

    size: float
    # The IOC's limit: the deepest level of the opener's side the slice needs.
    limit_price: float
    # What the round trip of this slice is expected to give away against the
    # mid -- the opener's average fill and the hedge's, both walked through
    # the book as it stands -- in bps of the slice's notional. Fees apart.
    expected_bps: float
    spread_bps: float
    # Base units inside the band on each side, as read.
    open_depth: float
    hedge_depth: float


@dataclass(frozen=True)
class Wait:
    """Why no slice goes out on this pass."""

    reason: str


def plan_slice(
    *,
    opener_buys: bool,
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    spec: MarketSpec,
    remaining: float,
    cap: float,
    settings: TakerConfig,
) -> SlicePlan | Wait:
    """Size and price the next slice against both sides of the book.

    Pure, so it can be tested on a book written out by hand.
    """
    opener_side = asks if opener_buys else bids
    hedge_side = bids if opener_buys else asks
    mine = side_depth(opener_side, taker_buys=opener_buys, band_bps=settings.max_slippage_bps)
    theirs = side_depth(hedge_side, taker_buys=not opener_buys, band_bps=settings.max_slippage_bps)
    if mine is None or theirs is None:
        return Wait("one side of the book is empty")

    best_bid = bids[0][0]
    best_ask = asks[0][0]
    if best_ask <= best_bid:
        return Wait(f"the book is crossed ({best_bid:g} / {best_ask:g})")
    mid = (best_bid + best_ask) / 2
    spread_bps = (best_ask - best_bid) / mid * 10_000
    if spread_bps > settings.max_spread_bps:
        return Wait(
            f"spread {spread_bps:.2f}bps is wider than max_spread_bps "
            f"{settings.max_spread_bps:g}"
        )

    # The thinner side decides. The opener walks its own side now; the hedge
    # walks the other one right after, as a market order with no price cap.
    room = settings.depth_fraction * min(mine.size, theirs.size)
    size = round_size(min(remaining, cap, room), spec)
    floor = min_order_size(spec, mine.touch)
    # Never leave less than one minimum order behind. A remainder under the
    # floor can be neither opened nor closed: on the way out it stayed on
    # both accounts as a hedged pair of dust that
    # no order the exchange accepts could reduce. So the last slice takes
    # it all when the book can carry it -- over the cap by under one
    # minimum order -- and otherwise stops short by a full minimum (plus a
    # margin for the price moving before the next slice), so the next slice
    # is still a legal order.
    reserve = floor * REMAINDER_MARGIN
    left = remaining - size
    if spec.lot_size * 1e-6 < left < reserve:
        if remaining <= room:
            size = round_size(remaining, spec)
        else:
            size = round_size(remaining - reserve, spec)
    if size < floor:
        return Wait(
            f"thin book: {settings.depth_fraction:.0%} of the depth within "
            f"{settings.max_slippage_bps:g}bps is {room:.8f}, under the "
            f"{floor:.8f} minimum order (open side {mine.size:.8f}, hedge "
            f"side {theirs.size:.8f})"
        )

    open_avg, open_last = walk(mine.levels, size)
    hedge_avg, _hedge_last = walk(theirs.levels, size)
    if opener_buys:
        given = (open_avg - mid) + (mid - hedge_avg)
    else:
        given = (mid - open_avg) + (hedge_avg - mid)
    return SlicePlan(
        size=size,
        limit_price=open_last,
        expected_bps=given / mid * 10_000,
        spread_bps=spread_bps,
        open_depth=mine.size,
        hedge_depth=theirs.size,
    )


# -- the executor -----------------------------------------------------------


@dataclass
class _Sent:
    """A slice that has gone out and whose fill the book has not shown yet."""

    position_before: float
    sent_at: float
    # The exchange said it filled something. None: no answer at all.
    filled: bool | None


class TakerExecutor:
    """Opens and closes a group's position with IOC slices sized to the book."""

    def __init__(
        self,
        *,
        sessions: dict[str, AccountSession],
        feed: MarketFeed,
        book: PositionBook,
        params: dict[str, ChaseParams],
        settings: TakerConfig,
        price_stale_timeout_s: float = 15.0,
    ):
        self.sessions = sessions
        self.feed = feed
        self.book = book
        # The per-market order cap lives here, as it does on the Chaser:
        # `Strategy._size_for_cycle` writes a redrawn cap into it.
        self.params = params
        self.settings = settings
        self.price_stale_timeout_s = price_stale_timeout_s
        # Set by the strategy that owns this executor; see `attach`.
        self._is_neutral = lambda roles: True
        self._waiting_on_a_read = lambda roles: False
        self._mark_unread = lambda session, why: None
        self._sent: dict[str, _Sent] = {}
        self._last_sent_at: dict[str, float] = {}
        self._said: dict[str, tuple[str, float]] = {}

    def attach(self, *, is_neutral, waiting_on_a_read, mark_unread) -> None:
        """Hand over the strategy's view of each leg.

        `is_neutral(roles)` -- nothing left to hedge, nothing in flight;
        `waiting_on_a_read(roles)` -- the book is known to be behind on one of
        the leg's accounts; `mark_unread(session, why)` -- say that it is.
        """
        self._is_neutral = is_neutral
        self._waiting_on_a_read = waiting_on_a_read
        self._mark_unread = mark_unread

    @property
    def interval_s(self) -> float:
        """How often `_drive_leg` steps a leg. Faster than a chase: a slice
        is sent only once the last one is hedged, so this paces nothing but
        how soon that is noticed."""
        return self.settings.poll_interval_s

    # -- what the strategy calls --------------------------------------------

    def remaining_size(self, roles: LegRoles, leg: LegState) -> float:
        position = abs(self.book.effective(roles.maker, roles.symbol))
        if roles.reduce_only:
            return position
        return max(0.0, leg.target_size - position)

    def may_be_resting(self, session: AccountSession, oid: str | None) -> bool:
        # An IOC never rests.
        return False

    async def cancel_leg(self, roles: LegRoles, leg: LegState) -> None:
        leg.oid = None

    async def step(self, roles: LegRoles, leg: LegState) -> ChaseOutcome:
        symbol = roles.symbol
        key = roles.key
        spec = self.feed.specs[symbol]
        session = self.sessions[roles.maker]

        if self._still_landing(roles):
            return ChaseOutcome(symbol, "waiting", "last slice still landing")
        if self._waiting_on_a_read(roles):
            return self._wait(key, symbol, "waiting for a position read")
        if not self._is_neutral(roles):
            return ChaseOutcome(symbol, "waiting", "last slice still being hedged")

        remaining = round_size(self.remaining_size(roles, leg), spec)
        quote = self.feed.quote(symbol)
        touch = quote.best_ask if roles.maker_is_buy else quote.best_bid
        if remaining < min_order_size(spec, touch or quote.reference_price):
            leg.complete = True
            self._forget(key)
            return ChaseOutcome(symbol, "complete", f"remaining={remaining:.8f}")

        since = time.monotonic() - self._last_sent_at.get(key, 0.0)
        if since < self.settings.slice_interval_s:
            return ChaseOutcome(symbol, "waiting", "between slices")

        if quote.age_s > self.price_stale_timeout_s:
            return self._wait(key, symbol, f"stale price ({quote.age_s:.1f}s old)")
        book_age = self.feed.book_age_s(symbol)
        if book_age > self.price_stale_timeout_s:
            return self._wait(key, symbol, "no fresh order book")
        if not quote.best_bid or not quote.best_ask:
            # `quote` drops the touch of a book it judges frozen. A taker
            # needs real depth, never the mark.
            return self._wait(key, symbol, "no usable order book")

        bids = self.feed.levels(symbol, True, self.settings.levels)
        asks = self.feed.levels(symbol, False, self.settings.levels)
        params = self.params.get(symbol)
        cap = remaining
        if params is not None:
            cap = params.max_order_size
        if roles.max_order_size is not None:
            cap = roles.max_order_size
        plan = plan_slice(
            opener_buys=roles.maker_is_buy, bids=bids, asks=asks, spec=spec,
            remaining=remaining, cap=cap, settings=self.settings,
        )
        if isinstance(plan, Wait):
            return self._wait(key, symbol, plan.reason)

        return await self._send(session, roles, plan)

    # -- helpers ------------------------------------------------------------

    async def _send(
        self, session: AccountSession, roles: LegRoles, plan: SlicePlan
    ) -> ChaseOutcome:
        key = roles.key
        symbol = roles.symbol
        before = self.book.effective(roles.maker, symbol)
        side = "BUY" if roles.maker_is_buy else "SELL"
        self._said.pop(key, None)
        log.info(
            "%s: taker slice %s %s %.8f %s IOC<=%.8f  spread %.2fbps, depth "
            "open/hedge %.8f/%.8f, expected give-up %.2fbps%s",
            key, session.name, side, plan.size, symbol, plan.limit_price,
            plan.spread_bps, plan.open_depth, plan.hedge_depth, plan.expected_bps,
            " (reduce-only)" if roles.reduce_only else "",
        )
        sent_at = time.monotonic()
        self._last_sent_at[key] = sent_at
        try:
            responses = await session.take(
                symbol, roles.maker_is_buy, plan.limit_price, plan.size,
                reduce_only=roles.reduce_only,
            )
        except OrderRejected as exc:
            # An answer: nothing traded. Counted toward the reject streak by
            # `submit`, like any refusal.
            log.warning("%s: taker slice refused: %s", key, describe(exc))
            return ChaseOutcome(symbol, "skipped", "refused")
        except Exception:
            # No answer: it may have filled. The book is behind until a read
            # says otherwise, and nothing more goes out on this leg until then.
            self._sent[key] = _Sent(before, sent_at, filled=None)
            self._mark_unread(session, "a taker slice got no answer")
            raise

        filled = any(r.status in _FILLED for r in responses)
        self._sent[key] = _Sent(before, sent_at, filled=filled)
        return ChaseOutcome(symbol, "placed", "IOC", plan.limit_price, plan.size)

    def _still_landing(self, roles: LegRoles) -> bool:
        """Whether the last slice's fill has yet to reach the position book.

        Its fill arrives on the account stream, a few milliseconds behind the
        order's answer as a rule. Sending again before it does would size the
        next slice off a position that is about to change -- and the hedge
        for the first could not be told from a hedge for the second.
        """
        sent = self._sent.get(roles.key)
        if sent is None:
            return False
        if self.book.effective(roles.maker, roles.symbol) != sent.position_before:
            del self._sent[roles.key]
            return False
        wait = self.settings.fill_wait_s
        if sent.filled is False:
            wait = min(wait, NO_FILL_GRACE_S)
        if time.monotonic() - sent.sent_at < wait:
            return True
        del self._sent[roles.key]
        if sent.filled:
            # The exchange said it traded and the stream never showed it.
            session = self.sessions.get(roles.maker)
            if session is not None:
                self._mark_unread(
                    session, "a taker slice filled and its fill never arrived"
                )
        return False

    def _wait(self, key: str, symbol: str, reason: str) -> ChaseOutcome:
        """Say why the leg is waiting -- once, and again only now and then."""
        now = time.monotonic()
        # Compared without its numbers: a spread or a depth that moves on
        # every pass is still the same reason, and would log five times a
        # second.
        kind = re.sub(r"[0-9][0-9.]*", "#", reason)
        said = self._said.get(key)
        if said is None or said[0] != kind or now - said[1] >= SAY_AGAIN_S:
            self._said[key] = (kind, now)
            log.info("%s: waiting -- %s", key, reason)
        return ChaseOutcome(symbol, "waiting", reason)

    def _forget(self, key: str) -> None:
        self._sent.pop(key, None)
        self._last_sent_at.pop(key, None)
        self._said.pop(key, None)

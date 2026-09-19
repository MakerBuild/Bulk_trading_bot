"""The delta-neutral cycle: OPEN -> HOLD -> EXIT -> COMPLETE, per leg.

Each leg runs that cycle on its own clock, as its own task. They shared one
phase once, and it cost real time: a leg filled on both accounts sat idle until
the other filled before its hold could start, and a leg that had closed could
not place its next entry until the other closed too.

Nothing required the synchrony. The hedge is computed per symbol --
`net = position[maker] + position[taker]` -- so the two legs never had anything
to agree about. What is genuinely shared moves to `_supervise`: the risk limits,
which are about the pair, and the position read, which is one pair of HTTP calls
either way.

Concurrency note, because it dictates the shape of this module: the SDK awaits
event handlers inline inside its WebSocket receive loop, and order responses are
resolved by that same loop. A handler that awaited an order submission would
therefore block the very loop that has to deliver its response, and deadlock
until the request timed out.

So handlers here are synchronous. They update the position book and signal a
worker; all order submission happens in separate tasks. That keeps the receive
loop free and still hedges within an event-loop hop of the fill arriving.
"""

from __future__ import annotations

import asyncio
import logging
import time

from bulk_api.common import Side, Topic

from .accounts import AccountSession
from .chaser import ChaseParams, Chaser
from .config import Config
from .feed import MarketFeed
from .fees import burned_usd as _burned
from .fees import realised_for_tree
from .hedger import Hedger, HedgeLimitExceeded, LegRoles
from .liquidation import LiquidationGuard, recent_liquidations
from .marketdata import round_notional, round_size, touch_text
from .notify import Notifier
from .positions import PositionBook, SeenTrades
from .reconcile import (
    cancel_all_orders,
    flatten,
    reconcile_net,
    sync_positions,
    sync_positions_http,
)
from .retry import describe
from .risk import RiskMonitor
from .state import Phase, StateStore, StrategyState
from .window import WindowTitle
from .ws_compat import fill_trade_id
import contextlib

log = logging.getLogger(__name__)


# Reconnects tolerated before a dropped socket is treated as a persistent fault
# rather than a blip -- five of them inside ten minutes.
#
# Counted over a moving window, not per cycle. It was written as a per-cycle
# budget and the reset was never implemented, so in practice it was five for the
# entire run: an unlimited run halted on its sixth drop no matter how many hours
# apart they fell. A window also survives the legs running independently, where
# "this cycle" is two different things at once and neither is the right moment
# to forgive a fault.
# How long after an unanswered submission a change in that symbol might still
# be our own.
DOUBT_WINDOW_S = 120.0
# And how often we may say so before treating the symbol as faulty. Counted
# over a window rather than for the life of the process: the bound is meant to
# catch a fault that keeps recurring, and a run lasting hours will collect
# unrelated single incidents that each deserved the benefit of the doubt.
MAX_DOUBT_DEFERRALS = 3
DEFERRAL_WINDOW_S = 900.0

MAX_RECONNECTS = 5
RECONNECT_WINDOW_S = 600.0

# How long a position read stays good enough to share. Three callers now want
# one -- the supervisor and each leg confirming a phase -- and a live run showed
# them fetching the same numbers three times within the same second. Short
# enough that nobody acts on anything stale, long enough to collapse a burst.
POSITION_FRESHNESS_S = 0.5


def phase_budget_s(phase: Phase, max_phase_minutes: float) -> float:
    """How long a phase may run, in seconds. 0 means no limit.

    HOLD is exempt: it ends on a deadline it set itself, so a cap could only
    cut a legitimately long hold short. OPEN and EXIT wait on fills, which may
    never arrive, and those are what this exists for.
    """
    if phase == Phase.HOLD or max_phase_minutes <= 0:
        return 0.0
    return max_phase_minutes * 60


class Halted(Exception):
    """Raised when a risk limit trips. Always followed by cancel + flatten."""


class Strategy:
    def __init__(
        self,
        *,
        config: Config,
        master: AccountSession,
        sub1: AccountSession,
        feed: MarketFeed,
        book: PositionBook,
        hedger: Hedger,
        chaser: Chaser,
        risk: RiskMonitor,
        store: StateStore,
        state: StrategyState,
        notifier: Notifier | None = None,
        title: WindowTitle | None = None,
    ):
        self.config = config
        # Both default to inert objects rather than None, so every call site
        # can use them unconditionally instead of guarding each one.
        self.notifier = notifier or Notifier()
        self.title = title or WindowTitle(cycles=config.cycles)
        self.master = master
        self.sub1 = sub1
        self.feed = feed
        self.book = book
        self.hedger = hedger
        self.chaser = chaser
        self.risk = risk
        self.store = store
        self.state = state

        self.sessions: dict[str, AccountSession] = {
            master.pubkey: master,
            sub1.pubkey: sub1,
        }
        self.symbols = [config.master_account.symbol, config.sub_account.symbol]
        self._seen_trades = SeenTrades()
        self.guard = LiquidationGuard(
            specs=feed.specs,
            names={master.pubkey: master.name, sub1.pubkey: sub1.name},
        )
        # Set the instant a position update shows an external reduction, so
        # the response does not wait for the next reconcile tick.
        self._liquidation_seen = asyncio.Event()
        self._hedge_queue: asyncio.Queue = asyncio.Queue()
        self._stop = asyncio.Event()
        # Why the stop happened, when it was asked for from outside rather than
        # reached by finishing. Distinguishes "the operator pressed stop" from
        # "the cycles ran out", which want different endings.
        self._stop_requested: str | None = None
        self._halt_reason: str | None = None
        # When each symbol's external-change signal was last put down to this
        # bot's own unanswered orders. Bounded over a window, so a fault that
        # keeps looking like our own doing cannot be deferred forever, while
        # incidents hours apart do not add up to one.
        self._doubt_deferrals: dict[str, list[float]] = {}
        self._started_at = time.monotonic()
        # burned, qualifying volume -- as of the last progress read.
        self._progress: tuple[float, float] = (0.0, 0.0)
        # When each recent reconnect happened. Older entries fall out of the
        # window on their own, so a drop an hour ago says nothing about this one.
        self._reconnect_times: list[float] = []
        self._sync_lock = asyncio.Lock()
        self._synced_at = 0.0

    def _persist(self) -> None:
        """Save the state file, and survive not being able to.

        A write can fail for reasons that have nothing to do with trading --
        the folder is in OneDrive, an antivirus has the file open, the disk is
        full. That used to propagate: a live run died mid-HOLD on a
        `PermissionError` from `os.replace` with hedged positions open on both
        accounts and nothing left running to close them.

        Abandoning open positions is a far worse outcome than a stale file, so
        this reports and carries on. The state is still correct in memory, the
        next tick tries again, and recovery reads positions back from the
        exchange anyway -- the file is a hint about phase, not the ledger.
        """
        try:
            self.store.save(self.state)
        except OSError as exc:
            log.error(
                "could not write the state file (%s) -- continuing. A restart "
                "may not know which phase this cycle was in; positions are read "
                "back from the exchange either way.",
                describe(exc),
            )

    @property
    def stop_reason(self) -> str | None:
        """Why this run ended early, or None if it ended on its own terms.

        The public half of `request_stop`. `cmd_run` needs to tell "the operator
        pressed stop" from "the cycles ran out" to choose an ending, and it was
        reading the underscored attribute across a module boundary to do it.
        """
        return self._stop_requested

    def request_stop(self, reason: str = "operator") -> None:
        """Ask both legs to stop, from outside the run loop.

        Safe to call at any moment and from any task. It sets a flag rather
        than cancelling anything: a leg checks it once per chase tick, so an
        order already being submitted completes and is accounted for, and the
        stop lands a second later with the book in a state the run can describe.
        Cancelling mid-flight is what leaves an order on the exchange that the
        state file does not know about.

        Idempotent -- pressing the key twice is not a harder stop.
        """
        if self._stop.is_set():
            return
        self._stop_requested = reason
        log.warning("stop requested (%s) -- finishing the current step", reason)
        self._stop.set()

    # -- leg roles ---------------------------------------------------------

    def roles_for(self, phase: Phase) -> list[LegRoles]:
        """Which account makes and which hedges, for a given phase.

        The swap between OPEN and EXIT is what lets one hedge rule serve both.
        Every maker leg happens to be a buy: entry buys to open the longs, exit
        buys to cover the shorts.
        """
        master, sub1 = self.master.pubkey, self.sub1.pubkey
        # Each leg is named for the account that opens it, which is what the
        # config keys mean. The master goes long `master_leg` and short
        # `sub_leg`; the sub-account does the reverse.
        master_leg = self.config.master_account.symbol
        sub_leg = self.config.sub_account.symbol

        if phase == Phase.EXIT:
            return [
                # Sub1 covers the short it took hedging the master's long.
                LegRoles(master_leg, maker=sub1, taker=master, maker_is_buy=True, reduce_only=True),
                # Master covers the short it took hedging sub1's long.
                LegRoles(sub_leg, maker=master, taker=sub1, maker_is_buy=True, reduce_only=True),
            ]

        # OPEN, and HOLD reuses the same roles so any drift is corrected on the
        # same account that was hedging during entry.
        return [
            LegRoles(master_leg, maker=master, taker=sub1, maker_is_buy=True, reduce_only=False),
            LegRoles(sub_leg, maker=sub1, taker=master, maker_is_buy=True, reduce_only=False),
        ]

    def _roles_by_symbol(self, phase: Phase) -> dict[str, LegRoles]:
        return {roles.symbol: roles for roles in self.roles_for(phase)}

    def leg_roles(self, symbol: str) -> LegRoles:
        """Roles for one symbol, at the phase that symbol is actually in.

        Legs advance independently, so asking for "the" phase is meaningless
        once one is exiting while the other is still opening.
        """
        return self._roles_by_symbol(self.state.leg(symbol).phase)[symbol]

    def _live_roles(self) -> list[LegRoles]:
        """Roles for every leg, each at its own phase."""
        return [self.leg_roles(symbol) for symbol in self.symbols]

    # -- handlers (synchronous; see module docstring) ----------------------

    def install_handlers(self) -> None:
        for session in (self.master, self.sub1):
            session.on(Topic.FILL, self._make_fill_handler(session))
            session.on(Topic.POSITION, self._make_position_handler(session))
            session.on(Topic.ACCOUNT, self._make_snapshot_handler(session))

    def _make_fill_handler(self, session: AccountSession):
        def handler(fill) -> None:
            symbol = getattr(fill, "symbol", None)
            if symbol not in self.symbols:
                return

            is_buy = fill.side == Side.BUY
            size = float(fill.size or 0.0)
            if size <= 0:
                return

            # A replayed fill (typically after a reconnect) must not be applied
            # to the book twice. Re-evaluating the hedge is still safe -- it is
            # derived from position state and is a no-op when already neutral --
            # so the trigger is kept and only the bookkeeping is skipped.
            # Prefer the id attached during parsing; fall back to the client's
            # dispatch-scoped stash for fills parsed before the patch applied.
            trade_id = fill_trade_id(fill)
            if not self._seen_trades.add_if_new(session.pubkey, trade_id):
                log.debug(
                    "ignoring duplicate fill %s on %s", trade_id, session.name
                )
                self._hedge_queue.put_nowait(symbol)
                return

            self.book.apply_fill(session.pubkey, symbol, is_buy, size)

            roles = self._roles_by_symbol(self.state.leg(symbol).phase).get(symbol)
            if roles is not None and session.pubkey == roles.taker:
                # This is one of our own hedge orders landing; retire its
                # reservation so net exposure reads correctly.
                self.hedger.note_taker_fill(symbol, size if is_buy else -size)

            # `role` and the touch are carried here rather than worked out
            # afterwards. Which side of the pair a fill belongs to was being
            # inferred by pairing fills within a few seconds of each other,
            # which left 108 of 2,067 hedges unmatched and mis-assigns any pair
            # that lands out of order. The handler already knows.
            if roles is None:
                role = "?"
            elif session.pubkey == roles.taker:
                role = "taker"
            elif session.pubkey == roles.maker:
                role = "maker"
            else:
                role = "?"
            log.info(
                "fill on %s: %s %.8f %s @ %.8f role=%s %s",
                session.name,
                "BUY" if is_buy else "SELL",
                size,
                symbol,
                float(fill.price or 0.0),
                role,
                touch_text(self.feed, symbol),
            )
            # Hand off to the worker rather than trading here -- awaiting an
            # order response inside this handler would deadlock the socket.
            self._hedge_queue.put_nowait(symbol)

        return handler

    async def _guard_liquidation(self, phase: Phase) -> bool:
        """Close the surviving leg if the other one was closed externally.

        Returns True when it acted, which means trading is over: a liquidation
        says the account could not carry the position, so the response is to
        get flat and stop, not to rebuild the pair.

        Reduce-only throughout, so a stale reading can never turn a close into
        a new position in the opposite direction.

        The local signal is a trigger, not a diagnosis. It fires on any change
        the bot cannot account for, and a submission that never came back may
        still have executed -- which looks identical. A live run ended that
        way: three hedges landed unacknowledged during an exchange outage, the
        position flipped, and the exchange had no record of any liquidation.

        So a symbol we have an unanswered order in gets one more chance, and
        only on a condition that matters: that a fresh position read actually
        succeeds. Deferring on the strength of a guess during an outage would
        be trading blind, which is the thing this exists to prevent. When the
        read works, the guess is replaced by the exchange's own answer and the
        ordinary hedge rule can correct from there.
        """
        events = self.guard.check(self.book, phase, [self.master.pubkey, self.sub1.pubkey])
        if not events:
            return False

        if await self._deferred_to_our_own_orders(events):
            return False

        confirmed = await self._liquidation_confirmed()
        label = "liquidation" if confirmed else "position closed externally"
        for event in events:
            log.critical("%s: %s", label.upper(), event.describe())

        # Close everything still standing in the affected symbols, on both
        # accounts. Which side was hit does not change the answer -- the pair
        # is broken either way, and a lone leg is outright exposure.
        affected = {event.symbol for event in events}
        for symbol in affected:
            for session in (self.master, self.sub1):
                size = self.book.authoritative(session.pubkey, symbol)
                rounded = round_size(abs(size), self.feed.specs[symbol])
                if rounded < self.feed.specs[symbol].lot_size:
                    continue
                try:
                    # size > 0 is long, so closing it is a sell.
                    await session.market(symbol, size < 0, rounded, reduce_only=True)
                    log.critical(
                        "closed %s %.8f on %s after %s",
                        symbol, rounded, session.name, label,
                    )
                except Exception as exc:
                    log.critical(
                        "COULD NOT CLOSE %s on %s after %s: %s -- "
                        "close it by hand now",
                        symbol, session.name, label, describe(exc),
                    )

        reason = f"{label} -- " + "; ".join(event.describe() for event in events)
        self.notifier.send_soon(self.notifier.halted(reason))
        self._trigger_halt(reason)
        return True

    async def _deferred_to_our_own_orders(self, events) -> bool:
        """True when every event is explained by an order of ours in the dark.

        Only ever true once the exchange has answered with fresh positions. If
        that read fails the caller carries on to the halt, because at that
        point nothing is known -- not what happened, and not what is open.
        """
        in_doubt: set[str] = set()
        for session in (self.master, self.sub1):
            in_doubt |= session.symbols_in_doubt(DOUBT_WINDOW_S)

        now = time.monotonic()
        for symbol, seen in self._doubt_deferrals.items():
            self._doubt_deferrals[symbol] = [
                at for at in seen if now - at < DEFERRAL_WINDOW_S
            ]

        explainable = [
            event for event in events
            if event.symbol in in_doubt
            and len(self._doubt_deferrals.get(event.symbol, ())) < MAX_DOUBT_DEFERRALS
        ]
        if not explainable or len(explainable) != len(events):
            return False

        try:
            # Force a read rather than accept a cached one: replacing the guess
            # is the entire justification for not halting here.
            await self._sync_positions(max_age_s=0.0)
        except Exception as exc:  # noqa: BLE001 - then we know nothing at all
            log.critical(
                "could not re-read positions to tell whether %s was our own "
                "doing (%s) -- treating it as external",
                ", ".join(sorted({e.symbol for e in events})), describe(exc),
            )
            return False

        for event in explainable:
            self._doubt_deferrals.setdefault(event.symbol, []).append(now)
            log.warning(
                "%s -- but an order of ours in %s went unanswered, and a fresh "
                "read of both accounts has now replaced the guess. Carrying on "
                "rather than calling it a liquidation (%d/%d in the last "
                "%.0f minutes).",
                event.describe(), event.symbol,
                len(self._doubt_deferrals[event.symbol]), MAX_DOUBT_DEFERRALS,
                DEFERRAL_WINDOW_S / 60,
            )
            self.guard.reset_symbol(event.symbol)
            for session in (self.master, self.sub1):
                session.settled(event.symbol)
        return True

    def _make_position_handler(self, session: AccountSession):
        def handler(update) -> None:
            symbol = getattr(update, "symbol", None)
            if symbol not in self.symbols:
                return
            self.book.set_authoritative(session.pubkey, symbol, float(update.size or 0.0))

            # Cheap and synchronous -- no I/O, just arithmetic on the book.
            # Closing the survivor happens in the worker; a handler that
            # awaited an order would deadlock the socket it arrived on.
            if self.guard.check(
                self.book,
                self._phases_by_symbol(),
                [self.master.pubkey, self.sub1.pubkey],
            ):
                self._liquidation_seen.set()

        return handler

    def _make_snapshot_handler(self, session: AccountSession):
        def handler(snapshot) -> None:
            positions = [
                p for p in (getattr(snapshot, "positions", None) or [])
                if p.symbol in self.symbols
            ]
            self.book.apply_snapshot(session.pubkey, positions)

        return handler

    # -- workers -----------------------------------------------------------

    async def _hedge_worker(self) -> None:
        """Drains fill signals and restores neutrality, one symbol at a time."""
        while not self._stop.is_set():
            # Checked first and on every pass, including the idle one. A
            # liquidation leaves the survivor outright directional, so it must
            # not queue behind a hedge -- and it makes any pending hedge moot.
            if self._liquidation_seen.is_set():
                self._liquidation_seen.clear()
                await self._guard_liquidation(self._phases_by_symbol())
                continue

            try:
                symbol = await asyncio.wait_for(self._hedge_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            if symbol not in self.symbols:
                continue
            roles = self.leg_roles(symbol)
            try:
                await self.hedger.hedge(roles, mark_price=self.feed.reference_price(symbol))
            except HedgeLimitExceeded as exc:
                self._trigger_halt(f"hedge limit exceeded -- {exc}")
            except Exception as exc:
                # The reconciler re-derives from position, so a single failure
                # is recoverable; a persistent one trips the reject streak.
                log.error("hedge for %s failed: %s", symbol, describe(exc))

    def _trigger_halt(self, reason: str) -> None:
        if self._halt_reason is None:
            log.critical("HALT: %s", reason)
            self._halt_reason = reason
            self.title.halted(reason)
            # Fire-and-forget: the halt path has orders to cancel and positions
            # to flatten, and must not wait on Telegram to do it.
            self.notifier.send_soon(self.notifier.halted(reason))
            self._stop.set()

    async def _liquidation_confirmed(self) -> bool:
        """Did the exchange actually liquidate something? True if it cannot say.

        Fails safe on purpose. This is asked in the middle of whatever went
        wrong, which is when the query is least likely to answer, and "I could
        not ask" must land on the same side as "yes": closing a healthy pair
        costs a spread, while trading on into a real liquidation does not have
        a bounded cost.
        """
        for session in (self.master, self.sub1):
            try:
                events = await asyncio.to_thread(
                    recent_liquidations, self.config.http_url, session.pubkey
                )
            except Exception as exc:  # noqa: BLE001 - unreachable means unknown
                log.warning(
                    "could not confirm with the exchange whether %s was "
                    "liquidated (%s) -- treating it as one",
                    session.name, describe(exc),
                )
                return True
            if events:
                for event in events:
                    log.critical(
                        "exchange confirms %s on %s: %s",
                        event.get("eventType", "risk event"), session.name,
                        event.get("reason", ""),
                    )
                return True
        log.warning(
            "the exchange reports no liquidation on either account, so this "
            "was something else closing the position -- a manual close, or an "
            "order of ours we never saw the answer to"
        )
        return False

    async def _clear_orphans(self, symbol: str) -> None:
        """Cancel anything resting in `symbol` after a submission with no answer.

        A placement that times out may still have been accepted. The chaser
        never learns the order id -- the exception is raised before it is
        returned -- so on the next tick it places another, and the one before
        it stays on the book. Each timeout therefore adds a live order.

        A run ended that way: three chase steps timed out over thirty seconds,
        all three orders were resting, all three filled, and the leg opened at
        three times its size with nothing hedging it:

            HALT: hedge limit exceeded -- BTC-USD: required hedge 0.09597600
            exceeds ceiling 0.06398400 (maker=0.09597600, taker=0.00000000)

        Cancel-all is the right tool and is already used for this reason on
        startup and on halt: an order this bot cannot account for is an
        unhedged fill waiting to happen. The chaser re-places on the next tick,
        losing only queue position -- which is worth less than the risk of a
        leg opening at a multiple of its size.
        """
        session = self.sessions[self.leg_roles(symbol).maker]
        if symbol not in session.symbols_in_doubt(DOUBT_WINDOW_S):
            # The submission was answered -- a rejection is an answer -- so
            # nothing of ours can be resting unaccounted for.
            return
        try:
            await session.cancel_all([symbol])
            log.warning(
                "%s: an order in %s went unanswered, so anything resting there "
                "has been cancelled rather than left to fill unhedged",
                session.name, symbol,
            )
            session.settled(symbol)
        except Exception as exc:  # noqa: BLE001 - the next tick tries again
            log.error(
                "%s: could not clear a possibly-resting %s order (%s) -- the "
                "leg may open larger than its size",
                session.name, symbol, describe(exc),
            )

    # -- connection recovery -----------------------------------------------

    async def _healed(self, violations) -> bool:
        """Try to reconnect a dropped socket. True if anything was restored.

        A dropped socket was treated as fatal, which made a cycle only as long
        as the exchange's least reliable minute -- two live runs ended this way
        mid-cycle, having done nothing wrong. It is a transient condition and
        deserves a retry before the kill switch.

        `disconnected` and `stale_stream` are both retried; every other
        violation -- exposure over the cap, a position too large, a streak of
        rejections -- says the strategy itself is misbehaving, and reconnecting
        would not address any of them.

        `stale_stream` used to halt outright, and that was the more damaging of
        the two. A socket whose peer vanished without a close frame still reads
        as connected, so the silence is all there is to go on -- and the
        watchdog fires at `ws_stale_timeout_s` (30s by default) while the
        library's own keepalive needs `ping_interval + ping_timeout` (80s) to
        notice. The stale check therefore always won, and a condition a
        reconnect fixes in two seconds was killing runs instead.

        Halting is still the outcome when the retry fails: an unattended bot
        that cannot see its fills must not keep resting orders on the book.

        And it is the outcome for a socket that keeps flapping. Healing without
        a limit would reconnect forever: no rejection is recorded, so the reject
        streak never trips, and the cycle would never finish while the log
        scrolled past unread. The pair stays hedged throughout -- the reconciler
        works over HTTP -- so this is about not hiding a persistent fault, not
        about exposure.

        "Keeps flapping" is measured over `RECONNECT_WINDOW_S`, so a socket that
        blips once an hour is forgiven each time and one that blips five times in
        ten minutes is not.
        """
        RECOVERABLE = ("disconnected", "stale_stream")
        dropped = [v for v in violations if v.kind in RECOVERABLE]
        if not dropped or len(dropped) != len(violations):
            return False

        now = time.monotonic()
        self._reconnect_times = [
            t for t in self._reconnect_times if now - t < RECONNECT_WINDOW_S
        ]
        if len(self._reconnect_times) >= MAX_RECONNECTS:
            log.error(
                "the socket has dropped %d times in the last %g minutes -- "
                "not reconnecting again",
                len(self._reconnect_times),
                RECONNECT_WINDOW_S / 60,
            )
            return False
        self._reconnect_times.append(now)

        restored = False
        failed = []
        stale_after = self.risk.config.ws_stale_timeout_s
        for session in (self.master, self.sub1):
            if session.dry_run:
                continue
            # `is_connected` alone is not enough: a half-open socket reports
            # connected and delivers nothing, which is the case this exists for.
            silent_for = session.last_message_age_s
            if session.is_connected and silent_for <= stale_after:
                continue
            if session.is_connected:
                log.warning(
                    "%s: WebSocket has delivered nothing for %.0fs but still "
                    "reads as connected -- reconnecting",
                    session.name, silent_for,
                )
            else:
                log.warning("%s: WebSocket dropped -- trying to reconnect", session.name)
            if await session.reconnect():
                restored = True
            else:
                failed.append(session)
                log.error("%s: could not reconnect", session.name)

        # The sessions are tried one after another, so an outage that ends
        # midway leaves the earlier one having spent its attempts against a
        # network that was still down. One socket coming back is proof the
        # network did, so anything that failed before that gets another go --
        # free, because the budget is charged per heal, not per attempt.
        #
        # This is what a DNS outage did: the master used its three attempts
        # between 15:09:30 and 15:09:35 and lost all of them, sub1 succeeded at
        # 15:09:42, and the halt fired a second later on a master that nobody
        # had tried again.
        if restored and failed:
            for session in failed:
                log.warning(
                    "%s: another socket is back, so the network is -- retrying",
                    session.name,
                )
                if await session.reconnect():
                    restored = True

        if restored:
            # The socket missed whatever happened while it was down, so the
            # next decision must be made on exchange truth rather than on a
            # book that stopped being updated. Read over HTTP, which did not
            # drop, rather than waiting for the stream to refill the book.
            sync_positions_http([self.master, self.sub1], self.book)
        return restored

    # -- phase driver ------------------------------------------------------

    # -- supervisor --------------------------------------------------------

    async def _supervise(self) -> None:
        """Safety and reconciliation for the pair, while the legs run themselves.

        One loop, not one per leg: the risk limits are about the pair, the
        position read is a single pair of HTTP calls, and running either twice
        as often would buy nothing.
        """
        last_reconcile = 0.0
        while not self._stop.is_set():
            violations = self.risk.check()
            if violations and await self._healed(violations):
                violations = self.risk.check()
            if violations:
                self._trigger_halt("; ".join(str(v) for v in violations))
                return

            now = time.monotonic()
            if now - last_reconcile >= self.config.reconcile_interval_s:
                last_reconcile = now
                # Re-read positions from the exchange before correcting against
                # them. The in-memory book is fed by position updates, and the
                # optimistic fill overlay expires -- so if those updates stall,
                # the book decays toward zero and would otherwise make the bot
                # believe it is flat while real positions are still open.
                try:
                    await self._sync_positions()
                except Exception as exc:  # noqa: BLE001 - retried next tick
                    log.error("position sync failed: %s", describe(exc))

                # Before hedging, not after. If a position was liquidated, the
                # hedge rule's answer is to open a fresh one on the account that
                # still holds something -- exactly the wrong response.
                if await self._guard_liquidation(self._phases_by_symbol()):
                    return

                try:
                    corrections = await reconcile_net(
                        self.hedger, self._live_roles(), self.feed
                    )
                    for correction in corrections:
                        log.info("reconciler corrected %s", correction)
                except HedgeLimitExceeded as exc:
                    self._trigger_halt(f"hedge limit exceeded -- {exc}")
                    return
                except Exception as exc:  # noqa: BLE001 - retried next tick
                    log.error("reconcile failed: %s", describe(exc))

                self.risk.log_exposure()
                self._log_untradeable_residuals()

            await asyncio.sleep(self.config.chase_interval_s)

    async def _sync_positions(self, max_age_s: float = POSITION_FRESHNESS_S) -> None:
        """Read positions from the exchange, sharing one read between callers.

        The legs and the supervisor all want the same numbers, and they ask on
        their own schedules. The lock makes a burst of callers wait on the first
        one's read rather than each issuing its own, which is what a live run
        showed happening: the same pair of account fetches three times over.

        Still the exchange's answer, not a guess -- only the request is shared.
        """
        async with self._sync_lock:
            if time.monotonic() - self._synced_at < max_age_s:
                return
            await sync_positions(self.sessions.values(), self.book)
            self._synced_at = time.monotonic()

    def _phases_by_symbol(self) -> dict[str, Phase]:
        return {symbol: self.state.leg(symbol).phase for symbol in self.symbols}

    # -- one leg's chase loop ----------------------------------------------

    async def _drive_leg(self, symbol: str, is_done, label: str) -> None:
        """Keep one leg's order near the market until `is_done()`.

        Only this leg. The other is a separate task at its own phase, which is
        the whole point: a filled leg must not wait on an unfilled one.
        """
        leg = self.state.leg(symbol)
        started = time.monotonic()
        budget = phase_budget_s(leg.phase, self.config.max_phase_minutes)

        while not self._stop.is_set():
            if budget and time.monotonic() - started > budget:
                self._trigger_halt(
                    f"{symbol} {label} did not finish within "
                    f"{self.config.max_phase_minutes:g} minutes"
                )
                return

            if leg.phase != Phase.HOLD and not leg.complete:
                try:
                    await self.chaser.step(self.leg_roles(symbol), leg)
                except Exception as exc:  # noqa: BLE001 - retried next tick
                    log.error("chase step for %s failed: %s", symbol, describe(exc))
                    await self._clear_orphans(symbol)

            self._persist()

            if is_done() and await self._confirm_done(is_done, f"{symbol} {label}"):
                log.info("%s %s complete", symbol, label)
                return

            await asyncio.sleep(self.config.chase_interval_s)

    def _leg_is_neutral(self, symbol: str) -> bool:
        """Whether this leg has no hedge left that could actually be placed.

        A residual smaller than one lot or below the market's minimum notional
        cannot be traded away, so it does not count as outstanding work -- if it
        did, the phase would wait forever on a hedge that can never be
        submitted. Such residuals are real exposure and are reported by
        `_log_untradeable_residuals`; the USD risk limits still police them.
        """
        roles = self.leg_roles(symbol)
        if abs(self.hedger.in_flight.total(symbol)) > 0:
            return False
        price = self.feed.reference_price(symbol)
        return self.hedger.actionable_hedge(roles, price) <= 0

    def _leg_is_flat(self, symbol: str) -> bool:
        spec = self.feed.specs[symbol]
        return all(
            abs(self.book.authoritative(session.pubkey, symbol)) < spec.lot_size
            for session in (self.master, self.sub1)
        )

    # -- one leg's phases --------------------------------------------------

    async def _leg_open(self, symbol: str, target_size: float) -> None:
        leg = self.state.leg(symbol)
        leg.phase = Phase.OPEN
        leg.complete = False
        leg.oid = None
        leg.target_size = target_size
        leg.hold_until = 0.0
        roles = self.leg_roles(symbol)
        log.info(
            "=== %s OPEN: maker=%s taker=%s target=%g ===",
            symbol, self.sessions[roles.maker].name,
            self.sessions[roles.taker].name, target_size,
        )
        self._persist()

        await self._drive_leg(
            symbol,
            lambda: leg.complete and self._leg_is_neutral(symbol),
            "open",
        )

    async def _leg_hold(self, symbol: str) -> None:
        leg = self.state.leg(symbol)
        if not leg.hold_until:
            # Drawn per leg and stored as a deadline, so a restart mid-hold
            # resumes this hold rather than rolling a fresh one -- and so a leg
            # that filled first starts counting first.
            leg.hold_until = time.time() + self.config.hold_minutes.pick() * 60
        leg.phase = Phase.HOLD
        self._persist()

        log.info("=== %s HOLD: %.1f minutes ===", symbol, leg.hold_remaining_s() / 60)
        await self._drive_leg(symbol, lambda: leg.hold_remaining_s() <= 0, "hold")

    async def _leg_exit(self, symbol: str) -> None:
        leg = self.state.leg(symbol)
        # Pull the entry order before reversing roles: it would fight the close.
        if leg.oid:
            await self.chaser.cancel_leg(self.leg_roles(symbol), leg)

        leg.phase = Phase.EXIT
        leg.complete = False
        leg.oid = None
        # The exit target is whatever is actually held, not the configured size:
        # the entry may have filled only partially.
        roles = self.leg_roles(symbol)
        leg.target_size = abs(self.book.effective(roles.maker, symbol))
        log.info("=== %s EXIT: %g to close ===", symbol, leg.target_size)
        self._persist()

        # The closing limit order only unwinds the maker side. The taker side is
        # reduced by hedges, and hedges stop once the pair is neutral -- so if
        # partial fills and lot rounding leave the two accounts differing by
        # less than one lot while both are still non-zero, no hedge will ever
        # fire and the taker's residual would sit there forever. Completion
        # therefore waits on the maker leg, then sweeps the rest.
        await self._drive_leg(symbol, lambda: leg.complete, "exit")

        if not self._stop.is_set() and not self._leg_is_flat(symbol):
            log.info("%s: closing residual left after the exit leg", symbol)
            await flatten(self.sessions, self.book, self.feed, [symbol])

    # -- one leg's cycle ---------------------------------------------------

    async def _run_leg(self, symbol: str, configured_size: float) -> None:
        """OPEN -> HOLD -> EXIT, repeatedly, for one leg on its own clock."""
        leg = self.state.leg(symbol)

        while not self._stop.is_set():
            if self.config.cycles and leg.cycle_index >= self.config.cycles:
                return
            reached = await self._target_reached()
            if reached:
                log.info("%s: execution target reached: %s", symbol, reached)
                return

            # A restart lands mid-cycle, so each phase is entered only if this
            # leg has not already passed it.
            if leg.phase in (Phase.IDLE, Phase.COMPLETE):
                leg.cycle_index += 1
                # Shown as one number, so it follows whichever leg is ahead.
                self.state.cycle_index = max(
                    self.state.cycle_index,
                    *(self.state.leg(s).cycle_index for s in self.symbols),
                )
                self.title.set_cycle(self.state.cycle_index)
                log.info("=== %s cycle %d ===", symbol, leg.cycle_index)
                await self._leg_open(symbol, configured_size)

            if self._stop.is_set():
                return
            if leg.phase == Phase.OPEN:
                await self._leg_hold(symbol)

            if self._stop.is_set():
                return
            if leg.phase == Phase.HOLD:
                await self._leg_exit(symbol)

            if self._stop.is_set():
                return
            leg.phase = Phase.COMPLETE
            leg.hold_until = 0.0
            self._persist()
            log.info("=== %s cycle %d complete ===", symbol, leg.cycle_index)
            # This leg legitimately went to zero, so its peaks would read as an
            # external close on the next entry.
            self.guard.reset_symbol(symbol)
            await self.notifier.cycle_complete(
                cycle=leg.cycle_index,
                of=self.config.cycles or None,
                detail=f"{symbol}: {await self.progress_detail()}",
            )

    async def _confirm_done(self, is_done, label: str) -> bool:
        """Re-check a completion claim against freshly fetched positions.

        Finishing a phase is irreversible in effect -- EXIT completing means the
        cycle is recorded as closed and the next one may open on top. That
        decision must never rest on a position book that could have gone stale,
        so it is confirmed against the exchange before being acted on.
        """
        try:
            await self._sync_positions()
        except Exception as exc:
            log.error("could not verify %s completion: %s", label, exc)
            return False

        if is_done():
            return True

        log.warning(
            "%s looked complete but the exchange disagrees -- continuing", label
        )
        return False

    def _log_untradeable_residuals(self) -> None:
        for roles in self._live_roles():
            price = self.feed.reference_price(roles.symbol)
            residual = self.hedger.untradeable_residual(roles, price)
            if residual:
                spec = self.feed.specs[roles.symbol]
                log.warning(
                    "%s carries %+.8f of unhedgeable exposure ($%s): below the "
                    "%s minimum notional of $%s. Increase the leg size so partial "
                    "fills clear that floor.",
                    roles.symbol,
                    residual,
                    round_notional(abs(residual) * (price or 0.0)),
                    roles.symbol,
                    spec.min_notional,
                )

    def _net_verdict(self, symbol: str, net: float) -> str:
        """How to describe a leftover imbalance, in the same terms as the hedger.

        Three outcomes, not two. A residual can be too small to trade -- under a
        lot, or worth less than the market's minimum order -- and calling that
        "NOT hedged" is alarming out of all proportion: a live stop reported
        $0.23 of BTC dust that way, in the same breath as the next line calling
        the very same amount unhedgeable. The operator reads a stop message as
        "am I exposed", and the honest answer for dust is no, not really.

        So the threshold is the one the hedger actually acts on, rather than a
        lot size that happens to be nearby. Anything it could close and has not
        is still reported loudly, because that one does need closing.
        """
        spec = self.feed.specs[symbol]
        if abs(net) < spec.lot_size:
            return ""

        price = self.feed.reference_price(symbol) or 0.0
        value = abs(net) * price
        if price and value < spec.min_notional:
            return (
                f" -- ${round_notional(value)} of dust, below {symbol}'s "
                f"${spec.min_notional} minimum order, so it cannot be closed"
            )
        return " -- NOT hedged"

    def _log_open_positions(self) -> None:
        """Say what is still open, in the log, at the moment the run ends.

        The first question after a stop is "what am I holding now". Answering it
        here means the log file alone answers it, without reconnecting to ask.
        """
        for symbol in self.symbols:
            leg = self.state.legs.get(symbol)
            phase = leg.phase.name if leg else "IDLE"
            cycle = leg.cycle_index if leg else 0
            held = {
                session.name: self.book.authoritative(session.pubkey, symbol)
                for session in (self.master, self.sub1)
            }
            net = sum(held.values())
            if any(abs(v) > 0 for v in held.values()):
                log.warning(
                    "%s left open after cycle %d in %s: %s, net %+.8f%s",
                    symbol,
                    cycle,
                    phase,
                    ", ".join(f"{name} {size:+.8f}" for name, size in held.items()),
                    net,
                    self._net_verdict(symbol, net),
                )
            else:
                log.info("%s flat after cycle %d", symbol, cycle)

    # -- entry point -------------------------------------------------------

    def status_lines(self) -> list[str]:
        """What a person watching the run would want on screen.

        Built from state already in memory -- phases, the position book, the
        last progress read -- because this is redrawn every second and must not
        cost a network round trip to do it.
        """
        from .screen import bar, humanise

        legs = []
        for symbol in self.symbols:
            leg = self.state.leg(symbol)
            phase = leg.phase.value.upper()
            left = leg.hold_remaining_s()
            note = f" {humanise(left)} left" if left > 0 else ""
            legs.append(f"{symbol} {phase} cycle {leg.cycle_index}{note}")

        burned, volume = self._progress
        target = self.config.target
        lines = ["  " + "     ".join(legs)]

        if target.volume_usd > 0:
            done = volume / target.volume_usd
            lines.append(
                f"  volume  {bar(done)}  ${volume:,.0f} / ${target.volume_usd:,.0f}"
                f"  {done * 100:.1f}%"
            )
        if target.burn_usd > 0:
            done = burned / target.burn_usd
            lines.append(
                f"  burn    {bar(done)}  ${burned:,.2f} / ${target.burn_usd:,.2f}"
                f"  {done * 100:.1f}%"
            )

        exposure = sum(self.risk.net_exposure_usd(s) for s in self.symbols)
        elapsed = humanise(time.monotonic() - self._started_at)
        note = self._halt_reason or self._stop_requested or "S = stop and cancel"
        lines.append(f"  burned ${burned:,.2f}   off-hedge ${exposure:,.0f}   "
                     f"running {elapsed}   |   {note}")
        return lines

    async def _refresh_status(self, interval_s: float = 1.0) -> None:
        """Keep the status block current while the legs work."""
        from .screen import SCREEN

        while not self._stop.is_set():
            try:
                SCREEN.update(self.status_lines())
            except Exception as exc:  # noqa: BLE001 - never stop a run over a redraw
                log.debug("could not redraw the status block: %s", describe(exc))
            await asyncio.sleep(interval_s)

    async def run(self) -> None:
        """Run both legs, each on its own clock, until they finish or a halt.

        The legs were once driven in lockstep by a single loop, which made each
        wait for the slowest: a filled leg could not start its hold until the
        other filled, and a closed leg could not re-open until the other closed.
        Nothing required that -- the hedge is computed per symbol -- so they now
        run as separate tasks and the shared work moves to `_supervise`.
        """
        self.install_handlers()
        worker = asyncio.create_task(self._hedge_worker())
        supervisor = asyncio.create_task(self._supervise())
        status = asyncio.create_task(self._refresh_status())
        sizes = {
            self.config.master_account.symbol: self.config.master_account.size,
            self.config.sub_account.symbol: self.config.sub_account.size,
        }
        legs: list[asyncio.Task] = []

        try:
            await self._recover()
            # After recovery, so an interrupted run is recognised as one and
            # keeps the count it already had.
            await self.capture_target_baseline()

            legs = [
                asyncio.create_task(self._run_leg(symbol, sizes[symbol]))
                for symbol in self.symbols
            ]
            # One leg raising must not leave the other trading on alone, so the
            # first exception cancels the rest before it propagates.
            done, pending = await asyncio.wait(
                legs, return_when=asyncio.FIRST_EXCEPTION
            )
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
            await asyncio.gather(*pending, return_exceptions=True)

            if self._stop_requested:
                # The legs left their loops without reaching the end of a phase,
                # so whatever was resting is still resting. Pull it: an order
                # left working after the bot exits fills with nothing watching,
                # and the hedge that would answer it is no longer running.
                log.warning(
                    "stopped on %s -- cancelling resting orders", self._stop_requested
                )
                await cancel_all_orders([self.master, self.sub1], self.symbols)
                self._log_open_positions()

            if self._halt_reason:
                raise Halted(self._halt_reason)

            # Reached the end on its own terms, so the goal is spent and the
            # next start measures a new one. Kept after a stop or a halt
            # instead: those are interruptions, and resuming should not hand
            # back progress that was already paid for.
            self.state.clear_baseline()
            self._persist()

        except Halted as exc:
            await self._emergency_stop(str(exc))
            raise
        except asyncio.CancelledError:
            log.warning("interrupted -- cancelling strategy orders")
            await cancel_all_orders([self.master, self.sub1], self.symbols)
            raise
        finally:
            self._stop.set()
            for task in (*legs, supervisor, worker, status):
                task.cancel()
            # The block stops being redrawn here, so whatever is under the
            # cursor is what the operator is left looking at.
            from .screen import SCREEN

            SCREEN.close()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(
                    *(*legs, supervisor, worker), return_exceptions=True
                )

    async def progress_detail(self) -> str:
        """Spend and volume so far, for a notification and the window title.

        Public because `cmd_run` reports it when a run finishes, and reaching
        across a module boundary for an underscored name is how a private
        method becomes an interface nobody agreed to.

        Reads the same fill history the execution target uses. A failure is not
        worth surfacing -- this is a progress line, not a control input -- so it
        degrades to an empty string and the cycle notification simply omits it.
        """
        target = self.config.target
        if not target.measures_fills:
            return ""
        try:
            totals = await self._read_totals()
        except Exception as exc:  # noqa: BLE001 - cosmetic
            log.debug("could not read totals for the progress line: %s", exc)
            return ""

        # Against the same baseline the target uses, or the notification would
        # report one number while the stop rule acted on another.
        burned = _burned(totals.fees_usd - self.state.baseline_fees_usd)
        volume = totals.qualifying_volume_usd - self.state.baseline_volume_usd

        parts = []
        if target.burn_usd > 0:
            parts.append(f"burn ${burned:,.4f} / ${target.burn_usd:,.2f}")
        if target.volume_usd > 0:
            parts.append(f"volume ${volume:,.2f} / ${target.volume_usd:,.2f}")
        # Cached for the status block, which redraws every second and must not
        # pay for a fill-history walk to do it.
        self._progress = (burned, volume)

        detail = "  ".join(parts)
        if detail:
            self.title.set_note(detail)
        return detail

    async def _read_totals(self):
        """The fill history, read without stopping everything else.

        `realised_for_tree` is synchronous `requests`, and this is called from
        inside the event loop -- between cycles, and once at the start of a run.
        Called directly it froze the loop for the length of the walk: measured
        at 2.9-3.8s against a 575-fill account, during which the chaser placed
        nothing, the hedge worker ran not at all, and fills arriving on the
        socket sat unprocessed. It gets worse as the history grows; the walk is
        paginated a thousand fills at a time.

        A thread keeps the loop turning. The same pattern `reconcile` already
        uses for its HTTP position read.
        """
        return await asyncio.to_thread(
            realised_for_tree, self.master.http, [self.master.pubkey, self.sub1.pubkey]
        )

    async def capture_target_baseline(self) -> None:
        """Record where the fill history stood, so the target counts from now.

        Taken once per run. If the state file already carries one, this run is
        the continuation of an interrupted one and keeps its count -- restarting
        after a crash should not hand back the progress already paid for.

        A read that fails leaves no baseline, and `_target_reached` treats a
        missing baseline as "cannot judge yet" rather than as zero. Treating it
        as zero would compare this run against the account's whole lifetime and
        stop immediately, which is the bug this replaces.
        """
        target = self.config.target
        if not target.measures_fills or self.state.has_baseline:
            return
        try:
            totals = await self._read_totals()
        except Exception as exc:  # noqa: BLE001 - never block trading on this
            log.warning("could not read fill history to start the target: %s", describe(exc))
            return

        self.state.baseline_fees_usd = totals.fees_usd
        self.state.baseline_volume_usd = totals.qualifying_volume_usd
        self.state.baseline_at = time.time()
        self._persist()
        log.info(
            "execution target counts from now: $%.2f burned / $%.2f volume "
            "already on the account do not count toward it",
            _burned(totals.fees_usd),
            totals.qualifying_volume_usd,
        )

    async def _target_reached(self) -> str | None:
        """Whether a spend or volume target has been met, as a reason string.

        Checked between cycles only. A target reached halfway through an open
        position is not a reason to abandon it -- the exit has to run, or the
        pair is left directional.

        Measured as the distance travelled since `capture_target_baseline`, not
        as the account's lifetime total. Totals still come from the exchange's
        fill history rather than a local counter, so the figure survives a
        restart and matches what was actually charged.

        A failure to read it is logged and treated as "not reached": refusing to
        trade because a read-only endpoint is down would be worse than
        overshooting a soft goal by one cycle.
        """
        target = self.config.target
        if not target.measures_fills or not self.state.has_baseline:
            return None

        try:
            totals = await self._read_totals()
        except Exception as exc:  # noqa: BLE001 - never block trading on this
            log.warning("could not read fill history for the execution target: %s", describe(exc))
            return None

        burned = _burned(totals.fees_usd - self.state.baseline_fees_usd)
        volume = totals.qualifying_volume_usd - self.state.baseline_volume_usd

        if target.burn_usd > 0 and burned >= target.burn_usd:
            return f"burned ${burned:,.4f} of ${target.burn_usd:,.2f}"
        if target.volume_usd > 0 and volume >= target.volume_usd:
            return (
                f"qualifying volume ${volume:,.2f} of ${target.volume_usd:,.2f}"
            )

        if target.burn_usd > 0:
            log.info("burn progress: $%.4f / $%.2f", burned, target.burn_usd)
        if target.volume_usd > 0:
            log.info(
                "volume progress: $%.2f / $%.2f qualifying "
                "(of which $%.2f traded between your own accounts)",
                volume,
                target.volume_usd,
                totals.self_trade_volume_usd,
            )
        return None

    async def _recover(self) -> None:
        """Reconcile persisted intent against exchange truth before trading.

        Truth comes from HTTP rather than the stream: at this point the account
        snapshot may not have arrived, and acting on an empty book would look
        exactly like having no positions.
        """
        sync_positions_http([self.master, self.sub1], self.book)

        if self.state.phase == Phase.HALTED:
            # Deliberately not cleared automatically, even though the per-leg
            # pass below would find both accounts flat. A halt means something
            # went wrong; restarting past it without the operator having read
            # the reason is how the same fault repeats unseen.
            raise RuntimeError(
                f"state file records a halt: {self.state.halted_reason}. "
                "Read that reason first. Then `run.bat flatten --live` to clear "
                "it -- that cancels every order, closes both accounts "
                "reduce-only (a no-op when they are already flat), and resets "
                "the state to IDLE."
            )

        # Decided per leg, because legs recover independently: one may have
        # been mid-entry while the other was already unwinding.
        for symbol in self.symbols:
            leg = self.state.leg(symbol)
            spec = self.feed.specs[symbol]
            leg_has_positions = any(
                abs(self.book.authoritative(session.pubkey, symbol)) >= spec.lot_size
                for session in (self.master, self.sub1)
            )

            if not leg_has_positions:
                if leg.phase not in (Phase.IDLE, Phase.COMPLETE):
                    log.warning(
                        "%s: state says %s but both accounts are flat -- starting clean",
                        symbol, leg.phase.value,
                    )
                leg.phase = Phase.IDLE
                leg.hold_until = 0.0
                continue

            log.warning(
                "%s: recovered open positions with phase=%s", symbol, leg.phase.value
            )
            if leg.phase in (Phase.IDLE, Phase.COMPLETE):
                # Positions exist that this bot has no plan for. Unwinding is
                # the only safe interpretation -- resuming an entry would add
                # to a position of unknown provenance.
                log.warning("%s: positions exist with no recorded plan -- exiting", symbol)
                leg.phase = Phase.HOLD
                leg.hold_until = 0.0

        self.state.phase = self.state.summary_phase
        self.state.hold_until = 0.0

        # Resting orders cannot be reliably matched to the recovered plan, and
        # an unrecognised order is an unhedged fill waiting to happen.
        await cancel_all_orders([self.master, self.sub1], self.symbols)
        for leg in self.state.legs.values():
            leg.oid = None
            leg.price = None
            leg.size = None
            leg.stale_oids = []

        # Correct any exposure inherited from the previous process.
        corrections = await reconcile_net(
            self.hedger, self._live_roles(), self.feed
        )
        for correction in corrections:
            log.warning("recovery corrected %s", correction)

        self._persist()

    async def _emergency_stop(self, reason: str) -> None:
        log.critical("emergency stop: %s", reason)
        self.state.phase = Phase.HALTED
        self.state.halted_reason = reason
        self._persist()

        await cancel_all_orders([self.master, self.sub1], self.symbols)
        await flatten(self.sessions, self.book, self.feed, self.symbols)
        self._persist()


def build_chase_params(config: Config) -> dict[str, ChaseParams]:
    return {
        leg.symbol: ChaseParams(
            offset_bps=leg.offset_bps,
            max_distance_bps=leg.max_distance_bps,
            max_order_size=leg.max_order_size,
            chase_patience_s=leg.chase_patience_s,
            improve_ticks=leg.improve_ticks,
        )
        for leg in (config.master_account, config.sub_account)
    }


def build_hedge_ceilings(config: Config) -> dict[str, float]:
    """Cap a single hedge at twice the leg's configured size.

    A required hedge larger than this means the position book and reality have
    diverged by more than the strategy can account for, and firing a very large
    market order on that basis would be worse than halting.
    """
    return {leg.symbol: leg.size * 2.0 for leg in (config.master_account, config.sub_account)}

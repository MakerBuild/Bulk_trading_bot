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
import random
import time

from bulk_api.common import Side, Topic

from .accounts import AccountSession
from .chaser import ChaseParams, Chaser
from .config import Config
from .feed import MarketFeed
from .fees import burned_usd as _burned
from .fees import realised_for_trees
from .hedger import Hedger, HedgeLimitExceeded, LegRoles
from .liquidation import LiquidationGuard, recent_liquidations
from .marketdata import round_notional, round_size, touch_text
from .sizing import draw_sizes, resolve_notionals
from .notify import Notifier
from .accounts import short_pubkey
from .pairing import Group, Pairing
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

# How many times in a row the supervisor will try to heal before halting.
#
# One pass was not enough, and a live log showed why: sub1's socket closed,
# the heal began, and nine seconds into its reconnect the master's socket
# closed too. The pass had already looked at the master and found it
# healthy, so it finished, the re-check found the master down, and the run
# halted on a drop that had never been offered a retry.
#
# That is not a rare shape. One flaky link carries both sockets, so the
# second drop lands DURING the first repair more often than not.
#
# Bounded rather than a loop: a pass that restores nothing ends it, and the
# reconnect budget over RECONNECT_WINDOW_S still governs a socket that
# flaps. This only covers drops arriving inside one repair.
MAX_HEAL_PASSES = 3

# How long a position read stays good enough to share. Three callers now want
# one -- the supervisor and each leg confirming a phase -- and a live run showed
# them fetching the same numbers three times within the same second. Short
# enough that nobody acts on anything stale, long enough to collapse a burst.
POSITION_FRESHNESS_S = 0.5

# How long an execution-target reading stays good enough to reuse.
#
# The read walks the paginated fill history of every account the run
# trades -- measured at 2.9-3.8 seconds against a single 575-fill account,
# and a pool has a hundred of them. A configured leg asks once per cycle,
# which is fine. The group dispatcher asks on every pass of its loop, and
# that loop turns every chase_interval_s: without this it would start a
# hundred paginated history walks twice a second, against an exchange that
# answered 429 to two accounts polling every five seconds.
#
# The cost is overshooting a target by up to this much trading. The target
# is already soft by design -- it is checked between cycles, never mid-
# position -- so a late stop is the same kind of late it already was.
TARGET_FRESHNESS_S = 30.0


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
        sessions: list[AccountSession] | None = None,
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

        # Every account this run can trade. Two in single and multi mode --
        # the master and its sub -- and every account under every key in pool
        # mode. `master` and `sub1` stay named because the commands that act on
        # one account still mean those two.
        pool = sessions if sessions else [master, sub1]
        self.sessions: dict[str, AccountSession] = {s.pubkey: s for s in pool}
        self.symbols = [leg.symbol for leg in config.active_legs]
        self._seen_trades = SeenTrades()
        self.guard = LiquidationGuard(
            specs=feed.specs,
            names={s.pubkey: s.name for s in self.sessions.values()},
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
        # How many fills arrived that could not be attributed to an account.
        # Counted rather than merely logged: if this is not zero at the end of
        # a run, the book was being rebuilt from HTTP rather than followed.
        self._unattributed_fills = 0
        # Set when something happened that the book did not see. Cleared only
        # by a successful read from the exchange, and no hedge is sent while
        # it stands.
        self._book_suspect = False
        # The last execution-target answer, and when it was read.
        self._target_answer: tuple[float, str | None] = (0.0, None)
        # The largest size each leg may be drawn at, captured after the
        # margin plan has had its say. A range is written in the settings
        # file, but what the accounts can actually carry is decided at
        # startup and may be smaller -- so a draw is clamped to what was
        # planned rather than to what was asked for.
        self._size_ceiling = {leg.symbol: leg.size for leg in config.active_legs}
        # Groups currently trading, by the leg key each was given. Empty
        # in single and multi mode, where the legs come from the config
        # and never change hands.
        self._groups: dict[str, Group] = {}
        self._group_ids: dict[str, int] = {}
        self.pairing: Pairing | None = None
        # Groups `_recover` brought back, for the dispatcher to start. None
        # until recovery has run.
        self._restored: list[tuple[int, Group, str]] | None = None
        # Set while the liquidation guard closes positions out. See
        # `_hedging_suspended`.
        self._closing_out = False
        # Seeded from the system clock, and held rather than using the
        # module-level generator: a run that has to be reproduced can be
        # given a seed here without reaching into every other user of
        # `random` in the process.
        self._rng = random.Random()

    @property
    def all_sessions(self) -> list[AccountSession]:
        """Every account this run trades, in pool order.

        Falls back to the named pair when no session map has been built. That
        is not only for tests: `_recover` and the halt path run before and
        after the map exists, and both of them enumerate accounts.
        """
        sessions = getattr(self, "sessions", None)
        if sessions:
            return list(sessions.values())
        return [s for s in (getattr(self, "master", None), getattr(self, "sub1", None)) if s]

    @property
    def _hedging_suspended(self) -> bool:
        """Whether the run is taking positions down rather than hedging them.

        True from the moment the liquidation guard starts closing, and for the
        whole of a halt -- whose emergency stop flattens every account while the
        worker is still running. A hedge in either window answers each close
        with a new position the other way.
        """
        return getattr(self, "_closing_out", False) or (
            getattr(self, "_halt_reason", None) is not None
        )

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
        exiting = phase == Phase.EXIT
        roles = []
        # Each leg is named for the account that opens it, which is what the
        # config keys mean: the master opens the first, the sub-account the
        # second. In EXIT the two swap, because the account that hedged a long
        # by going short is the one holding the short to cover. HOLD reuses the
        # OPEN roles, so drift is corrected on the account that was hedging.
        for index, leg in enumerate(self.config.active_legs):
            opener, hedger = (master, sub1) if index == 0 else (sub1, master)
            if exiting:
                opener, hedger = hedger, opener
            roles.append(
                LegRoles(
                    leg.symbol,
                    maker=opener,
                    taker=hedger,
                    maker_is_buy=True,
                    reduce_only=exiting,
                )
            )
        return roles

    def _roles_by_symbol(self, phase: Phase) -> dict[str, LegRoles]:
        return {roles.symbol: roles for roles in self.roles_for(phase)}

    def leg_roles(self, symbol: str) -> LegRoles:
        """Roles for one symbol, at the phase that symbol is actually in.

        Legs advance independently, so asking for "the" phase is meaningless
        once one is exiting while the other is still opening.
        """
        return self._roles_by_symbol(self.state.leg(symbol).phase)[symbol]

    def _live_roles(self) -> list[LegRoles]:
        """Roles for every leg actually trading, each at its own phase.

        The drawn groups, and the configured legs only when there are no
        groups. This used to be the configured legs always -- the named pair,
        by symbol -- which in a pool is two accounts out of however many and
        usually not the two that hold anything.

        The reconciler runs off this list. A live run opened $138 on two group
        makers, and the reconciler looked at the named pair, found it flat,
        and reported nothing to correct for three minutes while the pair sat
        directional.
        """
        if self._groups or self.pairing is not None:
            # A pool run with no group live has nothing to reconcile -- not
            # the configured pair, which in a pool is two accounts that may
            # each belong to a group that has not been restored yet.
            return [
                roles
                for roles in (self._roles_for_key(key) for key in self._groups)
                if roles is not None
            ]
        return [self.leg_roles(symbol) for symbol in self.symbols]

    def _name_of(self, pubkey: str) -> str:
        """An account's short name, or its pubkey when it has no session."""
        session = self.sessions.get(pubkey) if hasattr(self, "sessions") else None
        return session.name if session is not None else short_pubkey(pubkey)

    def _live_keys(self) -> list[str]:
        """The legs that are trading: the drawn groups, else the configured."""
        return list(self._groups) or list(self.symbols)

    def _key_for_account(self, pubkey: str, symbol: str) -> str | None:
        """Which leg this account is trading in this market, if any.

        An account is in at most one group, so this is a lookup and not a
        search for the best answer. Returns None for an account that is not
        in a group -- which, when no group exists at all, means the caller
        should fall back to the configured leg named by the symbol.
        """
        for key, group in self._groups.items():
            if group.symbol == symbol and pubkey in group.accounts:
                return key
        return None

    # -- handlers (synchronous; see module docstring) ----------------------

    def install_handlers(self) -> None:
        for session in self.all_sessions:
            session.on(Topic.FILL, self._make_fill_handler(session))
            session.on(Topic.POSITION, self._make_position_handler(session))
            session.on(Topic.ACCOUNT, self._make_snapshot_handler(session))

    def _make_fill_handler(self, session: AccountSession):
        def handler(fill) -> None:
            symbol = getattr(fill, "symbol", None)
            if symbol not in self.symbols:
                return

            mine = session.owns_this_update()
            if mine is False:
                # Another account on the same socket. Every account under one
                # key shares a socket, and the client fires every registered
                # handler for every message, so without this a $60 buy on one
                # account was booked on all three of them.
                return
            if mine is None:
                if self._unattributed_fills == 0:
                    log.warning(
                        "%s: a fill arrived without naming its account and this "
                        "socket carries %d -- re-reading positions instead of "
                        "guessing whose it was",
                        session.name, len(session.client.accounts),
                    )
                self._unattributed_fills += 1
                # The book is now behind the exchange by an unknown amount, so
                # it is marked rather than merely stale-dated. The worker will
                # not hedge until it has been re-read.
                #
                # Marking it was not enough on its own the first time: this
                # used to only reset the freshness clock, and the worker does
                # not consult it -- it hedges straight off the queue. So the
                # hedge ran six times against a book that never moved, each
                # order making the imbalance it was trying to correct larger,
                # until the ceiling stopped it $375 off-hedge.
                self._book_suspect = True
                self._hedge_queue.put_nowait(symbol)
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

            # By the account that filled, not by the market. Roles looked up
            # by symbol are the CONFIGURED pair's, and a group's accounts are
            # drawn from the pool -- so a fill on a group maker matched
            # neither side, logged `role=?`, and queued the bare symbol. The
            # worker then resolved that to the configured pair, found it flat,
            # and hedged nothing. Two groups opened $138 and no hedge was ever
            # sent.
            key = self._key_for_account(session.pubkey, symbol)
            roles = (
                self._roles_for_key(key) if key is not None
                else self._roles_by_symbol(self.state.leg(symbol).phase).get(symbol)
            )
            if roles is not None and session.pubkey in roles.hedgers:
                # This is one of our own hedge orders landing; retire its
                # reservation so net exposure reads correctly.
                #
                # Any hedger, not `roles.taker` -- which is only the FIRST of
                # them. A hedge split three ways reserved three slices and
                # retired one, so the other two sat in `in_flight` for ever.
                # Net exposure then read as short by the leftovers, the
                # hedger corrected it, that order reserved again and was not
                # retired either, and the leg thrashed: 700 alternating fills
                # of about a dollar each on one account in five minutes, each
                # one paying a taker fee for the privilege.
                self.hedger.note_taker_fill(roles.key, size if is_buy else -size)

            # `role` and the touch are carried here rather than worked out
            # afterwards. Which side of the pair a fill belongs to was being
            # inferred by pairing fills within a few seconds of each other,
            # which left 108 of 2,067 hedges unmatched and mis-assigns any pair
            # that lands out of order. The handler already knows.
            if roles is None:
                role = "?"
            elif session.pubkey in roles.hedgers:
                # Every hedger, for the same reason: with a split there are
                # several, and reading `role=?` against an account that was
                # doing exactly its job is how the thrash above stayed
                # invisible in a log full of it.
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
            # By leg key, not by market: the two are the same string until a
            # group owns the leg, and the worker must not have to guess which
            # of two legs on one market a fill belonged to.
            self._hedge_queue.put_nowait(roles.key if roles is not None else symbol)

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
        # Every account. A position closed out from under us on the fifth
        # account of a pool is the same event as one on the first, and the
        # response -- stop rebuilding the pair -- is the same too.
        events = self.guard.check(
            self.book, phase, [s.pubkey for s in self.all_sessions]
        )
        if not events:
            return False

        if await self._deferred_to_our_own_orders(events):
            return False

        if await self._settled_by_a_fresh_read(events):
            return False

        confirmed = await self._liquidation_confirmed()
        label = "liquidation" if confirmed else "position closed externally"
        for event in events:
            log.critical("%s: %s", label.upper(), event.describe())

        # Close everything still standing in the affected symbols, on both
        # accounts. Which side was hit does not change the answer -- the pair
        # is broken either way, and a lone leg is outright exposure.
        affected = {event.symbol for event in events}
        # Hedging stops before the first close goes out, and our own resting
        # orders come off the book. The hedge worker used to run on beside
        # this: on a live run it "hedged" the closes as they filled, and
        # the accounts the guard had just closed ended holding fresh shorts
        # (m1s1 -0.0103, m1s4 -0.0211). A resting order filling mid-close
        # would have been the same thing from the other side.
        self._closing_out = True
        try:
            await cancel_all_orders(self.all_sessions, sorted(affected))
        except Exception as exc:  # noqa: BLE001 - the closes still go
            log.critical("could not cancel resting orders before closing: %s", describe(exc))
        for symbol in affected:
            for session in self.all_sessions:
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
        for session in self.all_sessions:
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
            for session in self.all_sessions:
                session.settled(event.symbol)
        return True

    def _make_position_handler(self, session: AccountSession):
        def handler(update) -> None:
            symbol = getattr(update, "symbol", None)
            if symbol not in self.symbols:
                return

            mine = session.owns_this_update()
            if mine is False:
                return
            if mine is None:
                # `set_authoritative` is the strongest write there is -- it
                # replaces the position outright rather than adjusting it --
                # so a guess here would overwrite the truth for two accounts
                # out of three.
                self._book_suspect = True
                return

            self.book.set_authoritative(session.pubkey, symbol, float(update.size or 0.0))

            # Cheap and synchronous -- no I/O, just arithmetic on the book.
            # Closing the survivor happens in the worker; a handler that
            # awaited an order would deadlock the socket it arrived on.
            #
            # Every account, not the named pair: a liquidation on the fourth
            # account of a pool is a liquidation.
            if self.guard.check(
                self.book,
                self._phases_by_account(),
                [s.pubkey for s in self.all_sessions],
            ):
                self._liquidation_seen.set()

        return handler

    def _make_snapshot_handler(self, session: AccountSession):
        def handler(snapshot) -> None:
            mine = session.owns_this_update()
            if mine is False:
                return
            if mine is None:
                # A snapshot is every position this account holds, so applying
                # one account's to another does not merely add a wrong number:
                # it declares the other account flat in every symbol the
                # snapshot does not mention.
                self._book_suspect = True
                return

            positions = [
                p for p in (getattr(snapshot, "positions", None) or [])
                if p.symbol in self.symbols
            ]
            self.book.apply_snapshot(session.pubkey, positions)

        return handler

    # -- workers -----------------------------------------------------------

    def _roles_for_key(self, key: str) -> LegRoles | None:
        """The roles of the leg filed under `key`, at whatever phase it is in.

        A leg drawn from the account pool carries its own two accounts, so its
        roles come from the group rather than from the config. The swap between
        entry and exit is the same one the configured legs make: the account
        that hedged a long by going short is the one holding the short to
        cover.

        A leg keyed by its market resolves the way it always did.
        """
        group = self._groups.get(key)
        if group is not None:
            leg = self.state.leg(key, group.symbol)
            exiting = leg.phase == Phase.EXIT
            # The accounts do NOT swap on the way out, which is where a
            # group differs from a configured pair. Two accounts can swap,
            # because the one holding the short can rest the buy-back while
            # the other hedges. Three hedgers cannot: swapping would make one
            # of them the maker and leave the other two holding shorts that
            # nothing closes.
            #
            # So the opener rests the close as well, selling what it bought,
            # and the same hedgers cover it by buying back their own shares.
            # Every account that opened something closes it.
            return LegRoles(
                group.symbol,
                maker=group.maker,
                taker=group.takers[0],
                # The group's own side on the way in, its mirror on the way
                # out. This used to be `not exiting`, which made every maker in
                # every group a buyer for the life of the run.
                maker_is_buy=group.maker_is_buy != exiting,
                reduce_only=exiting,
                id=key,
                takers=group.takers,
                shares=group.shares,
                offset_bps=leg.offset_bps,
                max_order_size=leg.max_order_size,
            )

        # A pool run has no configured pair to fall back to. The "pair" here
        # is pool[0] and pool[1] -- in multi mode m1 and m1s1, two accounts
        # that usually sit in DIFFERENT groups -- so resolving an unowned key
        # to them hedged one against the other. After a restart mid-cycle
        # that bought or sold on an account no group owned, and nothing ever
        # closed it.
        if self.pairing is not None:
            return None

        leg = self.state.legs.get(key)
        symbol = leg.symbol if leg is not None else key
        if symbol not in self.symbols:
            return None
        return self._roles_by_symbol(self.state.leg(key, symbol).phase).get(symbol)

    def _keys_to_hedge(self, key: str) -> list[str]:
        """The legs a queued key stands for.

        Usually itself. A bare market name reaches the queue when a fill could
        not be tied to a group -- an account update that named nobody, or a
        fill on an account between groups -- and in a pool it means "check
        every group on that market", not "hedge the configured pair".
        """
        if self.pairing is None or key in self._groups or key not in self.symbols:
            return [key]
        return [k for k, group in self._groups.items() if group.symbol == key]

    async def _hedge_worker(self) -> None:
        """Drains fill signals and restores neutrality, one leg at a time."""
        while not self._stop.is_set():
            # Checked first and on every pass, including the idle one. A
            # liquidation leaves the survivor outright directional, so it must
            # not queue behind a hedge -- and it makes any pending hedge moot.
            if self._liquidation_seen.is_set():
                self._liquidation_seen.clear()
                await self._guard_liquidation(self._phases_by_account())
                continue

            try:
                key = await asyncio.wait_for(self._hedge_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            if self._hedging_suspended:
                # Closing out, or halted: the positions are being taken down,
                # and a hedge now would open the opposite of each close.
                continue

            if self._book_suspect and not await self._refreshed():
                # Reading failed, so the book is still wrong. Hedging off it
                # would size the order from a number we have just been told
                # not to trust -- and a hedge is a market order, so the cost
                # of being wrong is paid immediately and in full.
                continue

            for leg_key in self._keys_to_hedge(key):
                roles = self._roles_for_key(leg_key)
                if roles is None:
                    continue
                try:
                    # Before the market order, not after: our own remainder is
                    # resting on exactly the side it is about to sweep.
                    await self._clear_hedge_path(roles)
                    await self.hedger.hedge(
                        roles, mark_price=self.feed.reference_price(roles.symbol)
                    )
                except HedgeLimitExceeded as exc:
                    self._trigger_halt(f"hedge limit exceeded -- {exc}")
                except Exception as exc:
                    # The reconciler re-derives from position, so a single
                    # failure is recoverable; a persistent one trips the
                    # reject streak.
                    log.error("hedge for %s failed: %s", leg_key, describe(exc))

    def _least_crowded_side(self, symbol: str) -> bool:
        """The side fewest live legs are resting on, for the next group.

        Not unreadability -- that is what the coin flip underneath does. This
        is about self-trades. A hedge is a market order against its maker's
        own direction, so it sweeps the side that maker is resting on, and
        every OTHER account of ours resting there is in its path too. Keeping
        the groups on opposite sides means the only order in the way is the
        filling leg's own, which `_clear_hedge_path` can pull without
        disturbing anybody else's queue position.

        It cannot be a guarantee, and is not written as one. A group's side
        flips when it starts unwinding -- what it bought, it must sell -- and
        two groups whose phases have drifted apart can end up on one side with
        neither of them able to move. `_clear_hedge_path` is what makes the
        result correct; this only keeps it cheap.

        A tie is drawn rather than broken by a rule, so the first group of a
        run does not always open the same way.

        Counted over the PAIRING's active groups, which is the only record
        that is current at the moment of a draw. Two earlier versions were not:

        `state.legs` keeps a finished group's leg on purpose -- the state file
        is what a restart reads to find positions -- but the group is dropped
        from `_groups` on release, and `_roles_for_key` then falls through to
        the configured pair, whose side is the constant True. Every group that
        had ever finished was counted, and counted as a buy.

        `_groups` is current, but not yet. `_run_group` fills it, and
        `_run_group` is a task: the dispatcher draws, spawns it, and comes
        straight back round to draw again without yielding, so the second draw
        still saw an empty map. A live run opened with both groups on the bid:

            === group 1 drawn: BTC-USD 79Dg5R..DCa4 BUY ... at 2.20bps ===
            === group 2 drawn: BTC-USD 8rr5CY..YSLm BUY ... at 2.19bps ===

        `Pairing.draw` registers in `active` before it returns, so by the time
        the dispatcher asks about the next group, the last one is there.

        The phase still comes from the leg, because that is what says whether a
        group has turned around: a leg with no state yet has not, which is the
        right answer for one that was drawn a moment ago.
        """
        resting = [0, 0]
        active = self.pairing.active if self.pairing is not None else {}
        for group_id, group in active.items():
            if group.symbol != symbol:
                continue
            leg = self.state.legs.get(self.group_key(group_id, group.symbol))
            exiting = leg is not None and leg.phase == Phase.EXIT
            resting[group.maker_is_buy != exiting] += 1
        if resting[True] == resting[False]:
            return self._rng.random() < 0.5
        return resting[True] < resting[False]

    async def _clear_hedge_path(self, roles: LegRoles) -> None:
        """Pull our own resting orders off the side this hedge will sweep.

        A hedge covers its maker by trading the other way, so a maker that
        bought is covered by a market SELL -- which sweeps the bid, where the
        unfilled remainder of that same maker's order is resting, at the touch,
        first in the queue. That is not an edge case: it is where the chaser
        deliberately puts it.

        The exchange will not let one account cross itself -- `CANCELLED_
        SELFCROSSING` -- but two accounts under one master it matches like any
        strangers, while the fee documentation excludes that volume from the
        tier. Measured on a live run: 365 of 1465 trades.

        So every resting order of ours in this market on that side is pulled
        first, whichever group put it there. `_least_crowded_side` keeps the
        other groups off this side, so the ordinary case cancels exactly one
        order -- the filling leg's own -- and the chaser re-places it on the
        next tick.

        A cancel that fails does NOT stop the hedge. Unhedged exposure has no
        bounded cost and a self-trade costs a fee; when only one of the two can
        be avoided, it is never the hedge.
        """
        price = self.feed.reference_price(roles.symbol)
        if self.hedger.actionable_hedge(roles, price) <= 0:
            # Nothing will be sent, so nothing is in its way. Checked first
            # because a cancel costs a request against an exchange that has
            # answered 429 to two accounts polling every five seconds.
            return

        # Live groups, for the reason spelled out in `_least_crowded_side`:
        # a released leg resolves to the configured pair rather than to the
        # group that owned it, so its side is not its own.
        #
        # Every cancel here is a round trip the hedge waits behind, ~360ms
        # each on a live run -- and the hedge's cost rises with every one of
        # them: under 500ms it gave back 0.8bps against the maker's price,
        # over a second 2.6bps. So an order already filled is not cancelled
        # (70% of maker fills took the whole order, and the cancel still went
        # first), and the ones that are cancelled go out together.
        in_path = []
        for key, group in list(self._groups.items()):
            leg = self.state.legs.get(key)
            if group.symbol != roles.symbol or leg is None or not leg.oid:
                continue
            other = self._roles_for_key(key)
            if other is None or other.maker_is_buy != roles.maker_is_buy:
                continue
            session = self.sessions.get(other.maker)
            if session is None:
                continue
            if not self.chaser.may_be_resting(session, leg.oid):
                continue
            in_path.append((session, leg))

        async def pull(session, leg) -> None:
            try:
                await session.cancel(roles.symbol, leg.oid)
            except Exception as exc:  # noqa: BLE001 - the hedge still goes
                log.warning(
                    "%s: could not pull its %s order out of the hedge's path "
                    "(%s) -- hedging anyway, which may trade against it",
                    session.name, roles.symbol, describe(exc),
                )
                return
            leg.oid = None

        await asyncio.gather(*(pull(session, leg) for session, leg in in_path))

    async def _refreshed(self) -> bool:
        """Re-read positions because the book is known to be behind.

        Clears the suspicion only on success. The caller declines to hedge
        while it stands, so a failure here costs a delayed hedge -- and the
        pair stays hedged in the meantime, because what made the book
        suspect was an event it did not see, not a position it does not have.
        """
        try:
            await self._sync_positions(max_age_s=0.0)
        except Exception as exc:  # noqa: BLE001 - retried on the next signal
            log.error("could not re-read positions: %s", describe(exc))
            return False
        self._book_suspect = False
        return True

    def _trigger_halt(self, reason: str) -> None:
        if self._halt_reason is None:
            log.critical("HALT: %s", reason)
            self._halt_reason = reason
            self.title.halted(reason)
            # Fire-and-forget: the halt path has orders to cancel and positions
            # to flatten, and must not wait on Telegram to do it.
            self.notifier.send_soon(self.notifier.halted(reason))
            self._stop.set()

    async def _settled_by_a_fresh_read(self, events) -> bool:
        """True when a fresh position read says the shrink was never there.

        The book has two sources that do not agree instantly: the WebSocket,
        which carries a fill the moment it lands, and the periodic HTTP read,
        which can answer with state from before it. A read that arrives in the
        seconds after a hedge writes the OLDER number over the newer one, and
        the position then appears to have shrunk by exactly the size of our own
        fill.

        That happened on a live run and ended it:

            hedge BTC-USD: net=-0.00230000 -> BUY 0.00230000 across
                m1s1 0.00079300, m1s4 0.00150700
            fill on m1s1: BUY 0.00079300 ... role=taker
            m1s1 positions: BTC-USD=+0.01571200
            POSITION CLOSED EXTERNALLY: m1s1 BTC-USD position reduced
                externally: +0.01650500 -> +0.01571200

        0.01650500 - 0.01571200 = 0.00079300, the hedge fill to the lot. The
        exchange had already answered that no liquidation occurred, and five
        accounts were closed at market anyway.

        `_deferred_to_our_own_orders` does not catch this one: it defers on a
        submission that went UNANSWERED, and this hedge was answered -- the
        fill is in the log. What was stale is the read that came after it.

        The response to this guard is irreversible, so it is worth one HTTP
        round trip to be sure. A real close does not come back; a stale read
        does. The check that reported the event already re-baselined the peak
        to the low reading, so a recovered position simply reads as a new high
        and nothing is reported twice.

        A read that fails answers "no", for the same reason the liquidation
        query does: not being able to ask must land on the same side as yes.
        """
        try:
            await self._sync_positions(max_age_s=0.0)
        except Exception as exc:  # noqa: BLE001 - unreadable means unconfirmed
            log.warning(
                "could not re-read positions to test whether %s really shrank "
                "(%s) -- treating it as real",
                ", ".join(sorted({e.account_name for e in events})),
                describe(exc),
            )
            return False

        for event in events:
            spec = self.feed.specs.get(event.symbol)
            if spec is None:
                return False
            current = self.book.authoritative(event.account, event.symbol)
            if abs(event.previous) - abs(current) > spec.lot_size:
                return False

        log.warning(
            "%s: the position is back at its old size on a fresh read, so the "
            "shrink was a stale reading racing our own fill, not a close -- "
            "carrying on",
            ", ".join(sorted({e.account_name for e in events})),
        )
        return True

    async def _liquidation_confirmed(self) -> bool:
        """Did the exchange actually liquidate something? True if it cannot say.

        Fails safe on purpose. This is asked in the middle of whatever went
        wrong, which is when the query is least likely to answer, and "I could
        not ask" must land on the same side as "yes": closing a healthy pair
        costs a spread, while trading on into a real liquidation does not have
        a bounded cost.
        """
        for session in self.all_sessions:
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

    async def _clear_orphans(self, key: str) -> None:
        """Cancel anything resting in this leg's market after a silent submission.

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
        # By leg, not by market. `leg_roles(symbol)` resolves the CONFIGURED
        # pair, so for a leg drawn from the pool it named the wrong account
        # entirely -- cancelling orders on an account that has none, and
        # leaving the orphan it was called about resting.
        roles = self._roles_for_key(key)
        if roles is None:
            return
        symbol = roles.symbol
        session = self.sessions[roles.maker]
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

    async def _healed(self, violations, *, same_incident: bool = False) -> bool:
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

        `same_incident` is set by the supervisor when it calls again to catch a
        socket that dropped DURING the previous repair. Those passes do not
        spend the flap budget: they are one incident being repaired in stages,
        and charging each stage would turn a single bad minute into five and
        halt the run for flapping it never did.
        """
        RECOVERABLE = ("disconnected", "stale_stream")
        dropped = [v for v in violations if v.kind in RECOVERABLE]
        if not dropped or len(dropped) != len(violations):
            return False

        now = time.monotonic()
        self._reconnect_times = [
            t for t in self._reconnect_times if now - t < RECONNECT_WINDOW_S
        ]
        if same_incident:
            # Already charged for. The budget still governs: this pass only
            # exists because the previous one was allowed.
            pass
        elif len(self._reconnect_times) >= MAX_RECONNECTS:
            log.error(
                "the socket has dropped %d times in the last %g minutes -- "
                "not reconnecting again",
                len(self._reconnect_times),
                RECONNECT_WINDOW_S / 60,
            )
            return False
        if not same_incident:
            self._reconnect_times.append(now)

        restored = False
        failed = []
        stale_after = self.risk.config.ws_stale_timeout_s
        for session in self.all_sessions:
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
            #
            # Guarded, and off the event loop. This ran bare and synchronous
            # inside the supervisor: one failed read -- a 429 or a 504, likely
            # exactly when sockets are dropping -- raised out of `_supervise`
            # and ended it, taking the exposure limits, the liquidation guard
            # and the reconciler with it while the groups traded on. And on
            # the loop it stalled every socket for a request per account.
            try:
                await self._sync_positions(max_age_s=0.0)
            except Exception as exc:  # noqa: BLE001 - the book stays suspect
                log.error(
                    "could not re-read positions after reconnecting: %s -- "
                    "holding hedges until a read succeeds",
                    describe(exc),
                )
                self._book_suspect = True
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
        last_sync = 0.0
        while not self._stop.is_set():
            violations = self.risk.check()
            for pass_number in range(MAX_HEAL_PASSES):
                if not violations:
                    break
                if not await self._healed(violations, same_incident=pass_number > 0):
                    break
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
                reason = self._sync_reason(now, last_sync)
                if reason:
                    last_sync = now
                    try:
                        await self._sync_positions()
                    except Exception as exc:  # noqa: BLE001 - retried next tick
                        log.error("position sync failed: %s", describe(exc))

                # Before hedging, not after. If a position was liquidated, the
                # hedge rule's answer is to open a fresh one on the account that
                # still holds something -- exactly the wrong response.
                if await self._guard_liquidation(self._phases_by_account()):
                    return
                if self._hedging_suspended:
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

    def _sync_reason(self, now: float, last_sync: float) -> str:
        """Why positions should be re-read now, or "" for no reason to.

        The read exists for one failure: a socket that stops delivering without
        disconnecting. The book is fed by those deliveries and the optimistic
        fill overlay expires, so a silent stall decays the book toward empty --
        and the hedge rule, derived from an empty book, concludes the pair is
        flat while real positions are still open.

        That condition is measurable. Each session knows how long it has been
        since it last heard anything, so the read is triggered by the evidence
        rather than by a clock: a socket that is talking has nothing to be
        checked against, and asking anyway is a request per account every few
        seconds. Two accounts was already enough to draw a 429.

        The quiet threshold is half of the risk limit that halts on a stale
        socket, so the check happens while there is still time for it to mean
        something rather than in the same breath as the halt.

        The interval underneath is a backstop for the case the staleness clock
        cannot see: a socket that delivers regularly and is nonetheless wrong.
        """
        quiet_after = self.config.risk.ws_stale_timeout_s / 2
        for session in self.sessions.values():
            try:
                age = session.last_message_age_s
            except Exception:  # noqa: BLE001 - a session that cannot say is one to check
                return "a session could not report its age"
            if age >= quiet_after:
                return f"{session.name} has been quiet for {age:.0f}s"
        if now - last_sync >= self.config.position_sync_interval_s:
            return "periodic"
        return ""

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

    def _phases_by_account(self) -> dict[tuple[str, str], Phase]:
        """Each account's phase in each market it is trading.

        By account, because a phase belongs to the leg an account is in and
        several legs can share a market. The old answer was by SYMBOL, read
        off the leg keyed by the symbol itself -- and once every mode drew its
        accounts from the pool, nothing ever drove that leg. It sat at IDLE
        for the life of the run, IDLE is not an accumulating phase, and the
        liquidation guard therefore returned "nothing to see" on every call
        of every pool run it has ever made.

        Accounts in no group are left out rather than reported IDLE: they hold
        nothing, and a guard that watches them is a guard watching zero. When
        no group is drawn at all the map is empty, which says the same thing
        about the whole run -- a position opened by this run cannot exist
        before a group exists to open it.
        """
        phases: dict[tuple[str, str], Phase] = {}
        for key, group in self._groups.items():
            phase = self.state.leg(key, group.symbol).phase
            for pubkey in group.accounts:
                phases[(pubkey, group.symbol)] = phase
        return phases

    # -- one leg's chase loop ----------------------------------------------

    async def _drive_leg(self, key: str, is_done, label: str) -> None:
        """Keep one leg's order near the market until `is_done()`.

        Only this leg. The others are separate tasks at their own phases, which
        is the whole point: a filled leg must not wait on an unfilled one.

        Addressed by leg key rather than by market, because two legs may trade
        one market once groups are drawn from the account pool, and each has
        its own order, phase and clock.
        """
        leg = self.state.leg(key)
        symbol = leg.symbol
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
                roles = self._roles_for_key(key)
                if roles is None:
                    return
                try:
                    await self.chaser.step(roles, leg)
                except Exception as exc:  # noqa: BLE001 - retried next tick
                    log.error("chase step for %s failed: %s", key, describe(exc))
                    await self._clear_orphans(key)

            self._persist()

            if is_done() and await self._confirm_done(is_done, f"{symbol} {label}"):
                log.info("%s %s complete", symbol, label)
                return

            await asyncio.sleep(self.config.chase_interval_s)

    def _leg_is_neutral(self, key: str) -> bool:
        """Whether this leg has no hedge left that could actually be placed.

        A residual smaller than one lot or below the market's minimum notional
        cannot be traded away, so it does not count as outstanding work -- if it
        did, the phase would wait forever on a hedge that can never be
        submitted. Such residuals are real exposure and are reported by
        `_log_untradeable_residuals`; the USD risk limits still police them.
        """
        roles = self._roles_for_key(key)
        if roles is None:
            return True
        # By leg, not by market: another group's pending hedge on the same
        # market says nothing about whether this one is neutral.
        if abs(self.hedger.in_flight.total(roles.key)) > 0:
            return False
        price = self.feed.reference_price(roles.symbol)
        return self.hedger.actionable_hedge(roles, price) <= 0

    def _leg_is_flat(self, key: str) -> bool:
        """Whether this leg's own accounts hold nothing in its market.

        Its own, not every account: a second group trading the same market has
        positions of its own, and reading those would keep this leg waiting on
        a position it does not own and cannot close.
        """
        roles = self._roles_for_key(key)
        if roles is None:
            return True
        spec = self.feed.specs[roles.symbol]
        # Every account in the leg. `roles.taker` is only the first hedger, so
        # a split leg would have read as flat while the second and third still
        # held the shorts they opened -- the exact failure the exit roles were
        # shaped to prevent, one function further on.
        return all(
            abs(self.book.authoritative(pubkey, roles.symbol)) < spec.lot_size
            for pubkey in roles.accounts
        )

    # -- one leg's phases --------------------------------------------------

    async def _leg_open(self, key: str, target_size: float) -> None:
        leg = self.state.leg(key)
        leg.phase = Phase.OPEN
        leg.complete = False
        leg.oid = None
        leg.target_size = target_size
        leg.hold_until = 0.0
        roles = self._roles_for_key(key)
        if roles is None:
            return
        log.info(
            # Every hedger, not `roles.taker` -- which is the first of them.
            # A split hedge announced one account and used three, so the line
            # that says what a cycle is doing named two thirds of it wrong.
            "=== %s OPEN: maker=%s takers=%s target=%g ===",
            key,
            self.sessions[roles.maker].name,
            ",".join(self._name_of(pubkey) for pubkey in roles.hedgers),
            target_size,
        )
        self._persist()

        await self._drive_leg(
            key,
            lambda: leg.complete and self._leg_is_neutral(key),
            "open",
        )

    async def _leg_hold(self, key: str) -> None:
        leg = self.state.leg(key)
        if not leg.hold_until:
            # Drawn per leg and stored as a deadline, so a restart mid-hold
            # resumes this hold rather than rolling a fresh one -- and so a leg
            # that filled first starts counting first.
            leg.hold_until = time.time() + self.config.hold_minutes.pick() * 60
        leg.phase = Phase.HOLD
        self._persist()

        log.info("=== %s HOLD: %.1f minutes ===", key, leg.hold_remaining_s() / 60)
        await self._drive_leg(key, lambda: leg.hold_remaining_s() <= 0, "hold")

    async def _leg_exit(self, key: str) -> None:
        leg = self.state.leg(key)
        symbol = leg.symbol
        # Pull the entry order before reversing roles: it would fight the close.
        if leg.oid:
            entry_roles = self._roles_for_key(key)
            if entry_roles is not None:
                await self.chaser.cancel_leg(entry_roles, leg)

        leg.phase = Phase.EXIT
        leg.complete = False
        leg.oid = None
        # The exit target is whatever is actually held, not the configured size:
        # the entry may have filled only partially.
        #
        # Read after the phase flips, because the roles swap with it and the
        # maker of the exit is the account holding the short to cover.
        roles = self._roles_for_key(key)
        if roles is None:
            return
        leg.target_size = abs(self.book.effective(roles.maker, symbol))
        log.info("=== %s EXIT: %g to close ===", key, leg.target_size)
        self._persist()

        # The closing limit order only unwinds the maker side. The taker side is
        # reduced by hedges, and hedges stop once the pair is neutral -- so if
        # partial fills and lot rounding leave the two accounts differing by
        # less than one lot while both are still non-zero, no hedge will ever
        # fire and the taker's residual would sit there forever. Completion
        # therefore waits on the maker leg, then sweeps the rest.
        await self._drive_leg(key, lambda: leg.complete, "exit")

        if not self._stop.is_set() and not self._leg_is_flat(key):
            log.info("%s: closing residual left after the exit leg", key)
            # This leg's accounts, not every account. `self.sessions` is the
            # whole pool, and a sweep across it would market-close the
            # positions of every other group trading this market -- mid-hold,
            # from a cycle that has nothing to do with them.
            await flatten(
                {pubkey: self.sessions[pubkey] for pubkey in roles.accounts},
                self.book,
                self.feed,
                [symbol],
            )

    # -- one leg's cycle ---------------------------------------------------

    def _offset_for_cycle(self, symbol: str) -> float:
        """How far inside the touch this cycle rests, drawn when it is a range.

        Drawn per cycle rather than read per market, because the resting price
        is a pure function of the book and this number: two groups on one
        market, sharing one offset, compute the same tick and queue behind each
        other. Rotating the accounts does not separate them -- only the price
        does. A live run showed both groups resting BUY at 85814.47, tick for
        tick.

        Fixed for the cycle once drawn, for the same reason the hedge shares
        are: an offset that moved every tick would walk the order around for
        reasons the market never gave it.

        A leg written as a plain number returns that number, which is what
        every run before ranges did.
        """
        leg = next(
            (leg for leg in self.config.active_legs if leg.symbol == symbol), None
        )
        if leg is None:
            return 0.0
        return leg.offset_span.pick() if leg.offset_span else leg.offset_bps

    def _size_for_cycle(
        self, symbol: str, configured_size: float, key: str | None = None
    ) -> float:
        """This cycle's size for one leg, redrawn when it was written as a range.

        A fixed size repeats exactly, and an exact repeat is a shape in the
        fill history: the same notional, cycle after cycle, between the same
        two accounts. Drawing inside a range removes that for the cost of one
        `random.uniform` per cycle.

        Returns `configured_size` untouched for a leg that is not a range, for
        a price that is not available yet, or for anything that goes wrong in
        the draw. A cycle at the configured size is a normal cycle; a cycle
        that does not happen because the size could not be worked out is not.
        """
        config_leg = next(
            (leg for leg in self.config.active_legs if leg.symbol == symbol), None
        )
        span = getattr(config_leg, "notional_span", None)
        cap_span = getattr(config_leg, "max_order_span", None)
        varies = (span is not None and span.is_range) or (
            cap_span is not None and cap_span.is_range
        )
        if config_leg is None or not varies:
            return configured_size

        price = self.feed.reference_price(symbol)
        if not price:
            return configured_size
        try:
            draw_sizes([config_leg])
            resolve_notionals(
                legs=[config_leg], specs=self.feed.specs, prices={symbol: price}
            )
        except Exception as exc:  # noqa: BLE001 - a draw must not end the run
            log.warning("%s: could not redraw the size (%s)", symbol, describe(exc))
            return configured_size

        ceiling = self._size_ceiling.get(symbol, config_leg.size)
        drawn = min(config_leg.size, ceiling)
        # The chaser reads its cap from this object on every step, so writing
        # to it is what makes a redrawn cap take effect. Clamped for the same
        # reason the size is.
        #
        # A drawn group also keeps its own copy on its leg. The params are one
        # object per market, so with three groups open every one of them
        # rested whichever cap was drawn last -- three makers, one order size.
        cap = min(config_leg.max_order_size, ceiling)
        params = getattr(self.chaser, "params", {}).get(symbol)
        if params is not None:
            params.max_order_size = cap
        if key is not None and key in self._groups:
            self.state.leg(key, symbol).max_order_size = cap
        log.info(
            "%s: this cycle %g (drawn from %s)",
            symbol, drawn, span if span is not None else cap_span,
        )
        return drawn

    def restore_groups(self) -> list[tuple[int, Group, str]]:
        """Groups that were mid-cycle when the process stopped.

        Their accounts are marked busy again rather than re-drawn: the
        positions exist on the exchange whether or not this process remembers
        them, so drawing those accounts into a second group would have two
        groups computing their hedges from one set of positions.

        A leg that had finished its cycle is left alone -- it holds nothing,
        and resuming it would open a cycle nobody asked for.
        """
        resumed: list[tuple[int, Group, str]] = []
        for key, leg in self.state.legs.items():
            if not leg.group_id or not leg.maker or not leg.takers:
                continue
            if leg.phase in (Phase.IDLE, Phase.COMPLETE):
                continue
            group = Group(
                symbol=leg.symbol,
                maker=leg.maker,
                takers=tuple(leg.takers),
                shares=tuple(leg.shares) or (1.0,) * len(leg.takers),
                maker_is_buy=leg.maker_is_buy,
            )
            missing = [a for a in group.accounts if a not in self.sessions]
            if missing:
                # The key that signs for it is no longer in the key file, so
                # the position cannot be closed by this run. Said loudly
                # rather than skipped quietly: it is real exposure.
                log.error(
                    "%s was opened by accounts this run cannot sign for (%s) "
                    "-- its position is still open and needs the key that "
                    "opened it",
                    key, ", ".join(short_pubkey(a) for a in missing),
                )
                continue
            self._groups[key] = group
            self._group_ids[key] = leg.group_id
            if self.pairing is not None:
                self.pairing.reserve(leg.group_id, group)
            resumed.append((leg.group_id, group, key))
            log.warning("resuming group %d mid-%s: %s", leg.group_id, leg.phase.value, group)
        return resumed

    def group_key(self, group_id: int, symbol: str) -> str:
        """What a drawn group's leg is filed under.

        The id leads so that two groups on one market sort apart in a log, and
        the market is kept because every line that mentions a leg is read by
        someone who wants to know which market it was.
        """
        return f"g{group_id}:{symbol}"

    async def _run_group(self, group_id: int, group: Group, size: float) -> None:
        """One cycle for one drawn group, then give the accounts back.

        The accounts are released in a `finally`: a group that ends by halting,
        by the operator stopping, or by raising would otherwise hold its
        accounts out of the pool for the rest of the run, and a pool that leaks
        accounts quietly stops being able to draw.
        """
        key = self.group_key(group_id, group.symbol)
        self._groups[key] = group
        self._group_ids[key] = group_id
        # Written onto the leg before anything is opened, so a restart that
        # lands mid-cycle can work out which accounts hold what. The positions
        # are on the exchange either way; without this nobody can say whose
        # they are, and nothing would close them.
        leg = self.state.leg(key, group.symbol)
        leg.group_id = group_id
        leg.maker = group.maker
        leg.takers = list(group.takers)
        leg.shares = list(group.shares)
        leg.maker_is_buy = group.maker_is_buy
        # Only a fresh group draws. A resumed one already has an order resting
        # at an offset, and re-drawing here moved it mid-order -- which is
        # exactly what persisting the number was supposed to prevent.
        if leg.offset_bps is None:
            leg.offset_bps = self._offset_for_cycle(group.symbol)
        self._persist()
        log.info(
            "=== group %d drawn: %s at %.2fbps ===",
            group_id, group, leg.offset_bps,
        )
        try:
            await self._run_leg(key, size, once=True)
        finally:
            if self.pairing is not None:
                self.pairing.release(group_id)
            self._groups.pop(key, None)
            self._group_ids.pop(key, None)
            # The leg's state is kept, not dropped: a group that halted
            # mid-cycle has positions, and the state file is what a restart
            # reads to find them.
            log.info("=== group %d released ===", group_id)

    async def _dispatch_groups(self, sizes: dict[str, float]) -> None:
        """Keep up to `max_groups` groups trading until the run ends.

        Groups are started and not waited on: that is the whole point of a
        pool. A group whose market is slow must not hold up four others, in
        the same way and for the same reason that two configured legs stopped
        waiting on each other.

        A draw that comes back empty is the normal state of a busy run -- the
        cap is reached, or every free account is already in a group -- so it
        waits a beat rather than treating it as a fault.

        The first group to raise takes the run down with it, which is what
        already happens when a configured leg raises: an exception here means
        the exchange, the network or an invariant, and none of those get
        better by continuing to open positions.
        """
        running: set[asyncio.Task] = set()
        # Groups that were mid-cycle when the process stopped go first, and
        # before any new one is drawn: their accounts are already committed,
        # and a fresh draw made while they are unaccounted for could hand the
        # same accounts to a second group.
        # Restored by `_recover`, which had to do it before its reconcile;
        # restoring twice would reserve the same accounts twice.
        restored = getattr(self, "_restored", None)
        self._restored = []
        if restored is None:
            restored = self.restore_groups()
        for group_id, group, _key in restored:
            size = self.state.leg(self.group_key(group_id, group.symbol)).target_size
            running.add(asyncio.create_task(self._run_group(group_id, group, size)))
        try:
            while not self._stop.is_set():
                for task in [t for t in running if t.done()]:
                    running.discard(task)
                    # Raises here, inside the try, so the finally below still
                    # cancels the groups that are still trading.
                    task.result()

                reached = await self._target_reached()
                if reached:
                    log.info("execution target reached: %s", reached)
                    break

                drawn = None
                if self.pairing is not None:
                    symbol = self._rng.choice(self.symbols)
                    drawn = self.pairing.draw(
                        symbol, maker_is_buy=self._least_crowded_side(symbol)
                    )
                if drawn is None:
                    await asyncio.sleep(self.config.chase_interval_s)
                    continue

                group_id, group = drawn
                # The configured size, not a draw from it. `_run_leg` draws
                # once when it opens the leg, so drawing here too spent a draw
                # that was immediately overwritten -- and logged it as the
                # cycle's size, so the log named a number nothing traded.
                running.add(
                    asyncio.create_task(
                        self._run_group(group_id, group, sizes[group.symbol])
                    )
                )

            # Stopped or spent: let what is open finish its cycle rather than
            # abandoning positions mid-phase.
            if running:
                log.info("waiting for %d group(s) to finish their cycle", len(running))
                await asyncio.gather(*running)
        finally:
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)

    async def _run_leg(self, key: str, configured_size: float, once: bool = False) -> None:
        """OPEN -> HOLD -> EXIT for one leg on its own clock.

        `once` returns after a single cycle, which is what a drawn group wants:
        it exists for one cycle and hands its accounts back. A configured leg
        repeats until the run ends.
        """
        leg = self.state.leg(key)
        symbol = leg.symbol

        while not self._stop.is_set():
            # A restart lands mid-cycle, so each phase is entered only if this
            # leg has not already passed it.
            if leg.phase in (Phase.IDLE, Phase.COMPLETE):
                # Only at a cycle boundary. These used to run first on every
                # pass, so a group resumed mid-cycle after its target had been
                # met -- or on a leg whose count had reached `cycles` -- simply
                # returned: released as finished, positions still open, and
                # nothing left that would close them.
                if self.config.cycles and leg.cycle_index >= self.config.cycles:
                    return
                reached = await self._target_reached()
                if reached:
                    log.info("%s: execution target reached: %s", key, reached)
                    return
                leg.cycle_index += 1
                # Shown as one number, so it follows whichever leg is ahead.
                self.state.cycle_index = max(
                    self.state.cycle_index,
                    *(self.state.leg(s).cycle_index for s in self.symbols),
                )
                self.title.set_cycle(self.state.cycle_index)
                log.info("=== %s cycle %d ===", key, leg.cycle_index)
                await self._leg_open(
                    key, self._size_for_cycle(symbol, configured_size, key)
                )

            if self._stop.is_set():
                return
            if leg.phase == Phase.OPEN:
                await self._leg_hold(key)

            if self._stop.is_set():
                return
            # EXIT as well as HOLD: a leg resumed after a restart mid-exit is
            # already in EXIT, matched none of the branches above, and fell
            # straight through to COMPLETE -- released as finished with the
            # positions it was closing still open. `_leg_exit` re-derives its
            # target from what the maker actually holds, so entering it again
            # picks the close up where it stopped.
            if leg.phase in (Phase.HOLD, Phase.EXIT):
                await self._leg_exit(key)

            if self._stop.is_set():
                return
            leg.phase = Phase.COMPLETE
            leg.hold_until = 0.0
            self._persist()
            log.info("=== %s cycle %d complete ===", key, leg.cycle_index)
            # This leg legitimately went to zero, so its peaks would read as
            # an external close on the next entry. Only its own accounts: a
            # second group on this market may still be holding, and clearing
            # its high-water mark would hide the next real shrink.
            finished = self._roles_for_key(key)
            self.guard.reset_symbol(
                symbol, accounts=finished.accounts if finished else None
            )
            await self.notifier.cycle_complete(
                cycle=leg.cycle_index,
                of=self.config.cycles or None,
                detail=f"{key}: {await self.progress_detail()}",
            )
            if once:
                return

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
                for session in self.all_sessions
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

        # The legs that are trading, which are the drawn groups. It used to
        # read the leg keyed by the SYMBOL -- and once every mode drew its
        # accounts from the pool, nothing drove that leg. The block reported
        # `BTC-USD IDLE cycle 0` for an hour while fifteen cycles completed
        # underneath it.
        legs = []
        for key in self._live_keys():
            leg = self.state.legs.get(key)
            if leg is None:
                continue
            phase = leg.phase.value.upper()
            left = leg.hold_remaining_s()
            note = f" {humanise(left)} left" if left > 0 else ""
            legs.append(f"{key} {phase} cycle {leg.cycle_index}{note}")
        if not legs:
            legs = [f"{symbol} waiting" for symbol in self.symbols]

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

    async def _until_legs_finish(
        self,
        legs: list[asyncio.Task],
        supervisor: asyncio.Task,
        worker: asyncio.Task,
    ) -> None:
        """Wait for the legs, and fail the run if a watcher dies before them.

        Only the legs used to be awaited. The supervisor (risk limits,
        liquidation guard, reconciler) and the hedge worker ran beside them
        unobserved, so either could end -- by raising, or by returning with
        nothing asking it to -- and trading carried on without it. Their
        exceptions surfaced only in the final `gather`, which discards them.

        A watcher that ends after the run has been asked to stop is doing its
        job: the supervisor returns once it has triggered a halt.
        """
        watchers = {supervisor: "risk supervisor", worker: "hedge worker"}
        watched: set[asyncio.Task] = {*legs, *watchers}
        while not all(task.done() for task in legs):
            done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                watched.discard(task)
                if task in watchers:
                    name = watchers[task]
                    if not task.cancelled() and task.exception() is not None:
                        raise RuntimeError(
                            f"the {name} died: {describe(task.exception())}"
                        ) from task.exception()
                    if not self._stop.is_set():
                        raise RuntimeError(f"the {name} stopped on its own")
                elif task.exception() is not None:
                    # One leg raising must not leave the others trading on
                    # alone; the caller cancels them on the way out.
                    task.result()
        for task in legs:
            task.result()

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
        sizes = {leg.symbol: leg.size for leg in self.config.active_legs}
        legs: list[asyncio.Task] = []

        try:
            await self._recover()
            # After recovery, so an interrupted run is recognised as one and
            # keeps the count it already had.
            await self.capture_target_baseline()

            # One task, which starts and reaps the groups itself. The legs
            # here are not known in advance: they are drawn, traded and
            # disbanded for as long as the run lasts. Both modes come through
            # here -- they differ in which accounts reached the pool, and
            # that was settled before this point.
            legs = [asyncio.create_task(self._dispatch_groups(sizes))]
            await self._until_legs_finish(legs, supervisor, worker)

            if self._stop_requested:
                # The legs left their loops without reaching the end of a phase,
                # so whatever was resting is still resting. Pull it: an order
                # left working after the bot exits fills with nothing watching,
                # and the hedge that would answer it is no longer running.
                log.warning(
                    "stopped on %s -- cancelling resting orders", self._stop_requested
                )
                await cancel_all_orders(self.all_sessions, self.symbols)
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
            await cancel_all_orders(self.all_sessions, self.symbols)
            raise
        except Exception as exc:
            # Anything else used to leave straight through `finally`, which
            # stops the hedge worker but pulls nothing: every other group's
            # resting order stayed on the book, and a fill on one of them
            # landed with no hedge coming. One 429 inside a residual sweep
            # was enough.
            log.critical(
                "run failed (%s) -- cancelling every resting order", describe(exc)
            )
            try:
                await cancel_all_orders(self.all_sessions, self.symbols)
            except Exception as cancel_exc:  # noqa: BLE001 - still report the cause
                log.critical(
                    "COULD NOT CANCEL resting orders: %s -- cancel them by hand "
                    "now; nothing is hedging them",
                    describe(cancel_exc),
                )
            with contextlib.suppress(Exception):
                self._log_open_positions()
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

        `realised_for_trees` is synchronous `requests`, and this is called from
        inside the event loop -- between cycles, and once at the start of a run.
        Called directly it froze the loop for the length of the walk: measured
        at 2.9-3.8s against a 575-fill account, during which the chaser placed
        nothing, the hedge worker ran not at all, and fills arriving on the
        socket sat unprocessed. It gets worse as the history grows; the walk is
        paginated a thousand fills at a time.

        A thread keeps the loop turning. The same pattern `reconcile` already
        uses for its HTTP position read.
        """
        # Every account the run trades, not the first two. In pool mode
        # those two are two of a hundred and ten, so a volume or burn target
        # would have counted a fiftieth of the trading and never been reached.
        return await asyncio.to_thread(
            realised_for_trees, self.master.http, self._trees()
        )

    def _trees(self) -> list[list[str]]:
        """The run's accounts, grouped by the key that signs for them.

        Grouped rather than pooled because of what a self-trade is: a fill the
        exchange can see both sides of as one account holder. It can see that
        within a master's tree, and it cannot see it across two keys -- which
        is the reason for running more than one. Pooling them would score
        every cross-key hedge as self-trade volume and subtract it from the
        fee-tier figure the target is read against, understating the run by
        however much of its trading the pool did with itself.

        Sessions under one key share a socket, so the client identity is the
        grouping -- the same fact `build_pool` used to build them.
        """
        trees: dict[int, list[str]] = {}
        for session in self.all_sessions:
            trees.setdefault(id(session.client), []).append(session.pubkey)
        return list(trees.values())

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
        self.state.baseline_self_trade_usd = totals.self_trade_volume_usd
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

        read_at, answer = self._target_answer
        if time.monotonic() - read_at < TARGET_FRESHNESS_S:
            return answer

        try:
            totals = await self._read_totals()
        except Exception as exc:  # noqa: BLE001 - never block trading on this
            log.warning("could not read fill history for the execution target: %s", describe(exc))
            return None

        burned = _burned(totals.fees_usd - self.state.baseline_fees_usd)
        volume = totals.qualifying_volume_usd - self.state.baseline_volume_usd

        answer = None
        if target.burn_usd > 0 and burned >= target.burn_usd:
            answer = f"burned ${burned:,.4f} of ${target.burn_usd:,.2f}"
        elif target.volume_usd > 0 and volume >= target.volume_usd:
            answer = f"qualifying volume ${volume:,.2f} of ${target.volume_usd:,.2f}"
        # Cached only on a successful read. A failure is deliberately not
        # remembered: it is reported as "not reached", and holding that for
        # half a minute would turn one unreachable endpoint into a run that
        # cannot notice its own goal.
        self._target_answer = (time.monotonic(), answer)
        if answer:
            return answer

        # The status block redraws every second off this, and used to get it
        # only when a cycle finished -- so a six-minute OPEN phase showed
        # `$0 / $250,000  0.0%` while the log beside it counted past $19,000.
        self._progress = (burned, volume)

        if target.burn_usd > 0:
            log.info("burn progress: $%.4f / $%.2f", burned, target.burn_usd)
        if target.volume_usd > 0:
            log.info(
                "volume progress: $%.2f / $%.2f qualifying "
                "(of which $%.2f traded between your own accounts)",
                volume,
                target.volume_usd,
                totals.self_trade_volume_usd - self.state.baseline_self_trade_usd,
            )
        return None

    async def _recover(self) -> None:
        """Reconcile persisted intent against exchange truth before trading.

        Truth comes from HTTP rather than the stream: at this point the account
        snapshot may not have arrived, and acting on an empty book would look
        exactly like having no positions.
        """
        sync_positions_http(self.all_sessions, self.book)

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

        if self.pairing is not None:
            # A pool run: the groups are the plan. Restored HERE, before the
            # reconcile below, because that reconcile hedges whatever
            # `_live_roles` names -- and with the groups not yet back it named
            # the configured pair, m1 and m1s1, and traded one against the
            # other. The dispatcher starts these rather than restoring again.
            self._restored = self.restore_groups()
            self._refuse_unowned_positions()
            symbols_to_recover: list[str] = []
        else:
            symbols_to_recover = list(self.symbols)

        # Decided per leg, because legs recover independently: one may have
        # been mid-entry while the other was already unwinding.
        for symbol in symbols_to_recover:
            leg = self.state.leg(symbol)
            spec = self.feed.specs[symbol]
            leg_has_positions = any(
                abs(self.book.authoritative(session.pubkey, symbol)) >= spec.lot_size
                for session in self.all_sessions
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
        await cancel_all_orders(self.all_sessions, self.symbols)
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

    def _refuse_unowned_positions(self) -> None:
        """Stop before trading if a position belongs to no restored group.

        Every position a pool run opens is recorded against the group that
        opened it, and that record is how the next run knows who closes it. A
        position outside every record -- the state file lost or erased, or a
        trade made by hand -- has nobody to close it: no group will exit it,
        and an account holding it can be drawn into a new group whose hedge
        then treats it as that group's own imbalance.

        Dust is let through. A residual under the market's minimum order cannot
        be closed by anyone, and every run leaves some.
        """
        owned = {pubkey for group in self._groups.values() for pubkey in group.accounts}
        stray = []
        for symbol in self.symbols:
            spec = self.feed.specs[symbol]
            price = self.feed.reference_price(symbol) or 0.0
            for session in self.all_sessions:
                if session.pubkey in owned:
                    continue
                size = abs(self.book.authoritative(session.pubkey, symbol))
                if size < spec.lot_size or (price and size * price < spec.min_notional):
                    continue
                stray.append(f"{session.name} {symbol} {size:g}")
        if stray:
            raise RuntimeError(
                "positions open on accounts no recorded group owns: "
                + ", ".join(stray)
                + ". Nothing in this run would ever close them. Close them "
                "first -- `6. Close All Positions` -- then start again."
            )

    async def _emergency_stop(self, reason: str) -> None:
        log.critical("emergency stop: %s", reason)
        self.state.phase = Phase.HALTED
        self.state.halted_reason = reason
        self._persist()

        await cancel_all_orders(self.all_sessions, self.symbols)
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
        for leg in config.active_legs
    }


def build_hedge_ceilings(config: Config) -> dict[str, float]:
    """Cap a single hedge at twice the leg's configured size.

    A required hedge larger than this means the position book and reality have
    diverged by more than the strategy can account for, and firing a very large
    market order on that basis would be worse than halting.
    """
    return {leg.symbol: leg.size * 2.0 for leg in config.active_legs}

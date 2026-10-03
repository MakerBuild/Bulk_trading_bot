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

from .accounts import AccountSession, SharedReconnect
from .chaser import ChaseParams, Chaser
from .config import Config
from .feed import MarketFeed
from .fees import burned_usd as _burned
from .fees import realised_for_trees
from .hedger import Hedger, HedgeInDoubt, HedgeLimitExceeded, HedgeResult, LegRoles
from .liquidation import LiquidationGuard, recent_liquidations
from .marketdata import (
    epoch_seconds,
    is_tradeable,
    round_notional,
    touch_text,
    tradeable_size,
)
from .sizing import draw_sizes, resolve_notionals
from .notify import Notifier
from .accounts import short_pubkey
from .pairing import Group, Pairing
from .pause import PauseGate
from .positions import PositionBook, SeenTrades
from .scripts import script
from .reconcile import (
    cancel_all_orders,
    flatten,
    reconcile_net,
    sync_positions,
)
from .retry import describe
from .risk import RiskMonitor
from .state import Phase, StateStore, StrategyState
from .window import WindowTitle
from .ws_compat import fill_trade_id
import contextlib

log = logging.getLogger(__name__)


# How long after an unanswered submission a change in that symbol might still
# be our own.
DOUBT_WINDOW_S = 120.0

# A fill that reaches us this much later than its socket's usual delay says the
# socket is running behind; the socket is then distrusted for this long, renewed
# by every further sign of it. See `_note_stream_lag`.
STREAM_LAG_S = 1.5
STREAM_LAG_HOLD_S = 30.0
# And how often we may say so before treating the symbol as faulty. Counted
# over a window rather than for the life of the process: the bound is meant to
# catch a fault that keeps recurring, and a run lasting hours will collect
# unrelated single incidents that each deserved the benefit of the doubt.
MAX_DOUBT_DEFERRALS = 3
DEFERRAL_WINDOW_S = 900.0

# Pauses before the further reads of a shrink the exchange does not call a
# liquidation, each after the one before. The first read is sent at once and,
# close to the exchange, can still be served from before our own fill; one
# sent a READ_LAG_S later cannot. See `_settled_after_waiting`.
SHRINK_RECHECK_DELAYS_S = (1.0, 2.0)
# Pauses before asking the exchange again whether an account was liquidated,
# when it did not answer. Short: the shrink it is asked about may be a lone
# leg, and the fallback after the last one is to treat it as a liquidation.
CONFIRM_RETRY_DELAYS_S = (0.5, 1.0)

# How long a leg's hedge task waits before hedging AGAIN for fills that came in
# while it was busy. The first hedge after a fill still goes at once. From
# far away a hedge pass took ~650ms, so a maker order eaten by ten small trades
# was hedged in one or two orders; from Tokyo it would be ten, each with its
# own cancel. Hedge loss measured nearly flat in delay (2.04bps at 300-500ms),
# so gathering them costs little.
HEDGE_COALESCE_S = 0.1

# Reconnects tolerated before a dropped socket is treated as a persistent fault
# rather than a blip -- five of them inside ten minutes.
#
# Counted over a moving window, not per cycle. It was written as a per-cycle
# budget and the reset was never implemented, so in practice it was five for the
# entire run: an unlimited run halted on its sixth drop no matter how many hours
# apart they fell. A window also survives the legs running independently, where
# "this cycle" is two different things at once and neither is the right moment
# to forgive a fault.
MAX_RECONNECTS = 5
RECONNECT_WINDOW_S = 600.0

# Drops that land while another socket is being repaired are one incident, and
# only the first of them spends the budget above -- as does a socket dropping
# AGAIN inside the incident, which is flapping.
#
# A live log showed why: sub1's socket closed, the heal began, and nine seconds
# into its reconnect the master's socket closed too. One flaky link carries
# both sockets, so the second drop lands during the first repair more often
# than not, and charging it would turn one bad minute into two incidents. A
# drop this soon after the last repair ended counts as part of it too: a
# second socket noticed a tick after the first came back is the same outage.
HEAL_INCIDENT_GRACE_S = 10.0

# How often a leg paused for its socket tries again to pull an order the first
# cancel could not. See `_paused_for_its_sockets`.
PAUSED_CANCEL_RETRY_S = 5.0

# The violations a reconnect can fix. Every other one -- exposure over the
# cap, a position too large, a streak of rejections -- says the strategy itself
# is misbehaving, and halts at once.
SOCKET_FAULTS = ("disconnected", "stale_stream")

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
# How soon a FAILED read of the totals is tried again. A failure used to be
# retried on the next pass of whatever asked -- about once a second from the
# dispatcher and the status block alike -- and each retry was a twelve-account
# walk of `/account`. From Tokyo the first 429 kept itself going that way: the
# log showed the same warning every second until the run was stopped.
TARGET_RETRY_S = 10.0

# "Never happened", for a monotonic timestamp. Not 0.0: on Linux the monotonic
# clock counts from boot, and the bot starts as a service seconds after one.
# With 0.0, `now - 0.0 < 30` held for the first half-minute of the machine's
# life -- the target was taken as read moments ago and not read at all, and
# the first position syncs waited out windows that had never begun. Found by
# a test suite that failed only on a freshly booted Linux VM.
NEVER = float("-inf")

# A pool group whose OPEN or EXIT outruns `max_phase_minutes` is cut short
# rather than halting the run: an open keeps what it has filled, an exit closes
# the rest at market. Two live runs ended early on exactly this -- a quiet
# market, three groups resting on one side and sharing the same
# sellers, one of them 66% of the way to its target at thirty minutes. Nothing
# was wrong; the halt then closed every group at market anyway.
#
# The cut itself gets this long. Past it, something really is stuck, and that
# is still a halt.
CUT_SHORT_GRACE_S = 300.0
# Between market closes of one cut-short exit. Reduce-only, so a second close
# can take the position to flat but not past it; this only stops one close per
# chase tick while the first is still on its way back.
CUT_SHORT_RETRY_S = 5.0
# An OPEN this far to its target counts as about to turn round when the next
# group's side is chosen. See `_resting_side`.
TURNING_FRACTION = 0.5
# How often a leg still waiting on a fill says so. The chaser logs only when it
# moves the order, and an order sitting on the touch in a quiet market can go
# minutes without moving: a live operator read that silence as a hung bot and
# stopped it three times in five minutes, restarting the exit each time.
WAITING_LOG_S = 60.0


def waiting_line(
    key: str,
    label: str,
    *,
    left: float,
    price: float | None,
    waited_s: float,
    budget_s: float,
    group: bool,
) -> str:
    """The periodic "still waiting" line for one leg, and what ends the wait."""
    resting = f"order resting at {price:g}" if price else "no order resting yet"
    if not budget_s:
        then = "no time limit is set"
    elif not group:
        then = f"at {budget_s / 60:g} min the run halts"
    elif label == "exit":
        then = f"at {budget_s / 60:g} min the rest closes at market"
    else:
        then = f"at {budget_s / 60:g} min it keeps what has filled and moves on"
    return (
        f"{key} {label}: waiting for a fill -- {left:g} left, {resting}, "
        f"{waited_s / 60:.1f} min in; {then}"
    )


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
        # Limits per group as well as per market. See `RiskMonitor.check`.
        if isinstance(risk, RiskMonitor):
            risk.groups = self._risk_groups
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
        # Spread and slippage paid so far this run, fees apart. See
        # `_spread_cost`.
        self._spread_usd: float | None = None
        # When each recent reconnect happened. Older entries fall out of the
        # window on their own, so a drop an hour ago says nothing about this one.
        self._reconnect_times: list[float] = []
        # Reconnects under way, one per socket, by `id(client)`. See
        # `_start_heals`.
        self._heals: dict[int, asyncio.Task] = {}
        # The sockets healed in the current incident, and when the last heal
        # of it ended. See `_charge_reconnect`.
        self._incident: set[int] = set()
        self._incident_ended_at = NEVER
        # Legs holding off for a socket, and when each last sent a cancel.
        # See `_paused_for_its_sockets`.
        self._paused_legs: dict[str, float] = {}
        self._sync_lock = asyncio.Lock()
        self._synced_at = NEVER
        # How many fills arrived that could not be attributed to an account.
        # Counted rather than merely logged: if this is not zero at the end of
        # a run, the book was being rebuilt from HTTP rather than followed.
        self._unattributed_fills = 0
        # Per account: when something last happened there that the book did
        # not see -- an update that named no account, a socket that fell
        # behind or came back -- and when the last successful read of it
        # began. A leg with an account whose read is older than its doubt is
        # neither hedged nor traded until a read catches up. See
        # `_mark_unread`.
        self._unread_since: dict[str, float] = {}
        self._read_at: dict[str, float] = {}
        # The last execution-target answer, and when it was read.
        self._target_answer: tuple[float, str | None] = (NEVER, None)
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
        # Sides of a market a hedge is sweeping right now, as
        # (symbol, maker_is_buy) -> how many hedges. See `_hedge_leg`.
        self._sweeping: dict[tuple[str, bool], int] = {}
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

    def _risk_groups(self) -> list[tuple[str, str, tuple[str, ...]]]:
        """The live groups, for the risk monitor's per-group limit."""
        return [
            (f"group {self._group_ids.get(key, '?')}", group.symbol, tuple(group.accounts))
            for key, group in list(getattr(self, "_groups", {}).items())
        ]

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
                # The book is now behind the exchange by an unknown amount for
                # every account on this socket, so they are marked rather than
                # merely stale-dated: no leg with one of them is hedged until
                # a read has caught up.
                #
                # Marking was not enough on its own the first time: this used
                # to only reset the freshness clock, and the worker does not
                # consult it -- it hedges straight off the queue. So the hedge
                # ran six times against a book that never moved, each order
                # making the imbalance it was trying to correct larger, until
                # the ceiling stopped it $375 off-hedge.
                self._mark_unread(session, "a fill named no account")
                self._hedge_queue.put_nowait(symbol)
                return

            is_buy = fill.side == Side.BUY
            size = float(fill.size or 0.0)
            if size <= 0:
                return
            self._note_fill_delay(session, fill)

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
            if key is not None:
                roles = self._roles_for_key(key)
            elif getattr(self, "pairing", None) is not None:
                # A pool run with no group owning this account: there is no
                # configured pair to fall back to that means anything. Looking
                # one up created a phantom `state.legs[symbol]` -- which then
                # sat at IDLE, cycle 0, in every summary of the run -- and
                # booked reservations under the wrong key.
                roles = None
            else:
                roles = self._roles_by_symbol(self.state.leg(symbol).phase).get(symbol)
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

        One response at a time. The worker and the supervisor both come here,
        and `guard.check` no longer uses up what it reports -- so without the
        lock both would find the same shrink and close it twice. The second
        caller waits, looks again, and finds it acknowledged or explained.
        """
        lock = self.__dict__.setdefault("_guard_lock", asyncio.Lock())
        async with lock:
            return await self._respond_to_shrinks(phase)

    async def _respond_to_shrinks(self, phase) -> bool:
        # Every account. A position closed out from under us on the fifth
        # account of a pool is the same event as one on the first, and the
        # response -- stop rebuilding the pair -- is the same too.
        events = self.guard.check(
            self.book, phase, [s.pubkey for s in self.all_sessions]
        )
        if not events:
            return False

        # The legs the events are in, and only those. Their hedges wait while
        # this decides -- the hedge rule's answer to a liquidated hedger is a
        # fresh position on the account that just ran out of margin -- and
        # every other group trades and hedges on. This ran inline in the hedge
        # worker once, and every group's hedges waited out its reads, re-reads
        # and pauses behind it.
        hit = [s for s in self.all_sessions if s.pubkey in self._accounts_hit(events)]
        async with self._holding_hedges({s.pubkey for s in hit}):
            if await self._deferred_to_our_own_orders(events):
                return False

            if await self._settled_by_a_fresh_read(events):
                return False

            confirmed = await self._liquidation_confirmed(events)
            if not confirmed and await self._settled_after_waiting(events):
                return False
            label = "liquidation" if confirmed else "position closed externally"
            for event in events:
                log.critical("%s: %s", label.upper(), event.describe())
            # Answered from here on, so a look while the closes go out does
            # not report these again. Not before: until this point the
            # response could still have ended in "not a close at all", and an
            # event acknowledged then and not acted on would be lost.
            self.guard.acknowledge(events)

            # Hedging stops before the first close goes out, everywhere: the
            # run halts after this, and the emergency stop that follows closes
            # every account. The hedge worker used to run on beside this: on a
            # live run it "hedged" the closes as they filled, and the accounts
            # the guard had just closed ended holding fresh shorts (m1s1
            # -0.0103, m1s4 -0.0211). A resting order filling mid-close would
            # have been the same thing from the other side.
            self._closing_out = True
            reason = f"{label} -- " + "; ".join(event.describe() for event in events)
            # The halt is raised whatever happens in between. It used to follow
            # the closes, so anything escaping them -- a market with no spec is
            # a KeyError -- left the guard with hedging suspended and no halt:
            # the run ended through the generic failure path, which cancels
            # orders but flattens nothing and records no halt for the next
            # start to see.
            try:
                await self._close_broken_legs(hit, events, label)
            finally:
                # `_trigger_halt` sends the notification itself; sending it here
                # too delivered every liquidation halt twice.
                self._trigger_halt(reason)
        return True

    async def _close_broken_legs(self, hit, events, label: str) -> None:
        """Close what the broken legs still hold in the markets they broke in.

        Every account of each broken leg, whichever of them was hit: the pair
        is broken either way, and a lone leg is outright exposure. Other
        groups are hedged pairs, and the emergency stop the halt leads to
        closes them after.
        """
        affected = sorted({event.symbol for event in events})
        hit_keys = {s.pubkey for s in hit}
        async with contextlib.AsyncExitStack() as swept:
            # Both sides held as swept for as long as the cancels and closes
            # take: a chaser whose order is pulled re-places on its next tick
            # unless told not to, and a later close here would fill it.
            for symbol in affected:
                await swept.enter_async_context(self._sweep(symbol, True))
                await swept.enter_async_context(self._sweep(symbol, False))
            # All at once, and only these accounts -- this used to cancel every
            # account in the pool, one after another, before the first close.
            # Other groups' orders in the way of a close come off too, by id.
            # The closes go whether or not this worked, and any account it did
            # not work on is named: a position left open costs more than a
            # resting order that may fill against a close.
            await asyncio.gather(
                self._cancel_resting(affected, hit, concurrently=True),
                *(
                    self._clear_own_orders(symbol, side, skip=hit_keys)
                    for symbol in affected
                    for side in (True, False)
                ),
            )
            for symbol in affected:
                spec = self.feed.specs.get(symbol)
                if spec is None:
                    log.critical(
                        "no market spec for %s -- cannot size its close; "
                        "close it by hand now", symbol,
                        extra={"alert": True},
                    )
                    continue
                for session in hit:
                    size = self.book.authoritative(session.pubkey, symbol)
                    # Below the market's minimum the exchange refuses the
                    # close, and the refusal counts toward the reject streak.
                    # The lot alone used to decide here.
                    rounded = tradeable_size(
                        spec, abs(size), self._taking_price(symbol, buying=size < 0)
                    )
                    if not rounded:
                        continue
                    try:
                        # size > 0 is long, so closing it is a sell.
                        await session.close_market(symbol, size < 0, rounded)
                        log.critical(
                            "closed %s %.8f on %s after %s",
                            symbol, rounded, session.name, label,
                        )
                    except Exception as exc:  # noqa: BLE001 - the next close still goes
                        log.critical(
                            "COULD NOT CLOSE %s on %s after %s: %s -- "
                            "close it by hand now",
                            symbol, session.name, label, describe(exc),
                            extra={"alert": True},
                        )

    def _accounts_hit(self, events) -> set[str]:
        """Every account in a leg that one of these events is in."""
        return {
            pubkey
            for event in events
            for pubkey in self._leg_accounts(event.account, event.symbol)
        }

    @contextlib.asynccontextmanager
    async def _holding_hedges(self, accounts: set[str]):
        """Hold every hedge on a leg with an account in `accounts`, meanwhile.

        A held hedge is not dropped: its leg is queued again on the way out, so
        a leg the guard lets go is hedged against whatever came in while it
        was held.
        """
        holds = self.__dict__.setdefault("_guard_holds", {})
        for pubkey in accounts:
            holds[pubkey] = holds.get(pubkey, 0) + 1
        try:
            yield
        finally:
            for pubkey in accounts:
                holds[pubkey] -= 1
                if not holds[pubkey]:
                    del holds[pubkey]
            waiting = self.__dict__.setdefault("_held_keys", set())
            for key in sorted(waiting):
                self._hedge_queue.put_nowait(key)
            waiting.clear()

    def _held_by_guard(self, roles: LegRoles) -> bool:
        """Whether the liquidation guard is deciding about this leg; if so,
        it is noted to be queued again once the guard lets it go."""
        holds = self.__dict__.get("_guard_holds")
        if not holds or not any(pubkey in holds for pubkey in roles.accounts):
            return False
        self.__dict__.setdefault("_held_keys", set()).add(roles.key)
        return True

    async def _answer_liquidation(self) -> None:
        """The guard's response to a shrink the position handler saw, as a task."""
        try:
            await self._guard_liquidation(self._phases_by_account())
        except Exception as exc:  # noqa: BLE001 - a task nobody awaits must say so
            # Nothing is lost: an event is reported until it is acknowledged,
            # so the next position update flags it again, and the
            # supervisor's reconcile tick asks on its own.
            log.critical(
                "the liquidation guard failed: %s -- it looks again on the next "
                "position update and reconcile",
                describe(exc),
                extra={"alert": True},
            )

    async def _deferred_to_our_own_orders(self, events) -> bool:
        """True when every event is explained by an order of ours in the dark.

        Only ever true once the exchange has answered with fresh positions. If
        that read fails the caller carries on to the halt, because at that
        point nothing is known -- not what happened, and not what is open.

        Judged per leg. An unanswered order explains a shrink in the leg that
        sent it -- its maker's or a hedger's -- and nothing else: another
        group's timeout in the same market moved another group's accounts.
        """
        def in_doubt(event) -> bool:
            return any(
                event.symbol in session.symbols_in_doubt(DOUBT_WINDOW_S)
                for pubkey in self._leg_accounts(event.account, event.symbol)
                if (session := self.sessions.get(pubkey)) is not None
            )

        now = time.monotonic()
        for symbol, seen in self._doubt_deferrals.items():
            self._doubt_deferrals[symbol] = [
                at for at in seen if now - at < DEFERRAL_WINDOW_S
            ]

        explainable = [
            event for event in events
            if in_doubt(event)
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

        # Once per market per incident, not once per account in it: one
        # unanswered hedge moving two accounts is one event, and counting it
        # twice spent two thirds of the budget on it -- a live run printed
        # "4/3" on its second incident.
        for symbol in {event.symbol for event in explainable}:
            self._doubt_deferrals.setdefault(symbol, []).append(now)
        for event in explainable:
            log.warning(
                "%s -- but an order of ours in %s went unanswered, and a fresh "
                "read of its accounts has now replaced the guess. Carrying on "
                "rather than calling it a liquidation (%d/%d in the last "
                "%.0f minutes).",
                event.describe(), event.symbol,
                len(self._doubt_deferrals[event.symbol]), MAX_DOUBT_DEFERRALS,
                DEFERRAL_WINDOW_S / 60,
            )
            # This leg's accounts only. Both used to be cleared for the whole
            # market: every other group's high-water mark there -- so its next
            # real shrink would go unseen -- and every other group's doubt,
            # which is also what tells the orphan sweep that an unanswered
            # order of theirs may still be resting.
            self.guard.reset_symbol(event.symbol, accounts=(event.account,))
            for pubkey in self._leg_accounts(event.account, event.symbol):
                session = self.sessions.get(pubkey)
                if session is not None:
                    session.settled(event.symbol)
        return True

    def _leg_accounts(self, pubkey: str, symbol: str) -> tuple[str, ...]:
        """The accounts trading `symbol` alongside `pubkey`, itself included.

        Its group's, in a pool. With no group drawn and no pool -- a configured
        pair -- the run's accounts are the pair. An account between groups
        stands alone.
        """
        key = self._key_for_account(pubkey, symbol)
        if key is not None:
            return tuple(self._groups[key].accounts)
        if self.pairing is None and not self._groups:
            return tuple(session.pubkey for session in self.all_sessions)
        return (pubkey,)

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
                self._mark_unread(session, "a position update named no account")
                return
            if self._stream_lagging(session):
                # Behind by an unknown amount, so this is an OLD position, and
                # `set_authoritative` would write it over whatever is newer. A
                # live run did exactly that: the stream on one socket fell
                # thirty seconds behind, its stale updates outranked a fresh
                # read, and the hedger sold a position that was already hedged.
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
                self._mark_unread(session, "a snapshot named no account")
                return
            if self._stream_lagging(session):
                return  # stale, for the reason in the position handler

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
        """Drains fill signals and hands each leg's hedge to a task of its own.

        One task per leg, not one worker for all of them. The worker used to
        hedge each leg in turn, so group B's pre-hedge cancel and market order
        -- two round trips, ~640ms from here -- sat in front of group A's
        hedge, and a hedge that waits over a second gave back 2.6bps against
        0.8 under half a second. Legs share no accounts, so nothing orders
        their hedges but this loop did.

        The same leg is never hedged twice at once: a signal for a leg whose
        hedge is running is folded into one more pass once it finishes, which
        is what re-reading the queue after it used to do.
        """
        running: dict[str, asyncio.Task] = {}
        again: set[str] = set()
        responder: asyncio.Task | None = None
        try:
            while not self._stop.is_set():
                # Checked first and on every pass, including the idle one, and
                # answered by a task of its own. A liquidation leaves the
                # survivor outright directional, so it must not queue behind a
                # hedge; and it is no reason for every other group's hedges to
                # queue behind it -- the guard holds the legs it is deciding
                # about itself (`_holding_hedges`). One response at a time: a
                # flag raised while one runs is answered after it.
                if self._liquidation_seen.is_set() and (
                    responder is None or responder.done()
                ):
                    self._liquidation_seen.clear()
                    responder = asyncio.create_task(self._answer_liquidation())

                try:
                    key = await asyncio.wait_for(self._hedge_queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue

                if self._hedging_suspended:
                    # Closing out, or halted: the positions are being taken
                    # down, and a hedge now would open the opposite of each
                    # close.
                    continue

                # Nothing here waits on a read. A book behind the exchange is
                # judged per leg, by `_hedge_leg`, which reads in the
                # background and requeues: this loop used to force a full-pool
                # read inline, and every group's hedges waited behind it --
                # with no end at all while the read kept failing.
                for done in [k for k, task in running.items() if task.done()]:
                    del running[done]

                for leg_key in self._keys_to_hedge(key):
                    if leg_key in running:
                        again.add(leg_key)
                        continue
                    running[leg_key] = asyncio.create_task(
                        self._hedge_leg_until_quiet(leg_key, again)
                    )
        finally:
            # Waited for, not cancelled: a hedge cut off between sending its
            # order and hearing back is exactly the one nobody can account for.
            # `wait`, not `gather`, so a cancel of this task does not cascade
            # into them. The guard's response likewise: it may be mid-close.
            pending = [
                task for task in (*running.values(), responder)
                if task is not None and not task.done()
            ]
            if pending:
                await asyncio.wait(pending, timeout=15)

    async def _hedge_leg_until_quiet(self, leg_key: str, again: set[str]) -> None:
        """Hedge one leg, and again for as long as new signals arrived meanwhile."""
        try:
            await self._hedge_until_quiet(leg_key, again)
        except Exception as exc:  # noqa: BLE001 - a task nobody awaits must say so
            # A task's exception is only seen by whoever awaits it, and the
            # worker does not. When this ran inline, anything unexpected ended
            # the worker loudly; here it would vanish.
            log.critical(
                "hedge task for %s died: %s", leg_key, describe(exc),
                extra={"alert": True},
            )

    async def _hedge_until_quiet(self, leg_key: str, again: set[str]) -> None:
        while True:
            again.discard(leg_key)
            if self._hedging_suspended:
                return
            roles = self._roles_for_key(leg_key)
            if roles is None:
                return
            try:
                await self._hedge_leg(roles)
            except HedgeLimitExceeded as exc:
                self._trigger_halt(f"hedge limit exceeded -- {exc}")
                return
            except Exception as exc:  # noqa: BLE001 - the reconciler re-derives it
                # The reconciler re-derives from position, so a single failure
                # is recoverable; a persistent one trips the reject streak.
                log.error("hedge for %s failed: %s", leg_key, describe(exc))
            if leg_key not in again or self._stop.is_set():
                return
            # More fills arrived meanwhile. Let the rest of the burst land, so
            # the next pass hedges them in one order rather than one each.
            await asyncio.sleep(HEDGE_COALESCE_S)

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
        from `_groups` on release. Its leg then carried no side of its own:
        `_roles_for_key` used to fall through to the configured pair, whose
        side is the constant True, and now resolves it to nothing. Every
        group that had ever finished was counted, and counted as a buy.

        `_groups` is current, but not yet. `_run_group` fills it, and
        `_run_group` is a task: the dispatcher draws, spawns it, and comes
        straight back round to draw again without yielding, so the second draw
        still saw an empty map. A live run opened with both groups on the bid:

            === group 1 drawn: BTC-USD AAAAAA..AAA1 BUY ... at 2.20bps ===
            === group 2 drawn: BTC-USD BBBBBB..BBB2 BUY ... at 2.19bps ===

        `Pairing.draw` registers in `active` before it returns, so by the time
        the dispatcher asks about the next group, the last one is there.

        The phase still comes from the leg, because that is what says whether a
        group has turned around: a leg with no state yet has not, which is the
        right answer for one that was drawn a moment ago.

        And each group is counted on the side it is ABOUT to rest on, which is
        not always the one it rests on now -- see `_resting_side`.
        """
        resting = [0, 0]
        active = self.pairing.active if self.pairing is not None else {}
        for group_id, group in active.items():
            if group.symbol != symbol:
                continue
            resting[self._resting_side(group_id, group)] += 1
        if resting[True] == resting[False]:
            return self._rng.random() < 0.5
        return resting[True] < resting[False]

    def _resting_side(self, group_id: int, group: Group) -> bool:
        """The side a group's maker will be resting on for most of what is ahead.

        A group in HOLD, or well into its OPEN, is about to turn round: what it
        bought it must sell. Counted on the side it has now, it was invisible
        to the draw that mattered. A live run halted like this:

            g23 SELL, opening      g24 drawn BUY  (1 sell : 0 buys)
            g25 drawn on a tie     BUY
            g23 turns to its exit  BUY  -- three makers on the bid

        Three of our orders at one price split the same sellers three ways,
        each group filled at a third of the pace, and one ran out its thirty
        minutes at 66%. Counting g23 as the buyer it was about to become makes
        g25 a seller.
        """
        leg = self.state.legs.get(self.group_key(group_id, group.symbol))
        turning = False
        if leg is not None:
            if leg.phase in (Phase.HOLD, Phase.EXIT):
                turning = True
            elif leg.phase == Phase.OPEN and leg.target_size > 0:
                held = abs(self.book.effective(group.maker, group.symbol))
                turning = held >= TURNING_FRACTION * leg.target_size
        return group.maker_is_buy != turning

    async def _hedge_leg(self, roles: LegRoles) -> HedgeResult:
        """Hedge one leg: the one way every hedge in a run goes out.

        The worker and the reconciler both come through here. The reconciler
        used to call the hedger directly, so its hedges -- 6 to 15% of all of
        them on live runs -- swept our own resting orders without pulling
        them first.

        While it runs, the side it sweeps is marked, and `_drive_leg` places
        nothing there. `_clear_hedge_path` can only pull an order whose id is
        known, and a chaser mid-placement has none yet; worse, a second group
        re-placing onto the swept side is what turned one self-trade into a
        ping-pong on a live run -- six hedges in four seconds at one price,
        each filling the other group's fresh order and triggering its hedge.

        A slice with no answer stays reserved against this leg, and a position
        read in the background settles it. Nothing else waits for that read.
        """
        if self._held_by_guard(roles):
            # The liquidation guard is deciding whether this leg was broken,
            # and it queues the leg again when it lets go.
            return HedgeResult(roles.symbol, 0.0, 0.0, False, "held by the liquidation guard")

        def suspended() -> bool:
            # Asked again once the hedger holds the leg's lock: the guard may
            # have started on this leg while the hedge waited for it.
            return self._hedging_suspended or self._held_by_guard(roles)

        side = (roles.symbol, roles.maker_is_buy)
        sweeping = self.__dict__.setdefault("_sweeping", {})
        sweeping[side] = sweeping.get(side, 0) + 1
        try:
            # Before the market order, not after: our own remainder is
            # resting on exactly the side it is about to sweep.
            if self._waiting_on_a_read(roles):
                # Something happened on one of this leg's accounts that the
                # book did not see -- its socket fell behind or came back, or
                # sent an update that named no account -- and no read has
                # caught up since. The book for these accounts may count a
                # fill twice, miss one, or hold a stale position, and a hedge
                # sized from it is a market order paid in full. A read of them
                # runs in the background and requeues this leg; asked for
                # again here, so one that failed is retried on the next signal
                # or reconcile tick.
                self._settle_doubt_soon(roles.key)
                return HedgeResult(roles.symbol, 0.0, 0.0, False, "waiting for a read")
            await self._clear_hedge_path(roles)
            if self._hedging_suspended:
                # The guard started closing while the path was being cleared.
                # Hedges run concurrently now, so the check at the top of the
                # caller is not the last word.
                return HedgeResult(roles.symbol, 0.0, 0.0, False, "hedging suspended")
            return await self.hedger.hedge(
                roles,
                mark_price=self.feed.reference_price(roles.symbol),
                suspended=suspended,
            )
        except HedgeInDoubt:
            # This leg only. Marking the whole book suspect stopped every
            # hedge in the pool until a full read finished -- seconds, half a
            # minute on a large pool, and no end at all if one account's read
            # kept failing -- while every other group's makers went on filling
            # unhedged. The unanswered slice is already held reserved against
            # this leg, which is what keeps it from being hedged twice; the
            # read only settles that sooner, so it runs beside trading.
            #
            # No answer at all is also a socket that has stopped keeping up.
            for pubkey in roles.hedgers:
                session = self.sessions.get(pubkey)
                if session is not None:
                    self._note_stream_lag(session, "a hedge got no answer")
            self._settle_doubt_soon(roles.key)
            raise
        finally:
            sweeping[side] -= 1
            if not sweeping[side]:
                del sweeping[side]

    def _being_swept(self, roles: LegRoles) -> bool:
        """Whether a hedge is sweeping the side this leg's maker rests on."""
        sweeping = getattr(self, "_sweeping", {})
        return bool(sweeping.get((roles.symbol, roles.maker_is_buy)))

    @contextlib.asynccontextmanager
    async def _sweep(self, symbol: str, resting_is_buy: bool):
        """Mark one side of `symbol` as being swept by a taking order.

        The mark `_hedge_leg` sets around a hedge, for the other taking
        orders: while it is held, `_drive_leg` places nothing on that side.
        `resting_is_buy` is the side the order sweeps -- the bid for a market
        sell, the ask for a market buy.
        """
        side = (symbol, resting_is_buy)
        sweeping = self.__dict__.setdefault("_sweeping", {})
        sweeping[side] = sweeping.get(side, 0) + 1
        try:
            yield
        finally:
            sweeping[side] -= 1
            if not sweeping[side]:
                del sweeping[side]

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
        await self._clear_own_orders(roles.symbol, roles.maker_is_buy)

    async def _clear_own_orders(
        self, symbol: str, resting_is_buy: bool, skip: set[str] | frozenset = frozenset()
    ) -> None:
        """Pull every live group's resting order off one side of `symbol`.

        `resting_is_buy` is the side a taking order is about to sweep: the
        bid for a market sell, the ask for a market buy. For a hedge that is
        the hedging maker's own side (`_clear_hedge_path`); for a cut-short
        close, which trades the same way as its maker, it is the other one.
        `skip` names makers whose orders are already being cancelled another
        way.
        """
        # Live groups, for the reason spelled out in `_least_crowded_side`:
        # a released leg no longer has a group to take its side from, and
        # has no order of ours on the book either.
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
            if group.symbol != symbol or leg is None or not leg.oid:
                continue
            other = self._roles_for_key(key)
            if other is None or other.maker_is_buy != resting_is_buy:
                continue
            session = self.sessions.get(other.maker)
            if session is None or other.maker in skip:
                continue
            if not self.chaser.may_be_resting(session, leg.oid):
                continue
            in_path.append((session, leg))

        async def pull(session, leg) -> None:
            # Taken before the await. The chaser is a separate task and can
            # replace this order while the cancel is in flight; clearing
            # `leg.oid` blindly afterwards then dropped the REPLACEMENT's id --
            # an order left resting that nothing tracked, free to fill the
            # leg past its size.
            oid = leg.oid
            try:
                await session.cancel(symbol, oid)
            except Exception as exc:  # noqa: BLE001 - the taking order still goes
                log.warning(
                    "%s: could not pull its %s order out of the path "
                    "(%s) -- sending anyway, which may trade against it",
                    session.name, symbol, describe(exc),
                )
                return
            if leg.oid == oid:
                leg.oid = None

        await asyncio.gather(*(pull(session, leg) for session, leg in in_path))

    def _socket_trouble(self, session: AccountSession) -> str:
        """What is wrong with this session's socket, or "" for nothing."""
        if self._socket_down(session):
            return "down"
        if self._stream_lagging(session):
            return "behind"
        if self._waiting_for(session.pubkey):
            return "waiting on a position read"
        return ""

    async def _paused_for_its_sockets(self, key: str, roles: LegRoles, leg) -> bool:
        """Hold a leg with an account on a socket that is down or behind.

        Its maker order cannot be managed -- every replace and cancel on a
        stalled socket times out, and a dropped one refuses them -- and its
        fills cannot be hedged on time, or at all: on a live run the chaser
        kept placing on a stalled maker for two minutes ("could not clear a
        possibly-resting order ... the leg may open larger than its size")
        while that group's hedges timed out one after another. A socket that
        has dropped is worse again: nothing that fills on it is announced, and
        it is found only by the next position read. Opening more of a position
        that cannot be managed is the wrong trade at any price.

        So the leg's resting order is pulled -- cancel-all, which goes over
        HTTP when the socket cannot carry it -- and nothing is placed until the
        socket is back and, if it was behind, has kept up for
        `STREAM_LAG_HOLD_S`. Hedging is not paused: what is already open still
        has to be covered. An order the cancel could not pull stays tracked as
        the leg's own, and the cancel is tried again every
        `PAUSED_CANCEL_RETRY_S` for as long as the pause lasts.
        """
        troubled = [
            (session, trouble)
            for pubkey in roles.accounts
            if (session := self.sessions.get(pubkey)) is not None
            and (trouble := self._socket_trouble(session))
        ]
        paused = self._paused_legs
        if not troubled:
            if key in paused:
                del paused[key]
                log.info("%s: its sockets are back and keeping up -- resuming", key)
            return False
        now = time.monotonic()
        last_cancel = paused.get(key)
        if last_cancel is None:
            session, trouble = troubled[0]
            log.warning(
                "%s: pausing its orders while %s's socket is %s",
                key, session.name, trouble,
            )
        elif not leg.oid or now - last_cancel < PAUSED_CANCEL_RETRY_S:
            return True
        paused[key] = now
        maker = self.sessions.get(roles.maker)
        if maker is not None:
            # Taken before the await, for the reason `_clear_own_orders`
            # gives: a hedge clearing its path can change it meanwhile.
            oid = leg.oid
            try:
                await maker.cancel_all([roles.symbol])
            except Exception as exc:  # noqa: BLE001 - the order stays tracked
                # Kept, not cleared. It was dropped on the claim that the
                # orphan sweep would retry, but that sweep runs only for a
                # symbol an unanswered submission left in doubt. Tracked, the
                # order is pulled again from here, by a hedge sweeping its
                # side, and replaced or pulled by the chaser when the leg
                # resumes.
                log.error(
                    "%s: could not pull its order: %s -- still tracking it, "
                    "and trying again in %.0fs",
                    key, describe(exc), PAUSED_CANCEL_RETRY_S,
                )
            else:
                if leg.oid == oid:
                    leg.oid = None
        return True

    def _note_fill_delay(self, session: AccountSession, fill) -> None:
        """Judge a socket by how late this fill reached us over it.

        Measured against the lowest delay the socket has shown, not against
        zero: this machine's clock and the exchange's differ by some fixed
        amount, and only what is above it is lateness.
        """
        stamp = float(getattr(fill, "timestamp", 0) or 0)
        if stamp <= 0:
            return
        delay = time.time() - epoch_seconds(stamp)
        floors = self.__dict__.setdefault("_fill_delay_floor", {})
        client = id(session.client)
        floor = min(floors.get(client, delay), delay)
        floors[client] = floor
        if delay - floor > STREAM_LAG_S:
            self._note_stream_lag(session, f"a fill arrived {delay - floor:.1f}s late")

    def _note_stream_lag(self, session: AccountSession, why: str) -> None:
        """Distrust one socket's stream for a while, and read positions now.

        Seen live on a $110k run: one socket fell 7 to 30 seconds behind. Its
        hedges timed out while executing, its fills arrived after a position
        read had already counted them and were counted again, and its stale
        position updates outranked fresh reads -- one group was hedged back
        and forth four times before it settled.

        While a socket is behind, positions for its accounts come from HTTP,
        which kept up: its stream updates are ignored, reads overwrite them
        (`apply_read(force=True)`), and a leg with an account on it waits for
        a read begun after the lag was noticed. Legs on other sockets trade
        on untouched.
        """
        now = time.monotonic()
        fresh = now >= session.stream_lagging_until
        for other in self.all_sessions:
            if other.client is session.client:
                other.stream_lagging_until = now + STREAM_LAG_HOLD_S
        if fresh:
            log.warning(
                "%s: its socket is running behind (%s) -- positions for its "
                "accounts come from reads, not the stream, for %.0fs",
                session.name, why, STREAM_LAG_HOLD_S,
            )
        self._mark_unread(session)

    @staticmethod
    def _stream_lagging(session: AccountSession) -> bool:
        return time.monotonic() < session.stream_lagging_until

    def _mark_unread(self, session: AccountSession, why: str | None = None) -> None:
        """The book is behind something on this session's socket, as of now.

        Every account on the socket, because an update that named no account
        could have been any of theirs, and a socket behind or dropped is behind
        for all of them. Only those: this was one flag for the whole book once,
        and while it stood every hedge in the pool waited -- the worker blocked
        on a full-pool read, every hedge task and the reconciler declined --
        while every group's makers went on resting and filling. With reads that
        kept failing, the pool stayed unhedged with no end. Now the legs with
        an account here wait for a read of them, which starts at once in the
        background; every other leg trades and hedges on.

        Their makers are paused meanwhile (`_paused_for_its_sockets`): a leg
        that cannot be hedged must not keep filling.
        """
        now = time.monotonic()
        affected = [other for other in self.all_sessions if other.client is session.client]
        if why is not None and not any(self._waiting_for(other.pubkey) for other in affected):
            log.warning(
                "%s: %s -- re-reading its socket's positions before hedging "
                "or trading its legs",
                session.name, why,
            )
        for other in affected:
            self._unread_since[other.pubkey] = now
        mine = {other.pubkey for other in affected}
        for key, group in list(self._groups.items()):
            if mine.intersection(group.accounts):
                self._settle_doubt_soon(key)

    def _waiting_for(self, pubkey: str) -> bool:
        """Whether this account has news no successful read has caught up with."""
        return self._unread_since.get(pubkey, NEVER) > self._read_at.get(pubkey, NEVER)

    def _waiting_on_a_read(self, roles: LegRoles) -> bool:
        """Whether this leg has an account the book is known to be behind on."""
        return any(self._waiting_for(pubkey) for pubkey in roles.accounts)

    def _settle_doubt_soon(self, key: str) -> None:
        """Re-read a leg's accounts in the background, and hedge it after.

        For a leg with an unanswered slice, or one the book is behind on (see
        `_mark_unread`). Nothing waits on it. A slice's reservation already
        stops the leg being hedged twice, and a leg the book is behind on is
        not hedged at all meanwhile; the read replaces the guess with the
        exchange's answer, and the leg is then queued to be re-evaluated
        against the settled book.
        """
        keys = self.__dict__.setdefault("_doubt_keys", set())
        keys.add(key)
        task = getattr(self, "_doubt_task", None)
        if task is None or task.done():
            self._doubt_task = asyncio.create_task(self._settle_doubt())

    async def _settle_doubt(self) -> None:
        keys = self.__dict__.setdefault("_doubt_keys", set())
        # Looped: a slice that went unanswered while a read was under way
        # was not seen by it, and needs a read that begins after it.
        while keys and not self._stop.is_set():
            batch = set(keys)
            keys.clear()
            # Those legs' accounts, not the pool: over a hundred accounts a
            # full read is half a minute, and only these are in question.
            wanted = {
                pubkey
                for key in batch
                if (roles := self._roles_for_key(key)) is not None
                for pubkey in roles.accounts
            }
            try:
                await self._sync_positions(
                    max_age_s=0.0,
                    sessions=[self.sessions[p] for p in sorted(wanted) if p in self.sessions],
                )
            except Exception as exc:  # noqa: BLE001 - the reservation still holds
                log.error(
                    "could not re-read positions for %s: %s -- an unanswered "
                    "hedge stays reserved, and a leg the book is behind on is "
                    "not hedged or traded, until a read succeeds; tried again "
                    "on its next signal or reconcile tick",
                    ", ".join(sorted(batch)), describe(exc),
                )
                return
            for key in batch:
                self._hedge_queue.put_nowait(key)

    async def _cancel_resting(
        self, symbols, sessions=None, *, concurrently: bool = False
    ) -> set[str]:
        """Cancel resting orders in `symbols`, and name every account it could not.

        Every account the run trades unless `sessions` narrows it. Returns the
        pubkeys whose cancel failed, so the caller can act on them -- not least
        by not forgetting the orders it failed to pull.
        """
        failures = await cancel_all_orders(
            self.all_sessions if sessions is None else list(sessions),
            list(symbols),
            concurrently=concurrently,
        )
        if failures:
            log.critical(
                "COULD NOT CANCEL resting orders in %s on %s -- cancel them by "
                "hand now; an order left working fills with nothing hedging it",
                ", ".join(symbols),
                ", ".join(f"{session.name} ({describe(exc)})" for session, exc in failures),
                extra={"alert": True},
            )
        return {session.pubkey for session, _exc in failures}

    def _trigger_halt(self, reason: str) -> None:
        if self._halt_reason is None:
            log.critical("HALT: %s", reason, extra={"alert": True})
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
        does. The check that reported the event left the peak where it was --
        only acting on an event acknowledges it -- so a recovered position
        reads as no change at all and nothing is reported twice.

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
            # Sizes compared without their sign read a position that flipped
            # from +x to -x as "back at its old size".
            if current * event.previous < 0:
                return False

        log.warning(
            "%s: the position is back at its old size on a fresh read, so the "
            "shrink was a stale reading racing our own fill, not a close -- "
            "carrying on",
            ", ".join(sorted({e.account_name for e in events})),
        )
        return True

    async def _settled_after_waiting(self, events) -> bool:
        """Read again, later, before closing on a shrink the exchange denies.

        `_settled_by_a_fresh_read` asks at once, and close to the exchange
        "at once" is milliseconds after our own fill -- soon enough to be
        served from before it and to confirm the stale shrink it was meant to
        refute. The exchange has just said it liquidated nothing, so the shrink
        is either a manual close or a reading of ours; a few more seconds of a
        possibly lone leg is a small price next to closing every account at
        market over a reading.

        Only on that denial. When the exchange confirms a liquidation, or
        cannot be asked, the survivor is closed without waiting.
        """
        for delay in SHRINK_RECHECK_DELAYS_S:
            await asyncio.sleep(delay)
            if await self._settled_by_a_fresh_read(events):
                return True
        return False

    async def _liquidation_confirmed(self, events) -> bool:
        """Did the exchange actually liquidate something? True if it cannot say.

        Fails safe on purpose. This is asked in the middle of whatever went
        wrong, which is when the query is least likely to answer, and "I could
        not ask" must land on the same side as "yes": closing a healthy pair
        costs a spread, while trading on into a real liquidation does not have
        a bounded cost.

        Only the accounts that shrank are asked, about the markets they shrank
        in, all at once. It used to ask every account in the pool, one after
        another, and the first that failed to answer -- any of a hundred, most
        of them nowhere near the event -- made the shrink a liquidation; a
        liquidation of some other market in the last five minutes did the
        same. An account that does not answer is asked again before it counts.
        """
        wanted: dict[str, tuple[str, set[str]]] = {}
        for event in events:
            name, symbols = wanted.setdefault(event.account, (event.account_name, set()))
            symbols.add(event.symbol)
        answers = await asyncio.gather(*(
            self._exchange_liquidated(pubkey, name, symbols)
            for pubkey, (name, symbols) in wanted.items()
        ))
        if any(answers):
            return True
        log.warning(
            "the exchange reports no liquidation on %s, so this was something "
            "else closing the position -- a manual close, or an order of ours "
            "we never saw the answer to",
            ", ".join(sorted(name for name, _symbols in wanted.values())),
        )
        return False

    async def _exchange_liquidated(self, pubkey: str, name: str, symbols: set[str]) -> bool:
        """Whether the exchange recorded a risk event on this account in these
        markets recently. True when it could not be asked."""
        failure: Exception | None = None
        for delay in (0.0, *CONFIRM_RETRY_DELAYS_S):
            if delay:
                await asyncio.sleep(delay)
            try:
                recorded = await asyncio.to_thread(
                    recent_liquidations, self.config.http_url, pubkey
                )
            except Exception as exc:  # noqa: BLE001 - asked again, then unknown
                failure = exc
                continue
            # This account, these markets. An event that does not name its
            # market or its account cannot be ruled out, so it counts.
            relevant = [
                event for event in recorded
                if event.get("symbol") in (None, *symbols)
                and (event.get("user") or event.get("account") or pubkey) == pubkey
            ]
            for event in relevant:
                log.critical(
                    "exchange confirms %s on %s %s: %s",
                    event.get("eventType", "risk event"), name,
                    event.get("symbol", ""), event.get("reason", ""),
                )
            return bool(relevant)
        log.warning(
            "could not confirm with the exchange whether %s was liquidated "
            "(%s, %d attempts) -- treating it as one",
            name, describe(failure), 1 + len(CONFIRM_RETRY_DELAYS_S),
        )
        return True

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

    def _watched_sessions(self) -> list[AccountSession]:
        """Every session whose socket must stay up: the accounts', and the
        market data one, which carries no account but every price."""
        return [*self.all_sessions, *getattr(self.risk, "watch", ())]

    def _socket_down(self, session: AccountSession) -> bool:
        """Whether this session's socket is down, or being reconnected.

        Being reconnected counts: a heal begins by closing the socket, and a
        socket that read as connected while delivering nothing -- the stale
        case -- is no better while the heal is deciding that.
        """
        if session.dry_run:
            return False
        heal = self._heals.get(id(session.client))
        return not session.is_connected or (heal is not None and not heal.done())

    def _start_heals(self) -> None:
        """Start reconnecting every socket that is down or silent, one task each.

        A dropped socket was treated as fatal, which made a cycle only as long
        as the exchange's least reliable minute -- two live runs ended this way
        mid-cycle, having done nothing wrong. It is a transient condition and
        deserves a retry before the kill switch.

        Silence counts as well as a drop. A socket whose peer vanished without
        a close frame still reads as connected, so the silence is all there is
        to go on -- and the watchdog fires at `ws_stale_timeout_s` (30s by
        default) while the library's own keepalive needs `ping_interval +
        ping_timeout` (80s) to notice. That used to halt outright, killing runs
        over a condition a reconnect fixes in two seconds.

        In tasks of their own, not in the supervisor's loop. The supervisor
        used to await the heal, and one reconnect can take minutes (see
        `AccountSession.reconnect`): for all of that time the risk limits, the
        liquidation guard and the reconciler stopped, while makers on the
        dropped socket rested on and could fill with no fill event to say so.
        Now the supervisor keeps ticking, the legs on a socket being healed are
        paused and their orders pulled over HTTP (`_paused_for_its_sockets`),
        and the run halts only when a heal gives up.

        One task per socket: every session on it shares one client, and
        `reconnect_client` would make them share one set of attempts anyway.
        """
        stale_after = self.risk.config.ws_stale_timeout_s
        for session in self._watched_sessions():
            if session.dry_run:
                continue
            socket = id(session.client)
            heal = self._heals.get(socket)
            if heal is not None and not heal.done():
                continue
            # `is_connected` alone is not enough: a half-open socket reports
            # connected and delivers nothing, which is the case this exists for.
            if session.is_connected and session.last_message_age_s <= stale_after:
                continue
            # Charged here, as each task is created, and not inside it: two
            # sockets found down in one pass both start before either task
            # runs, and each would see the other under way and call itself the
            # second drop of somebody else's incident.
            refused = self._charge_reconnect(socket)
            if refused:
                log.error("%s: %s", session.name, refused)
                self._trigger_halt(f"disconnected: {session.name} -- {refused}")
                return
            self._heals[socket] = asyncio.create_task(self._heal(session))

    async def _heal(self, session: AccountSession) -> None:
        """Reconnect one socket, or halt the run when that cannot be done.

        Halting is still the outcome when the reconnect fails: an unattended
        bot that cannot see its fills must not keep trading on the accounts it
        cannot see. (A socket that keeps flapping halts before this starts --
        see `_charge_reconnect`.)
        """
        client = session.client
        restored = False
        try:
            if session.is_connected:
                log.warning(
                    "%s: WebSocket has delivered nothing for %.0fs but still "
                    "reads as connected -- reconnecting; its legs are paused "
                    "meanwhile",
                    session.name, session.last_message_age_s,
                )
            else:
                log.warning(
                    "%s: WebSocket dropped -- trying to reconnect; its legs are "
                    "paused meanwhile",
                    session.name,
                )
            began = SharedReconnect.generation
            restored = await session.reconnect()
            if not restored and await self._network_came_back(client, began):
                log.warning(
                    "%s: another socket is back, so the network is -- retrying",
                    session.name,
                )
                restored = await session.reconnect()
            if not restored:
                log.error("%s: could not reconnect", session.name)
                self._trigger_halt(
                    f"disconnected: {session.name} WebSocket could not be reconnected"
                )
                return
            await self._read_after_reconnect(session)
        except Exception as exc:  # noqa: BLE001 - a task nobody awaits must say so
            log.critical("the reconnect of %s failed: %s", session.name, describe(exc))
            self._trigger_halt(f"the reconnect of {session.name} failed: {describe(exc)}")
        finally:
            self._incident_ended_at = time.monotonic()
        if restored:
            # Hedged against what the read found, at once rather than on the
            # next reconcile tick: anything that filled before the paused
            # makers' orders came off was booked by the read and nothing else.
            for key, group in list(self._groups.items()):
                if any(
                    (other := self.sessions.get(p)) is not None and other.client is client
                    for p in group.accounts
                ):
                    self._hedge_queue.put_nowait(key)

    def _charge_reconnect(self, socket: int) -> str | None:
        """Spend the reconnect budget on this heal, or say why it is refused.

        Healing without a limit would reconnect forever: no rejection is
        recorded, so the reject streak never trips, and the cycle would never
        finish while the log scrolled past unread. "Keeps flapping" is measured
        over `RECONNECT_WINDOW_S`, so a socket that blips once an hour is
        forgiven each time and one that blips five times in ten minutes is not.

        One incident is charged once (see `HEAL_INCIDENT_GRACE_S`): a second
        socket dropping while the first is being repaired is the same outage
        seen twice. The same socket dropping again inside it is not, and pays.
        """
        now = time.monotonic()
        self._reconnect_times = [
            t for t in self._reconnect_times if now - t < RECONNECT_WINDOW_S
        ]
        ongoing = now - self._incident_ended_at < HEAL_INCIDENT_GRACE_S or any(
            not heal.done() for other, heal in self._heals.items() if other != socket
        )
        if not ongoing:
            self._incident = set()
        again = socket in self._incident
        self._incident.add(socket)
        if ongoing and not again:
            return None
        if len(self._reconnect_times) >= MAX_RECONNECTS:
            return (
                f"the sockets have dropped {len(self._reconnect_times)} times in "
                f"the last {RECONNECT_WINDOW_S / 60:g} minutes -- not reconnecting again"
            )
        self._reconnect_times.append(now)
        return None

    async def _network_came_back(self, client, began: int) -> bool:
        """After a failed reconnect: did any socket come back meanwhile?

        Waits first for every other socket's attempts still under way. The
        sockets are tried at the same time, so an outage that ends midway
        leaves one having spent its attempts against a network that was still
        down while another, timed differently, gets through. One socket coming
        back is proof the network did, and the one that failed gets another go
        -- free, because the budget is charged per heal, not per attempt.

        This is what a DNS outage did: the master used its attempts between
        15:09:30 and 15:09:35 and lost all of them, sub1 succeeded at 15:09:42,
        and the halt fired a second later on a master that nobody had tried
        again.

        Waits on the attempts themselves, not on the other heals: a heal that
        failed waits here too, and two of them waiting on each other would
        wait for ever.
        """
        attempts = {
            id(other.client): other.client.reconnect_share.task
            for other in self._watched_sessions()
            if other.client is not client
            and other.client.reconnect_share.task is not None
            and not other.client.reconnect_share.task.done()
        }
        if attempts:
            await asyncio.wait(list(attempts.values()))
        return SharedReconnect.generation != began

    async def _read_after_reconnect(self, session: AccountSession) -> None:
        """Re-read positions after a socket came back.

        The socket missed whatever happened while it was down, so the next
        decision must be made on exchange truth rather than on a book that
        stopped being updated. Read over HTTP, which did not drop, rather than
        waiting for the stream to refill the book.

        Guarded, and off the event loop. This ran bare and synchronous inside
        the supervisor once: one failed read -- a 429 or a 504, likely exactly
        when sockets are dropping -- raised out of `_supervise` and ended it,
        taking the exposure limits, the liquidation guard and the reconciler
        with it while the groups traded on.
        """
        # Behind as of the drop, whether or not this read works: a read that
        # fails leaves its legs waiting for one that does.
        self._mark_unread(session)
        mine = [other for other in self.all_sessions if other.client is session.client]
        try:
            await self._sync_positions(max_age_s=0.0, sessions=mine)
        except Exception as exc:  # noqa: BLE001 - its legs wait for a read
            log.error(
                "%s: could not re-read positions after reconnecting: %s -- "
                "its legs are not hedged or traded until a read succeeds",
                session.name, describe(exc),
            )

    # -- supervisor --------------------------------------------------------

    async def _supervise(self) -> None:
        """The run's safety checks, while the legs run themselves.

        One loop, not one per leg: the risk limits are about the pool, and
        checking them twice as often would buy nothing. It does only what is
        cheap and must be prompt -- the limits, and noticing a socket that is
        down -- every `chase_interval_s`. The position reads, the liquidation
        guard and the reconciler, which wait on the exchange, run beside it in
        `_reconcile_loop`; inline, a read of a large pool held up the next risk
        check by as long as it took.

        A violation a reconnect can fix starts a heal and does not halt; every
        other one halts at once, mixed with a drop or not -- otherwise a
        blinking socket would keep clearing a genuine violation.
        """
        reconciler = asyncio.create_task(self._reconcile_loop())
        try:
            while not self._stop.is_set():
                if reconciler.done():
                    # Its exception, if it died, ends this task with it, and
                    # `_until_legs_finish` reports it. Returning on its own is
                    # only allowed once the run is stopping.
                    reconciler.result()
                    if not self._stop.is_set():
                        raise RuntimeError("the reconciler stopped on its own")
                    return
                violations = self.risk.check()
                if any(v.kind not in SOCKET_FAULTS for v in violations):
                    self._trigger_halt("; ".join(str(v) for v in violations))
                    return
                if violations:
                    self._start_heals()
                await asyncio.sleep(self.config.chase_interval_s)
        finally:
            if not reconciler.done():
                reconciler.cancel()
            await asyncio.gather(reconciler, return_exceptions=True)

    async def _reconcile_loop(self) -> None:
        """Positions, the liquidation guard and the reconciler, on their own clock.

        Over HTTP throughout, so it carries on while a socket is down: legs on
        it are paused and their orders pulled, positions are read over HTTP,
        and every leg is hedged against them -- a leg whose hedger is on the
        dead socket once that socket is back.
        """
        last_sync = NEVER
        while not self._stop.is_set():
            now = time.monotonic()
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

            # Through `_hedge_leg`, which keeps the worker's rule: no hedge
            # for a leg the book is known to be behind on. The reconciler
            # once ignored it, and after a failed re-read went on hedging
            # every five seconds off the very book just declared stale.
            try:
                await self._reconcile_live_legs()
            except HedgeLimitExceeded as exc:
                self._trigger_halt(f"hedge limit exceeded -- {exc}")
                return
            except Exception as exc:  # noqa: BLE001 - retried next tick
                log.error("reconcile failed: %s", describe(exc))

            self.risk.log_exposure()
            self._log_untradeable_residuals()

            await asyncio.sleep(self.config.reconcile_interval_s)

    async def _reconcile_live_legs(self) -> None:
        """Re-run the hedge rule on every live leg, as `reconcile_net` does.

        Through `_hedge_leg`, so a correction pulls our own orders out of its
        path like any other hedge. One leg failing does not skip the rest; a
        hedge limit still ends the pass, because it ends the run.
        """
        for roles in self._live_roles():
            try:
                result = await self._hedge_leg(roles)
            except HedgeLimitExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 - the next leg still goes
                log.error("reconcile of %s failed: %s", roles.key, describe(exc))
                continue
            if result.acted:
                log.info(
                    "reconciler corrected %s %s %.8f (net was %+.8f)",
                    roles.key, "BUY" if result.is_buy else "SELL",
                    result.hedged_size, result.net_before,
                )

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
        awaiting = getattr(self.book, "awaiting_read", None)
        if awaiting is not None and awaiting():
            # A read was left unused because a fill arrived while it was in
            # flight; the fill is held until a read sent after it confirms it.
            return "a fill is waiting on a confirming read"
        if now - last_sync >= self.config.position_sync_interval_s:
            return "periodic"
        return ""

    async def _sync_positions(
        self,
        max_age_s: float = POSITION_FRESHNESS_S,
        sessions: list[AccountSession] | None = None,
    ) -> None:
        """Read positions from the exchange, sharing one read between callers.

        The legs and the supervisor all want the same numbers, and they ask on
        their own schedules. The lock makes a burst of callers wait on the first
        one's read rather than each issuing its own, which is what a live run
        showed happening: the same pair of account fetches three times over.

        `sessions` narrows it to some accounts -- a leg confirming its phase,
        a socket that came back. Every read goes through here, so each
        account's `_read_at` says when the last read of it began, whoever
        asked.

        Still the exchange's answer, not a guess -- only the request is shared.
        """
        wanted = list(self.sessions.values()) if sessions is None else list(sessions)
        requested = time.monotonic()
        async with self._sync_lock:
            if sessions is None and time.monotonic() - self._synced_at < max_age_s:
                return
            if wanted and all(
                self._read_at.get(s.pubkey, NEVER) >= requested for s in wanted
            ):
                # A read that began after this caller asked has just finished
                # while it waited on the lock. It answers the question; a
                # second one behind it would only repeat it.
                return
            started = time.monotonic()
            await sync_positions(wanted, self.book)
            if sessions is None:
                self._synced_at = time.monotonic()
            for session in wanted:
                self._read_at[session.pubkey] = started
            # A hedge slice that got no answer was held reserved because it
            # might have traded. This read began after it was sent, so the
            # book now says whether it did, and holding it any longer would
            # count it twice -- for the legs whose accounts it read.
            if self.hedger is not None:
                read = {s.pubkey for s in wanted}
                keys = None if sessions is None else [
                    key for key, group in self._groups.items()
                    if read.issuperset(group.accounts)
                ]
                self.hedger.in_flight.settle_doubtful(started, keys=keys)

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
        cut_at: float | None = None
        closed_at = NEVER
        said_at = started

        while not self._stop.is_set():
            if budget and cut_at is None and time.monotonic() - started > budget:
                limit = f"{symbol} {label} did not finish within " \
                        f"{self.config.max_phase_minutes:g} minutes"
                # Only a drawn group. A configured leg is the whole run, and
                # there is no other group to carry on beside it.
                if not leg.group_id:
                    self._trigger_halt(limit)
                    return
                cut_at = time.monotonic()
                await self._cut_short(key, leg, limit)
            elif cut_at is not None and time.monotonic() - cut_at > CUT_SHORT_GRACE_S:
                self._trigger_halt(
                    f"{symbol} {label} did not finish within "
                    f"{self.config.max_phase_minutes:g} minutes, nor "
                    f"{CUT_SHORT_GRACE_S / 60:g} more after it was cut short"
                )
                return

            if cut_at is not None and leg.phase == Phase.EXIT and not leg.complete:
                roles = self._roles_for_key(key)
                if roles is None:
                    return
                ready = time.monotonic() - closed_at >= CUT_SHORT_RETRY_S
                if await self._close_rest_at_market(roles, leg, send=ready):
                    closed_at = time.monotonic()
            elif leg.phase != Phase.HOLD and not leg.complete:
                roles = self._roles_for_key(key)
                if roles is None:
                    return
                if self._being_swept(roles):
                    # A hedge is taking this side right now. Anything placed
                    # here lands in its path; the next tick re-places it.
                    await asyncio.sleep(self.config.chase_interval_s)
                    continue
                if await self._paused_for_its_sockets(key, roles, leg):
                    await asyncio.sleep(self.config.chase_interval_s)
                    continue
                try:
                    await self.chaser.step(roles, leg)
                except Exception as exc:  # noqa: BLE001 - retried next tick
                    log.error("chase step for %s failed: %s", key, describe(exc))
                    await self._clear_orphans(key)
                    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                        maker = self.sessions.get(roles.maker)
                        if maker is not None:
                            self._note_stream_lag(maker, "an order got no answer")
                now = time.monotonic()
                if not leg.complete and now - said_at >= WAITING_LOG_S:
                    said_at = now
                    log.info("%s", waiting_line(
                        key, label,
                        left=self.chaser.remaining_size(roles, leg),
                        price=leg.price,
                        waited_s=now - started,
                        budget_s=budget,
                        group=bool(leg.group_id),
                    ))

            self._persist()

            if is_done() and await self._confirm_done(is_done, f"{symbol} {label}", key):
                log.info("%s %s complete", symbol, label)
                return

            await asyncio.sleep(self.config.chase_interval_s)

    async def _cut_short(self, key: str, leg, limit: str) -> None:
        """End one group's overlong phase without ending the run.

        An OPEN keeps whatever it has filled: its target becomes what is held,
        so the next chase step finds nothing left and the group moves on to
        HOLD and a normal exit. It is already hedged; nothing trades.

        An EXIT pulls its resting order and `_drive_leg` closes the maker's
        remainder at market from then on. The close lands as a maker fill,
        which the hedger answers the way it answers any exit fill -- reduce-only
        on the takers -- so the pair stays neutral while it unwinds, and the
        sweep in `_leg_exit` takes any residual after.
        """
        roles = self._roles_for_key(key)
        if roles is None:
            return
        if leg.oid:
            await self.chaser.cancel_leg(roles, leg)
        held = abs(self.book.effective(roles.maker, roles.symbol))
        if leg.phase == Phase.OPEN:
            leg.target_size = held
            what = f"keeping the {held:g} it has filled and moving on"
        else:
            what = f"closing the remaining {held:g} at market"
        self._persist()
        log.warning("%s: %s -- %s", key, limit, what)
        self.notifier.send_soon(self.notifier.error(f"{limit}\n{key}: {what}"))

    async def _close_rest_at_market(self, roles: LegRoles, leg, *, send: bool) -> bool:
        """Send a reduce-only market close for what the maker still holds.

        True when one went out. Below a lot or the minimum notional there is
        nothing that can be sent, and the leg is marked complete exactly as the
        chaser would have marked it. That is checked every tick; `send` only
        says whether another close may go out yet.

        Like a hedge, the close is a market order that sweeps a side our own
        orders may rest on -- and with the groups kept on opposite sides,
        another group's maker usually does. So that side is cleared first
        and marked as swept while the close is on its way, the way
        `_hedge_leg` does it. Not the same side as for a hedge: a hedge
        trades against its maker, this close trades with it.
        """
        spec = self.feed.specs[roles.symbol]
        held = self.book.effective(roles.maker, roles.symbol)
        # Measured at the touch the close takes, not the mark: a sell fills at
        # the bid, and a size that clears the minimum at the mark can fall
        # short of it there and be refused.
        size = tradeable_size(
            spec, abs(held), self._taking_price(roles.symbol, buying=held < 0)
        )
        if not size:
            leg.complete = True
            return False
        if not send:
            return False
        # A long closes with a sell, which sweeps the bid: our buys.
        swept_is_buy = held > 0
        await self._clear_own_orders(roles.symbol, swept_is_buy)
        try:
            async with self._sweep(roles.symbol, swept_is_buy):
                await self.sessions[roles.maker].close_market(roles.symbol, held < 0, size)
        except Exception as exc:  # noqa: BLE001 - retried after CUT_SHORT_RETRY_S
            log.error("%s: market close of a cut-short exit failed: %s",
                      roles.key, describe(exc))
            return False
        log.info("%s: sent a market close of %g on %s",
                 roles.key, size, self.sessions[roles.maker].name)
        return True

    def _taking_price(self, symbol: str, *, buying: bool) -> float | None:
        """The price a market order in `symbol` goes out at: the far touch.

        A buy takes the ask and a sell the bid. Falls back to the reference
        price when that side of the book is not there, which is what the
        hedger measures against too.
        """
        quote = self.feed.quote(symbol)
        touch = quote.best_ask if buying else quote.best_bid
        return touch or self.feed.reference_price(symbol)

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
        switched_off: list[str] = []
        for key, leg in self.state.legs.items():
            if not leg.group_id or not leg.maker or not leg.takers:
                continue
            if leg.phase in (Phase.IDLE, Phase.COMPLETE):
                continue
            if leg.symbol not in self.symbols:
                # A market switched off with a cycle still open on it. This run
                # has no spec, no feed and no chase settings for it, so resuming
                # the group got as far as the first hedge and died on a
                # KeyError naming the market -- which told the operator nothing.
                switched_off.append(f"{key} (mid-{leg.phase.value})")
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
                    extra={"alert": True},
                )
                continue
            self._groups[key] = group
            self._group_ids[key] = leg.group_id
            if self.pairing is not None:
                self.pairing.reserve(leg.group_id, group)
            resumed.append((leg.group_id, group, key))
            log.warning("resuming group %d mid-%s: %s", leg.group_id, leg.phase.value, group)
        if switched_off:
            raise RuntimeError(
                "a cycle is still open on a market switched off in settings.yaml: "
                + ", ".join(switched_off)
                + ". This run cannot finish it. Close it with `6. Close All "
                "Positions` -- that covers switched-off markets too -- then "
                "start again."
            )
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

        The accounts are released in a `finally`, so a group that finishes its
        cycle hands them back however it got there -- a pool that leaks
        accounts quietly stops being able to draw. A group that ends mid-cycle
        keeps them: it still holds positions, and only the run ending takes a
        group out mid-cycle.
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
            leg_now = self.state.legs.get(key)
            if leg_now is not None and leg_now.phase not in (Phase.IDLE, Phase.COMPLETE):
                # Ended mid-cycle -- stopped, halted, raised, or cancelled
                # because another group raised. It still holds positions, so
                # it stays registered: the liquidation guard and the
                # reconciler see its accounts only through `_groups`, and so
                # does the last hedge pass in `run`. Released, its fills
                # during the closing cancel sweep had no roles to be hedged
                # against, and the run exited holding them.
                #
                # Decided by the phase, not by the stop flag it used to be:
                # a group cancelled because a sibling raised was cancelled
                # before anything had set the stop, and dropped out of
                # everything that watches positions while it still had some.
                #
                # Not a `return`: that would swallow whatever `_run_leg` raised.
                log.info("=== group %d ended mid-cycle ===", group_id)
            else:
                if self.pairing is not None:
                    self.pairing.release(group_id)
                self._groups.pop(key, None)
                self._group_ids.pop(key, None)
                # The leg's state is kept, not dropped: a group that halted
                # mid-cycle has positions, and the state file is what a
                # restart reads to find them.
                log.info("=== group %d released ===", group_id)

    def _forget_finished_groups(self, keep: set[str]) -> None:
        """Drop earlier runs' finished groups, and never reuse their ids.

        A finished group's leg is kept while its run lasts -- a restart reads
        the state file to find positions -- but a leg that reached COMPLETE, or
        never left IDLE, holds nothing, and carrying it forward only grew the
        file by one leg per group ever drawn (serialised twice per save).

        Its id is still skipped: see `Pairing.skip_ids_through`.
        """
        if getattr(self, "pairing", None) is None:
            return
        ids = [leg.group_id for leg in self.state.legs.values() if leg.group_id]
        if ids:
            self.pairing.skip_ids_through(max(ids))
        finished = [
            key for key, leg in self.state.legs.items()
            if leg.group_id and key not in keep
            and leg.phase in (Phase.IDLE, Phase.COMPLETE)
        ]
        for key in finished:
            del self.state.legs[key]
        if finished:
            log.debug("forgot %d finished group leg(s) from earlier runs", len(finished))
            self._persist()

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
        self._forget_finished_groups({key for _i, _g, key in restored})
        for group_id, group, _key in restored:
            size = self.state.leg(self.group_key(group_id, group.symbol)).target_size
            running.add(asyncio.create_task(self._run_group(group_id, group, size)))
        # Groups this run has started, resumed ones included. `cycles` is
        # counted here because a drawn group lives for exactly one cycle under
        # a key of its own: the per-leg count in `_run_leg` never passed 1,
        # so `cycles: 1` traded until someone pressed stop.
        #
        # Taken from the state file rather than counted afresh: a restart
        # mid-run continues the run, and counting from zero again let
        # `cycles: 10` trade up to twenty. The resumed groups were counted
        # when they were first drawn. A file written before the count was
        # kept knows only about those.
        started = max(self.state.groups_started, len(restored))
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
                if self.config.cycles and started >= self.config.cycles:
                    log.info(
                        "%d of %d cycle(s) started -- drawing no more groups",
                        started, self.config.cycles,
                    )
                    break

                drawn = None
                if self.pairing is not None:
                    symbol = self._rng.choice(self.symbols)
                    if self._market_paused(symbol):
                        # Fast market or a scheduled hour: no new group. Those
                        # already trading carry on and finish their cycle.
                        await asyncio.sleep(self.config.chase_interval_s)
                        continue
                    drawn = self.pairing.draw(
                        symbol, maker_is_buy=self._least_crowded_side(symbol)
                    )
                if drawn is None:
                    await asyncio.sleep(self.config.chase_interval_s)
                    continue

                group_id, group = drawn
                started += 1
                self.state.groups_started = started
                self._persist()
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
            if any(not task.done() for task in running):
                # Groups are cut off only when the run is ending -- one of them
                # raised, or this was cancelled -- so it is said first, and
                # everything they do on the way out reads it as the end.
                self._stop.set()
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)

    def _market_paused(self, symbol: str) -> bool:
        """Whether new groups on `symbol` are held back right now.

        Every market's gate is fed on every call, not only the one asked about:
        the movement window needs a steady stream of prices to mean anything,
        and the dispatcher asks about one market at a time.
        """
        pause = getattr(self.config, "pause", None)
        if pause is None or not pause.enabled:
            return False
        gates = self.__dict__.setdefault("_pause_gates", {})
        for sym in self.symbols:
            gate = gates.get(sym)
            if gate is None:
                gate = gates[sym] = PauseGate(pause, label=sym.split("-")[0])
            change = gate.update(self.feed.reference_price(sym))
            if change is not None:
                self._announce_pause(sym, change)
        gate = gates.get(symbol)
        return gate is not None and gate.paused

    def _announce_pause(self, symbol: str, change) -> None:
        if change.paused:
            log.warning("%s: pausing new groups -- %s", symbol, change.reason)
            text, prefix = f"{symbol}: no new groups -- {change.reason}", "⏸ pause"
        else:
            log.warning("%s: resuming new groups", symbol)
            text, prefix = f"{symbol}: market calm again, opening new groups", "▶️ resume"
        notifier = getattr(self, "notifier", None)
        if notifier is not None:
            notifier.send_soon(notifier.send(text, prefix=prefix))

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
                if not once:
                    # Shown as one number, so it follows whichever leg is
                    # ahead. Not for a drawn group: each lives one cycle under
                    # its own key, and reading the market-keyed legs here
                    # created them -- phantoms at IDLE, cycle 0, that the
                    # summary phase and the title then reported. A group's
                    # cycle is counted when it completes, below.
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
            if once:
                # The run's count of finished group cycles.
                self.state.cycle_index += 1
                self.title.set_cycle(self.state.cycle_index)
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
            # In the background. It was awaited here, with a fill-history walk
            # in front of it, while the group still held its accounts: every
            # cycle waited on Telegram and on paging through every account's
            # history before the accounts went back to the pool.
            # For a group, the run's count: its own leg lives for one cycle,
            # so its index is always 1 and every report said "cycle 1 of N".
            self.notifier.send_soon(
                self._cycle_report(key, self.state.cycle_index if once else leg.cycle_index)
            )
            if once:
                return

    async def _cycle_report(self, key: str, cycle: int) -> None:
        """The cycle-complete notification, progress line included."""
        await self.notifier.cycle_complete(
            cycle=cycle,
            of=self.config.cycles or None,
            detail=f"{key}: {await self.progress_detail()}",
        )

    async def _confirm_done(self, is_done, label: str, key: str | None = None) -> bool:
        """Re-check a completion claim against freshly fetched positions.

        Finishing a phase is irreversible in effect -- EXIT completing means the
        cycle is recorded as closed and the next one may open on top. That
        decision must never rest on a position book that could have gone stale,
        so it is confirmed against the exchange before being acted on.

        Only this leg's accounts are read. It read the whole pool, one account
        at a time, to confirm one group of three -- over a pool of a hundred,
        half a minute, with every other caller of the lock waiting behind it.

        Through `_sync_positions` all the same. Read around it, the read did
        not count: a leg waiting on a read of these accounts kept waiting, and
        the hedge slices it had just accounted for stayed reserved.
        """
        roles = self._roles_for_key(key) if key else None
        try:
            if roles is not None and self._groups:
                await self._sync_positions(
                    sessions=[self.sessions[p] for p in roles.accounts if p in self.sessions],
                )
            else:
                await self._sync_positions()
        except Exception as exc:
            log.error("could not verify %s completion: %s", label, describe(exc))
            return False

        if is_done():
            return True

        log.warning(
            "%s looked complete but the exchange disagrees -- continuing", label
        )
        return False

    def _log_untradeable_residuals(self) -> None:
        """Say once, per leg and amount, that a residual is too small to hedge.

        It was said every reconcile tick -- the same +0.000007 eight times in
        thirty-five seconds -- with advice to raise the leg size. These are a
        few lots left by rounding and by reduce-only slices clipped to what
        an account holds, not by partial fills, and no leg size removes them.
        """
        reported = self.__dict__.setdefault("_residuals_reported", {})
        live = set()
        for roles in self._live_roles():
            price = self.feed.reference_price(roles.symbol)
            residual = self.hedger.untradeable_residual(roles, price)
            if not residual:
                reported.pop(roles.key, None)
                continue
            live.add(roles.key)
            if reported.get(roles.key) == residual:
                continue
            reported[roles.key] = residual
            spec = self.feed.specs[roles.symbol]
            log.warning(
                "%s carries %+.8f of unhedgeable exposure ($%s): below the "
                "%s minimum order of $%s, so no hedge can be sent for it. It is "
                "closed with the leg's exit.",
                roles.key,
                residual,
                round_notional(abs(residual) * (price or 0.0)),
                roles.symbol,
                spec.min_notional,
            )
        for key in [key for key in reported if key not in live]:
            del reported[key]

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
            # Where the run stood: the market's own leg for a configured pair,
            # or each group still holding it in a pool. The market's leg is
            # never driven in a pool, so it read "IDLE cycle 0" beside
            # positions that groups mid-cycle were holding.
            groups = [
                (key, self.state.legs[key])
                for key, group in getattr(self, "_groups", {}).items()
                if group.symbol == symbol and key in self.state.legs
            ]
            if groups:
                where = ", ".join(f"{key} {leg.phase.name}" for key, leg in groups)
            else:
                leg = self.state.legs.get(symbol)
                where = (
                    f"cycle {leg.cycle_index} {leg.phase.name}" if leg
                    else f"cycle {self.state.cycle_index}"
                )
            held = {
                session.name: self.book.authoritative(session.pubkey, symbol)
                for session in self.all_sessions
            }
            net = sum(held.values())
            if any(abs(v) > 0 for v in held.values()):
                log.warning(
                    "%s left open (%s): %s, net %+.8f%s",
                    symbol,
                    where,
                    ", ".join(
                        f"{name} {size:+.8f}" for name, size in held.items() if size
                    ),
                    net,
                    self._net_verdict(symbol, net),
                )
            else:
                log.info("%s flat (%s)", symbol, where)

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
        paused = [
            f"{sym} paused: {gate.reason}"
            for sym, gate in getattr(self, "_pause_gates", {}).items() if gate.paused
        ]
        note = (
            self._halt_reason or self._stop_requested
            or ("; ".join(paused) if paused else "S = stop and cancel")
        )
        lines.append(f"  {self._cost_text(burned)}   off-hedge ${exposure:,.0f}   "
                     f"running {elapsed}   |   {note}")
        return lines

    async def _refresh_status(self, interval_s: float = 1.0) -> None:
        """Keep the status block current while the legs work."""
        from .screen import SCREEN

        refreshing: asyncio.Task | None = None
        while not self._stop.is_set():
            # The totals were only read when the dispatcher asked whether the
            # target was met, and once it was, nothing asked again: the groups
            # still open went on closing for twenty minutes under a block that
            # no longer moved. Re-read on the same freshness clock meanwhile.
            read_at, _answer = getattr(self, "_target_answer", (NEVER, None))
            if (
                self.config.target.measures_fills
                and time.monotonic() - read_at >= TARGET_FRESHNESS_S
                and (refreshing is None or refreshing.done())
            ):
                refreshing = asyncio.create_task(self._refresh_totals())
            try:
                SCREEN.update(self.status_lines())
            except Exception as exc:  # noqa: BLE001 - never stop a run over a redraw
                log.debug("could not redraw the status block: %s", describe(exc))
            await asyncio.sleep(interval_s)

    async def _refresh_totals(self) -> None:
        """Re-read spend and volume for the status block, and log the cost.

        Only once the run has a baseline, and only if it is still the same one
        when the read comes back. The status loop starts before recovery has
        captured the baseline, so its first read had no start to count from
        and walked each account's LIFETIME history -- forty seconds over
        twelve accounts, landing after the run had begun and printing the
        accounts' lifetime fees and volume one second after "$0.00".
        """
        if not self.state.has_baseline:
            return
        since = self.state.baseline_at
        try:
            totals = await self._read_totals()
        except Exception as exc:  # noqa: BLE001 - cosmetic
            log.debug("could not refresh the totals: %s", describe(exc))
            self._totals_failed()
            return
        if not self.state.has_baseline or self.state.baseline_at != since:
            return  # the run's window moved while this was reading
        burned = _burned(totals.fees_usd)
        self._spread_usd = self._spread_cost(totals)
        answer = getattr(self, "_target_answer", (NEVER, None))[1]
        self._target_answer = (time.monotonic(), answer)
        self._progress = (burned, totals.qualifying_volume_usd)
        log.info("cost so far: %s", self._cost_text(burned))

    async def _final_cost_report(self) -> None:
        """Read the totals once more at the end, log them, and show them.

        The last redraw is what stays on screen after the run, so it has to
        be the final figure -- not one taken minutes before the last group
        closed.
        """
        if not self.config.target.measures_fills:
            return
        await self._refresh_totals()
        burned, volume = self._progress
        log.info(
            "run cost: %s on $%s of qualifying volume",
            self._cost_text(burned), f"{volume:,.2f}",
        )
        from .screen import SCREEN

        try:
            SCREEN.update(self.status_lines())
        except Exception as exc:  # noqa: BLE001 - cosmetic
            log.debug("could not redraw the final status block: %s", describe(exc))

    async def _wind_down(self) -> None:
        """End a run that is not halting: pull, hedge what filled, say what is left.

        One routine for every such ending -- a stop, a failure, an interrupt --
        because they leave the same thing behind: orders resting and fills
        that landed while they were pulled. Only the stop used to hedge those;
        a run that failed cancelled the same way and exited holding them.

        The stop is set first, so the worker and the supervisor stand down and
        this pass is the only hedging left. A halt ends through
        `_emergency_stop` instead, which closes everything rather than
        hedging it.
        """
        self._stop.set()
        await self._cancel_resting(self.symbols)
        await self._last_hedge_pass()
        with contextlib.suppress(Exception):
            self._log_open_positions()

    async def _last_hedge_pass(self) -> None:
        """Hedge whatever filled while a stop was pulling the orders.

        The hedge worker leaves its loop the moment the stop is set, and the
        cancel sweep that follows goes one account at a time -- over a large
        pool, tens of seconds. A maker fill in that window was booked and never
        hedged, and the run exited holding it; the comment in the dispatcher
        said open groups "finish their cycle", which they did not.

        So once nothing is resting, positions are re-read and every leg still
        registered gets the ordinary hedge rule one more time. A failure here
        is logged loudly rather than raised: the run is ending either way, and
        the open-positions report right after says what is left.
        """
        try:
            await self._sync_positions(max_age_s=0.0)
            await self._reconcile_live_legs()
        except Exception as exc:  # noqa: BLE001 - reported, the run ends anyway
            log.critical(
                "could not hedge fills that landed during the stop: %s -- "
                "check the positions below",
                describe(exc),
                extra={"alert": True},
            )

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
            if self.state.begin_run(time.time()):
                self._persist()
            self.title.set_cycle(self.state.cycle_index)
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
                await self._wind_down()

            # Before the baseline is cleared: the totals are read from it.
            await self._final_cost_report()

            if self._halt_reason:
                raise Halted(self._halt_reason)

            # Reached the end on its own terms, so the goal is spent and the
            # next start measures a new one. Kept after a stop or a halt
            # instead: those are interruptions, and resuming should not hand
            # back progress that was already paid for.
            self.state.remember_run(time.time())
            self.state.clear_baseline()
            self._persist()

        except Halted as exc:
            await self._emergency_stop(str(exc))
            raise
        except asyncio.CancelledError:
            log.warning("interrupted -- cancelling strategy orders")
            await self._wind_down()
            raise
        except Exception as exc:
            # Anything else used to leave straight through `finally`, which
            # stops the hedge worker but pulls nothing: every other group's
            # resting order stayed on the book, and a fill on one of them
            # landed with no hedge coming. One 429 inside a residual sweep
            # was enough.
            log.critical(
                "run failed (%s) -- cancelling every resting order", describe(exc),
                extra={"alert": True},
            )
            await self._wind_down()
            raise
        finally:
            self._stop.set()
            for task in (*legs, supervisor, worker, status):
                task.cancel()
            doubt_task = getattr(self, "_doubt_task", None)
            if doubt_task is not None:
                doubt_task.cancel()
            # A reconnect has nothing left to serve, and one that ran on would
            # reopen a socket after the run ended.
            for heal in self._heals.values():
                heal.cancel()
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
        read_at, _answer = getattr(self, "_target_answer", (NEVER, None))
        if time.monotonic() - read_at < TARGET_FRESHNESS_S:
            # The target check read it moments ago. This ran after every
            # group's cycle and walked every account's whole fill history each
            # time, around the cache that exists for exactly that cost -- and
            # drew 429s doing it.
            burned, volume = self._progress
        else:
            try:
                totals = await self._read_totals()
            except Exception as exc:  # noqa: BLE001 - cosmetic
                log.debug("could not read totals for the progress line: %s", describe(exc))
                self._totals_failed()
                return ""
            # Counted the same way the target counts, or the notification
            # would report one number while the stop rule acted on another.
            burned = _burned(totals.fees_usd)
            volume = totals.qualifying_volume_usd
            self._spread_usd = self._spread_cost(totals)

        parts = []
        if target.burn_usd > 0:
            parts.append(f"burn ${burned:,.4f} / ${target.burn_usd:,.2f}")
        if target.volume_usd > 0:
            parts.append(f"volume ${volume:,.2f} / ${target.volume_usd:,.2f}")
        parts.append(self._cost_text(burned))
        # Cached for the status block, which redraws every second and must not
        # pay for a fill-history walk to do it.
        self._progress = (burned, volume)

        detail = "  ".join(parts)
        if detail:
            self.title.set_note(detail)
        return detail

    def _spread_cost(self, totals) -> float | None:
        """Spread and slippage paid since the run began, as a positive cost.

        Everything the trades lost on price, fees apart: the gap between each
        maker fill and the hedges that covered it, spread paid on closes, and
        the like. Together with the fees it is what the accounts' balances
        went down by -- checked against a live run to the cent ($36.56 =
        $22.08 fees + $14.49 of this).

        None when a market with a position change has no price to value it.
        """
        prices = {}
        for symbol, base in getattr(totals, "base_by_symbol", {}).items():
            if abs(base) < 1e-12:
                continue
            price = self.feed.reference_price(symbol)
            if not price:
                return None
            prices[symbol] = price
        if not hasattr(totals, "price_result_usd"):
            return None
        return -totals.price_result_usd(prices)

    def _cost_text(self, burned: float) -> str:
        """`fees $X + spread/slippage $Y = $Z`, or fees alone when Y is unknown."""
        spread = getattr(self, "_spread_usd", None)
        if spread is None:
            return f"fees ${burned:,.2f}"
        return (
            f"fees ${burned:,.2f} + spread/slippage ${spread:,.2f} "
            f"= ${burned + spread:,.2f}"
        )

    def _totals_failed(self) -> None:
        """Hold off reading the totals again for TARGET_RETRY_S.

        Done by aging the last read's timestamp rather than with a separate
        clock, because every reader -- the target check, the status block, the
        progress line -- already waits on that one. The answer it carried is
        kept: a failure says nothing new about whether the target was met.
        """
        _read_at, answer = getattr(self, "_target_answer", (NEVER, None))
        retry_from = time.monotonic() - TARGET_FRESHNESS_S + TARGET_RETRY_S
        self._target_answer = (retry_from, answer)

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
        #
        # Counted from the baseline's moment, not as a lifetime total with the
        # baseline's lifetime total subtracted. The history walk stops at a
        # page cap; past it both totals stop growing and their difference
        # stops meaning anything -- an account past 20,000 fills could never
        # reach its target, or reached it at once.
        since = self.state.baseline_at if self.state.has_baseline else None
        return await asyncio.to_thread(
            realised_for_trees, self.master.http, self._trees(),
            since_ms=since * 1000 if since else None,
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
        # A moment, not a snapshot of totals: the history is read from here
        # on (see `_read_totals`). No read is needed to take it, so there is
        # nothing left here that can fail.
        self.state.baseline_fees_usd = 0.0
        self.state.baseline_volume_usd = 0.0
        self.state.baseline_self_trade_usd = 0.0
        self.state.baseline_at = time.time()
        self._persist()
        log.info("execution target counts fills from now; earlier ones do not count")

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
            self._totals_failed()
            return None

        burned = _burned(totals.fees_usd)
        volume = totals.qualifying_volume_usd
        self._spread_usd = self._spread_cost(totals)
        # Split, because only one part of it is ours to change: fees follow
        # the schedule, spread and slippage follow how the bot trades.
        log.info("cost so far: %s", self._cost_text(burned))

        answer = None
        if target.burn_usd > 0 and burned >= target.burn_usd:
            answer = f"burned ${burned:,.4f} of ${target.burn_usd:,.2f}"
        elif target.volume_usd > 0 and volume >= target.volume_usd:
            answer = f"qualifying volume ${volume:,.2f} of ${target.volume_usd:,.2f}"
        # A failure is remembered too, but only for TARGET_RETRY_S and without
        # an answer of its own (see `_totals_failed`): long enough not to
        # hammer an endpoint that is refusing us, short enough that a goal
        # reached meanwhile is still noticed within seconds.
        self._target_answer = (time.monotonic(), answer)
        # The status block redraws every second off this, and used to get it
        # only when a cycle finished -- so a six-minute OPEN phase showed
        # `$0 / $250,000  0.0%` while the log beside it counted past $19,000.
        #
        # Before the early return, not after it: the reading that REACHED the
        # target was the one never shown, and the block sat at 97.5% for the
        # rest of the run while the log said the target was met.
        self._progress = (burned, volume)
        if answer:
            return answer

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
        if getattr(totals, "truncated", False):
            log.warning(
                "the fill history walk stopped at its page cap -- the progress "
                "above may be short of the real figure"
            )
        return None

    async def _recover(self) -> None:
        """Reconcile persisted intent against exchange truth before trading.

        Truth comes from HTTP rather than the stream: at this point the account
        snapshot may not have arrived, and acting on an empty book would look
        exactly like having no positions.
        """
        # Off the loop, like every other read: the hedge worker and the
        # supervisor are already running, and the sockets are live.
        await self._sync_positions(max_age_s=0.0)

        if self.state.phase == Phase.HALTED:
            # Deliberately not cleared automatically, even though the per-leg
            # pass below would find both accounts flat. A halt means something
            # went wrong; restarting past it without the operator having read
            # the reason is how the same fault repeats unseen.
            #
            # Its orders are pulled all the same: refusing to start is no
            # reason to leave the halted run's orders working.
            await self._cancel_resting(self.symbols)
            raise RuntimeError(
                f"state file records a halt: {self.state.halted_reason}. "
                f"Read that reason first. Then `{script('run')} flatten --live` to clear "
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
            try:
                self._restored = self.restore_groups()
                self._refuse_unowned_positions()
            except RuntimeError:
                # Refusing to trade is right; leaving the last process's orders
                # resting while the operator reads why is not. They are pulled
                # first, below, on every other path -- this one raised before
                # getting there.
                await self._cancel_resting(self.symbols)
                raise
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
        failed = await self._cancel_resting(self.symbols)
        for key, leg in list(self.state.legs.items()):
            if failed and self._order_account(key, leg) in failed | {None}:
                # Its cancel failed, so its order may still be resting. The ids
                # stay: the chaser then finds it in the order map and replaces
                # or pulls it as its own. Every leg used to be wiped whatever
                # the cancel said, and an order that survived it rested on with
                # nothing in this run knowing it was there.
                continue
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

    def _order_account(self, key: str, leg) -> str | None:
        """The account a leg's resting order is on: its maker's, as of its phase."""
        roles = self._roles_for_key(key)
        return roles.maker if roles is not None else leg.maker

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
                if not is_tradeable(spec, size, price):
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

        failed = await self._cancel_resting(self.symbols)
        # Flattened whatever the cancel said: the positions are the exposure,
        # and a resting order is only a chance of more.
        await flatten(self.sessions, self.book, self.feed, self.symbols)
        if failed:
            # Once more for the accounts that refused. An order still working
            # there can reopen what the flatten just closed, with nothing left
            # running to hedge it.
            await self._cancel_resting(
                self.symbols, [self.sessions[p] for p in failed if p in self.sessions]
            )
        self._persist()


def build_chase_params(config: Config) -> dict[str, ChaseParams]:
    return {
        leg.symbol: ChaseParams(
            offset_bps=leg.offset_bps,
            max_distance_bps=leg.max_distance_bps,
            max_order_size=leg.max_order_size,
            chase_patience_s=leg.chase_patience_s,
            improve_ticks=leg.improve_ticks,
            join_depth_usd=leg.join_depth_usd,
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

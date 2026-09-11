"""The delta-neutral cycle: OPEN -> HOLD -> EXIT -> COMPLETE.

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
from .fees import realised_for_tree
from .hedger import Hedger, HedgeLimitExceeded, LegRoles
from .marketdata import round_notional
from .notify import Notifier
from .positions import PositionBook, SeenTrades
from .reconcile import (
    cancel_all_orders,
    flatten,
    reconcile_net,
    sync_positions,
    sync_positions_http,
)
from .risk import RiskMonitor
from .state import Phase, StateStore, StrategyState
from .window import WindowTitle
from .ws_compat import fill_trade_id
import contextlib

log = logging.getLogger(__name__)


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
        self.symbols = [config.btc.symbol, config.sol.symbol]
        self._seen_trades = SeenTrades()
        self._hedge_queue: asyncio.Queue = asyncio.Queue()
        self._stop = asyncio.Event()
        self._halt_reason: str | None = None

    # -- leg roles ---------------------------------------------------------

    def roles_for(self, phase: Phase) -> list[LegRoles]:
        """Which account makes and which hedges, for a given phase.

        The swap between OPEN and EXIT is what lets one hedge rule serve both.
        Every maker leg happens to be a buy: entry buys to open the longs, exit
        buys to cover the shorts.
        """
        master, sub1 = self.master.pubkey, self.sub1.pubkey
        btc, sol = self.config.btc.symbol, self.config.sol.symbol

        if phase == Phase.EXIT:
            return [
                # Sub1 covers its BTC short; master sells to close its long.
                LegRoles(btc, maker=sub1, taker=master, maker_is_buy=True, reduce_only=True),
                # Master covers its SOL short; sub1 sells to close its long.
                LegRoles(sol, maker=master, taker=sub1, maker_is_buy=True, reduce_only=True),
            ]

        # OPEN, and HOLD reuses the same roles so any drift is corrected on the
        # same account that was hedging during entry.
        return [
            LegRoles(btc, maker=master, taker=sub1, maker_is_buy=True, reduce_only=False),
            LegRoles(sol, maker=sub1, taker=master, maker_is_buy=True, reduce_only=False),
        ]

    def _roles_by_symbol(self, phase: Phase) -> dict[str, LegRoles]:
        return {roles.symbol: roles for roles in self.roles_for(phase)}

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

            roles = self._roles_by_symbol(self.state.phase).get(symbol)
            if roles is not None and session.pubkey == roles.taker:
                # This is one of our own hedge orders landing; retire its
                # reservation so net exposure reads correctly.
                self.hedger.note_taker_fill(symbol, size if is_buy else -size)

            log.info(
                "fill on %s: %s %.8f %s @ %.8f",
                session.name,
                "BUY" if is_buy else "SELL",
                size,
                symbol,
                float(fill.price or 0.0),
            )
            # Hand off to the worker rather than trading here -- awaiting an
            # order response inside this handler would deadlock the socket.
            self._hedge_queue.put_nowait(symbol)

        return handler

    def _make_position_handler(self, session: AccountSession):
        def handler(update) -> None:
            symbol = getattr(update, "symbol", None)
            if symbol not in self.symbols:
                return
            self.book.set_authoritative(session.pubkey, symbol, float(update.size or 0.0))

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
            try:
                symbol = await asyncio.wait_for(self._hedge_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            roles = self._roles_by_symbol(self.state.phase).get(symbol)
            if roles is None:
                continue
            try:
                await self.hedger.hedge(roles, mark_price=self.feed.reference_price(symbol))
            except HedgeLimitExceeded as exc:
                self._trigger_halt(f"hedge limit exceeded -- {exc}")
            except Exception as exc:
                # The reconciler re-derives from position, so a single failure
                # is recoverable; a persistent one trips the reject streak.
                log.error("hedge for %s failed: %s", symbol, exc)

    def _trigger_halt(self, reason: str) -> None:
        if self._halt_reason is None:
            log.critical("HALT: %s", reason)
            self._halt_reason = reason
            self.title.halted(reason)
            # Fire-and-forget: the halt path has orders to cancel and positions
            # to flatten, and must not wait on Telegram to do it.
            self.notifier.send_soon(self.notifier.halted(reason))
            self._stop.set()

    # -- phase driver ------------------------------------------------------

    async def _drive(self, phase: Phase, is_done, label: str) -> None:
        """Run the chase/reconcile/risk loop until `is_done()` or a halt."""
        last_reconcile = 0.0
        roles_list = self.roles_for(phase)

        while not self._stop.is_set():
            violations = self.risk.check()
            if violations:
                self._trigger_halt("; ".join(str(v) for v in violations))
                break

            for roles in roles_list:
                leg = self.state.leg(roles.symbol)
                if leg.complete and phase != Phase.HOLD:
                    continue
                if phase == Phase.HOLD:
                    continue
                try:
                    await self.chaser.step(roles, leg)
                except Exception as exc:
                    log.error("chase step for %s failed: %s", roles.symbol, exc)

            now = time.monotonic()
            if now - last_reconcile >= self.config.reconcile_interval_s:
                last_reconcile = now
                # Re-read positions from the exchange before correcting against
                # them. The in-memory book is fed by position updates, and the
                # optimistic fill overlay expires -- so if those updates stall,
                # the book decays toward zero and would otherwise make the bot
                # believe it is flat while real positions are still open.
                try:
                    await sync_positions(self.sessions.values(), self.book)
                except Exception as exc:
                    log.error("position sync failed: %s", exc)

                try:
                    corrections = await reconcile_net(self.hedger, roles_list, self.feed)
                    for correction in corrections:
                        log.info("reconciler corrected %s", correction)
                except HedgeLimitExceeded as exc:
                    self._trigger_halt(f"hedge limit exceeded -- {exc}")
                    break
                except Exception as exc:
                    log.error("reconcile failed: %s", exc)
                self.risk.log_exposure()
                self._log_untradeable_residuals()

            self.store.save(self.state)

            if is_done() and await self._confirm_done(is_done, label):
                log.info("%s complete", label)
                return

            await asyncio.sleep(self.config.chase_interval_s)

        if self._halt_reason:
            raise Halted(self._halt_reason)

    async def _confirm_done(self, is_done, label: str) -> bool:
        """Re-check a completion claim against freshly fetched positions.

        Finishing a phase is irreversible in effect -- EXIT completing means the
        cycle is recorded as closed and the next one may open on top. That
        decision must never rest on a position book that could have gone stale,
        so it is confirmed against the exchange before being acted on.
        """
        try:
            await sync_positions(self.sessions.values(), self.book)
        except Exception as exc:
            log.error("could not verify %s completion: %s", label, exc)
            return False

        if is_done():
            return True

        log.warning(
            "%s looked complete but the exchange disagrees -- continuing", label
        )
        return False

    # -- completion predicates --------------------------------------------

    def _is_neutral(self) -> bool:
        """Whether no hedge remains that could actually be placed.

        A residual smaller than one lot or below the market's minimum notional
        cannot be traded away, so it does not count as outstanding work -- if it
        did, the phase would wait forever on a hedge that can never be
        submitted. Such residuals are real exposure and are reported by
        `_log_untradeable_residuals`; the USD risk limits still police them.
        """
        for roles in self.roles_for(self.state.phase):
            if abs(self.hedger.in_flight.total(roles.symbol)) > 0:
                return False
            price = self.feed.reference_price(roles.symbol)
            if self.hedger.actionable_hedge(roles, price) > 0:
                return False
        return True

    def _log_untradeable_residuals(self) -> None:
        for roles in self.roles_for(self.state.phase):
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

    def _legs_complete(self) -> bool:
        return all(self.state.leg(symbol).complete for symbol in self.symbols)

    def _all_flat(self) -> bool:
        """Whether both accounts hold nothing in either strategy symbol.

        Uses confirmed positions only. This gates the end of the cycle, and an
        unconfirmed guess is not good enough to declare positions closed.
        """
        for symbol in self.symbols:
            spec = self.feed.specs[symbol]
            for session in (self.master, self.sub1):
                if abs(self.book.authoritative(session.pubkey, symbol)) >= spec.lot_size:
                    return False
        return True

    # -- phases ------------------------------------------------------------

    async def _phase_open(self) -> None:
        log.info("=== OPEN: %s ===", " | ".join(
            f"{r.symbol} maker={self.sessions[r.maker].name} taker={self.sessions[r.taker].name}"
            for r in self.roles_for(Phase.OPEN)
        ))
        self.state.phase = Phase.OPEN
        self.title.set_phase("OPEN")
        self.state.reset_legs(
            {
                self.config.btc.symbol: self.config.btc.size,
                self.config.sol.symbol: self.config.sol.size,
            }
        )
        self.store.save(self.state)

        await self._drive(
            Phase.OPEN,
            lambda: self._legs_complete() and self._is_neutral(),
            "open",
        )

    async def _phase_hold(self) -> None:
        if not self.state.hold_until:
            self.state.hold_until = time.time() + self.config.hold_minutes * 60
        self.state.phase = Phase.HOLD
        self.title.set_phase("HOLD")
        self.store.save(self.state)

        remaining = self.state.hold_remaining_s()
        log.info("=== HOLD: %.1f minutes ===", remaining / 60)

        await self._drive(
            Phase.HOLD,
            lambda: self.state.hold_remaining_s() <= 0,
            "hold",
        )

    async def _phase_exit(self) -> None:
        log.info("=== EXIT ===")
        # Pull anything left from the entry phase before reversing roles: an
        # entry order still resting would fight the closes.
        await self._cancel_strategy_orders(Phase.OPEN)

        self.state.phase = Phase.EXIT
        self.title.set_phase("EXIT")
        # Exit targets are whatever is actually held, not the configured size --
        # the entry may have filled only partially.
        self.state.reset_legs(
            {
                roles.symbol: abs(self.book.effective(roles.maker, roles.symbol))
                for roles in self.roles_for(Phase.EXIT)
            }
        )
        self.store.save(self.state)

        # The closing limit orders only unwind the maker side of each pair. The
        # taker side is reduced by hedges, and hedges stop once the pair is
        # neutral -- so if partial fills and lot rounding leave the two accounts
        # differing by less than one lot while both are still non-zero, no hedge
        # will ever fire and the taker's residual would sit there forever.
        # Completion therefore waits on the maker legs, then sweeps the rest.
        await self._drive(Phase.EXIT, self._legs_complete, "exit")

        if not self._all_flat():
            log.info("closing residual positions left after the exit legs")
            await flatten(self.sessions, self.book, self.feed, self.symbols)

    async def _cancel_strategy_orders(self, phase: Phase) -> None:
        for roles in self.roles_for(phase):
            leg = self.state.legs.get(roles.symbol)
            if leg and leg.oid:
                await self.chaser.cancel_leg(roles, leg)

    # -- entry point -------------------------------------------------------

    async def run(self) -> None:
        self.install_handlers()
        worker = asyncio.create_task(self._hedge_worker())

        try:
            await self._recover()

            cycle = 0
            while self.config.cycles == 0 or cycle < self.config.cycles:
                reached = self._target_reached()
                if reached:
                    log.info("execution target reached: %s", reached)
                    break
                cycle += 1
                self.state.cycle_index += 1
                self.state.cycle_started_at = time.time()
                self.title.set_cycle(self.state.cycle_index)
                log.info(
                    "=== cycle %d%s ===",
                    self.state.cycle_index,
                    f"/{self.config.cycles}" if self.config.cycles else "",
                )

                if self.state.phase in (Phase.IDLE, Phase.COMPLETE):
                    self.state.hold_until = 0.0
                    await self._phase_open()

                if self.state.phase == Phase.OPEN:
                    await self._phase_hold()

                if self.state.phase == Phase.HOLD:
                    await self._phase_exit()

                self.state.phase = Phase.COMPLETE
                self.state.hold_until = 0.0
                self.store.save(self.state)
                log.info("=== cycle %d complete ===", self.state.cycle_index)
                await self.notifier.cycle_complete(
                    cycle=self.state.cycle_index,
                    of=self.config.cycles or None,
                    detail=self._progress_detail(),
                )

        except Halted as exc:
            await self._emergency_stop(str(exc))
            raise
        except asyncio.CancelledError:
            log.warning("interrupted -- cancelling strategy orders")
            await cancel_all_orders([self.master, self.sub1], self.symbols)
            raise
        finally:
            self._stop.set()
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    def _progress_detail(self) -> str:
        """Spend and volume so far, for a notification and the window title.

        Reads the same fill history the execution target uses. A failure is not
        worth surfacing -- this is a progress line, not a control input -- so it
        degrades to an empty string and the cycle notification simply omits it.
        """
        target = self.config.target
        if not target.measures_fills:
            return ""
        try:
            totals = realised_for_tree(
                self.master.http, [self.master.pubkey, self.sub1.pubkey]
            )
        except Exception as exc:  # noqa: BLE001 - cosmetic
            log.debug("could not read totals for the progress line: %s", exc)
            return ""

        parts = []
        if target.burn_usd > 0:
            parts.append(f"burn ${totals.fees_usd:,.4f} / ${target.burn_usd:,.2f}")
        if target.volume_usd > 0:
            parts.append(
                f"volume ${totals.qualifying_volume_usd:,.2f} / ${target.volume_usd:,.2f}"
            )
        detail = "  ".join(parts)
        if detail:
            self.title.set_note(detail)
        return detail

    def _target_reached(self) -> str | None:
        """Whether a spend or volume target has been met, as a reason string.

        Checked between cycles only. A target reached halfway through an open
        position is not a reason to abandon it -- the exit has to run, or the
        pair is left directional.

        Totals come from the exchange's fill history rather than a local
        counter, so a restart resumes against the real figure. A failure to
        read it is logged and treated as "not reached": refusing to trade
        because a read-only endpoint is down would be worse than overshooting
        a soft goal by one cycle.
        """
        target = self.config.target
        if not target.measures_fills:
            return None

        try:
            totals = realised_for_tree(
                self.master.http, [self.master.pubkey, self.sub1.pubkey]
            )
        except Exception as exc:  # noqa: BLE001 - never block trading on this
            log.warning("could not read fill history for the execution target: %s", exc)
            return None

        if target.burn_usd > 0 and totals.fees_usd >= target.burn_usd:
            return (
                f"burned ${totals.fees_usd:,.4f} of ${target.burn_usd:,.2f}"
            )
        if target.volume_usd > 0 and totals.qualifying_volume_usd >= target.volume_usd:
            return (
                f"qualifying volume ${totals.qualifying_volume_usd:,.2f} "
                f"of ${target.volume_usd:,.2f}"
            )

        if target.burn_usd > 0:
            log.info(
                "burn progress: $%.4f / $%.2f", totals.fees_usd, target.burn_usd
            )
        if target.volume_usd > 0:
            log.info(
                "volume progress: $%.2f / $%.2f qualifying (self-trades $%.2f excluded)",
                totals.qualifying_volume_usd,
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

        has_positions = any(
            abs(self.book.authoritative(session.pubkey, symbol)) >= self.feed.specs[symbol].lot_size
            for session in (self.master, self.sub1)
            for symbol in self.symbols
        )

        if self.state.phase == Phase.HALTED:
            raise RuntimeError(
                f"state file records a halt: {self.state.halted_reason}. "
                "Investigate, run `bulkdn flatten` if positions remain, then delete "
                "the state file to start again."
            )

        if not has_positions:
            if self.state.phase not in (Phase.IDLE, Phase.COMPLETE):
                log.warning(
                    "state says %s but both accounts are flat -- starting clean",
                    self.state.phase.value,
                )
            self.state.phase = Phase.IDLE
            self.state.hold_until = 0.0
        else:
            log.warning(
                "recovered open positions with state phase=%s", self.state.phase.value
            )
            if self.state.phase in (Phase.IDLE, Phase.COMPLETE):
                # Positions exist that this bot has no plan for. Unwinding is
                # the only safe interpretation -- resuming an entry would add
                # to a position of unknown provenance.
                log.warning("positions exist with no recorded plan -- exiting them")
                self.state.phase = Phase.HOLD
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
            self.hedger, self.roles_for(self.state.phase), self.feed
        )
        for correction in corrections:
            log.warning("recovery corrected %s", correction)

        self.store.save(self.state)

    async def _emergency_stop(self, reason: str) -> None:
        log.critical("emergency stop: %s", reason)
        self.state.phase = Phase.HALTED
        self.state.halted_reason = reason
        self.store.save(self.state)

        await cancel_all_orders([self.master, self.sub1], self.symbols)
        await flatten(self.sessions, self.book, self.feed, self.symbols)
        self.store.save(self.state)


def build_chase_params(config: Config) -> dict[str, ChaseParams]:
    return {
        leg.symbol: ChaseParams(
            offset_bps=leg.offset_bps,
            max_distance_bps=leg.max_distance_bps,
            max_order_size=leg.max_order_size,
        )
        for leg in (config.btc, config.sol)
    }


def build_hedge_ceilings(config: Config) -> dict[str, float]:
    """Cap a single hedge at twice the leg's configured size.

    A required hedge larger than this means the position book and reality have
    diverged by more than the strategy can account for, and firing a very large
    market order on that basis would be worse than halting.
    """
    return {leg.symbol: leg.size * 2.0 for leg in (config.btc, config.sol)}

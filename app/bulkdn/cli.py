"""Command line entry point.

Trading is off by default: `run` will not submit a single order unless `--live`
is passed, so a config file alone cannot start trading.

This bot is mainnet-only, so `--live` is the ONLY interlock and it always
spends real funds. The mainnet banner is a warning, not a confirmation prompt,
and there is no rehearsal network to fall back to.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import logging.handlers
import sys

from bulk_api.common import SignatureDomain

from .accounts import (
    build_pool,
    verify_sub_account,
)
from .chaser import Chaser
from .console import watch_for_stop
from .config import Config, ConfigError, load_config
from .feed import MarketFeed
from .notify import Notifier
from .referral import AccessDenied
from .referral import check_access as check_referral_access
from .window import WindowTitle
from .impact import ImpactBook
from .hedger import Hedger
from .pairing import Pairing
from .positions import PositionBook
from . import proxy
from . import screen as screen_mod
from .reconcile import (
    cancel_all_orders,
    flatten,
    flatten_limit,
    sync_positions_http,
)
from .retry import describe
from .risk import RiskMonitor
from .settings import current_leverage, set_leverage
from .sizing import plan_sizes, resolve_notionals
from .state import Phase, StateStore
from .ws_compat import apply_ws_compat, quieten_sdk_prints
from .strategy import Halted, Strategy, build_chase_params, build_hedge_ceilings

log = logging.getLogger("bulkdn")


# Everything printed to the console is also appended here, in the folder the
# operator already has open. A console window scrolls, and is gone when it is
# closed -- so "it stopped overnight and I don't know why" had no answer. This
# file is that answer, and it is the first thing to ask anyone for.
LOG_FILE = "logs.txt"
# Rolls at 5MB and keeps two older files. A long live run writes a few MB a day,
# so this is roughly a week of history and cannot fill a disk.
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 2


# Loggers whose INFO output is bookkeeping rather than news: position reads,
# exposure sums, every order placed and every fill. About ninety lines per five
# minutes against twenty worth reading. All of it still goes to `logs.txt`,
# which is where it is wanted when something has to be reconstructed.
_QUIET_ON_SCREEN = (
    "bulkdn.chaser",
    "bulkdn.hedger",
    "bulkdn.reconcile",
    "bulkdn.risk",
)
# And the two from `strategy` that are the same kind of thing.
_QUIET_MESSAGES = ("fill on ", "reconciler corrected ")


class _ConsoleFilter(logging.Filter):
    """Keeps the screen to what a person watching would want to see.

    Anything WARNING or worse always passes: the point is to hide bookkeeping,
    not to hide trouble.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        if record.name in _QUIET_ON_SCREEN:
            return False
        return not record.getMessage().startswith(_QUIET_MESSAGES)


class _StatusHandler(logging.StreamHandler):
    """Prints above the status block instead of over the top of it."""

    def __init__(self, block):
        super().__init__()
        self.block = block

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.block.write_above(self.format(record))
        except Exception:  # noqa: BLE001 - logging must not take down a run
            self.handleError(record)


def configure_logging(level: str, log_file: str | None = LOG_FILE) -> None:
    """Log to the console, and to `log_file` alongside it.

    The file gets full timestamps where the console gets clock time only: on
    screen the date is obvious, in a file read days later it is the point.

    The console also gets far less of it -- see `_ConsoleFilter`.
    """
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # basicConfig's own handler prints straight to the stream, which would
    # scroll through the status block. Replaced rather than added to.
    root = logging.getLogger()
    for handler in list(root.handlers):
        if type(handler) is logging.StreamHandler:
            root.removeHandler(handler)
    console = _StatusHandler(screen_mod.SCREEN)
    console.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", datefmt="%H:%M:%S")
    )
    console.addFilter(_ConsoleFilter())
    root.addHandler(console)
    if log_file:
        try:
            handler = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUPS,
                encoding="utf-8",
            )
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            logging.getLogger().addHandler(handler)
        except OSError as exc:
            # A read-only folder or a file held open by an editor. Losing the
            # file is not a reason to refuse to trade, so this is a warning and
            # the console log carries on alone.
            logging.getLogger("bulkdn").warning(
                "could not open %s for logging (%s) -- console only", log_file, exc
            )

    # The SDK logs every frame at DEBUG, which drowns out the strategy.
    logging.getLogger("bulk_api").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    # And it prints, which no logger level can reach. Called again once the
    # SDK is fully imported -- this only reaches modules already loaded.
    quieten_sdk_prints()


class Runtime:
    """Wires the components together and owns their lifecycle."""

    def __init__(self, config: Config, dry_run: bool):
        self.config = config
        self.dry_run = dry_run
        self.symbols = [leg.symbol for leg in config.active_legs]

        # Must be installed before any client is constructed: it repairs fill
        # parsing and TLS handling inside the SDK itself.
        apply_ws_compat(
            insecure_ssl=config.ws_insecure_ssl,
            auto_bypass=config.ws_ssl_auto_bypass,
        )
        # Now that the SDK is fully imported, silence the rest of its printing.
        quieten_sdk_prints()

        # Read off the master rather than configured: a sub-account has no key
        # of its own, so its pubkey is a fact about the master, not a choice
        # the operator should have to copy by hand.
        domain = SignatureDomain[config.signature_domain_name]

        # The mode is a choice of KEYS, and nothing downstream of here needs
        # to know which was made. `single` narrows the pool to one master's
        # own accounts, so every group drawn from it is inside that tree;
        # `multi` hands over every key, so a group can span two masters. The
        # drawing, the trading and the accounting are the same code either
        # way -- which is why there is no second path to keep in step.
        keys = self._keys_in_play(config)
        # Every account under every key in play, on one socket per key.
        # Discovery is an HTTP call per key rather than something the operator
        # copies by hand: a sub-account is a fact about its master, and a list
        # typed into a file is a list that can be wrong.
        self.pool = build_pool(
            private_keys=keys,
            ws_url=config.ws_url,
            http_url=config.http_url,
            domain=domain,
            symbols=self.symbols,
            dry_run=dry_run,
        )
        if len(self.pool) < 2:
            raise ConfigError(
                f"{config.mode} mode needs at least two accounts to pair, and "
                f"the key(s) it uses produced {len(self.pool)}. Create a "
                "sub-account from Accounts Management."
            )
        # The first two keep their old names for the commands that act on
        # one account -- status, transfer, the market feed's socket.
        self.master, self.sub1 = self.pool[0], self.pool[1]
        log.info(
            "%s: %d accounts under %d key(s) on %d socket(s)",
            config.mode,
            len(self.pool),
            len(keys),
            len({id(s.client) for s in self.pool}),
        )

        self.sessions = {session.pubkey: session for session in self.pool}
        self.notifier = Notifier(config.telegram)
        self.title = WindowTitle(cycles=config.cycles)
        self.book = PositionBook(overlay_ttl_ms=config.overlay_ttl_ms)
        self.impact = ImpactBook(config.http_url)
        self.feed = MarketFeed(self.master, self.symbols)
        self.store = StateStore(config.state_file)

    @staticmethod
    def _same_tree(one, other) -> bool:
        """Whether these two accounts are signed for by the same key.

        Accounts under one key share a socket, so the client identity says
        it. The parent-child check below only means something inside a tree:
        in multi mode the second account of the pool can be another MASTER,
        and demanding that one master be a sub-account of another would fail
        a correctly configured run at startup.
        """
        return one.client is other.client

    @staticmethod
    def _keys_in_play(config: Config) -> list[str]:
        """The signing keys this run trades, which is what the mode decides.

        In single mode that is one key -- the one the operator picked, by its
        line number in the key file. Narrowing here rather than when a group
        is drawn means the other masters have no session at all: nothing
        subscribes on their behalf, nothing polls for them, and no later code
        has to remember that some accounts are present but off limits.
        """
        keys = config.private_keys or (
            [config.private_key] if config.private_key else []
        )
        if config.mode != "single":
            return keys
        index = max(1, config.single_master) - 1
        if index >= len(keys):
            raise ConfigError(
                f"single mode is set to master {config.single_master}, but only "
                f"{len(keys)} key(s) were loaded"
            )
        return [keys[index]]

    async def start(self, verify: bool = True) -> None:
        log.info(
            # The markets are named here because the mode can come from the
            # command line, so the settings file is no longer proof of what a
            # run actually traded. This line is.
            "mainnet mode=%s master=%s sub1=%s legs=%s (%s)",
            "DRY-RUN" if self.dry_run else "LIVE",
            self.master.pubkey,
            self.sub1.pubkey,
            ",".join(self.symbols),
            self.config.mode,
        )
        # Before anything is placed. A gate that tripped later would abandon
        # open positions and leave the pair directional.
        self.check_access()

        self.feed.load_specs()
        if verify and self._same_tree(self.master, self.sub1):
            verify_sub_account(self.master, self.sub1)

        # Curves are executor-published and change slowly, so they are read
        # once here rather than per hedge.
        if self.config.risk.max_hedge_impact_bps > 0:
            self.impact.refresh(self.symbols)
            missing = [s for s in self.symbols if not self.impact.has(s)]
            if missing:
                log.warning(
                    "no impact curve published for %s -- the hedge slippage "
                    "ceiling cannot be enforced there",
                    ", ".join(missing),
                )

        self.apply_sizing()

        if not self.dry_run:
            self.apply_leverage()

        await self.master.connect()
        await self.sub1.connect()
        await self.feed.subscribe()
        # Let the initial account snapshots and book updates land before any
        # decision is made on them.
        await asyncio.sleep(2.0)

    def check_access(self) -> None:
        """Refuse to start unless the master signed up under an allowed referral.

        The master is what gets checked, not the sub-account: a sub is created
        by the master and has no referral record of its own.
        """
        # Not guarded by config.access.enabled: a sealed build decides that
        # for itself, and a config flag that skips the call is the same bypass
        # as a config flag that empties the allow-list. check_access returns
        # "gating is off" on its own when the build is unsealed and the config
        # says so.
        decision = check_referral_access(self.master.pubkey, self.config.access)
        if not decision.allowed:
            raise AccessDenied(decision.reason)
        log.info("access granted: %s", decision.reason)

    async def stop(self) -> None:
        await self.master.disconnect()
        await self.sub1.disconnect()

    def apply_sizing(self) -> None:
        """Turn the configured sizes into sizes both accounts can carry.

        Dollar amounts are converted to quantities first, then every leg is
        weighed against the margin actually held.

        Runs in dry-run too, so a rehearsal shows the sizes a live run would
        really use rather than the ones written in the file.

        The legs are mutated in place because everything downstream -- the
        chaser's targets, the hedge ceilings, the exit sizes -- reads them from
        the config, and a second source of truth for size is how the two end up
        disagreeing.
        """
        legs = list(self.config.active_legs)
        # Priced over HTTP, not from the feed: this runs before the WebSocket
        # is connected, so the ticker cache is still empty and every price
        # would read as zero.
        prices = {s: self.feed.http_price(s) for s in self.symbols}

        # Dollar-denominated legs become base-coin sizes here, before anything
        # weighs them against margin. After this every leg has a `size`.
        resolve_notionals(legs=legs, specs=self.feed.specs, prices=prices)

        margin = {}
        for session in (self.master, self.sub1):
            account = session.full_account().get("margin") or {}
            margin[session.name] = float(account.get("availableMargin") or 0.0)

        plan = plan_sizes(
            legs=legs,
            specs=self.feed.specs,
            prices=prices,
            available_margin=margin,
            max_margin_fraction=self.config.max_margin_fraction,
        )

        for leg, sized in zip(legs, plan.legs, strict=True):
            leg.size = sized.actual
            # The per-order cap must come down with the leg, or it stops
            # capping anything.
            leg.max_order_size = min(leg.max_order_size, sized.actual)

        log.info(
            "sizing: %s  (margin available: %s)",
            plan.describe(),
            ", ".join(f"{name} ${value:,.2f}" for name, value in margin.items()),
        )

    def apply_leverage(self) -> None:
        """Set each leg's configured leverage on both accounts.

        Sub-accounts copy the master's settings at creation and are independent
        afterwards, so each account is set explicitly rather than assuming the
        child inherited anything.

        Only what differs is sent, and the exchange's own per-market ceiling
        from /exchangeInfo is checked first -- asking for more than a market
        allows is a config error, not something to discover from a rejection.
        """
        wanted = {
            leg.symbol: leg.leverage
            for leg in self.config.active_legs
            if leg.leverage is not None
        }
        if not wanted:
            return

        for symbol, value in wanted.items():
            ceiling = self.feed.specs[symbol].max_leverage
            if ceiling and value > ceiling:
                raise ConfigError(
                    f"legs leverage {value} exceeds {symbol}'s maximum of {ceiling}"
                )

        for session in (self.master, self.sub1):
            current = current_leverage(session.full_account())
            for symbol, value in wanted.items():
                if current.get(symbol) == value:
                    continue
                result = set_leverage(
                    http_url=self.config.http_url,
                    private_key=self.config.private_key,
                    domain=SignatureDomain[self.config.signature_domain_name],
                    symbol=symbol,
                    leverage=value,
                    account=session.pubkey,
                )
                if result.ok:
                    log.info(
                        "%s: %s leverage %s -> %s",
                        session.name,
                        symbol,
                        current.get(symbol, "unset"),
                        value,
                    )
                else:
                    raise RuntimeError(
                        f"{session.name}: could not set {symbol} leverage to "
                        f"{value}: {result.response_json}"
                    )

    def build_strategy(self) -> Strategy:
        hedger = Hedger(
            book=self.book,
            sessions=self.sessions,
            specs=self.feed.specs,
            tolerance_lots=self.config.hedge_tolerance_lots,
            max_hedge_size=build_hedge_ceilings(self.config),
            in_flight_ttl_ms=self.config.overlay_ttl_ms,
            impact=self.impact,
            max_impact_bps=self.config.risk.max_hedge_impact_bps,
            feed=self.feed,
        )
        chaser = Chaser(
            sessions=self.sessions,
            feed=self.feed,
            book=self.book,
            params=build_chase_params(self.config),
            price_stale_timeout_s=self.config.risk.price_stale_timeout_s,
        )
        risk = RiskMonitor(
            config=self.config.risk,
            book=self.book,
            feed=self.feed,
            sessions=self.sessions,
            symbols=self.symbols,
        )
        strategy = Strategy(
            config=self.config,
            master=self.master,
            sub1=self.sub1,
            feed=self.feed,
            book=self.book,
            hedger=hedger,
            chaser=chaser,
            risk=risk,
            store=self.store,
            state=self.store.load(),
            notifier=self.notifier,
            title=self.title,
            sessions=self.pool,
        )
        # Built here rather than inside Strategy so the accounts it draws
        # from are exactly the sessions that exist -- a pool listing an
        # account with no session would draw a group nothing could trade.
        # The mode already narrowed those sessions, so this is the same call
        # whichever mode is running.
        strategy.pairing = Pairing(
            pool=[session.pubkey for session in self.pool],
            max_groups=self.config.max_groups,
            max_takers=self.config.max_takers,
        )
        return strategy


# -- commands --------------------------------------------------------------


STOP_BANNER = [
    "S  --  stop and cancel",
    "",
    "Stops both legs at the next tick and pulls every resting",
    "order, then returns to the menu. Open positions are left",
    "as they are -- close them with `6. Close All Positions`.",
]


def _print_stop_banner() -> None:
    """The one control available while the run has the terminal.

    Measured rather than typed: hand-aligned box borders drift the moment a
    line is edited, and a crooked box is the first thing read as "unfinished".
    """
    width = max(len(line) for line in STOP_BANNER) + 4
    rule = "  +" + "-" * width + "+"
    print()
    print(rule)
    for line in STOP_BANNER:
        print("  |  " + line.ljust(width - 2) + "|")
    print(rule)
    print()


async def cmd_run(config: Config, dry_run: bool) -> int:
    runtime = Runtime(config, dry_run)
    await runtime.start()
    strategy = runtime.build_strategy()
    await runtime.notifier.run_started(
        endpoint=config.http_url,
        master=runtime.master.pubkey,
        sub1=runtime.sub1.pubkey,
        dry_run=dry_run,
    )
    _print_stop_banner()
    # The banner scrolls away within a minute, so the reminder lives in the
    # title bar, which does not.
    runtime.title.set_hint("S = stop")
    # Started before the legs and cancelled after them, so the key works for the
    # whole run including the recovery pass at the start.
    stopper = asyncio.create_task(watch_for_stop(strategy.request_stop))
    try:
        await strategy.run()
        if strategy.stop_reason:
            log.info("stopped on request -- see above for what is still open")
            runtime.title.set_phase("stopped")
            return 0
        log.info("all cycles complete")
        runtime.title.set_phase("done")
        await runtime.notifier.run_finished(
            cycles=strategy.state.cycle_index, detail=await strategy.progress_detail()
        )
        return 0
    except Halted as exc:
        # The halt itself was already reported from _trigger_halt, which fires
        # before the flatten so the alert does not wait on it.
        log.critical("halted: %s", describe(exc))
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception:
        # Logged rather than only raised, so the traceback reaches the log file
        # and not just the console window that is about to close.
        log.exception("run failed")
        raise
    finally:
        stopper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stopper
        await runtime.stop()


async def cmd_flatten(
    config: Config, dry_run: bool, *, limit: bool = False, timeout_s: float = 300.0
) -> int:
    """Cancel everything and close both accounts, reduce-only.

    `limit` posts resting orders at the front of the book instead of taking the
    spread. It is the cheaper close and the slower one, and it can run out of
    time -- in which case the state is still reset and the positions are still
    reported, so the operator can follow up at market.

    Returns 1 when a limit close ran out of time with positions still open.
    "I could not finish" and "you are flat" must not share an exit code: this
    command is the panic button, and something scripted on top of it would
    otherwise read a timeout as success and stop watching.
    """
    runtime = Runtime(config, dry_run)
    await runtime.start(verify=False)
    closed = True
    try:
        # Every account, not the first two. This is the panic button, and in
        # pool mode those two are two of however many the keys produced --
        # leaving a resting order on the rest is leaving an unhedged fill
        # waiting to happen on an account nobody is watching any more.
        await cancel_all_orders(runtime.pool, runtime.symbols)
        if limit:
            closed = await flatten_limit(
                runtime.sessions,
                runtime.book,
                runtime.feed,
                runtime.symbols,
                improve_ticks=config.master_account.improve_ticks,
                timeout_s=timeout_s,
            )
        else:
            await flatten(
                runtime.sessions, runtime.book, runtime.feed, runtime.symbols
            )

        state = runtime.store.load()
        # Legs, not the summary: a leg can hold an order id with the pair
        # reading IDLE, and leaving that behind is what made a flatten look
        # like it had done nothing.
        if state.summary_phase != Phase.IDLE or state.legs or state.has_baseline:
            state.phase = Phase.IDLE
            state.halted_reason = None
            state.hold_until = 0.0
            state.legs = {}
            # This is the "start over" command, so the execution target starts
            # over with it. Without this, resuming after a flatten would carry
            # the interrupted run's spend into what the operator reads as a
            # fresh one.
            state.clear_baseline()
            runtime.store.save(state)
            log.info("state reset to IDLE")
        else:
            log.info("state was already IDLE -- nothing to reset")

        if not closed:
            print("")
            print("  Limit close ran out of time -- positions are STILL OPEN.")
            print("  The orders have been cancelled. Run it again, or close")
            print("  at market if you need to be flat now.")
            return 1
        return 0
    finally:
        await runtime.stop()


async def cmd_status(config: Config) -> int:
    """Read-only: print positions, open orders, and persisted state."""
    runtime = Runtime(config, dry_run=True)
    runtime.feed.load_specs()

    sync_positions_http([runtime.master, runtime.sub1], runtime.book)
    state = runtime.store.load()

    print(f"endpoint     : {config.http_url}")
    print(f"master       : {runtime.master.pubkey}")
    print(f"sub1         : {runtime.sub1.pubkey}")
    # Per leg, because they no longer move together: one can be holding while
    # the other is still filling, and a single phase would hide that.
    print(f"phase        : {state.summary_phase.value}")
    for symbol in runtime.symbols:
        leg = state.leg(symbol)
        held = (
            f", {leg.hold_remaining_s() / 60:.1f} min of hold left"
            if leg.hold_remaining_s() > 0 else ""
        )
        print(f"  {symbol:10} {leg.phase.value:9} cycle {leg.cycle_index}{held}")
    if state.halted_reason:
        print(f"halted       : {state.halted_reason}")

    print("\npositions:")
    for symbol in runtime.symbols:
        master_size = runtime.book.authoritative(runtime.master.pubkey, symbol)
        sub_size = runtime.book.authoritative(runtime.sub1.pubkey, symbol)
        print(
            f"  {symbol:<10} master={master_size:+.8f}  sub1={sub_size:+.8f}  "
            f"net={master_size + sub_size:+.8f}"
        )

    print("\nopen orders:")
    for session in (runtime.master, runtime.sub1):
        try:
            orders = session.open_orders()
        except Exception as exc:
            print(f"  {session.name}: query failed ({exc})")
            continue
        relevant = [o for o in orders if o.get("symbol") in runtime.symbols]
        if not relevant:
            print(f"  {session.name}: none")
        for order in relevant:
            print(
                f"  {session.name}: {order.get('symbol')} "
                f"{'BUY' if order.get('isBuy') else 'SELL'} "
                f"{order.get('size')} @ {order.get('price')} oid={order.get('orderId')}"
            )
    return 0


async def cmd_check(config: Config) -> int:
    """Validate configuration and account wiring without connecting to trade."""
    runtime = Runtime(config, dry_run=True)
    runtime.feed.load_specs()
    print("market specs OK")
    if not Runtime._same_tree(runtime.master, runtime.sub1):
        # Nothing is wrong: in multi mode the pool's first two accounts can be
        # two different masters, and neither is the other's child.
        print("sub-account relationship: n/a -- these two are separate masters")
        return 0
    try:
        verify_sub_account(runtime.master, runtime.sub1)
        print("sub-account relationship OK")
    except Exception as exc:
        print(f"sub-account check FAILED: {exc}")
        print(
            "\nIf the account does not exist yet, the master itself has to be "
            "created first by an on-chain deposit -- this bot cannot do that. "
            "Once the master is funded, run `bulkdn create-subaccount --name "
            "<name>`, then put the returned pubkey in the config."
        )
        return 1
    return 0


async def cmd_transfer(
    config: Config, to_pubkey: str, amount: float, from_pubkey: str | None
) -> int:
    """Move margin between the master and one of its accounts.

    The Python SDK has no signer for this action, so the wincode bytes are
    built in bulkdn/subaccounts.py and verified there against bulk-keychain.

    Defaults to sending from the master, which is the direction that matters:
    a freshly created sub-account has zero margin and cannot open a position
    until it is funded.
    """
    from bulk_api.common import SignatureDomain
    from bulk_api.common.signer import TransactionSigner

    from .subaccounts import submit_transfer

    source = from_pubkey or TransactionSigner(config.private_key).public_key
    print(
        f"\ntransferring {amount} USDC\n"
        f"  from {source}\n"
        f"  to   {to_pubkey}\n"
        f"  via  {config.http_url}\n"
    )

    result = submit_transfer(
        http_url=config.http_url,
        private_key=config.private_key,
        domain=SignatureDomain[config.signature_domain_name],
        from_pubkey=source,
        to_pubkey=to_pubkey,
        margin_amount=amount,
    )

    print(f"HTTP {result.response_status}")
    print(result.response_json)
    if not result.ok:
        print("\ntransfer rejected -- balances unchanged")
        return 1
    print("\ntransfer accepted; confirm with `bulkdn status`")
    return 0


def cmd_encrypt_key(config: Config) -> int:
    """Encrypt the key file in place, or change its password.

    The key is taken from whatever `load_config` already resolved, so this
    works on a plaintext file, on an already-encrypted one (re-encrypting it
    under a new password), and on a key supplied through the environment.
    """
    import getpass
    import sys

    from . import keystore
    from .config import PRIVATE_KEY_FILE

    if not config.private_keys:
        print("no key to encrypt")
        return 1

    if not sys.stdin.isatty():
        # getpass would fall back to an echoing read, which puts the password
        # on screen and into the scrollback.
        print("this needs a terminal to type the password into")
        return 1

    was_encrypted = keystore.is_encrypted(PRIVATE_KEY_FILE)
    count = len(config.private_keys)
    print(
        f"\n{PRIVATE_KEY_FILE} is currently "
        f"{'encrypted' if was_encrypted else 'PLAINTEXT'} and holds "
        f"{count} key{'s' if count != 1 else ''}."
    )
    print("An empty password uses a default that is published in the source:")
    print("it keeps the key off the screen and out of a backup, nothing more.\n")

    first = getpass.getpass("Enter password to encrypt privatekeys (empty for default): ")
    second = getpass.getpass("Repeat: ")
    if first != second:
        print("passwords do not match -- nothing was written")
        return 1
    password = first or keystore.DEFAULT_PASSWORD
    if not first:
        print("\nusing the default password")

    try:
        # Every key, not only the first. Writing one back would encrypt
        # the file and silently discard the rest of it -- the worst shape
        # a bug about key files can take, because it reports success and
        # the other accounts are simply gone.
        keystore.save_all(PRIVATE_KEY_FILE, config.private_keys, password)
    except keystore.KeystoreError as exc:
        print(f"failed: {exc}")
        return 1

    print(f"\n{PRIVATE_KEY_FILE} written encrypted (argon2id + xsalsa20-poly1305).")
    if not was_encrypted:
        print("The plaintext it replaced may still exist in backups or in your")
        print("shell history -- rotate the key if that matters.")
    return 0


async def cmd_create_subaccount(
    config: Config, name: str, margin_amount: float | None,
    *, private_key: str | None = None,
) -> int:
    """Create a sub-account with a hand-serialized `createSubAccount` transaction.

    The Python SDK has no signer for this action, so the wincode bytes are built
    in bulkdn/subaccounts.py.

    `private_key` picks which master owns the result. It defaults to the first
    key in the file, which is the only one there is for most operators; the
    menu passes a chosen one when the file holds several, because a
    sub-account belongs to exactly one master and there is no way to move it
    afterwards.
    """
    from bulk_api.common import SignatureDomain

    from .subaccounts import build_and_submit

    print(
        f"\nsubmitting createSubAccount(name={name!r}) to {config.http_url}\n"
        f"mainnet, domain byte {SignatureDomain[config.signature_domain_name].value}\n"
    )

    result = build_and_submit(
        http_url=config.http_url,
        private_key=private_key or config.private_key,
        domain=SignatureDomain[config.signature_domain_name],
        name=name,
        margin_amount=margin_amount,
    )

    print(f"HTTP {result.response_status}")
    print(result.response_json)

    if result.ok:
        pubkey = result.sub_pubkey
        if pubkey:
            # Nothing to copy anywhere: Runtime reads the master's own record
            # to find this at startup.
            print(f"\ncreated: {pubkey}")
        else:
            print(
                "\naccepted, but the response carried no pubkey -- check `bulkdn status` "
                "or the BULK UI, then add it as sub1_pubkey in your config."
            )
        return 0

    print(
        "\nrejected. If the message is `bad signature`, the signed bytes disagree "
        "with what the server expects. Nothing was changed on the account."
    )
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bulkdn",
        description="Delta-neutral BTC/SOL bot across a BULK master account and sub-account",
    )
    parser.add_argument("--config", default="settings.yaml", help="path to the settings file")
    parser.add_argument("--log-level", help="override the log level in the config file")
    parser.add_argument(
        "--mode",
        # `pool` is what multi was called while it was the third of three.
        # Accepted so a script written against that name keeps running.
        choices=("single", "multi", "pool"),
        help=(
            "override the settings file: single trades one master and its own "
            "sub-accounts, multi trades every master and sub in one pool. "
            "Which markets are traded is a separate setting"
        ),
    )

    sub = parser.add_subparsers(dest="command", required=False)
    sub.add_parser("menu", help="interactive menu (default with no subcommand)")

    run = sub.add_parser("run", help="run the strategy")
    run.add_argument(
        "--live",
        action="store_true",
        help="actually submit orders (without this, orders are only logged)",
    )

    sub.add_parser("status", help="print positions, orders, and persisted state")
    sub.add_parser("check", help="validate config and account wiring")

    flat = sub.add_parser("flatten", help="cancel all orders and close all strategy positions")
    flat.add_argument("--live", action="store_true", help="actually submit the closing orders")
    flat.add_argument(
        "--limit",
        action="store_true",
        help="close with resting limit orders instead of at market: no spread "
             "and no taker fee, but it takes time and may not finish",
    )
    flat.add_argument(
        "--limit-timeout",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="how long --limit waits before giving up (default: 300)",
    )

    xfer = sub.add_parser("transfer", help="move margin between master and sub-account")
    xfer.add_argument("--to", dest="to_pubkey", required=True, help="destination pubkey")
    xfer.add_argument("--amount", type=float, required=True, help="margin amount to move")
    xfer.add_argument(
        "--from", dest="from_pubkey", default=None,
        help="source pubkey (default: the master, i.e. the signing key)",
    )

    sub.add_parser("encrypt-key", help="encrypt the key file, or change its password")

    create_sub = sub.add_parser(
        "create-subaccount",
        help="create a sub-account (best-effort -- SDK has no signer for this action)",
    )
    create_sub.add_argument("--name", required=True, help="sub-account name, 1-32 chars")
    create_sub.add_argument(
        "--margin-amount", type=float, default=None,
        help="initial margin to move from master (untested path; default none)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(
            args.config,
            require_credentials=True,
            # `create-subaccount` produces sub1_pubkey rather than assuming it.
            require_sub1=args.command
            not in (None, "menu", "create-subaccount", "transfer", "encrypt-key"),
            mode=args.mode,
        )
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    configure_logging(args.log_level or config.log_level)

    # Before anything opens a socket, and after logging so the choice is on the
    # record. A bad proxy line stops the bot here rather than being ignored:
    # connecting directly from a country that blocks BULK looks exactly like the
    # exchange being down, and that is a long way to chase the wrong problem.
    try:
        proxy.configure()
    except proxy.ProxyError as exc:
        print(f"proxy error: {exc}", file=sys.stderr)
        return 1

    dry_run = not getattr(args, "live", False)
    if not dry_run:
        log.warning("=" * 70)
        log.warning("LIVE TRADING ON MAINNET -- real funds are at risk")
        log.warning("=" * 70)

    try:
        if args.command in (None, "menu"):
            # Imported here, not at the top. `menu` is a front end over these
            # commands and calls back into them, so importing it at module
            # scope made the two mutually dependent -- and paid for with eight
            # `from .cli import ...` lines hidden inside menu's functions. One
            # deferred import in the one place that needs it costs less and
            # says which direction the dependency actually runs.
            from .menu import run_menu

            return run_menu(config, args.config)
        if args.command == "run":
            return asyncio.run(cmd_run(config, dry_run))
        if args.command == "flatten":
            return asyncio.run(
                cmd_flatten(
                    config,
                    dry_run,
                    limit=args.limit,
                    timeout_s=args.limit_timeout,
                )
            )
        if args.command == "status":
            return asyncio.run(cmd_status(config))
        if args.command == "check":
            return asyncio.run(cmd_check(config))
        if args.command == "encrypt-key":
            return cmd_encrypt_key(config)
        if args.command == "transfer":
            return asyncio.run(
                cmd_transfer(config, args.to_pubkey, args.amount, args.from_pubkey)
            )
        if args.command == "create-subaccount":
            return asyncio.run(
                cmd_create_subaccount(config, args.name, args.margin_amount)
            )
    except AccessDenied as exc:
        # A refusal is an expected outcome, not a crash -- say so plainly
        # rather than unwinding a traceback over it.
        print(f"\naccess denied: {exc}", file=sys.stderr)
        print(
            "This build runs only for accounts signed up under the owner's "
            "referral code.",
            file=sys.stderr,
        )
        return 3
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130

    parser.error(f"unknown command {args.command}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

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
import logging
import sys

from bulk_api.common import SignatureDomain

from .accounts import build_sessions, discover_sub_account, verify_sub_account
from .chaser import Chaser
from .config import Config, ConfigError, load_config
from .menu import run_menu
from .feed import MarketFeed
from .notify import Notifier
from .referral import AccessDenied
from .referral import check_access as check_referral_access
from .window import WindowTitle
from .impact import ImpactBook
from .hedger import Hedger
from .positions import PositionBook
from .reconcile import cancel_all_orders, flatten, sync_positions_http
from .risk import RiskMonitor
from .settings import current_leverage, set_leverage
from .state import Phase, StateStore
from .ws_compat import apply_ws_compat
from .strategy import Halted, Strategy, build_chase_params, build_hedge_ceilings

log = logging.getLogger("bulkdn")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # The SDK logs every frame at DEBUG, which drowns out the strategy.
    logging.getLogger("bulk_api").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


class Runtime:
    """Wires the components together and owns their lifecycle."""

    def __init__(self, config: Config, dry_run: bool):
        self.config = config
        self.dry_run = dry_run
        self.symbols = [config.btc.symbol, config.sol.symbol]

        # Must be installed before any client is constructed: it repairs fill
        # parsing and TLS handling inside the SDK itself.
        apply_ws_compat(
            insecure_ssl=config.ws_insecure_ssl,
            auto_bypass=config.ws_ssl_auto_bypass,
        )

        # Read off the master rather than configured: a sub-account has no key
        # of its own, so its pubkey is a fact about the master, not a choice
        # the operator should have to copy by hand.
        sub1_pubkey = config.sub1_pubkey or discover_sub_account(
            private_key=config.private_key, http_url=config.http_url
        )

        self.master, self.sub1 = build_sessions(
            private_key=config.private_key,
            sub1_pubkey=sub1_pubkey,
            ws_url=config.ws_url,
            http_url=config.http_url,
            domain=SignatureDomain[config.signature_domain_name],
            symbols=self.symbols,
            dry_run=dry_run,
        )
        self.sessions = {self.master.pubkey: self.master, self.sub1.pubkey: self.sub1}
        self.notifier = Notifier(config.telegram)
        self.title = WindowTitle(cycles=config.cycles)
        self.book = PositionBook(overlay_ttl_ms=config.overlay_ttl_ms)
        self.impact = ImpactBook(config.http_url)
        self.feed = MarketFeed(self.master, self.symbols)
        self.store = StateStore(config.state_file)

    async def start(self, verify: bool = True) -> None:
        log.info(
            "mainnet mode=%s master=%s sub1=%s",
            "DRY-RUN" if self.dry_run else "LIVE",
            self.master.pubkey,
            self.sub1.pubkey,
        )
        # Before anything is placed. A gate that tripped later would abandon
        # open positions and leave the pair directional.
        self.check_access()

        self.feed.load_specs()
        if verify:
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
        if not self.config.access.enabled:
            return

        decision = check_referral_access(self.master.pubkey, self.config.access)
        if not decision.allowed:
            raise AccessDenied(decision.reason)
        log.info("access granted: %s", decision.reason)

    async def stop(self) -> None:
        await self.master.disconnect()
        await self.sub1.disconnect()

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
            for leg in (self.config.btc, self.config.sol)
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
        return Strategy(
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
        )


# -- commands --------------------------------------------------------------


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
    try:
        await strategy.run()
        log.info("all cycles complete")
        runtime.title.set_phase("done")
        await runtime.notifier.run_finished(
            cycles=strategy.state.cycle_index, detail=strategy._progress_detail()
        )
        return 0
    except Halted as exc:
        # The halt itself was already reported from _trigger_halt, which fires
        # before the flatten so the alert does not wait on it.
        log.critical("halted: %s", exc)
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        await runtime.stop()


async def cmd_flatten(config: Config, dry_run: bool) -> int:
    """Cancel everything and market-close both accounts, reduce-only."""
    runtime = Runtime(config, dry_run)
    await runtime.start(verify=False)
    try:
        await cancel_all_orders([runtime.master, runtime.sub1], runtime.symbols)
        await flatten(runtime.sessions, runtime.book, runtime.feed, runtime.symbols)

        state = runtime.store.load()
        if state.phase != Phase.IDLE:
            state.phase = Phase.IDLE
            state.halted_reason = None
            state.hold_until = 0.0
            state.legs = {}
            runtime.store.save(state)
            log.info("state reset to IDLE")
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
    print(f"phase        : {state.phase.value}")
    print(f"cycle        : {state.cycle_index}")
    if state.hold_until:
        print(f"hold remaining: {state.hold_remaining_s() / 60:.1f} min")
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

    if not config.private_key:
        print("no key to encrypt")
        return 1

    if not sys.stdin.isatty():
        # getpass would fall back to an echoing read, which puts the password
        # on screen and into the scrollback.
        print("this needs a terminal to type the password into")
        return 1

    was_encrypted = keystore.is_encrypted(PRIVATE_KEY_FILE)
    print(
        f"\n{PRIVATE_KEY_FILE} is currently "
        f"{'encrypted' if was_encrypted else 'PLAINTEXT'}."
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
        keystore.save(PRIVATE_KEY_FILE, config.private_key, password)
    except keystore.KeystoreError as exc:
        print(f"failed: {exc}")
        return 1

    print(f"\n{PRIVATE_KEY_FILE} written encrypted (argon2id + xsalsa20-poly1305).")
    if not was_encrypted:
        print("The plaintext it replaced may still exist in backups or in your")
        print("shell history -- rotate the key if that matters.")
    return 0


async def cmd_create_subaccount(
    config: Config, name: str, margin_amount: float | None
) -> int:
    """Create a sub-account with a hand-serialized `createSubAccount` transaction.

    The Python SDK has no signer for this action, so the wincode bytes are built
    in bulkdn/subaccounts.py.
    """
    from bulk_api.common import SignatureDomain

    from .subaccounts import build_and_submit

    print(
        f"\nsubmitting createSubAccount(name={name!r}) to {config.http_url}\n"
        f"mainnet, domain byte {SignatureDomain[config.signature_domain_name].value}\n"
    )

    result = build_and_submit(
        http_url=config.http_url,
        private_key=config.private_key,
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
        )
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    configure_logging(args.log_level or config.log_level)

    dry_run = not getattr(args, "live", False)
    if not dry_run:
        log.warning("=" * 70)
        log.warning("LIVE TRADING ON MAINNET -- real funds are at risk")
        log.warning("=" * 70)

    try:
        if args.command in (None, "menu"):
            return run_menu(config, args.config)
        if args.command == "run":
            return asyncio.run(cmd_run(config, dry_run))
        if args.command == "flatten":
            return asyncio.run(cmd_flatten(config, dry_run))
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

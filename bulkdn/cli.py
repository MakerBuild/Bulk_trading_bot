"""Command line entry point.

Trading is off by default: `run` will not submit a single order unless `--live`
is passed, so a config file alone cannot start trading.

Note that `--live` is the ONLY interlock. The config now defaults to mainnet,
so `run --live` with no other flags trades real funds -- the mainnet banner is
a warning, not a confirmation prompt. Pass `--network testnet` to rehearse.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import List, Optional

from bulk_api.common import SignatureDomain

from .accounts import build_sessions, verify_sub_account
from .chaser import Chaser
from .config import Config, ConfigError, NETWORKS, load_config
from .feed import MarketFeed
from .hedger import Hedger
from .positions import PositionBook
from .reconcile import cancel_all_orders, flatten, sync_positions_http
from .risk import RiskMonitor
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

        self.master, self.sub1 = build_sessions(
            private_key=config.private_key,
            sub1_pubkey=config.sub1_pubkey,
            ws_url=config.ws_url,
            http_url=config.http_url,
            domain=SignatureDomain[config.signature_domain_name],
            symbols=self.symbols,
            dry_run=dry_run,
        )
        self.sessions = {self.master.pubkey: self.master, self.sub1.pubkey: self.sub1}
        self.book = PositionBook(overlay_ttl_ms=config.overlay_ttl_ms)
        self.feed = MarketFeed(self.master, self.symbols)
        self.store = StateStore(config.state_file)

    async def start(self, verify: bool = True) -> None:
        log.info(
            "network=%s mode=%s master=%s sub1=%s",
            self.config.network,
            "DRY-RUN" if self.dry_run else "LIVE",
            self.master.pubkey,
            self.sub1.pubkey,
        )
        self.feed.load_specs()
        if verify:
            verify_sub_account(self.master, self.sub1)

        await self.master.connect()
        await self.sub1.connect()
        await self.feed.subscribe()
        # Let the initial account snapshots and book updates land before any
        # decision is made on them.
        await asyncio.sleep(2.0)

    async def stop(self) -> None:
        await self.master.disconnect()
        await self.sub1.disconnect()

    def build_strategy(self) -> Strategy:
        hedger = Hedger(
            book=self.book,
            sessions=self.sessions,
            specs=self.feed.specs,
            tolerance_lots=self.config.hedge_tolerance_lots,
            max_hedge_size=build_hedge_ceilings(self.config),
            in_flight_ttl_ms=self.config.overlay_ttl_ms,
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
        )


# -- commands --------------------------------------------------------------


async def cmd_run(config: Config, dry_run: bool) -> int:
    runtime = Runtime(config, dry_run)
    await runtime.start()
    strategy = runtime.build_strategy()
    try:
        await strategy.run()
        log.info("all cycles complete")
        return 0
    except Halted as exc:
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

    print(f"network      : {config.network}")
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
            "\nThis bot does not create sub-accounts: the Python SDK cannot sign a "
            "createSubAccount action. Create it in the BULK UI or with the Rust "
            "`bulk-cli`, fund it, then put its pubkey in the config."
        )
        return 1
    return 0


async def cmd_create_subaccount(
    config: Config, name: str, margin_amount: Optional[float]
) -> int:
    """Create a sub-account with a hand-serialized `createSubAccount` transaction.

    The Python SDK has no signer for this action, so the wincode bytes are built
    in bulkdn/subaccounts.py.
    """
    from bulk_api.common import SignatureDomain

    from .subaccounts import build_and_submit

    print(
        f"\nsubmitting createSubAccount(name={name!r}) to {config.http_url}\n"
        f"network: {config.network} (domain byte "
        f"{SignatureDomain[config.signature_domain_name].value})\n"
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
        print(
            "\nlikely succeeded -- check `bulkdn status` or the BULK UI for the new "
            "sub-account's pubkey, then add it as sub1_pubkey in your config."
        )
        return 0

    print(
        "\nrejected. If the message is `bad signature`, the signed bytes disagree "
        "with what the server expects -- check that `network:` matches the endpoint "
        "(staging signs on the devnet domain, not mainnet). Nothing was changed on "
        "the account."
    )
    return 1


async def cmd_faucet(config: Config, amount: Optional[float]) -> int:
    """Request testnet funds for both accounts."""
    if config.network == "mainnet":
        print("faucet is not available on mainnet")
        return 1
    runtime = Runtime(config, dry_run=False)
    for session in (runtime.master, runtime.sub1):
        try:
            result = session.http.request_faucet(user=session.pubkey, amount=amount)
            print(f"{session.name}: {result}")
        except Exception as exc:
            print(f"{session.name}: faucet failed ({exc})")
    return 0


# -- argument parsing ------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bulkdn",
        description="Delta-neutral BTC/SOL bot across a BULK master account and sub-account",
    )
    parser.add_argument("--config", default="config.yaml", help="path to the config file")
    parser.add_argument(
        "--network",
        choices=sorted(NETWORKS),
        help="override the network in the config file",
    )
    parser.add_argument("--log-level", help="override the log level in the config file")

    sub = parser.add_subparsers(dest="command", required=True)

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

    faucet = sub.add_parser("faucet", help="request testnet funds for both accounts")
    faucet.add_argument("--amount", type=float, default=None)

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


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(
            args.config,
            network_override=args.network,
            require_credentials=True,
            # `create-subaccount` produces sub1_pubkey rather than assuming it.
            require_sub1=args.command != "create-subaccount",
        )
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    configure_logging(args.log_level or config.log_level)

    dry_run = not getattr(args, "live", False)
    if config.network == "mainnet" and not dry_run:
        log.warning("=" * 70)
        log.warning("LIVE TRADING ON MAINNET -- real funds are at risk")
        log.warning("=" * 70)

    try:
        if args.command == "run":
            return asyncio.run(cmd_run(config, dry_run))
        if args.command == "flatten":
            return asyncio.run(cmd_flatten(config, dry_run))
        if args.command == "status":
            return asyncio.run(cmd_status(config))
        if args.command == "check":
            return asyncio.run(cmd_check(config))
        if args.command == "faucet":
            return asyncio.run(cmd_faucet(config, args.amount))
        if args.command == "create-subaccount":
            return asyncio.run(
                cmd_create_subaccount(config, args.name, args.margin_amount)
            )
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130

    parser.error(f"unknown command {args.command}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

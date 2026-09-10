"""Interactive menu.

The subcommands remain the scriptable interface; this is a front end over the
same functions, so there is no second implementation of anything.

Two rules shape it. Every action that spends money asks for confirmation
first -- the bot is mainnet-only, so there is no harmless mistake here. And an
item with no working backend says so plainly instead of appearing to work:
a menu entry that silently does nothing is worse than one that admits it.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable

import requests

from .accounts import unwrap_full_account
from .config import Config, ConfigError

BOX_WIDTH = 46


class NoAccountTree(Exception):
    """The signing key has no BULK account yet, so nothing can be listed."""


# -- rendering --------------------------------------------------------------


def _box(title: str, lines: list[str]) -> str:
    top = "+" + "-" * (BOX_WIDTH - 2) + "+"
    out = [top, "|" + title.center(BOX_WIDTH - 2) + "|", top]
    for line in lines:
        out.append("| " + line.ljust(BOX_WIDTH - 4) + " |")
    out.append(top)
    return "\n".join(out)


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return "0"


def _confirm(what: str) -> bool:
    """Real funds are at stake, so require the word, not a keystroke."""
    print(f"\n  !!  {what}")
    print("      This is mainnet. Type 'yes' to proceed, anything else aborts.")
    return _ask("      > ") == "yes"


def _pause() -> None:
    _ask("\n  [enter] to return to the menu ")


# -- helpers ----------------------------------------------------------------


def _accounts(config: Config) -> list[tuple[str, str]]:
    """(label, pubkey) for the master and every sub-account it owns.

    Read from the exchange rather than the config: the config names one sub,
    but the master may own others, and balancing has to see all of them.
    """
    from bulk_api.common.signer import TransactionSigner

    master = TransactionSigner(config.private_key).public_key
    body = requests.post(
        f"{config.http_url}/account",
        json={"type": "fullAccount", "user": master},
        timeout=25,
    )
    if body.status_code == 404:
        # The usual case for a fresh key, and the raw 404 explains none of it.
        raise NoAccountTree(
            f"no account exists on mainnet for master {_short(master)}.\n"
            "  A BULK account is created by an on-chain USDC deposit into the\n"
            "  Solana vault -- this bot cannot do that. Deposit first, then\n"
            "  Accounts Management -> Create New Subaccount."
        )
    body.raise_for_status()
    account = unwrap_full_account(body.json())
    out = [("master", master)]
    for entry in account.get("subAccounts") or []:
        if isinstance(entry, dict) and entry.get("pubkey"):
            out.append(("sub", entry["pubkey"]))
    return out


def _transferable(config: Config, pubkey: str) -> float:
    body = requests.post(
        f"{config.http_url}/account",
        json={"type": "fullAccount", "user": pubkey},
        timeout=25,
    )
    if body.status_code != 200:
        return 0.0
    margin = unwrap_full_account(body.json()).get("margin") or {}
    # transferableBalance, not totalMargin: margin backing an open position
    # cannot be moved, and asking to move it just gets the transfer rejected.
    return float(margin.get("transferableBalance") or 0.0)


def _short(pubkey: str) -> str:
    return pubkey if len(pubkey) <= 16 else f"{pubkey[:6]}..{pubkey[-4:]}"


# -- menu items -------------------------------------------------------------


def _start(config: Config) -> None:
    from .cli import cmd_run

    print("\n  1. dry run  -- connects and logs, submits nothing")
    print("  2. live     -- REAL FUNDS")
    print("  0. back")
    choice = _ask("\n  > ")
    if choice == "1":
        asyncio.run(cmd_run(config, dry_run=True))
    elif choice == "2":
        if _confirm(f"Start a live cycle: {config.cycles or 'unlimited'} cycle(s), "
                    f"{config.hold_minutes:g} min hold."):
            asyncio.run(cmd_run(config, dry_run=False))
        else:
            print("  aborted")
    _pause()


def _active_strategy(config: Config) -> None:
    from .cli import cmd_status

    asyncio.run(cmd_status(config))
    _pause()


def _history(config: Config) -> None:
    """Recent fills for every account in the tree, with volume and fees.

    Volume and fees are summed from the fills themselves rather than tracked
    separately, so the numbers cannot drift from what the exchange recorded.
    """
    from bulk_api.api.bulk_http import BulkHttpClient
    from bulk_api.common import SignatureDomain

    http = BulkHttpClient(
        base_url=config.http_url,
        private_key=config.private_key,
        signature_domain=SignatureDomain[config.signature_domain_name],
    )

    grand_volume = 0.0
    grand_fees = 0.0
    for label, pubkey in _accounts(config):
        print(f"\n  {label} {_short(pubkey)}")
        try:
            page = http.get_fills_page(pubkey, limit=20)
        except Exception as exc:
            print(f"    query failed: {exc}")
            continue

        rows = list(getattr(page, "data", None) or [])
        if not rows:
            print("    no fills")
            continue

        print(f"    {'symbol':<10} {'side':<5} {'size':>12} {'price':>12} {'fee':>10}")
        for fill in rows:
            side = "buy" if fill.is_buy else "sell"
            print(
                f"    {fill.symbol:<10} {side:<5} {fill.amount:>12.6f} "
                f"{fill.price:>12.3f} {fill.fee:>10.4f}"
            )
        volume = sum(f.amount * f.price for f in rows)
        fees = sum(f.fee for f in rows)
        grand_volume += volume
        grand_fees += fees
        print(f"    -- {len(rows)} fills, volume ${volume:,.2f}, fees ${fees:,.4f}")

    print(f"\n  total volume ${grand_volume:,.2f}   total fees ${grand_fees:,.4f}")
    print("  (last 20 fills per account; self-trades inside one master tree")
    print("   do not count toward BULK's fee-tier volume)")
    _pause()


def _create_subaccount(config: Config) -> None:
    from .cli import cmd_create_subaccount

    name = _ask("\n  name (1-32 chars, A-Z a-z 0-9 - _): ")
    if not name:
        print("  aborted")
        _pause()
        return
    asyncio.run(cmd_create_subaccount(config, name, None))
    print("\n  put the returned pubkey into config.yaml as sub1_pubkey")
    _pause()


def _balance_subaccounts(config: Config) -> None:
    """Even out transferable margin across the master and its sub-accounts."""
    from .subaccounts import submit_transfer
    from bulk_api.common import SignatureDomain

    accounts = _accounts(config)
    if len(accounts) < 2:
        print("\n  the master owns no sub-accounts yet")
        _pause()
        return

    balances = [(label, pk, _transferable(config, pk)) for label, pk in accounts]
    total = sum(b for _, _, b in balances)
    target = total / len(balances)

    print(f"\n  {'account':<22} {'transferable':>14} {'delta':>12}")
    for label, pk, bal in balances:
        print(f"  {label + ' ' + _short(pk):<22} {bal:>14.2f} {bal - target:>12.2f}")
    print(f"\n  total {total:,.2f} across {len(balances)} accounts -> {target:,.2f} each")

    # Anything below a cent is noise; moving it costs a transaction for nothing.
    senders = [(pk, bal - target) for _, pk, bal in balances if bal - target > 0.01]
    receivers = [(pk, target - bal) for _, pk, bal in balances if target - bal > 0.01]
    if not senders or not receivers:
        print("\n  already balanced")
        _pause()
        return

    moves = []
    si = ri = 0
    while si < len(senders) and ri < len(receivers):
        (src, surplus), (dst, deficit) = senders[si], receivers[ri]
        amount = round(min(surplus, deficit), 2)
        if amount > 0.01:
            moves.append((src, dst, amount))
        senders[si] = (src, surplus - amount)
        receivers[ri] = (dst, deficit - amount)
        if senders[si][1] <= 0.01:
            si += 1
        if receivers[ri][1] <= 0.01:
            ri += 1

    print("\n  planned transfers:")
    for src, dst, amount in moves:
        print(f"    {_short(src)} -> {_short(dst)}  {amount:,.2f}")

    if not _confirm(f"Submit {len(moves)} transfer(s)."):
        print("  aborted")
        _pause()
        return

    for src, dst, amount in moves:
        result = submit_transfer(
            http_url=config.http_url,
            private_key=config.private_key,
            domain=SignatureDomain[config.signature_domain_name],
            from_pubkey=src,
            to_pubkey=dst,
            margin_amount=amount,
        )
        state = "ok" if result.ok else f"FAILED {result.response_json}"
        print(f"    {_short(src)} -> {_short(dst)} {amount:,.2f}: {state}")
    _pause()


def _accounts_menu(config: Config) -> None:
    while True:
        print("\n" + _box("ACCOUNTS MANAGEMENT", [
            "1. Create New Subaccount",
            "2. Balance All Subaccounts",
            "3. Back",
        ]))
        choice = _ask("\n  > ")
        if choice == "1":
            _create_subaccount(config)
        elif choice == "2":
            _balance_subaccounts(config)
        elif choice in ("3", "0"):
            return


def _set_cycles(config: Config, config_path: str) -> None:
    """Edit `cycles:` in place, preserving the file's comments."""
    print(f"\n  current: {config.cycles}  (0 means run forever)")
    raw = _ask("  new value: ")
    if not raw:
        return
    try:
        value = int(raw)
        if value < 0:
            raise ValueError
    except ValueError:
        print("  must be a non-negative integer")
        return

    with open(config_path, encoding="utf-8") as handle:
        text = handle.read()
    new_text, count = re.subn(r"(?m)^cycles:\s*\d+", f"cycles: {value}", text)
    if count != 1:
        print(f"  could not find a single `cycles:` line in {config_path}")
        return
    with open(config_path, "w", encoding="utf-8") as handle:
        handle.write(new_text)
    config.cycles = value
    print(f"  cycles set to {value} in {config_path}")


def _configuration(config: Config, config_path: str) -> None:
    while True:
        print("\n" + _box("CONFIGURATION", [
            "Execution Target:",
            "",
            f"1. Number of Cycles      [{config.cycles}]",
            "2. Total Amount to Burn   (not wired)",
            "3. Total Trading Volume   (not wired)",
            "4. Back",
        ]))
        choice = _ask("\n  > ")
        if choice == "1":
            _set_cycles(config, config_path)
        elif choice in ("2", "3"):
            # Storing a stop target the run loop does not read would be worse
            # than refusing: the bot would keep trading past a limit the
            # operator believes is in force.
            print("\n  Not implemented. The cycle only stops on `cycles`, so a")
            print("  burn or volume target set here would never be enforced and")
            print("  the bot would trade straight past it. History (menu item 3)")
            print("  reports realised volume and fees in the meantime.")
            _pause()
        elif choice in ("4", "0"):
            return


def _close_all(config: Config) -> None:
    from .cli import cmd_flatten

    print("\n  Cancels every order and closes all strategy positions at market.")
    print("  Reduce-only throughout, so it can never open a position.")
    print("\n  1. dry run -- show what would be sent")
    print("  2. live    -- REAL FUNDS")
    print("  0. back")
    choice = _ask("\n  > ")
    if choice == "1":
        asyncio.run(cmd_flatten(config, dry_run=True))
    elif choice == "2":
        if _confirm("Close all positions at market."):
            asyncio.run(cmd_flatten(config, dry_run=False))
        else:
            print("  aborted")
    _pause()


# -- entry point ------------------------------------------------------------


def run_menu(config: Config, config_path: str) -> int:
    items = [
        "1. Start",
        "2. Active Strategy",
        "3. History",
        "4. Accounts Management",
        "5. Configuration",
        "6. Close All Positions",
        "0. Exit",
    ]
    actions: dict[str, Callable[[], None]] = {
        "1": lambda: _start(config),
        "2": lambda: _active_strategy(config),
        "3": lambda: _history(config),
        "4": lambda: _accounts_menu(config),
        "5": lambda: _configuration(config, config_path),
        "6": lambda: _close_all(config),
    }

    while True:
        print("\n" + _box("DELTA-NEUTRAL BOT", items))
        print(f"  mainnet  {config.http_url}")
        choice = _ask("\n  > ")
        if choice == "0":
            return 0
        action = actions.get(choice)
        if action is None:
            print("  no such option")
            continue
        try:
            action()
        except NoAccountTree as exc:
            print(f"\n  {exc}")
            _pause()
        except ConfigError as exc:
            print(f"\n  configuration error: {exc}")
            _pause()
        except KeyboardInterrupt:
            print("\n  interrupted")
        except Exception as exc:  # noqa: BLE001 - the menu must survive anything
            print(f"\n  {type(exc).__name__}: {exc}")
            _pause()

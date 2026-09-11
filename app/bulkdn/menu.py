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
from collections.abc import Callable

import requests

from .accounts import short_pubkey, unwrap_full_account
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
            f"no account exists on mainnet for master {short_pubkey(master)}.\n"
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


def _http(config: Config):
    """An authenticated HTTP client for read paths that need one."""
    from bulk_api.api.bulk_http import BulkHttpClient
    from bulk_api.common import SignatureDomain

    return BulkHttpClient(
        base_url=config.http_url,
        private_key=config.private_key,
        signature_domain=SignatureDomain[config.signature_domain_name],
    )


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
                    f"{config.hold_minutes} min hold."):
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
    from .fees import Realised, _fills_page

    http = _http(config)
    accounts = _accounts(config)
    tree = {pubkey for _, pubkey in accounts}
    grand = Realised()

    for label, pubkey in accounts:
        print(f"\n  {label} {short_pubkey(pubkey)}")
        try:
            # Raw dicts, not the SDK's parsed model -- see fees._fills_page.
            rows, _ = _fills_page(http, pubkey, limit=20, cursor=None)
        except Exception as exc:
            print(f"    query failed: {exc}")
            continue

        if not rows:
            print("    no fills")
            continue

        print(f"    {'symbol':<10} {'side':<5} {'size':>12} {'price':>12} {'fee':>10}")
        for fill in rows:
            side = "buy" if fill.get("isBuy") else "sell"
            print(
                f"    {str(fill.get('symbol', '?')):<10} {side:<5} "
                f"{float(fill.get('amount') or 0):>12.6f} "
                f"{float(fill.get('price') or 0):>12.3f} "
                f"{float(fill.get('fee') or 0):>10.4f}"
            )
        totals = Realised.from_fills(rows, tree)
        grand = grand + totals
        print(
            f"    -- {totals.fills} fills, volume ${totals.volume_usd:,.2f}, "
            f"fees ${totals.fees_usd:,.4f}"
        )

    print(f"\n  volume      ${grand.volume_usd:,.2f}")
    print(f"  self-trades ${grand.self_trade_volume_usd:,.2f}  (earn no tier credit)")
    print(f"  qualifying  ${grand.qualifying_volume_usd:,.2f}")
    print(f"  fees        ${grand.fees_usd:,.4f}")
    print("\n  (last 20 fills per account -- Configuration -> Progress walks it all)")
    _pause()


def _create_subaccount(config: Config) -> None:
    from .cli import cmd_create_subaccount

    name = _ask("\n  name (1-32 chars, A-Z a-z 0-9 - _): ")
    if not name:
        print("  aborted")
        _pause()
        return
    asyncio.run(cmd_create_subaccount(config, name, None))
    print("\n  ready to use -- the bot finds it from your master on startup")
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
        print(f"  {label + ' ' + short_pubkey(pk):<22} {bal:>14.2f} {bal - target:>12.2f}")
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
        print(f"    {short_pubkey(src)} -> {short_pubkey(dst)}  {amount:,.2f}")

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
        print(f"    {short_pubkey(src)} -> {short_pubkey(dst)} {amount:,.2f}: {state}")
    _pause()


def _encrypt_key(config: Config) -> None:
    from .cli import cmd_encrypt_key

    cmd_encrypt_key(config)
    _pause()


def _accounts_menu(config: Config) -> None:
    from . import keystore
    from .config import PRIVATE_KEY_FILE

    while True:
        state = "encrypted" if keystore.is_encrypted(PRIVATE_KEY_FILE) else "PLAINTEXT"
        print("\n" + _box("ACCOUNTS MANAGEMENT", [
            "1. Create New Subaccount",
            "2. Balance All Subaccounts",
            f"3. Encrypt Private Key   [{state}]",
            "4. Back",
        ]))
        choice = _ask("\n  > ")
        if choice == "1":
            _create_subaccount(config)
        elif choice == "2":
            _balance_subaccounts(config)
        elif choice == "3":
            _encrypt_key(config)
        elif choice in ("4", "0"):
            return


def _render_number(value: float) -> str:
    """YAML-safe decimal.

    Never scientific notation: YAML 1.1 does not reliably read `1e+06` as a
    number, and a target silently parsed as a string would disable the limit it
    was meant to set.
    """
    if value == int(value):
        return str(int(value))
    return f"{value:.8f}".rstrip("0").rstrip(".")


TARGET_BLOCK_COMMENT = (
    "# Execution target: whichever limit is reached first ends the run.\n"
    "# 0 disables a limit. burn_usd and volume_usd are measured from the\n"
    "# exchange's own fill records, so they survive a restart."
)


def _write_target(config_path: str, key: str, value: float, defaults) -> None:
    """Set one key under `execution_target`, keeping the file's comments.

    Walks lines rather than matching a pattern: the block is two levels deep
    and YAML numbers come in several shapes, both of which a regex handles
    badly. Writes the whole block when the file predates execution targets.
    """
    with open(config_path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()

    header = next(
        (i for i, line in enumerate(lines) if line.strip() == "execution_target:"), None
    )

    if header is None:
        block = [TARGET_BLOCK_COMMENT, "execution_target:"]
        for name in ("cycles", "burn_usd", "volume_usd"):
            chosen = value if name == key else getattr(defaults, name)
            block.append(f"  {name}: {_render_number(chosen)}")
        lines = [*lines, "", *block]
    else:
        end = next(
            (
                i
                for i in range(header + 1, len(lines))
                if lines[i].strip() and not lines[i].startswith((" ", "\t"))
            ),
            len(lines),
        )
        at = next(
            (
                i
                for i in range(header + 1, end)
                if lines[i].strip().startswith(f"{key}:")
            ),
            None,
        )
        if at is None:
            lines.insert(header + 1, f"  {key}: {_render_number(value)}")
        else:
            line = lines[at]
            indent = line[: len(line) - len(line.lstrip())]
            trailing = "  " + line[line.index("#") :] if "#" in line else ""
            lines[at] = f"{indent}{key}: {_render_number(value)}{trailing}"

    with open(config_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines).rstrip("\n") + "\n")


def _edit_target(config: Config, config_path: str, key: str, label: str) -> None:
    """Prompt for one execution-target value and persist it."""
    current = getattr(config.target, key)
    print(f"\n  current {label}: {_render_number(float(current))}  (0 disables it)")
    raw = _ask("  new value: ")
    if not raw:
        return
    try:
        value = float(raw)
        if value < 0:
            raise ValueError
    except ValueError:
        print("  must be a non-negative number")
        return

    _write_target(config_path, key, value, config.target)
    setattr(config.target, key, int(value) if key == "cycles" else value)
    if key == "cycles":
        config.cycles = int(value)
    print(f"  {label} set to {_render_number(value)} in {config_path}")


def _target_progress(config: Config) -> None:
    """Realised spend and volume against the configured targets."""
    from .fees import account_fee_tier, fee_state, realised_for_tree

    accounts = [pk for _, pk in _accounts(config)]
    http = _http(config)
    totals = realised_for_tree(http, accounts)

    print(f"\n  fills {totals.fills}")
    print(f"  fees            ${totals.fees_usd:,.4f}", end="")
    if config.target.burn_usd > 0:
        print(f"  of ${config.target.burn_usd:,.2f}")
    else:
        print("  (no burn target)")
    print(f"  volume          ${totals.volume_usd:,.2f}")
    print(f"  self-trades     ${totals.self_trade_volume_usd:,.2f}  (earn no tier credit)")
    print(f"  qualifying      ${totals.qualifying_volume_usd:,.2f}", end="")
    if config.target.volume_usd > 0:
        print(f"  of ${config.target.volume_usd:,.2f}")
    else:
        print("  (no volume target)")

    quote = account_fee_tier(config.http_url, accounts[0])
    if quote:
        print(
            f"\n  master tier {quote.tier_index} in the {quote.window_days}-day window: "
            f"maker {quote.maker_bps} bps, taker {quote.taker_bps} bps"
        )
    else:
        schedule = fee_state(config.http_url).get("global")
        if schedule:
            tier = schedule.tier_for(0.0)
            print(
                f"\n  no tier quote yet; the {schedule.window_days}-day schedule starts at "
                f"maker {tier.maker_bps} bps, taker {tier.taker_bps} bps"
            )
    _pause()


def _configuration(config: Config, config_path: str) -> None:
    while True:
        target = config.target
        print("\n" + _box("CONFIGURATION", [
            "Execution Target:",
            "  whichever is reached first; 0 disables",
            "",
            f"1. Number of Cycles      [{target.cycles or 'unlimited'}]",
            f"2. Total Amount to Burn  [${target.burn_usd:,.2f}]",
            f"3. Total Trading Volume  [${target.volume_usd:,.2f}]",
            "4. Progress",
            "5. Back",
        ]))
        choice = _ask("\n  > ")
        if choice == "1":
            _edit_target(config, config_path, "cycles", "cycle count")
        elif choice == "2":
            _edit_target(config, config_path, "burn_usd", "burn target (USD of fees)")
        elif choice == "3":
            _edit_target(config, config_path, "volume_usd", "volume target (USD, qualifying)")
        elif choice == "4":
            _target_progress(config)
        elif choice in ("5", "0"):
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

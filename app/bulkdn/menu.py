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
import logging
import pathlib
import shutil
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
    print("\n  Once it is running, press S to stop and cancel -- you do not")
    print("  need a second window, and you do not need to close this one.")
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


def _logs() -> None:
    """The tail of the log file, and where to find the whole thing.

    Here so that "send me your logs" needs no explanation of where they are:
    the path is printed even when the file does not exist yet.
    """
    from .cli import LOG_FILE

    path = pathlib.Path(LOG_FILE).resolve()
    print(f"\n  {path}")

    if not path.exists():
        print("\n  No log file yet -- it is written the first time the bot runs.")
        _pause()
        return

    if path.stat().st_size == 0:
        print("\n  Empty so far. It fills up as the bot runs.")
        _pause()
        return

    print(f"  {_human_size(path.stat().st_size)}\n")
    try:
        # Read as bytes from the end: a long run's log is megabytes, and there
        # is no reason to pull all of it into memory to show the last screen.
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 16_000))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError as exc:
        print(f"  could not read it: {exc}")
        _pause()
        return

    lines = tail.splitlines()
    if size > 16_000 and lines:
        lines = lines[1:]  # the first line is probably cut in half
    for line in lines[-40:]:
        print(f"  {line}")
    print("\n  Open the file itself for everything above this.")
    _pause()


def _history(config: Config) -> None:
    """Recent fills for every account in the tree, with volume and fees.

    Volume and fees are summed from the fills themselves rather than tracked
    separately, so the numbers cannot drift from what the exchange recorded.
    """
    from .fees import Realised, _fills_page, burned_usd

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
            f"burned ${burned_usd(totals.fees_usd):,.4f}"
        )

    print(f"\n  volume      ${grand.volume_usd:,.2f}")
    print(f"  self-trades ${grand.self_trade_volume_usd:,.2f}  (earn no tier credit)")
    print(f"  qualifying  ${grand.qualifying_volume_usd:,.2f}")
    print(f"  burned      ${burned_usd(grand.fees_usd):,.4f}")
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


def _human_size(total: float) -> str:
    for unit in ("B", "KB", "MB"):
        if total < 1024 or unit == "MB":
            return f"{total:,.0f} {unit}" if unit == "B" else f"{total:,.1f} {unit}"
        total /= 1024.0
    return f"{total:,.1f} MB"


def _size_of(path: pathlib.Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _cache_dirs(root: pathlib.Path) -> list[pathlib.Path]:
    """Compiled bytecode and lint caches.

    Worth offering because a .pyc embeds the absolute path of the source it was
    compiled from, so these carry the operator's username off the machine if the
    folder is ever shared. They regenerate on the next run, which is what makes
    them safe to remove -- unlike .venv, which is the install itself and is
    deliberately not listed here.
    """
    found = [d for d in root.rglob("__pycache__") if ".venv" not in d.parts]
    found += [d for d in root.rglob(".ruff_cache") if ".venv" not in d.parts]
    return sorted(found)


def _shown(path: pathlib.Path) -> str:
    """Relative to the project folder where possible.

    An absolute path in a delete list reads as another machine's file,
    which is alarming at exactly the wrong moment."""
    try:
        return str(path.relative_to(pathlib.Path.cwd()))
    except ValueError:
        return str(path)


def _close_log_handlers() -> None:
    """Detach the file handlers so the log file can be removed on Windows.

    Windows will not unlink a file another handle has open, and this process is
    holding one.

    Not reattached afterwards, deliberately. Someone who just erased their logs
    would not expect the file to reappear a moment later because the menu kept
    writing to it. The console still shows everything, and the next run starts
    a fresh file.
    """
    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h, logging.FileHandler)]:
        root.removeHandler(handler)
        handler.close()


def _delete(path: pathlib.Path) -> str:
    """Remove a path, except the key file, which is emptied in place.

    Deleting that one would take away the place the next key is pasted,
    and getting it back means re-running install.bat. What has to go is
    the key inside it, not the file."""
    from .cli import LOG_FILE
    from .config import PRIVATE_KEY_FILE, PRIVATE_KEY_TEMPLATE

    try:
        if path.name == PRIVATE_KEY_FILE:
            path.write_text(PRIVATE_KEY_TEMPLATE, encoding="utf-8")
            return f"  emptied {_shown(path)} -- ready for a new key"
        if path.name.startswith(LOG_FILE):
            _close_log_handlers()
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        return f"  FAILED  {_shown(path)}  ({exc})"
    return f"  deleted {_shown(path)}"


def _live_cycle_warning(state_file: pathlib.Path) -> str | None:
    """Whether the state file says a cycle may still be open on the exchange.

    Read from disk rather than from the exchange: this runs without connecting,
    and a file that says OPEN is exactly the case worth warning about.
    """
    from .state import Phase, StateStore

    try:
        state = StateStore(str(state_file)).load()
    except Exception:  # noqa: BLE001 - an unreadable file warns about nothing
        return None
    if state.summary_phase in (Phase.IDLE, Phase.COMPLETE):
        return None
    return (
        f"the state file records phase {state.summary_phase.value}. If that is still "
        f"true on the exchange, deleting it leaves the bot unable to find those "
        f"positions on the next run. Close All Positions first."
    )


def _log_files() -> list[pathlib.Path]:
    """The log and its rollovers, newest first."""
    from .cli import LOG_FILE

    return sorted(pathlib.Path.cwd().glob(f"{LOG_FILE}*"))


def _erase_targets(config: Config) -> list[tuple[str, str, list[pathlib.Path], str]]:
    from .config import PRIVATE_KEY_FILE

    return [
        ("1", "Trading state", [pathlib.Path(config.state_file)],
         "what the bot has open, and any recorded halt"),
        ("2", "Private key", [pathlib.Path(PRIVATE_KEY_FILE)],
         "wipes the key; the file stays, ready for a new one"),
        ("3", "Build caches", _cache_dirs(pathlib.Path.cwd()),
         "bytecode and lint caches -- these hold your username"),
        ("4", "Logs", _log_files(),
         "every run this machine has made, and your addresses"),
    ]


def _erase_data(config: Config) -> None:
    """Delete what this machine has stored. Nothing here reaches the exchange."""
    from .config import PRIVATE_KEY_FILE

    state_file = pathlib.Path(config.state_file)
    key_file = pathlib.Path(PRIVATE_KEY_FILE)

    # _box does not wrap, so these lines are fitted to BOX_WIDTH by hand.
    # test_erase fails the screen if one of them grows past it.
    print("\n" + _box("ERASE LOCAL DATA", [
        "Deletes only what this computer stores.",
        "Your BULK account, positions and funds are",
        "untouched. Nothing reaches the exchange.",
    ]))

    entries = _erase_targets(config)
    print()
    for key, label, paths, note in entries:
        present = [p for p in paths if p.exists()]
        if not present:
            print(f"  {key}. {label:14}  nothing to delete")
            continue
        size = _human_size(sum(_size_of(p) for p in present))
        where = _shown(present[0]) if len(present) == 1 else f"{len(present)} folders"
        print(f"  {key}. {label:14}  {where:34} {size:>10}")
        print(f"      {note}")
    print("  9. All of the above")
    print("  0. back")

    choice = _ask("\n  > ")
    if choice not in ("1", "2", "3", "4", "9"):
        return

    wanted = entries if choice == "9" else [e for e in entries if e[0] == choice]
    targets = [p for _k, _l, paths, _n in wanted for p in paths if p.exists()]
    if not targets:
        print("\n  Nothing to delete.")
        _pause()
        return

    print("\n  About to erase:")
    for path in targets:
        print(f"    {_shown(path)}")

    if state_file in targets:
        warning = _live_cycle_warning(state_file)
        if warning:
            print(f"\n  !!  {warning}")

    if key_file in targets:
        # Not the usual confirmation. Losing the only copy of a signing key
        # loses the account itself -- from every tool, not just this one -- and
        # there is no amount of care afterwards that recovers it.
        print("\n  !!  THE PRIVATE KEY IS THE ONLY WAY TO SIGN FOR THIS ACCOUNT.")
        print("      Without another copy, you will not be able to trade or")
        print("      withdraw from it again -- from anywhere, not just here.")
        print("      Make sure it is written down somewhere else first.")
        print("\n      Type 'DELETE KEY' to confirm, anything else aborts.")
        if _ask("      > ") != "DELETE KEY":
            print("  aborted")
            _pause()
            return
    elif not _confirm(f"Erase {len(targets)} item(s) from this computer."):
        print("  aborted")
        _pause()
        return

    print()
    for path in targets:
        print(_delete(path))
    _pause()


def _key_state(path: str) -> str:
    """How well the key file is actually protected, not merely whether it is.

    An empty password encrypts under a constant published in keystore.py, so
    the file opens for anyone holding this repository. Reporting that as
    "encrypted" tells an operator they are protected when they are not -- and
    the warning they saw is months behind them. It is detected the only way
    available: by opening the file with that constant.

    Costs one Argon2 derivation, so it is read once per visit rather than on
    every redraw of the menu.
    """
    from . import keystore

    if not keystore.is_encrypted(path):
        return "PLAINTEXT"
    try:
        keystore.load(path, keystore.DEFAULT_PASSWORD)
    except Exception:  # noqa: BLE001 - anything but success means a real password
        return "encrypted"
    return "default password"


def _accounts_menu(config: Config) -> None:
    from .config import PRIVATE_KEY_FILE

    state = _key_state(PRIVATE_KEY_FILE)
    while True:
        print("\n" + _box("ACCOUNTS MANAGEMENT", [
            "1. Create New Subaccount",
            "2. Balance All Subaccounts",
            f"3. Encrypt Private Key   [{state}]",
            "4. Erase Local Data",
            "5. Back",
        ]))
        choice = _ask("\n  > ")
        if choice == "1":
            _create_subaccount(config)
        elif choice == "2":
            _balance_subaccounts(config)
        elif choice == "3":
            _encrypt_key(config)
            state = _key_state(PRIVATE_KEY_FILE)
        elif choice == "4":
            _erase_data(config)
            state = _key_state(PRIVATE_KEY_FILE)
        elif choice in ("5", "0"):
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
    """Realised spend and volume, for this run and for the account's lifetime.

    Both, because they answer different questions and showing only the lifetime
    figure is what made a finished target look permanent.
    """
    from .fees import account_fee_tier, burned_usd, fee_state, realised_for_tree
    from .state import StateStore

    accounts = [pk for _, pk in _accounts(config)]
    http = _http(config)
    totals = realised_for_tree(http, accounts)
    state = StateStore(config.state_file).load()

    print(f"\n  fills {totals.fills}")
    print("\n  ALL TIME")
    print(f"    burned        ${burned_usd(totals.fees_usd):,.4f}")
    print(f"    volume        ${totals.volume_usd:,.2f}")
    print(f"    self-trades   ${totals.self_trade_volume_usd:,.2f}  (earn no tier credit)")
    print(f"    qualifying    ${totals.qualifying_volume_usd:,.2f}")

    print("\n  THIS RUN", end="")
    if not state.has_baseline:
        print("\n    not started -- the target is measured from the next start")
    else:
        burned = burned_usd(totals.fees_usd - state.baseline_fees_usd)
        volume = totals.qualifying_volume_usd - state.baseline_volume_usd
        print()
        print(f"    burned        ${burned:,.4f}", end="")
        if config.target.burn_usd > 0:
            print(f"  of ${config.target.burn_usd:,.2f}")
        else:
            print("  (no burn target)")
        print(f"    qualifying    ${volume:,.2f}", end="")
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
        "7. Logs",
        "0. Exit",
    ]
    actions: dict[str, Callable[[], None]] = {
        "1": lambda: _start(config),
        "2": lambda: _active_strategy(config),
        "3": lambda: _history(config),
        "4": lambda: _accounts_menu(config),
        "5": lambda: _configuration(config, config_path),
        "6": lambda: _close_all(config),
        "7": _logs,
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

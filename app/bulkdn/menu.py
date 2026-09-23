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
import contextlib
import copy
import logging
import math
import pathlib
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass

import requests

from .accounts import short_pubkey, unwrap_full_account
from .cli import (
    ACCEPTED,
    EXIT_UNKNOWN,
    LOG_FILE,
    UNKNOWN,
    UNKNOWN_ADVICE,
    UNSENT,
    cmd_create_subaccount,
    cmd_encrypt_key,
    cmd_flatten,
    cmd_run,
    cmd_status,
    submit_signed,
)
from .config import Config, ConfigError, LegConfig
from .retry import describe
from . import proxy

log = logging.getLogger(__name__)

BOX_WIDTH = 46


class NoAccountTree(Exception):
    """The signing key has no BULK account yet, so nothing can be listed."""


# -- rendering --------------------------------------------------------------


def _box(title: str, lines: list[str]) -> str:
    """A framed menu. Grows rather than letting a long line break the frame.

    `ljust` pads but never truncates, so an entry wider than BOX_WIDTH hung
    out past the right-hand border and left the box visibly broken -- which
    is exactly what a menu looks like the day it gains a longer label. The
    width follows the content instead, and every screen that already fits
    keeps the size it always had.
    """
    width = max(BOX_WIDTH, max((len(line) for line in lines), default=0) + 4)
    top = "+" + "-" * (width - 2) + "+"
    out = [top, "|" + title.center(width - 2) + "|", top]
    for line in lines:
        out.append("| " + line.ljust(width - 4) + " |")
    out.append(top)
    return "\n".join(out)


class Cancelled(Exception):
    """Ctrl+C or end-of-input at a prompt: abandon the current action.

    An exception rather than a sentinel answer. `_ask` used to turn Ctrl+C
    into "0", which is "back" on a menu -- and a perfectly good VALUE on every
    prompt that asks for one. Ctrl+C at "new value:" wrote a burn target of 0,
    which disables the limit, and Ctrl+C at "name:" submitted a real
    createSubAccount called "0". Raising means no caller can mistake a cancel
    for an answer: whatever it was about to write or send is simply never
    reached. `run_menu` catches it and returns to the main menu.
    """


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt) as exc:
        print()
        raise Cancelled() from exc


def _confirm(what: str) -> bool:
    """Real funds are at stake, so require the word, not a keystroke."""
    print(f"\n  !!  {what}")
    print("      This is mainnet. Type 'yes' to proceed, anything else aborts.")
    try:
        return _ask("      > ") == "yes"
    except Cancelled:
        return False


def _pause() -> None:
    # Ctrl+C here means "get me out", which is where this goes anyway.
    with contextlib.suppress(Cancelled):
        _ask("\n  [enter] to return to the menu ")


def _fresh(config: Config) -> Config:
    """A private copy of the settings for one run.

    A run writes into the config it is given: the sizing plan replaces each
    leg's size with what the thinnest account can carry, and a leg written as
    a range redraws its size every cycle. That is by design inside a run --
    but the menu held ONE config for its whole life, so a dry run's sizes
    became the next live run's sizes, capped by whatever margin the rehearsal
    happened to see. Each run now gets its own copy, and nothing it does
    outlives it. The menu's own edits -- targets, markets, mode -- still land
    on the original, which is what every later copy is taken from.
    """
    return copy.deepcopy(config)


# -- helpers ----------------------------------------------------------------


@dataclass(frozen=True)
class Tree:
    """One signing key, its master account, and the subs that master owns.

    Trees are kept apart rather than pooled into one list of accounts because
    the key is what makes them separate. A transfer is signed by the key that
    owns both ends, so margin cannot cross from one tree to another; and the
    exchange can only call a fill a self-trade when it can see that both sides
    belong to the same master, which across two keys it cannot.
    """

    index: int
    private_key: str
    master: str
    subs: tuple[str, ...]

    @property
    def accounts(self) -> tuple[str, ...]:
        return (self.master, *self.subs)

    def title(self, alone: bool) -> str:
        return "master" if alone else f"master {self.index}"

    def labelled(self, alone: bool) -> list[tuple[str, str]]:
        """(label, pubkey) for this tree, numbered only when there are others.

        One key is still the common case, and numbering its only master
        `master 1` would be noise about a choice the operator never made.
        """
        tag = "" if alone else f" {self.index}"
        return [(f"master{tag}", self.master)] + [(f"sub{tag}", pk) for pk in self.subs]


def _tree(config: Config, private_key: str, index: int) -> Tree:
    """Read one key's account tree from the exchange.

    Read rather than configured: the config names one sub, but the master may
    own others, and balancing has to see all of them.
    """
    from bulk_api.common.signer import TransactionSigner

    master = TransactionSigner(private_key).public_key
    body = requests.post(
        f"{config.http_url}/account",
        json={"type": "fullAccount", "user": master},
        timeout=25,
    )
    if body.status_code == 404:
        # The usual case for a fresh key, and the raw 404 explains none of it.
        # The key is named because with several in the file, "which one" is
        # the first thing the operator has to work out.
        raise NoAccountTree(
            f"no account exists on mainnet for master {short_pubkey(master)}"
            f" (key {index} in private_key.local).\n"
            "  A BULK account is created by an on-chain USDC deposit into the\n"
            "  Solana vault -- this bot cannot do that. Deposit first, then\n"
            "  Accounts Management -> Create New Subaccount."
        )
    body.raise_for_status()
    account = unwrap_full_account(body.json())
    subs = tuple(
        entry["pubkey"]
        for entry in (account.get("subAccounts") or [])
        if isinstance(entry, dict) and entry.get("pubkey")
    )
    return Tree(index=index, private_key=private_key, master=master, subs=subs)


def _trees(config: Config) -> list[Tree]:
    """Every key's tree, in the order the keys appear in the file.

    One HTTP call per key. That is the whole cost of the menu having noticed
    the other keys at all: before this it read `config.private_key`, which is
    the first line of the file, and an operator who had added a second master
    saw no sign of it anywhere -- not in the balance table, not in the history,
    not in the progress figures it was being judged against.
    """
    # `private_keys` only: Config.__post_init__ already folds a lone
    # `private_key` into it, so a fallback here would be a second rule for
    # "which keys exist" free to disagree with the first.
    return [
        _tree(config, key, index)
        for index, key in enumerate(config.private_keys, start=1)
    ]


def _accounts(config: Config) -> list[tuple[str, str]]:
    """(label, pubkey) for every account under every key."""
    trees = _trees(config)
    alone = len(trees) == 1
    return [pair for tree in trees for pair in tree.labelled(alone)]


def _transferable(config: Config, pubkey: str) -> float | None:
    """What this account could move right now, or None when it is not known.

    None, never 0.0, for a read that failed. A 429 or a 502 used to come back
    as 0.0, which is indistinguishable from an empty account -- and Balance
    then planned transfers INTO it, levelling the tree against a figure that
    was never read. The callers leave an unknown account out of the plan.
    """
    try:
        body = requests.post(
            f"{config.http_url}/account",
            json={"type": "fullAccount", "user": pubkey},
            timeout=25,
        )
        if body.status_code != 200:
            log.warning(
                "balance read for %s failed: HTTP %s",
                short_pubkey(pubkey), body.status_code,
            )
            return None
        margin = unwrap_full_account(body.json()).get("margin") or {}
        # transferableBalance, not totalMargin: margin backing an open position
        # cannot be moved, and asking to move it just gets the transfer rejected.
        return float(margin.get("transferableBalance") or 0.0)
    except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
        log.warning("balance read for %s failed: %s", short_pubkey(pubkey), describe(exc))
        return None


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


def _stop_conditions(config: Config) -> str:
    """When this run will end, in the words the confirmation shows.

    The confirmation used to say "unlimited cycle(s)" whenever `cycles` was 0
    -- which the shipped settings are -- even with a $3 burn target that would
    end the run within the hour. Every limit that applies is named, because
    "how long will this spend my money for" is the question being confirmed.
    """
    target = config.target
    limits = []
    if target.cycles:
        limits.append(f"{target.cycles} cycle(s)")
    if target.burn_usd:
        limits.append(f"${target.burn_usd:,.2f} of fees burned")
    if target.volume_usd:
        limits.append(f"${target.volume_usd:,.2f} of qualifying volume")
    if not limits:
        return "no stop condition set -- runs until you press S"
    if len(limits) == 1:
        return f"stops after {limits[0]}"
    return f"stops at {', '.join(limits[:-1])} or {limits[-1]}, whichever comes first"


def _start(config: Config) -> None:
    # Named here because the mode can also come from the command line, so the
    # settings file is not proof of what this run will trade.
    print(f"\n  {config.mode}: {', '.join(leg.symbol for leg in config.active_legs)}")
    print(f"  {_stop_conditions(config)}")
    print("\n  1. dry run  -- connects and logs, submits nothing")
    print("  2. live     -- REAL FUNDS")
    print("  0. back")
    print("\n  Once it is running, press S to stop and cancel -- you do not")
    print("  need a second window, and you do not need to close this one.")
    choice = _ask("\n  > ")
    if choice == "1":
        asyncio.run(cmd_run(_fresh(config), dry_run=True))
    elif choice == "2":
        if _confirm(f"Start LIVE trading, {config.mode} mode, on "
                    f"{', '.join(leg.symbol for leg in config.active_legs)}: "
                    f"{_stop_conditions(config)}; "
                    f"{config.hold_minutes} min hold per cycle."):
            asyncio.run(cmd_run(_fresh(config), dry_run=False))
        else:
            print("  aborted")
    _pause()


def _active_strategy(config: Config) -> None:
    asyncio.run(cmd_status(_fresh(config)))
    _pause()


def _logs() -> None:
    """The tail of the log file, and where to find the whole thing.

    Here so that "send me your logs" needs no explanation of where they are:
    the path is printed even when the file does not exist yet.
    """
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
    from .fees import Realised, burned_usd, fills_page

    http = _http(config)
    trees = _trees(config)
    alone = len(trees) == 1
    # Each account carries the set the exchange can see it belongs to: its own
    # master's tree, and not the accounts under some other key.
    walk = [
        (set(tree.accounts), label, pubkey)
        for tree in trees
        for label, pubkey in tree.labelled(alone)
    ]
    grand = Realised()
    # Spans every account of every key: a trade we were both sides of appears
    # in both histories, and the grand total must count it once.
    seen: set = set()

    for kin, label, pubkey in walk:
        print(f"\n  {label} {short_pubkey(pubkey)}")
        try:
            # Raw dicts, not the SDK's parsed model -- see fees.fills_page.
            rows, _ = fills_page(http, pubkey, limit=20, cursor=None)
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
        # Twice, deliberately. The printed line is this account's own view
        # of its fills; the grand total is the deduplicated walk. Handing the
        # `seen` set to the line as well would make the second account of a
        # self-trade report volume its history plainly shows.
        totals = Realised.from_fills(rows, kin)
        grand = grand + Realised.from_fills(rows, kin, seen)
        print(
            f"    -- {totals.fills} fills, volume ${totals.volume_usd:,.2f}, "
            f"burned ${burned_usd(totals.fees_usd):,.4f}"
        )

    print(f"\n  volume      ${grand.volume_usd:,.2f}")
    print(f"  self-trades ${grand.self_trade_volume_usd:,.2f}  (between your own accounts)")
    print(f"  qualifying  ${grand.qualifying_volume_usd:,.2f}  (referral window)")
    print(f"  fee tier    ${grand.tier_volume_usd:,.2f}  (docs say self-trades do not count)")
    print(f"  burned      ${burned_usd(grand.fees_usd):,.4f}")
    if not alone:
        print(
            f"\n  {len(trees)} masters. A trade between two of them is not a "
            "self-trade:\n  nothing on the exchange links one master to another."
        )
    print("\n  (last 20 fills per account -- Configuration -> Progress walks it all)")
    _pause()


def _pick_tree(config: Config, what: str) -> Tree | None:
    """Which master to act on. Asks only when there is a choice to make."""
    trees = _trees(config)
    if len(trees) == 1:
        return trees[0]

    print(f"\n  under which master? ({what})")
    for tree in trees:
        owned = f"{len(tree.subs)} sub-account(s)" if tree.subs else "no sub-accounts"
        print(f"    {tree.index}. {short_pubkey(tree.master)}  {owned}")
    answer = _ask("\n  > ")
    for tree in trees:
        if answer == str(tree.index):
            return tree
    print("  aborted")
    return None


# What the exchange accepts as a sub-account name, per the prompt's own text.
SUBACCOUNT_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")


def _create_subaccount(config: Config) -> None:
    tree = _pick_tree(config, "the new sub-account belongs to one of them")
    if tree is None:
        _pause()
        return
    name = _ask("\n  name (1-32 chars, A-Z a-z 0-9 - _): ")
    if not name:
        print("  aborted")
        _pause()
        return
    if not SUBACCOUNT_NAME.fullmatch(name):
        # Checked here rather than left to the exchange: a refusal there costs
        # a signed transaction and says less.
        print("  a name is 1-32 characters of A-Z a-z 0-9 - _ -- nothing was sent")
        _pause()
        return
    # A sub-account cannot be deleted or moved to another master, so this is
    # confirmed like anything else that changes the account.
    if not _confirm(
        f"Create sub-account {name!r} under master {short_pubkey(tree.master)}. "
        "It cannot be deleted afterwards."
    ):
        print("  aborted")
        _pause()
        return

    code = asyncio.run(
        cmd_create_subaccount(config, name, None, private_key=tree.private_key)
    )
    # What the exchange said, not what was hoped. This printed "ready to use"
    # whatever came back, including after a rejection.
    if code == 0:
        print("\n  ready to use -- the bot finds it from your master on startup")
    elif code == EXIT_UNKNOWN:
        print("\n  NOT CONFIRMED -- see above, and check before creating it again")
    else:
        print("\n  not created -- see the exchange's answer above")
    _pause()


def _settle(balances: list[tuple[str, str, float]]) -> list[tuple[str, str, float]]:
    """The transfers that bring a set of balances level, greedily matched."""
    target = sum(bal for _, _, bal in balances) / len(balances)

    # Anything below a cent is noise; moving it costs a transaction for nothing.
    senders = [(pk, bal - target) for _, pk, bal in balances if bal - target > 0.01]
    receivers = [(pk, target - bal) for _, pk, bal in balances if target - bal > 0.01]

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
    return moves


def _balance_subaccounts(config: Config) -> None:
    """Even out transferable margin inside each master's own tree.

    Inside each tree, not across all of them. A margin transfer is signed by
    the key that owns both ends, so nothing can move from one master to
    another without an on-chain withdrawal and deposit -- which this bot
    cannot do. Levelling the whole pool to one figure is therefore not on
    offer here, and pretending otherwise would only produce transfers the
    exchange rejects.
    """
    trees = _trees(config)
    alone = len(trees) == 1
    plan: list[tuple[Tree, str, str, float]] = []

    for tree in trees:
        accounts = tree.labelled(alone)
        if len(accounts) < 2:
            print(f"\n  {tree.title(alone)} {short_pubkey(tree.master)} "
                  "owns no sub-accounts yet")
            continue

        read = [(label, pk, _transferable(config, pk)) for label, pk in accounts]
        # An account whose balance could not be read is left out, not counted
        # as empty: planning around a guessed 0 would move margin INTO it on
        # the strength of a failed request.
        unread = [(label, pk) for label, pk, bal in read if bal is None]
        balances = [(label, pk, bal) for label, pk, bal in read if bal is not None]
        for label, pk in unread:
            print(f"\n  !! could not read {label} {short_pubkey(pk)} -- left out "
                  "of the plan. Try again in a minute.")
        if len(balances) < 2:
            print(f"\n  {tree.title(alone)} {short_pubkey(tree.master)}: fewer than "
                  "two balances could be read, so nothing is planned for it")
            continue
        total = sum(bal for _, _, bal in balances)
        target = total / len(balances)

        print(f"\n  {'account':<22} {'transferable':>14} {'delta':>12}")
        for label, pk, bal in balances:
            print(
                f"  {label + ' ' + short_pubkey(pk):<22} "
                f"{bal:>14.2f} {bal - target:>12.2f}"
            )
        print(
            f"\n  total {total:,.2f} across {len(balances)} accounts "
            f"-> {target:,.2f} each"
        )

        moves = _settle(balances)
        if not moves:
            print("  already balanced")
            continue
        plan.extend((tree, src, dst, amount) for src, dst, amount in moves)

    if not plan:
        _pause()
        return

    print("\n  planned transfers:")
    for _, src, dst, amount in plan:
        print(f"    {short_pubkey(src)} -> {short_pubkey(dst)}  {amount:,.2f}")

    if not _confirm(f"Submit {len(plan)} transfer(s)."):
        print("  aborted")
        _pause()
        return

    _submit_plan(config, plan)
    _pause()


def _submit_plan(config: Config, plan: list[tuple[Tree, str, str, float]]) -> None:
    """Send a list of transfers, one at a time, and say what became of each.

    A failure used to end the loop with a traceback halfway down the list --
    the exchange timing out on the third of six transfers left the operator
    with two done, four not, and no summary saying which. Now each is caught
    on its own:

      * refused outright   the next one is still sent: each move is sized
                           from its own two balances, so one refusal does not
                           make any other one wrong.
      * outcome UNKNOWN,   the network failed under it. The rest are NOT sent:
        or unreachable     whatever broke that one is likely to break the
                           next, and a pile of unknowns is harder to check by
                           hand than one.
    """
    from .subaccounts import submit_transfer
    from bulk_api.common import SignatureDomain

    done: list[str] = []
    refused: list[str] = []
    unknown: list[str] = []
    for index, (tree, src, dst, amount) in enumerate(plan):
        line = f"{short_pubkey(src)} -> {short_pubkey(dst)} {amount:,.2f}"
        try:
            _result, outcome, detail = submit_signed(
                submit_transfer,
                http_url=config.http_url,
                # The owning key, not the first one in the file. A transfer
                # signed by a key that owns neither end is a transfer the
                # exchange refuses, and it would refuse it one account at a time.
                private_key=tree.private_key,
                domain=SignatureDomain[config.signature_domain_name],
                from_pubkey=src,
                to_pubkey=dst,
                margin_amount=amount,
            )
        except Exception as exc:  # noqa: BLE001 - one transfer must not lose the summary
            # Raised before anything reached the wire -- signing, serialising.
            outcome, detail = "error", describe(exc)
        if outcome == ACCEPTED:
            done.append(line)
            print(f"    {line}: ok")
        elif outcome in (UNKNOWN, UNSENT):
            if outcome == UNKNOWN:
                unknown.append(line)
                print(f"    {line}: UNKNOWN ({detail})")
            else:
                refused.append(line)
                print(f"    {line}: exchange unreachable, not sent ({detail})")
            skipped = [
                f"{short_pubkey(s)} -> {short_pubkey(d)} {a:,.2f}"
                for _t, s, d, a in plan[index + 1:]
            ]
            break
        else:
            refused.append(line)
            print(f"    {line}: refused ({detail})")
    else:
        skipped = []

    print(f"\n  {len(done)} done, {len(refused)} refused, {len(unknown)} unknown, "
          f"{len(skipped)} not sent")
    for line in skipped:
        print(f"    not sent: {line}")
    if unknown:
        print(f"\n  {UNKNOWN_ADVICE}")


def _collect_to_master(config: Config) -> None:
    """Sweep every sub-account's transferable margin up to its own master.

    Each tree into its own master, for the same reason Balance stays inside
    one tree: the key that signs owns both ends, and a sub under another key
    has no master here to be swept into.
    """
    trees = _trees(config)
    alone = len(trees) == 1
    plan: list[tuple[Tree, str, float]] = []

    for tree in trees:
        if not tree.subs:
            print(f"\n  {tree.title(alone)} {short_pubkey(tree.master)} "
                  "owns no sub-accounts yet")
            continue

        print(f"\n  {tree.title(alone)} {short_pubkey(tree.master)}")
        print(f"  {'sub-account':<22} {'transferable':>14}")
        for label, pk in tree.labelled(alone)[1:]:
            bal = _transferable(config, pk)
            if bal is None:
                # Unknown is not zero, and it is not "everything" either:
                # nothing is planned for an account that could not be read.
                print(f"  {label + ' ' + short_pubkey(pk):<22} {'unreadable':>14}"
                      "  -- left out")
                continue
            print(f"  {label + ' ' + short_pubkey(pk):<22} {bal:>14.2f}")
            # Floored, not rounded: rounding 10.005 up asks for a cent the
            # account does not have, and the whole transfer is refused for it.
            amount = int(bal * 100) / 100
            if amount > 0.01:
                plan.append((tree, pk, amount))

    if not plan:
        print("\n  nothing to collect")
        _pause()
        return

    print("\n  planned transfers:")
    for tree, src, amount in plan:
        print(f"    {short_pubkey(src)} -> {short_pubkey(tree.master)}  {amount:,.2f}")
    print(f"\n  total {sum(a for _, _, a in plan):,.2f}")
    print("  Margin backing an open position stays where it is -- only the")
    print("  transferable balance moves.")

    if not _confirm(f"Submit {len(plan)} transfer(s) to the master account."):
        print("  aborted")
        _pause()
        return

    _submit_plan(config, [(tree, src, tree.master, amount) for tree, src, amount in plan])
    _pause()


def _encrypt_key(config: Config) -> None:
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


def _same_file(a: pathlib.Path, b: pathlib.Path) -> bool:
    """Whether two paths name the same file, however each was spelled.

    `settings.yaml`, `.\\settings.yaml` and an absolute path are all the same
    file and all reachable here, since one side comes from --config and the
    other from a listing.
    """
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _delete(path: pathlib.Path, settings: pathlib.Path | None = None) -> str:
    """Remove a path -- except the two that have to keep existing.

    The key file is emptied in place: deleting it would take away the place the
    next key is pasted, and getting it back means re-running install.bat.

    The settings file is rewritten from the shipped template, for the same
    reason and one more -- "erase my data" should leave a copy that still runs,
    and a bot with no settings file does not start at all. What has to go is
    the sizes and the leverage, not the file.

    `settings` is passed in rather than inferred. This once matched any `.yaml`
    path, which meant any future yaml added to the erase list would have been
    silently overwritten with the settings template instead of deleted.
    """
    from .config import PRIVATE_KEY_FILE, PRIVATE_KEY_TEMPLATE, SETTINGS_TEMPLATE

    try:
        if path.name == PRIVATE_KEY_FILE:
            path.write_text(PRIVATE_KEY_TEMPLATE, encoding="utf-8")
            return f"  emptied {_shown(path)} -- ready for a new key"
        if path.name == proxy.PROXY_FILE:
            # Emptied rather than removed, for the same reason as the key file:
            # it is where the next one gets pasted, and the instructions inside
            # are most of what the file is.
            path.write_text(proxy.PROXY_TEMPLATE, encoding="utf-8")
            return f"  emptied {_shown(path)} -- ready for a new proxy"
        if settings is not None and _same_file(path, settings):
            template = pathlib.Path(SETTINGS_TEMPLATE)
            if not template.exists():
                return (
                    f"  FAILED  {_shown(path)}  ({_shown(template)} is missing, "
                    "so there is nothing to reset to -- run update.bat)"
                )
            shutil.copyfile(template, path)
            return f"  reset {_shown(path)} to the shipped defaults"
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
    return sorted(pathlib.Path.cwd().glob(f"{LOG_FILE}*"))


def _erase_targets(
    config: Config, config_path: str
) -> list[tuple[str, str, list[pathlib.Path], str]]:
    from .config import PRIVATE_KEY_FILE
    from .state import dry_run_path

    return [
        ("1", "Trading state", [pathlib.Path(config.state_file),
                                pathlib.Path(dry_run_path(config.state_file))],
         "what the bot has open, and any recorded halt"),
        ("2", "Private key", [pathlib.Path(PRIVATE_KEY_FILE)],
         "wipes the key; the file stays, ready for a new one"),
        ("3", "Build caches", _cache_dirs(pathlib.Path.cwd()),
         "bytecode and lint caches -- these hold your username"),
        ("4", "Logs", _log_files(),
         "every run this machine has made, and your addresses"),
        ("5", "Settings", [pathlib.Path(config_path)],
         "resets sizes, leverage and targets to the shipped defaults"),
        ("6", "Proxy", [pathlib.Path(proxy.PROXY_FILE)],
         "wipes the proxy address; it usually carries a password"),
    ]


def _venv_note() -> list[str]:
    """What an erase cannot reach, and what to do about it.

    The bot runs from inside `app\\.venv`, and Windows will not let a running
    program delete its own executable -- a rmtree from in here fails with
    WinError 5 partway through and leaves a broken environment, which is worse
    than leaving it alone. So it is reported rather than attempted.
    """
    venv = pathlib.Path("app/.venv")
    if not venv.exists():
        return []
    return [
        "",
        f"  Not removable from here: {_shown(venv)}  ({_human_size(_size_of(venv))})",
        "    No settings live in it -- but your Windows username does, in",
        "    pyvenv.cfg, the activate scripts and the pip shims. The bot is",
        "    running from inside it, so it cannot delete itself.",
        "    To finish: close this window, delete the app\\.venv folder,",
        "    then run install.bat to build a fresh one.",
    ]


def _erase_data(config: Config, config_path: str) -> None:
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

    entries = _erase_targets(config, config_path)
    print()
    for key, label, paths, note in entries:
        present = [p for p in paths if p.exists()]
        if not present:
            print(f"  {key}. {label:14}  nothing to delete")
            continue
        size = _human_size(sum(_size_of(p) for p in present))
        where = _shown(present[0]) if len(present) == 1 else f"{len(present)} items"
        print(f"  {key}. {label:14}  {where:34} {size:>10}")
        print(f"      {note}")
    print("  9. All of the above  -- back to a freshly installed copy")
    print("  0. back")
    for line in _venv_note():
        print(line)

    choice = _ask("\n  > ")
    if choice not in tuple(entry[0] for entry in entries) + ("9",):
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
        print(_delete(path, settings=pathlib.Path(config_path)))
    for line in _venv_note():
        print(line)
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
    from nacl.exceptions import CryptoError

    from . import keystore

    if not keystore.is_encrypted(path):
        return "PLAINTEXT"
    try:
        keystore.load(path, keystore.DEFAULT_PASSWORD)
    except keystore.KeystoreError as exc:
        # Only a failed decryption means "a password other than the default".
        # A file that is truncated, from a newer build, or unreadable also
        # fails to open -- and calling that "encrypted" told the operator
        # their key was safely protected when the bot could not read it at
        # all, which they would discover at the next start.
        if isinstance(exc.__cause__, CryptoError):
            return "encrypted"
        return "UNREADABLE"
    except Exception:  # noqa: BLE001 - any other failure is not "protected"
        return "UNREADABLE"
    return "default password"


def _accounts_menu(config: Config, config_path: str) -> None:
    from .config import PRIVATE_KEY_FILE

    state = _key_state(PRIVATE_KEY_FILE)
    # Counted from the file, not from the exchange: this line has to be right
    # before any account is read, and it is the answer to "I added a second
    # master -- did it take?" A screen that acts on every key should say how
    # many it found.
    keys = len(config.private_keys) or 1
    while True:
        print("\n" + _box("ACCOUNTS MANAGEMENT", [
            "1. Create New Subaccount",
            "2. Balance All Subaccounts",
            "3. Collect Funds to Main",
            f"4. Encrypt Private Key   [{state}]",
            "5. Erase Local Data",
            "6. Back",
        ]))
        print(f"  {keys} master key(s) in private_key.local")
        choice = _ask("\n  > ")
        if choice == "1":
            _create_subaccount(config)
        elif choice == "2":
            _balance_subaccounts(config)
        elif choice == "3":
            _collect_to_master(config)
        elif choice == "4":
            _encrypt_key(config)
            state = _key_state(PRIVATE_KEY_FILE)
        elif choice == "5":
            _erase_data(config, config_path)
            state = _key_state(PRIVATE_KEY_FILE)
        elif choice in ("6", "0"):
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
        # `inf` and `nan` parse as floats. inf crashed `_render_number` on
        # the way to the file; nan passed `< 0` and would have been written
        # as a target that no progress ever reaches or exceeds.
        if not math.isfinite(value) or value < 0:
            raise ValueError
        if key == "cycles" and not value.is_integer():
            # Written as typed and read back as a whole number, so 2.5 would
            # save "2.5" and then refuse to load.
            raise ValueError
    except ValueError:
        print("  must be a non-negative number"
              + (" (a whole number of cycles)" if key == "cycles" else ""))
        return

    _write_target(config_path, key, value, config.target)
    setattr(config.target, key, int(value) if key == "cycles" else value)
    if key == "cycles":
        config.cycles = int(value)
    print(f"  {label} set to {_render_number(value)} in {config_path}")


def _write_scalar(config_path: str, key: str, value: str) -> None:
    """Set a top-level key, keeping the rest of the file as it is.

    Inserted above the markets block when absent rather than appended,
    because that is where it is documented and where someone reading the file
    will look for it. Line-walking rather than a regex for the same reason
    the execution target uses one: the file is full of comments that must
    survive.
    """
    with open(config_path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()

    at = next(
        (i for i, line in enumerate(lines) if line.strip().startswith(f"{key}:")
         and not line.startswith((" ", "\t"))),
        None,
    )
    if at is not None:
        lines[at] = f"{key}: {value}"
    else:
        anchor = next(
            (i for i, line in enumerate(lines) if line.rstrip() in ("markets:", "legs:")),
            None,
        )
        block = [f"{key}: {value}", ""]
        lines = [*lines, "", *block] if anchor is None else [
            *lines[:anchor], *block, *lines[anchor:]
        ]

    with open(config_path, "w", encoding="utf-8", newline="") as handle:
        handle.write("\n".join(lines) + "\n")


def _write_mode(config_path: str, mode: str) -> None:
    _write_scalar(config_path, "mode", mode)


_SYMBOL_LINE = re.compile(r"""^\s*(?:-\s+)?symbol\s*:\s*(['"]?)([^'"#\s]+)\1\s*(?:#.*)?$""")


def _symbol_on(line: str) -> str | None:
    """The market a `symbol:` line names, however it is spelled.

    Matched by exact text before, so `symbol: "ETH-USD"`, `symbol: ETH-USD  #
    the dear one` or `symbol:  ETH-USD` were all invisible -- and turning that
    market on or off from the menu failed with "not in the settings file" for a
    market the file plainly listed. Quotes, spacing and a trailing comment are
    what YAML itself ignores, so they are ignored here too.
    """
    match = _SYMBOL_LINE.match(line)
    return match.group(2) if match else None


def _block_at(lines: list[str], symbol: str) -> tuple[int, int] | None:
    """The line range of the market block naming `symbol`, or None.

    Found by the symbol rather than by the block's name, because the file has
    two spellings and the symbol is the one thing both of them carry. A
    `markets:` list writes `- symbol: BTC-USD`; a `legs:` mapping writes
    `symbol: BTC-USD` under a named block. Either way that line is the block,
    and the block runs until a line at or above its own indentation.
    """
    for index, line in enumerate(lines):
        if _symbol_on(line) != symbol:
            continue
        indent = len(line) - len(line.lstrip())
        if line.lstrip().startswith("- "):
            # The dash sits at the block's own indentation; its fields are
            # indented past it.
            indent += 2
        end = len(lines)
        for after in range(index + 1, len(lines)):
            body = lines[after]
            if not body.strip() or body.lstrip().startswith("#"):
                continue
            if len(body) - len(body.lstrip()) < indent:
                end = after
                break
        return index, end
    return None


def _write_market_enabled(config_path: str, symbol: str, enabled: bool) -> None:
    """Turn one market on or off, leaving the rest of the file alone.

    Line-walking rather than loading and re-dumping the YAML, for the reason
    every other writer here does it: the file is mostly comments, and a
    round trip through a YAML library throws all of them away. Someone who
    turns ETH off for a week should find their notes about it when they turn
    it back on.
    """
    with open(config_path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()

    found = _block_at(lines, symbol)
    if found is None:
        raise ConfigError(f"{symbol} is not in {config_path}")
    at, block_end = found
    value = "true" if enabled else "false"
    indent = " " * (len(lines[at]) - len(lines[at].lstrip()))
    if lines[at].lstrip().startswith("- "):
        indent += "  "

    for index in range(at, block_end):
        if lines[index].strip().startswith("enabled:"):
            lines[index] = f"{indent}enabled: {value}"
            break
    else:
        lines.insert(at + 1, f"{indent}enabled: {value}")

    with open(config_path, "w", encoding="utf-8", newline="") as handle:
        handle.write("\n".join(lines) + "\n")


def _append_market(
    config_path: str,
    symbol: str,
    template: LegConfig,
    *,
    notional: str | None = None,
    cap: str | None = None,
    leverage: float | None = None,
) -> None:
    """Add a market to the file, copying its numbers from an existing one.

    Copied rather than asked for one field at a time: the numbers that matter
    are the size and the caps, and a market added with somebody's best guess
    at seven settings is a market that trades wrong. The copy is a starting
    point the operator can edit, and the screen says which market it came
    from.

    `notional`, `cap` and `leverage` are what `_add_market` worked out for the
    NEW market -- the template's dollar size (or a stand-in when it has none),
    and its leverage clamped to the new market's ceiling. Left as None they
    fall back to the template's own, for callers that have nothing to adjust.
    """
    with open(config_path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()

    listed = next(
        (i for i, line in enumerate(lines) if line.rstrip() == "markets:"), None
    )
    mapped = next((i for i, line in enumerate(lines) if line.rstrip() == "legs:"), None)
    if listed is None and mapped is None:
        raise ConfigError(f"{config_path} has neither a markets: nor a legs: block")

    if notional is None:
        notional = _render_span(template.notional_span, template.notional_usd)
    if cap is None and template.max_order_notional_usd > 0:
        cap = _render_span(template.max_order_span, template.max_order_notional_usd)
    if leverage is None:
        leverage = template.leverage

    fields = [
        f"notional_usd: {notional}",
        f"leverage: {_render_number(leverage)}" if leverage else None,
        f"offset_bps: {_render_span(template.offset_span, template.offset_bps)}",
        f"max_distance_bps: {_render_number(template.max_distance_bps)}",
        f"chase_patience_s: {_render_number(template.chase_patience_s)}",
        f"improve_ticks: {template.improve_ticks}",
        # Omitted rather than written as 0 when there is no dollar cap to copy:
        # absent means "the whole leg", which is what a coin cap copied into a
        # different coin could not honestly mean anyway.
        f"max_order_notional_usd: {cap}" if cap else None,
        "enabled: true",
    ]
    fields = [field for field in fields if field]

    if listed is not None:
        at = _end_of_block(lines, listed)
        block = [f"  - symbol: {symbol}"] + [f"    {field}" for field in fields]
    else:
        at = _end_of_block(lines, mapped)
        # A name of its own, because the mapping spelling needs one and the
        # two it ships with are named after accounts that no longer pick
        # anything. The whole symbol, not the coin: `sol` was the spelling
        # this file used two versions ago, and naming a block that walked
        # straight into the check that refuses it.
        name = symbol.lower().replace("-", "_")
        # The symbol is a field here rather than part of the header: the
        # mapping spelling names a block and puts the market inside it.
        block = [f"  {name}:"] + [
            f"    {field}" for field in [f"symbol: {symbol}", *fields]
        ]

    lines[at:at] = [f"  # added from the menu, copied from {template.symbol}", *block]

    with open(config_path, "w", encoding="utf-8", newline="") as handle:
        handle.write("\n".join(lines) + "\n")


def _end_of_block(lines: list[str], header: int) -> int:
    """Where a top-level block ends, skipping the comments that trail it.

    The trailing comments belong to whatever comes NEXT -- they are the
    header of the following section -- so a market inserted after them would
    appear underneath somebody else's heading.
    """
    end = len(lines)
    for index in range(header + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line.startswith((" ", "\t")):
            end = index
            break
    while end > header + 1 and (
        not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")
    ):
        end -= 1
    return end


def _render_span(span, fallback: float) -> str:
    """A size as the file spells it: `50-100` for a range, `100` otherwise."""
    if span is not None and span.low != span.high:
        return f"{_render_number(span.low)}-{_render_number(span.high)}"
    value = span.high if span is not None else fallback
    return _render_number(value)


def _market_line(leg: LegConfig) -> str:
    """One market, as the chooser shows it."""
    if leg.notional_usd > 0 or leg.notional_span is not None:
        size = f"${_render_span(leg.notional_span, leg.notional_usd)}"
    else:
        size = f"{_render_number(leg.size)} {leg.symbol.split('-')[0]}"
    state = "on " if leg.enabled else "off"
    return f"[{state}] {leg.symbol:<10} {size} per cycle"


def _exchange_markets(config: Config) -> dict[str, dict]:
    """Every market the exchange lists, keyed by symbol, from one request.

    One read serves the whole add screen -- the list to choose from, the new
    market's minimum order and its leverage ceiling -- where it used to be
    fetched twice, and the second copy could disagree with the first.
    """
    info = _http(config).get_exchange_info()
    markets = info if isinstance(info, list) else info.get("symbols", [])
    return {
        m["symbol"]: m for m in markets if isinstance(m, dict) and "symbol" in m
    }


# What a market added from the menu starts at when no existing market is sized
# in dollars to copy from. The shipped settings' own figure.
NEW_MARKET_NOTIONAL_USD = 100.0


def _template_market(config: Config) -> LegConfig | None:
    """The market a new one copies its size from: one written in dollars.

    Only a dollar size means the same thing in another market. `size: 0.001`
    is a quantity of BTC; copied into ETH-USD it would be a thousandth of an
    ether. And copying it as `notional_usd` -- which is what happened -- wrote
    `notional_usd: 0`, a market with no size, which the next load refused and
    took the whole bot down with it. Prefers one switched on, since that is the
    one somebody tuned recently.
    """
    in_dollars = [
        leg for leg in config.markets
        if leg.notional_usd > 0 or leg.notional_span is not None
    ]
    enabled = [leg for leg in in_dollars if leg.enabled]
    return (enabled or in_dollars or [None])[0]


def _add_market(config: Config, config_path: str) -> None:
    """Add one of the exchange's markets, copied from a market already set up."""
    have = {leg.symbol for leg in config.markets}
    try:
        listed = _exchange_markets(config)
    except Exception as exc:  # noqa: BLE001 - the menu must survive the exchange
        print(f"\n  could not read the market list: {exc}")
        return
    available = sorted(s for s in listed if s not in have)
    if not available:
        print("\n  every market the exchange lists is already in the file")
        return

    print()
    for index, symbol in enumerate(available, start=1):
        print(f"    {index}. {symbol}")
    answer = _ask("\n  which market? (0 aborts) > ")
    if not answer.isdigit() or not 1 <= int(answer) <= len(available):
        print("  aborted")
        return
    symbol = available[int(answer) - 1]
    spec = listed[symbol]

    def read(key: str) -> float:
        try:
            return float(spec.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    floor = read("minNotional")
    ceiling = read("maxLeverage")

    template = _template_market(config)
    switch_off = False
    if template is None:
        # Every market is sized in coins. The chase settings still copy fine;
        # the size cannot, so it starts at a stand-in and switched off.
        template = config.markets[0]
        start = max(floor, NEW_MARKET_NOTIONAL_USD)
        notional, cap = _render_number(start), None
        switch_off = True
        print(
            f"\n  !! {template.symbol} is sized in coins, which mean something else "
            f"in {symbol}."
        )
        print(f"     It will be added switched off at notional_usd: {notional}.")
        print("     Set the size you want for it, then turn it on here.")
    else:
        notional, cap = None, None
        smallest = (
            template.notional_span.low
            if template.notional_span is not None
            else template.notional_usd
        )
        if floor and smallest and smallest < floor:
            # Said before it is written, not discovered as a rejected order an
            # hour into a run.
            switch_off = True
            print(
                f"\n  !! {symbol} will not accept an order under ${floor:g}, and the "
                f"size copied from {template.symbol} starts at ${smallest:g}."
            )
            print("     It will be added switched off. Raise notional_usd for it,")
            print("     then turn it on here.")

    leverage = template.leverage
    if leverage and ceiling and leverage > ceiling:
        # The run checks this at startup and refuses to start -- over a number
        # the menu itself wrote. Clamped here instead, and said.
        print(
            f"\n  {template.symbol}'s leverage {_render_number(leverage)} is above "
            f"{symbol}'s maximum of {_render_number(ceiling)} -- using "
            f"{_render_number(ceiling)}."
        )
        leverage = ceiling

    _append_market(
        config_path, symbol, template,
        notional=notional, cap=cap, leverage=leverage,
    )
    if switch_off:
        _write_market_enabled(config_path, symbol, False)
    print(f"\n  {symbol} added to {config_path}, copied from {template.symbol}")
    # There is no restart item in the menu; the file is read once, at start.
    print("  It is read when the bot starts: exit (0) and run it again to trade it.")


def _markets_screen(config: Config, config_path: str) -> None:
    """Which markets trade. Every mode trades all of the ones switched on."""
    while True:
        print("\n  markets in the settings file:")
        for index, leg in enumerate(config.markets, start=1):
            print(f"    {index}. {_market_line(leg)}")
        live = [leg for leg in config.markets if leg.enabled]
        print(f"\n  {len(live)} of {len(config.markets)} switched on")
        print("\n  1-9  turn one on or off")
        print("  a    add a market")
        print("  0    back")

        answer = _ask("\n  > ").strip().lower()
        if answer in ("", "0"):
            return
        if answer == "a":
            _add_market(config, config_path)
            continue
        if not answer.isdigit() or not 1 <= int(answer) <= len(config.markets):
            print("  not one of the choices")
            continue

        leg = config.markets[int(answer) - 1]
        if leg.enabled and len(live) == 1:
            print("\n  that is the only market switched on -- there would be")
            print("  nothing left to trade. Turn another on first.")
            continue
        _write_market_enabled(config_path, leg.symbol, not leg.enabled)
        leg.enabled = not leg.enabled
        print(f"  {leg.symbol} is now {'on' if leg.enabled else 'off'}")


MODE_CHOICES = ("multi", "single")

MODE_SUMMARY = {
    "multi": "every master and every sub, in one pool",
    "single": "one master and its own sub-accounts",
}


def _accounts_summary(config: Config) -> str:
    """One line saying which accounts the current mode puts in play."""
    keys = len(config.private_keys)
    if config.mode == "single":
        return f"single -- master {config.single_master} of {keys}"
    return f"multi -- every master ({keys} key(s))"


def _pick_master(config: Config, config_path: str) -> bool:
    """Which master single mode trades. True when one was chosen."""
    try:
        trees = _trees(config)
    except NoAccountTree as exc:
        print(f"\n  {exc}")
        return False

    print("\n  which master?")
    for tree in trees:
        marker = "*" if tree.index == config.single_master else " "
        owned = f"{len(tree.subs)} sub-account(s)" if tree.subs else "no sub-accounts"
        print(f"  {marker} {tree.index}. {short_pubkey(tree.master)}  {owned}")
    answer = _ask("\n  > ")
    chosen = next((t for t in trees if answer == str(t.index)), None)
    if chosen is None:
        print("  aborted")
        return False
    if len(chosen.subs) < 1:
        # A group needs two accounts, and single mode has only this tree to
        # draw them from.
        print(f"\n  {short_pubkey(chosen.master)} owns no sub-accounts, so there")
        print("  is nothing for it to pair with. Create one first.")
        return False

    _write_scalar(config_path, "single_master", str(chosen.index))
    config.single_master = chosen.index
    return True


def _accounts_screen(config: Config, config_path: str) -> None:
    """Which accounts trade together -- one master's, or everyone's.

    The difference is what the exchange can see. A trade between two accounts
    under one master is a self-trade to it, and its own fee documentation
    excludes those from qualifying volume. A trade between accounts under two
    different masters is not a self-trade to anyone, because nothing on the
    exchange links one master to another.
    """
    print(f"\n  now: {_accounts_summary(config)}")
    for index, mode in enumerate(MODE_CHOICES, start=1):
        marker = "*" if mode == config.mode else " "
        print(f"  {marker} {index}. {mode:<7} {MODE_SUMMARY[mode]}")
    print("    0. back")

    answer = _ask("\n  > ")
    if answer in ("", "0"):
        return
    if not answer.isdigit() or not 1 <= int(answer) <= len(MODE_CHOICES):
        print("  not one of the choices")
        return

    chosen = MODE_CHOICES[int(answer) - 1]
    if not config.private_keys:
        print("\n  no keys in private_key.local -- nothing can trade yet")
        return
    if chosen == "single" and not _pick_master(config, config_path):
        return
    if chosen != config.mode:
        _write_mode(config_path, chosen)
        config.mode = chosen
    print(f"  accounts: {_accounts_summary(config)}")


def _markets(config: Config, config_path: str) -> None:
    """Markets and accounts: what is traded, and by whom.

    One screen because they are the two halves of the same question and used
    to be one setting. They were separated when it became clear they had
    never been the same thing: how many markets are open says nothing about
    which accounts are on either side of a trade.
    """
    while True:
        traded = ", ".join(leg.symbol for leg in config.active_legs) or "none"
        print("\n" + _box("MARKETS & ACCOUNTS", [
            f"1. Accounts   [{_accounts_summary(config)}]",
            f"2. Markets    [{traded}]",
            "3. Back",
        ]))
        print(f"  up to {config.max_groups} group(s) at once, "
              f"{config.max_takers} account(s) per hedge")
        choice = _ask("\n  > ")
        if choice == "1":
            _accounts_screen(config, config_path)
        elif choice == "2":
            _markets_screen(config, config_path)
        elif choice in ("3", "0", ""):
            return


def _target_progress(config: Config) -> None:
    """Realised spend and volume, for this run and for the account's lifetime.

    Both, because they answer different questions and showing only the lifetime
    figure is what made a finished target look permanent.
    """
    from .fees import account_fee_tier, burned_usd, fee_state, realised_for_trees
    from .state import StateStore

    trees = _trees(config)
    http = _http(config)
    totals = realised_for_trees(http, [tree.accounts for tree in trees])
    state = StateStore(config.state_file).load()

    print(f"\n  fills {totals.fills}")
    print("\n  ALL TIME")
    print(f"    burned        ${burned_usd(totals.fees_usd):,.4f}")
    print(f"    volume        ${totals.volume_usd:,.2f}")
    print(f"    self-trades   ${totals.self_trade_volume_usd:,.2f}  (between your own accounts)")
    print(f"    qualifying    ${totals.qualifying_volume_usd:,.2f}  (referral window)")
    print(f"    fee tier      ${totals.tier_volume_usd:,.2f}  (docs say self-trades do not count)")

    print("\n  THIS RUN", end="")
    if not state.has_baseline:
        print("\n    not started -- the target is measured from the next start")
    else:
        # Read from the run's start, the way the stop check reads it. The
        # run now records a start time and zero baselines, so subtracting
        # those from a lifetime walk printed the lifetime totals here as
        # "this run".
        run = realised_for_trees(
            http, [tree.accounts for tree in trees],
            since_ms=state.baseline_at * 1000,
        )
        burned = burned_usd(run.fees_usd)
        volume = run.qualifying_volume_usd
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

    # One line per master. The tier is a fact about a master account, so
    # with several keys there is no single "the" tier to print -- and the
    # cheapest of them tells the operator nothing about what the others pay.
    alone = len(trees) == 1
    print()
    quoted = False
    for tree in trees:
        quote = account_fee_tier(config.http_url, tree.master)
        if not quote:
            continue
        quoted = True
        print(
            f"  {tree.title(alone)} tier {quote.tier_index} in the "
            f"{quote.window_days}-day window: "
            f"maker {quote.maker_bps} bps, taker {quote.taker_bps} bps"
        )
    if not quoted:
        schedule = fee_state(config.http_url).get("global")
        if schedule:
            tier = schedule.tier_for(0.0)
            print(
                f"  no tier quote yet; the {schedule.window_days}-day schedule starts at "
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
            "",
            f"4. Markets & Accounts    [{config.mode}]",
            f"     {', '.join(leg.symbol for leg in config.active_legs)}",
            "5. Progress",
            "6. Back",
        ]))
        choice = _ask("\n  > ")
        if choice == "1":
            _edit_target(config, config_path, "cycles", "cycle count")
        elif choice == "2":
            _edit_target(config, config_path, "burn_usd", "burn target (USD of fees)")
        elif choice == "3":
            _edit_target(config, config_path, "volume_usd", "volume target (USD, qualifying)")
        elif choice == "4":
            _markets(config, config_path)
        elif choice == "5":
            _target_progress(config)
        elif choice in ("6", "0"):
            return


LIMIT_CLOSE_TIMEOUT_S = 300.0


def _close_all(config: Config) -> None:
    """Close everything, at market or with resting limit orders.

    Both are reduce-only, so neither can open a position. The difference is what
    they cost and what they promise:

      market  pays the spread and a taker fee on every unit, and is done in
              seconds. Use it when being flat matters more than the price.
      limit   rests at the front of the book and pays neither, but it waits on
              someone else to trade with it, and can run out of time.
    """
    print("\n  Cancels every order and closes all strategy positions.")
    print("  Reduce-only throughout, so it can never open a position.")
    print("\n  1. dry run       -- show what would be sent, submit nothing")
    print("  2. market close  -- REAL FUNDS, immediate, pays the spread")
    print("  3. limit close   -- REAL FUNDS, rests at the best price, no taker")
    print(f"                      fee. Gives up after "
          f"{LIMIT_CLOSE_TIMEOUT_S / 60:g} minutes if it cannot fill.")
    print("  0. back")

    choice = _ask("\n  > ")
    if choice == "1":
        asyncio.run(cmd_flatten(_fresh(config), dry_run=True))
    elif choice == "2":
        if _confirm("Close all positions at market."):
            asyncio.run(cmd_flatten(_fresh(config), dry_run=False))
        else:
            print("  aborted")
    elif choice == "3":
        if _confirm(
            f"Close all positions with resting limit orders, giving up after "
            f"{LIMIT_CLOSE_TIMEOUT_S / 60:g} minutes."
        ):
            print("\n  Resting at the front of the book. This waits on the market,")
            print("  so it can take a while -- Ctrl+C cancels the orders and stops.")
            asyncio.run(
                cmd_flatten(
                    _fresh(config), dry_run=False, limit=True,
                    timeout_s=LIMIT_CLOSE_TIMEOUT_S,
                )
            )
        else:
            print("  aborted")
    _pause()


# -- entry point ------------------------------------------------------------


# The main menu, in order. Numbered from this list rather than by hand, and the
# one place anything else learns an item's number from -- the stop banner a
# live run prints names "Close All Positions" by its number, and that number
# was a literal in cli.py waiting for the day this list changed.
CLOSE_ALL_LABEL = "Close All Positions"
MAIN_ITEMS = (
    "Start",
    "Active Strategy",
    "History",
    "Accounts Management",
    "Configuration",
    CLOSE_ALL_LABEL,
    "Logs",
)


def main_item(label: str) -> str:
    """`label` as the main menu shows it, number included: `6. Close ...`."""
    return f"{MAIN_ITEMS.index(label) + 1}. {label}"


def run_menu(config: Config, config_path: str) -> int:
    items = [main_item(label) for label in MAIN_ITEMS] + ["0. Exit"]
    handlers: dict[str, Callable[[], None]] = {
        "Start": lambda: _start(config),
        "Active Strategy": lambda: _active_strategy(config),
        "History": lambda: _history(config),
        "Accounts Management": lambda: _accounts_menu(config, config_path),
        "Configuration": lambda: _configuration(config, config_path),
        CLOSE_ALL_LABEL: lambda: _close_all(config),
        "Logs": _logs,
    }
    actions = {
        str(number): handlers[label]
        for number, label in enumerate(MAIN_ITEMS, start=1)
    }

    while True:
        print("\n" + _box("DELTA-NEUTRAL BOT", items))
        print(f"  mainnet  {config.http_url}")
        try:
            choice = _ask("\n  > ")
        except Cancelled:
            # Ctrl+C or a closed input at the top level means leave. Looping
            # instead would spin forever on an input that is gone.
            return 0
        if choice == "0":
            return 0
        action = actions.get(choice)
        if action is None:
            print("  no such option")
            continue
        try:
            action()
        except Cancelled:
            # Whatever the prompt was about to write or send, it did not.
            print("  cancelled -- nothing was changed")
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

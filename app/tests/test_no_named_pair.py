"""Nothing that acts on the accounts may address them as `master` and `sub1`.

This is one fault caught eight times. The bot was written for two accounts
named `master` and `sub1`; a pool hands it six under two keys, and every place
that still spelled the accounts as that pair kept working on two of them and
silently ignored the rest. Each one surfaced as a different symptom, hours
apart, in a live run:

  * one order booked on three accounts, and a halt on a hedge ceiling
  * a hedge that never went out, $138 sitting directional for three minutes
  * leverage set on two accounts out of six
  * the cycle sized against the margin of two accounts out of six
  * a socket that was never dialled, found only when a flatten could not
    cancel on it
  * a socket left open after every stop
  * orders left resting on four accounts by the panic button
  * a hedge split three ways that retired one reservation and thrashed

The tests in the other files pin each behaviour. This one pins the SHAPE, so
the next place that reaches for the pair is caught here rather than in a log
at midnight.

It is deliberately a source check. The alternative is a fixture with six
accounts exercising every path, which is worth having and is not what stops a
new call site being written -- nothing makes a fresh `self.sub1` fail a test
that does not know the code exists yet.
"""

import pathlib
import re

import pytest

BULKDN = pathlib.Path(__file__).resolve().parent.parent / "bulkdn"

# Where naming the pair is the point rather than a mistake.
#
# `master` and `sub1` still exist: they are the first two accounts of the
# pool, and the commands that act on ONE account -- the market data socket,
# the referral gate, the run's identity line -- need one and do not care
# which. What they may not do is stand in for "the accounts".
ALLOWED = {
    # The pool's first two, which is where the names come from.
    ("cli.py", "self.master, self.sub1 = self.pool[0], self.pool[1]"),
    # One socket carries the market feed; a second would be a second
    # subscription to the same public data.
    ("cli.py", "self.feed = MarketFeed(self.master, self.symbols)"),
    # The gate is about who owns the build, not about who trades.
    ("cli.py", "decision = check_referral_access(self.master.pubkey, self.config.access)"),
    # Parent-child is a claim about two specific accounts, and it is guarded
    # by `_same_tree` so it is only made inside one tree.
    ("cli.py", "if verify and self._same_tree(self.master, self.sub1):"),
    ("cli.py", "verify_sub_account(self.master, self.sub1)"),
    ("cli.py", "if not Runtime._same_tree(runtime.master, runtime.sub1):"),
    ("cli.py", "verify_sub_account(runtime.master, runtime.sub1)"),
    # Handed to the strategy, which keeps them for the same reasons.
    ("cli.py", "master=self.master,"),
    ("cli.py", "sub1=self.sub1,"),
    ("strategy.py", "self.master = master"),
    ("strategy.py", "self.sub1 = sub1"),
    # Any account's HTTP client will do; this one is to hand.
    ("strategy.py", "realised_for_trees, self.master.http, self._trees(),"),
    # The configured pair's own roles, which is what a leg with no group
    # falls back to and the only place the pair is the answer.
    ("strategy.py", "master, sub1 = self.master.pubkey, self.sub1.pubkey"),
}

# Lines that merely name the run for a human. They are checked separately,
# because a log line naming two of six accounts is misleading rather than
# wrong, and fixing it does not change what the bot does.
LOG_ONLY = {
    ("cli.py", "self.master.pubkey,"),
    ("cli.py", "self.sub1.pubkey,"),
    ("cli.py", "master=runtime.master.pubkey,"),
    ("cli.py", "sub1=runtime.sub1.pubkey,"),
}

PAIR_NAMES = ("master", "sub1")


def source_lines():
    """Every line of the package, with its file and number."""
    for path in sorted(BULKDN.glob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            yield path.name, number, line.strip()


def names_the_pair(line: str) -> bool:
    """Whether a line reaches for `.master` or `.sub1` as an account."""
    if line.startswith(("#", '"')):
        return False
    return any(
        f".{name}" in line and f".{name}_" not in line for name in PAIR_NAMES
    )


def test_no_new_place_addresses_the_pool_as_a_pair():
    offenders = [
        (name, number, line)
        for name, number, line in source_lines()
        # menu.py has its own `Tree.master`, which is a master account and
        # nothing to do with the session named `master`.
        if name not in ("menu.py", "accounts.py", "subaccounts.py")
        and names_the_pair(line)
        and (name, line) not in ALLOWED
        and (name, line) not in LOG_ONLY
    ]

    assert not offenders, (
        "these reach for master/sub1 where the pool is meant:\n"
        + "\n".join(f"  {n}:{ln}  {text}" for n, ln, text in offenders)
        + "\n\nIf the pair really is the answer there, add the line to ALLOWED "
        "with a reason."
    )


def test_the_allowlist_has_not_gone_stale():
    """An entry that no longer matches any line is an entry nobody re-read."""
    present = {(name, line) for name, _number, line in source_lines()}
    missing = sorted((ALLOWED | LOG_ONLY) - present)

    assert not missing, f"allowlisted lines that no longer exist: {missing}"


# -- and the other half of the same fault: assuming one hedger --------------


def test_nothing_reads_the_first_taker_as_the_only_one():
    """`roles.taker` is the FIRST hedger. A split hedge has several, and
    reading only the first is what left two reservations standing and set a
    leg thrashing at a dollar a trade."""
    allowed = {
        # The compatibility field itself, and what builds it.
        ("hedger.py", "taker: str"),
        ("strategy.py", "maker, taker = group.maker, group.takers[0]"),
        ("strategy.py", "taker=group.takers[0],"),
        ("strategy.py", "maker, taker = taker, maker"),
        ("hedger.py", "return self.takers or (self.taker,)"),
    }
    offenders = [
        (name, number, line)
        for name, number, line in source_lines()
        # A word boundary keeps `.takers` and `.taker_bps` out: both
        # continue the word, so neither ends one.
        if re.search(r"\.taker\b", line)
        and not line.startswith("#")
        and (name, line) not in allowed
    ]

    assert not offenders, (
        "these read one hedger where a leg may have several:\n"
        + "\n".join(f"  {n}:{ln}  {text}" for n, ln, text in offenders)
    )


@pytest.mark.parametrize("name", ["hedgers", "accounts", "weights"])
def test_the_roles_expose_the_whole_leg(name):
    """What the call sites above are supposed to use instead."""
    from bulkdn.hedger import LegRoles

    assert isinstance(getattr(LegRoles, name), property)


# -- and the guard itself has to catch something ----------------------------
#
# An earlier version of this file passed because its pattern was broken: the
# regex held a stray control character and matched nothing at all. A check
# that cannot fail is worse than no check, because it reads as a check.


@pytest.mark.parametrize("line", [
    "await self.sub1.connect()",
    "for session in (self.master, self.sub1):",
    "sync_positions_http([runtime.master, runtime.sub1], runtime.book)",
    "size = self.book.authoritative(self.master.pubkey, symbol)",
])
def test_the_pair_detector_catches_the_shapes_that_broke(line):
    assert names_the_pair(line)


@pytest.mark.parametrize("line", [
    "config.master_account.symbol",
    "leg = config.sub_account",
    "print(tree.master)",
])
def test_it_does_not_catch_unrelated_names(line):
    assert not names_the_pair(line) or ".master_" in line or "tree." in line


@pytest.mark.parametrize("line", [
    "if session.pubkey == roles.taker:",
    "book.effective(roles.taker, symbol)",
    "self.sessions[roles.taker].name,",
])
def test_the_single_taker_detector_catches_them(line):
    assert re.search(r"\.taker\b", line), line


@pytest.mark.parametrize("line", [
    "for pubkey in roles.takers:",
    "f\"taker {quote.taker_bps} bps\"",
    "takers=group.takers,",
])
def test_it_leaves_the_plural_and_the_fee_alone(line):
    assert not re.search(r"\.taker\b", line), line

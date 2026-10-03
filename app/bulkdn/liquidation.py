"""Detecting a position closed by someone other than this bot.

A liquidation breaks the strategy's central assumption. The pair is only
market-neutral because two opposite positions exist; when the exchange force-
closes one of them, the survivor becomes outright directional exposure of the
full leg size, and it stays that way until something notices.

**The normal hedge rule makes this worse, not better.** It computes
`net = maker + taker` and corrects by trading on the *taker* account. If the
taker was the one liquidated, the correction is to open a fresh position on an
account that just ran out of margin: at best rejected, at worst re-entering the
position that was liquidated moments ago. The right response to a liquidation
is the opposite one -- close the survivor.

**Detection.** The bot knows what it does. During OPEN and HOLD it only ever
adds to a position; nothing in those phases reduces one. So a position that
shrinks during them was reduced by someone else, and that is the signal. This
catches liquidation, ADL and a manual close from the web UI identically, which
is correct: all three mean the pair is broken and re-hedging is wrong.

EXIT is excluded because reducing positions is exactly what it does.

**Confirmation.** The local signal is a good trigger and a poor diagnosis. It
fires on anything the bot cannot account for, and during an exchange outage
that includes the bot's own orders: a hedge whose response timed out still
executed, and the position it moved looks exactly like one someone else closed.
A live run ended that way -- three duplicate hedges landed unacknowledged, the
position flipped, and the bot reported a liquidation the exchange had no record
of.

So the trigger stays local and immediate, and `recent_liquidations` is asked
afterwards what really happened. It is far too slow to detect with, but it is
authoritative about what it reports, and the difference decides whether the run
is over or merely out of sync.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import requests

from .marketdata import MarketSpec, epoch_seconds
from .positions import PositionBook
from .state import Phase

log = logging.getLogger(__name__)

# Phases in which the bot only reduces a position with a hedge it has told the
# guard about (`note_own_order`), so any other reduction is external. EXIT is
# deliberately absent.
_ACCUMULATING = (Phase.OPEN, Phase.HOLD)

# How long a hedge of ours explains a shrink on its account. Its position
# update normally lands within a second; a socket running behind has delivered
# them half a minute late. Bounded rather than open-ended because an order that
# stays on the books as an explanation also hides up to its size of a real
# liquidation on that account -- and a refused one is withdrawn at once anyway.
OWN_ORDER_HOLD_S = 60.0


@dataclass(frozen=True)
class Liquidation:
    """A position that shrank without this bot closing it."""

    account: str
    account_name: str
    symbol: str
    previous: float
    current: float

    @property
    def fully_closed(self) -> bool:
        # Not `== 0.0`: a size that came through float arithmetic is rarely
        # exactly zero when it means zero.
        return abs(self.current) < 1e-12

    def describe(self) -> str:
        what = "closed" if self.fully_closed else "reduced"
        return (
            f"{self.account_name} {self.symbol} position {what} externally: "
            f"{self.previous:+.8f} -> {self.current:+.8f}"
        )


@dataclass
class LiquidationGuard:
    """Watches for positions reduced by anything other than this bot.

    Tracks the largest absolute position seen for each account and symbol.
    A drop below that peak, by more than one lot, during an accumulating phase
    is the signal. One lot of slack keeps dust and rounding from tripping it.
    """

    specs: dict[str, MarketSpec]
    names: dict[str, str] = field(default_factory=dict)
    # Signed, so a wiped short is reported as the short it was. Storing only
    # the magnitude means reading the direction off the current position, and
    # a fully closed one is zero -- which renders every liquidated short as a
    # long.
    _peak: dict[tuple[str, str], float] = field(default_factory=dict)
    # Hedges of ours sent on each account, as [unexplained size, sign, expiry].
    # See `note_own_order`.
    _own: dict[tuple[str, str], list[list[float]]] = field(default_factory=dict)

    def note_own_order(self, account: str, symbol: str, signed_size: float) -> None:
        """A hedge of ours is going out on this account.

        "The bot never reduces a position while opening" was the premise of
        this guard, and it is false for hedgers: when the book is briefly
        wrong -- an update landing between the halves of a fill that crosses
        zero is enough -- the hedge rule over-hedges and then sells the excess
        back, which shrinks the hedgers it bought on. That shrink was read as
        a position closed from outside, and a live run halted and closed every
        account at market over its own correction, with the exchange reporting
        no liquidation at all. Before the guard's own fix such shrinks seen
        over the socket were silently dropped, which is the only reason this
        had not happened earlier.

        Every slice is noted, whichever way it goes. Only one pointing against
        the account's position can explain a shrink, and `check` decides that
        against the peak at the time.
        """
        if signed_size == 0:
            return
        self._own.setdefault((account, symbol), []).append(
            [abs(signed_size), 1.0 if signed_size > 0 else -1.0,
             time.monotonic() + OWN_ORDER_HOLD_S]
        )

    def withdraw_own_order(self, account: str, symbol: str, signed_size: float) -> None:
        """The exchange refused this hedge, so it explains nothing."""
        sign = 1.0 if signed_size > 0 else -1.0
        entries = self._own.get((account, symbol), [])
        for entry in entries:
            if entry[1] == sign and abs(entry[0] - abs(signed_size)) < 1e-12:
                entries.remove(entry)
                return

    def _explained(self, key: tuple[str, str], peak: float, shrink: float, slack: float) -> bool:
        """Whether hedges of ours account for `shrink` off `peak`, using them up if so.

        Only orders pointing against the position count -- a sell shrinks a
        long, a buy a short -- and only when they cover all of it, within the
        guard's usual slack. A liquidation larger than our own hedges is
        still reported whole.
        """
        now = time.monotonic()
        entries = [e for e in self._own.get(key, ()) if e[2] > now]
        against = [e for e in entries if (e[1] > 0) != (peak > 0)]
        if shrink > sum(e[0] for e in against) + slack:
            self._own[key] = entries
            return False
        left = shrink
        for entry in against:
            taken = min(entry[0], left)
            entry[0] -= taken
            left -= taken
        self._own[key] = [e for e in entries if e[0] > 1e-12]
        return True

    def reset_symbol(self, symbol: str, accounts=None) -> None:
        """Forget peaks in one symbol, for one leg's accounts.

        Legs finish their cycles independently, so clearing more than the
        finishing leg's own would erase the high-water mark of a leg still
        holding a position -- and its next real shrink would then go unnoticed,
        which is the one thing this whole guard exists to see.

        That was true of the other leg when legs meant markets. With an account
        pool two legs can share a market, so the accounts have to be named:
        `accounts=None` still clears the whole symbol, which is what the halt
        path and a single configured pair both mean by it.
        """
        for held in (self._peak, self._own):
            for key in [
                k for k in held
                if k[1] == symbol and (accounts is None or k[0] in accounts)
            ]:
                del held[key]

    def reset(self) -> None:
        """Forget peaks. Called when a cycle ends, since EXIT legitimately
        takes every position back to zero."""
        self._peak.clear()
        self._own.clear()

    def observe(self, account: str, symbol: str, size: float) -> None:
        """Record a position without judging it. Used during EXIT."""
        key = (account, symbol)
        if abs(size) >= abs(self._peak.get(key, 0.0)):
            self._peak[key] = size

    def check(
        self,
        book: PositionBook,
        phase: Phase | dict[str, Phase] | dict[tuple[str, str], Phase],
        accounts: list[str],
    ) -> list[Liquidation]:
        """Return every position that has shrunk externally and not yet been
        acknowledged.

        Asking does not use the answer up. A shrink stays reported until
        `acknowledge` says it was dealt with, because more than one caller
        looks: the position handler checks on every update to notice it at
        once, and the worker that responds checks again to find out what it is
        responding to. This used to lower the peak as it reported, so the
        handler's look consumed the event, the worker's found nothing, and a
        liquidation seen over the socket was flagged and then dropped.

        Uses confirmed positions only. An optimistic overlay exists to make
        hedging fast and can briefly show a fill the exchange has not applied;
        deciding a liquidation happened on that basis would be a false alarm
        with an expensive response.

        `phase` may be given three ways, and the third is the one that is
        right once accounts are drawn from a pool:

        * one phase for everything, which is what a single pair had;
        * by symbol, from when two configured legs ran on their own clocks;
        * by (account, symbol), which is the only spelling that survives
          several groups trading ONE market at once.

        Why the middle one stopped working: a group's phase belongs to its
        accounts, not to the market. Two groups on BTC-USD can have one
        opening while the other unwinds, and asking "what phase is BTC-USD
        in?" has no answer -- take the opener's and the unwinder's deliberate
        shrink reads as a liquidation; take the unwinder's and a real one goes
        unseen. Asking per account has an answer, because an account is in at
        most one group.
        """
        def accumulating(account: str, symbol: str) -> bool:
            if isinstance(phase, dict):
                per_leg = phase.get((account, symbol), phase.get(symbol, Phase.IDLE))
            else:
                per_leg = phase
            return per_leg in _ACCUMULATING

        if not any(
            accumulating(account, symbol)
            for account in accounts
            for symbol in self.specs
        ):
            # Still track peaks, so a later phase starts from the real high.
            for account in accounts:
                for symbol in self.specs:
                    self.observe(account, symbol, book.authoritative(account, symbol))
            return []

        found: list[Liquidation] = []
        for account in accounts:
            for symbol, spec in self.specs.items():
                current = book.authoritative(account, symbol)
                if not accumulating(account, symbol):
                    # This leg is unwinding, so shrinking is the plan. The peak
                    # is only raised here, never lowered; the next entry starts
                    # from a clean peak because a finished leg's accounts are
                    # reset by `reset_symbol`.
                    self.observe(account, symbol, current)
                    continue
                key = (account, symbol)
                peak = self._peak.get(key, 0.0)

                if abs(current) > abs(peak):
                    self._peak[key] = current
                    continue

                shrink = abs(peak) - abs(current)
                if shrink > spec.lot_size and self._explained(
                    key, peak, shrink, spec.lot_size
                ):
                    # Our own hedge took it down. Re-baselined here rather
                    # than reported, so the next real shrink is measured from
                    # what the account holds now.
                    log.info(
                        "%s %s shrank %+.8f -> %+.8f by a hedge of ours",
                        self.names.get(account, account[:8]), symbol, peak, current,
                    )
                    self._peak[key] = current
                    continue

                if shrink > spec.lot_size:
                    found.append(
                        Liquidation(
                            account=account,
                            account_name=self.names.get(account, account[:8]),
                            symbol=symbol,
                            previous=peak,
                            current=current,
                        )
                    )

        return found

    def acknowledge(self, events: list[Liquidation]) -> None:
        """Take these shrinks as dealt with, so each is reported once.

        The peak comes down to the size the event reported, and only if it
        still stands where the event found it: a position that has grown again
        since has set a new high of its own, and lowering it would hide the
        next real shrink. One that has shrunk further is reported again, from
        here, as the new event it is.
        """
        for event in events:
            key = (event.account, event.symbol)
            peak = self._peak.get(key)
            if peak is not None and abs(peak) <= abs(event.previous):
                self._peak[key] = event.current


def recent_liquidations(
    http_url: str,
    user: str,
    *,
    within_s: float = 300.0,
    timeout: int = 10,
) -> list[dict]:
    """Liquidations and ADLs the exchange recorded for `user`, recently.

    Raises on any failure rather than returning an empty list. "Nothing was
    liquidated" and "I could not ask" must not look the same to the caller:
    one of them means the run can continue, and guessing it during an outage --
    which is exactly when this is asked -- would be guessing in the unsafe
    direction.
    """
    response = requests.post(
        f"{http_url}/account",
        json={"type": "riskEvents", "user": user},
        timeout=timeout,
    )
    response.raise_for_status()
    body = response.json()
    events = body.get("data") if isinstance(body, dict) else body
    if not isinstance(events, list):
        raise ValueError(f"riskEvents returned {type(body).__name__}, not a list")

    # Judged in seconds, whatever unit the event carries. The cutoff was in
    # nanoseconds while the rest of the feed stamps in milliseconds, and a
    # millisecond stamp read as nanoseconds is always "long ago".
    cutoff = time.time() - within_s
    return [
        event
        for event in events
        if isinstance(event, dict)
        and epoch_seconds(float(event.get("timestamp") or 0)) >= cutoff
    ]

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

from .marketdata import MarketSpec
from .positions import PositionBook
from .state import Phase

log = logging.getLogger(__name__)

# Phases in which the bot never reduces a position, so any reduction is
# external. EXIT is deliberately absent.
_ACCUMULATING = (Phase.OPEN, Phase.HOLD)


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
        return abs(self.current) == 0.0

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

    def reset_symbol(self, symbol: str) -> None:
        """Forget one symbol's peaks, leaving the other leg's intact.

        Legs finish their cycles independently, so clearing both would erase the
        high-water mark of a leg that is still holding a position -- and its
        next real shrink would then go unnoticed.
        """
        for key in [k for k in self._peak if k[1] == symbol]:
            del self._peak[key]

    def reset(self) -> None:
        """Forget peaks. Called when a cycle ends, since EXIT legitimately
        takes every position back to zero."""
        self._peak.clear()

    def observe(self, account: str, symbol: str, size: float) -> None:
        """Record a position without judging it. Used during EXIT."""
        key = (account, symbol)
        if abs(size) >= abs(self._peak.get(key, 0.0)):
            self._peak[key] = size

    def check(
        self,
        book: PositionBook,
        phase: Phase | dict[str, Phase],
        accounts: list[str],
    ) -> list[Liquidation]:
        """Return any position that shrank externally since the last check.

        Uses confirmed positions only. An optimistic overlay exists to make
        hedging fast and can briefly show a fill the exchange has not applied;
        deciding a liquidation happened on that basis would be a false alarm
        with an expensive response.
        """
        # Legs run their own phases, so `phase` may be a mapping of symbol to
        # phase. One leg exiting must not stop the other being watched.
        def accumulating(symbol: str) -> bool:
            per_leg = phase[symbol] if isinstance(phase, dict) else phase
            return per_leg in _ACCUMULATING

        if not any(accumulating(symbol) for symbol in self.specs):
            # Still track peaks, so a later phase starts from the real high.
            for account in accounts:
                for symbol in self.specs:
                    self.observe(account, symbol, book.authoritative(account, symbol))
            return []

        found: list[Liquidation] = []
        for account in accounts:
            for symbol, spec in self.specs.items():
                current = book.authoritative(account, symbol)
                if not accumulating(symbol):
                    # This leg is unwinding, so shrinking is the plan. Keep the
                    # peak current or its next entry would start from a high
                    # that belongs to the position it just closed.
                    self.observe(account, symbol, current)
                    continue
                key = (account, symbol)
                peak = self._peak.get(key, 0.0)

                if abs(current) > abs(peak):
                    self._peak[key] = current
                    continue

                if abs(peak) - abs(current) > spec.lot_size:
                    found.append(
                        Liquidation(
                            account=account,
                            account_name=self.names.get(account, account[:8]),
                            symbol=symbol,
                            previous=peak,
                            current=current,
                        )
                    )
                    # Re-baseline, so one event is reported once rather than on
                    # every tick until the phase ends.
                    self._peak[key] = current

        return found


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

    cutoff_ns = (time.time() - within_s) * 1e9
    return [
        event
        for event in events
        if isinstance(event, dict) and float(event.get("timestamp") or 0) >= cutoff_ns
    ]

"""Risk limits and the kill switch.

The original strategy description has no halt condition. It needs one: the bot
fires market orders unattended in response to fills, and if a hedge leg starts
failing -- insufficient margin on a hedging account, a risk-limit rejection, a dead socket --
the position stops being delta-neutral and nothing in the happy path notices.

Every check here is a reason to stop trading, cancel everything, and flatten.
None of them are advisory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from collections.abc import Callable, Sequence

from .accounts import AccountSession
from .config import RiskConfig
from .feed import MarketFeed
from .marketdata import round_notional
from .positions import PositionBook

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Violation:
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


class RiskMonitor:
    """Evaluates hard limits against current state."""

    def __init__(
        self,
        *,
        config: RiskConfig,
        book: PositionBook,
        feed: MarketFeed,
        sessions: dict[str, AccountSession],
        symbols: Sequence[str],
        watch: Sequence[AccountSession] = (),
    ):
        self.config = config
        self.book = book
        self.feed = feed
        self.sessions = sessions
        self.symbols = list(symbols)
        # Sockets that carry no account but must stay up -- the market data
        # one. Checked for silence and drops, and nothing else.
        self.watch = list(watch)
        # The live groups, as (label, symbol, accounts), when the run has any.
        # Set by the strategy. See `check`.
        self.groups: Callable[[], list[tuple[str, str, tuple[str, ...]]]] | None = None
        # The last price each market had. See `_price`.
        self._last_price: dict[str, float] = {}
        self._warned_no_price: set[str] = set()

    def _price(self, symbol: str) -> float:
        """The market's price, or the last one it had.

        A missing ticker used to read as $0, which made every exposure and
        every position $0 as well: the limits passed silently for exactly as
        long as the price feed was down. The last known price is a far better
        estimate of what a position is worth than nothing.
        """
        price = self.feed.reference_price(symbol) or 0.0
        if price > 0:
            self._last_price[symbol] = price
            self._warned_no_price.discard(symbol)
            return price
        last = self._last_price.get(symbol, 0.0)
        if symbol not in self._warned_no_price:
            self._warned_no_price.add(symbol)
            log.warning(
                "%s: no price -- risk limits use %s",
                symbol, f"the last one (${last:,.2f})" if last else "nothing yet",
            )
        return last

    def net_exposure_usd(self, symbol: str) -> float:
        """Directional exposure the pair is carrying, in dollars.

        Uses confirmed positions only. Optimistic overlays exist to make hedging
        fast, but a risk limit should not be satisfied by a fill the exchange
        has not acknowledged.
        """
        accounts = list(self.sessions)
        net = sum(self.book.authoritative(acct, symbol) for acct in accounts)
        return abs(net) * self._price(symbol)

    def group_exposure_usd(self, symbol: str, accounts: Sequence[str]) -> float:
        """The same, over one group's accounts only."""
        net = sum(self.book.authoritative(acct, symbol) for acct in accounts)
        return abs(net) * self._price(symbol)

    def check(self) -> list[Violation]:
        """Return every breached limit. An empty list means it is safe to trade."""
        violations: list[Violation] = []

        # Per group as well as in total. Summed over every account, two groups
        # on one market cancel out: +$300 and -$300 reported $0 while each
        # group sat directional on its own accounts.
        for label, symbol, accounts in (self.groups() if self.groups else []):
            exposure = self.group_exposure_usd(symbol, accounts)
            if exposure > self.config.max_net_exposure_usd:
                violations.append(
                    Violation(
                        "net_exposure",
                        f"{label} {symbol} net exposure ${round_notional(exposure)} "
                        f"exceeds ${round_notional(self.config.max_net_exposure_usd)}",
                    )
                )

        for symbol in self.symbols:
            price = self._price(symbol)

            exposure = self.net_exposure_usd(symbol)
            if exposure > self.config.max_net_exposure_usd:
                violations.append(
                    Violation(
                        "net_exposure",
                        f"{symbol} net exposure ${round_notional(exposure)} exceeds "
                        f"${round_notional(self.config.max_net_exposure_usd)}",
                    )
                )

            for session in self.sessions.values():
                size = abs(self.book.authoritative(session.pubkey, symbol))
                notional = size * price
                if notional > self.config.max_position_usd:
                    violations.append(
                        Violation(
                            "position_size",
                            f"{session.name} {symbol} position ${round_notional(notional)} "
                            f"exceeds ${round_notional(self.config.max_position_usd)}",
                        )
                    )

        for session in self.sessions.values():
            if session.reject_streak >= self.config.max_reject_streak:
                # The reason travels with the violation because this is what
                # gets written into the state file, and that file is read long
                # after the console it was logged to has closed.
                cause = (
                    f" -- last was {session.last_reject}"
                    if session.last_reject
                    else ""
                )
                violations.append(
                    Violation(
                        "reject_streak",
                        f"{session.name} has {session.reject_streak} consecutive "
                        f"rejected transactions{cause}",
                    )
                )

        for session in [*self.sessions.values(), *self.watch]:
            if session.dry_run:
                continue

            if not session.is_connected:
                violations.append(
                    Violation("disconnected", f"{session.name} WebSocket is not connected")
                )
            elif session.last_message_age_s > self.config.ws_stale_timeout_s:
                violations.append(
                    Violation(
                        "stale_stream",
                        f"{session.name} has received nothing for "
                        f"{session.last_message_age_s:.0f}s",
                    )
                )

        return violations

    def log_exposure(self) -> None:
        parts = []
        for symbol in self.symbols:
            accounts = list(self.sessions.values())
            sizes = " ".join(
                f"{s.name}={self.book.effective(s.pubkey, symbol):+.8f}" for s in accounts
            )
            net = sum(self.book.effective(s.pubkey, symbol) for s in accounts)
            # The dollar figure from the same net it is printed beside. It came
            # from confirmed positions while the net came from the optimistic
            # ones, and the log showed `net=-0.00000000 $145.24` 175 times.
            parts.append(
                f"{symbol}[{sizes} net={net:+.8f} "
                f"${round_notional(abs(net) * self._price(symbol))}]"
            )
        log.info("exposure: %s", "  ".join(parts))

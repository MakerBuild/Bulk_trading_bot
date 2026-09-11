"""Risk limits and the kill switch.

The original strategy description has no halt condition. It needs one: the bot
fires market orders unattended in response to fills, and if a hedge leg starts
failing -- insufficient margin on Sub1, a risk-limit rejection, a dead socket --
the position stops being delta-neutral and nothing in the happy path notices.

Every check here is a reason to stop trading, cancel everything, and flatten.
None of them are advisory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from collections.abc import Sequence

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
    ):
        self.config = config
        self.book = book
        self.feed = feed
        self.sessions = sessions
        self.symbols = list(symbols)

    def net_exposure_usd(self, symbol: str) -> float:
        """Directional exposure the pair is carrying, in dollars.

        Uses confirmed positions only. Optimistic overlays exist to make hedging
        fast, but a risk limit should not be satisfied by a fill the exchange
        has not acknowledged.
        """
        accounts = list(self.sessions)
        net = sum(self.book.authoritative(acct, symbol) for acct in accounts)
        price = self.feed.reference_price(symbol) or 0.0
        return abs(net) * price

    def check(self) -> list[Violation]:
        """Return every breached limit. An empty list means it is safe to trade."""
        violations: list[Violation] = []

        for symbol in self.symbols:
            price = self.feed.reference_price(symbol) or 0.0

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
            parts.append(
                f"{symbol}[{sizes} net={net:+.8f} "
                f"${round_notional(self.net_exposure_usd(symbol))}]"
            )
        log.info("exposure: %s", "  ".join(parts))

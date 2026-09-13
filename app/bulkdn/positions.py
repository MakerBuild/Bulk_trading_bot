"""Signed position tracking across both accounts.

Positions are signed: positive is long, negative is short. This is the same
convention the exchange uses in `PositionUpdate.size`, so no translation is
needed.

Two sources feed this book:

* **Authoritative** -- `accountSnapshot` and `positionUpdate` from the account
  stream. This is truth, but it lags a fill by a round-trip.
* **Optimistic overlay** -- deltas applied the moment a fill arrives, so the
  hedger can react without waiting for the position update.

Overlay entries expire after a TTL. That bounds the damage from a delta that
never gets confirmed (a dropped frame, a reconnect) -- the book converges back
onto exchange truth instead of drifting forever on a bad guess.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from collections.abc import Iterable

Key = tuple[str, str]  # (account pubkey, symbol)


class SeenTrades:
    """Remembers which fills have already been applied to the book.

    API v1.0.17 gave account fill updates a lossless `tradeId`, which makes it
    possible to recognise a fill that arrives twice -- the realistic case being
    a replay after a WebSocket reconnect. Without this, a replayed fill would be
    applied to the optimistic overlay a second time and briefly misstate the
    position.

    Trade ids are scoped by account on purpose: the changelog states that maker,
    taker, and isolated-account views of the same execution share one id, so a
    global set would discard the second account's legitimate view of a trade
    that crossed between the master and the sub-account.

    Bounded, because a long-running bot would otherwise grow this without limit.
    """

    def __init__(self, capacity: int = 20_000):
        self.capacity = capacity
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()

    def add_if_new(self, account: str, trade_id: str | None) -> bool:
        """Record a trade id. Returns False if it had already been seen.

        A missing trade id is always treated as new -- an exchange that has not
        yet been upgraded must not have all of its fills silently dropped.
        """
        if not trade_id:
            return True
        key = (account, trade_id)
        if key in self._seen:
            self._seen.move_to_end(key)
            return False
        self._seen[key] = None
        while len(self._seen) > self.capacity:
            self._seen.popitem(last=False)
        return True

    def __len__(self) -> int:
        return len(self._seen)


@dataclass
class _Overlay:
    delta: float
    expires_at: float
    label: str


@dataclass
class PositionBook:
    """Per-(account, symbol) signed sizes with a short-lived optimistic layer."""

    overlay_ttl_ms: int = 2000
    _authoritative: dict[Key, float] = field(default_factory=dict)
    _overlays: dict[Key, list[_Overlay]] = field(default_factory=dict)

    # -- writes ------------------------------------------------------------

    def set_authoritative(self, account: str, symbol: str, size: float) -> None:
        """Record an exchange-reported position.

        Any overlay for this key is dropped: the exchange has now spoken, and
        keeping a guess on top of truth is how double-counting starts. A fill
        that arrives after this update creates a fresh overlay.
        """
        key = (account, symbol)
        self._authoritative[key] = float(size)
        self._overlays.pop(key, None)

    def apply_snapshot(self, account: str, positions: Iterable) -> None:
        """Replace all of an account's positions from a full snapshot.

        Symbols absent from the snapshot are zeroed -- the exchange omits
        positions it considers closed, and a stale non-zero entry here would
        make the hedger chase a position that no longer exists.
        """
        stale = [key for key in self._authoritative if key[0] == account]
        for key in stale:
            self._authoritative[key] = 0.0
            self._overlays.pop(key, None)
        for position in positions:
            self.set_authoritative(account, position.symbol, position.size)

    def add_overlay(self, account: str, symbol: str, delta: float, label: str = "") -> None:
        """Optimistically shift a position before the exchange confirms it."""
        if delta == 0:
            return
        key = (account, symbol)
        expires_at = time.monotonic() + (self.overlay_ttl_ms / 1000.0)
        self._overlays.setdefault(key, []).append(
            _Overlay(delta=float(delta), expires_at=expires_at, label=label)
        )

    def apply_fill(self, account: str, symbol: str, is_buy: bool, size: float, label: str = "fill") -> None:
        """Overlay a fill. A buy moves the position up, a sell moves it down."""
        self.add_overlay(account, symbol, size if is_buy else -size, label)

    # -- reads -------------------------------------------------------------

    def authoritative(self, account: str, symbol: str) -> float:
        """Exchange-reported position, ignoring anything unconfirmed."""
        return self._authoritative.get((account, symbol), 0.0)

    def effective(self, account: str, symbol: str) -> float:
        """Position including unexpired optimistic deltas.

        This is what the hedger acts on -- it is the best available estimate of
        where the position actually is right now.
        """
        key = (account, symbol)
        base = self._authoritative.get(key, 0.0)
        overlays = self._prune(key)
        return base + sum(entry.delta for entry in overlays)

    def net(self, account_a: str, account_b: str, symbol: str) -> float:
        """Combined signed exposure across both accounts. Zero means neutral."""
        return self.effective(account_a, symbol) + self.effective(account_b, symbol)

    def snapshot(self) -> dict[str, dict[str, float]]:
        """Nested {account: {symbol: effective_size}}, for logs and status output."""
        out: dict[str, dict[str, float]] = {}
        for account, symbol in list(self._authoritative):
            out.setdefault(account, {})[symbol] = self.effective(account, symbol)
        return out

    # -- internals ---------------------------------------------------------

    def _prune(self, key: Key) -> list[_Overlay]:
        overlays = self._overlays.get(key)
        if not overlays:
            return []
        now = time.monotonic()
        live = [entry for entry in overlays if entry.expires_at > now]
        if live:
            self._overlays[key] = live
        else:
            self._overlays.pop(key, None)
        return live

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


# How long a fill left unconfirmed by a read is held while a confirming read
# is fetched. Bounded, so a read that never succeeds does not pin a fill in
# the book for good.
CONFIRM_HOLD_S = 30.0

# How far the exchange's HTTP answer may trail its own stream. A read sent
# shortly AFTER a fill or position update reached us can still be served from
# state that predates it. From far away the ~320ms round trip hid this: by the
# time a read sent after an update reached the exchange, the exchange had long
# applied it. From Tokyo the read arrives within milliseconds, and a live run
# already caught a read sent after the stream's update answering with the
# position from before it -- which the liquidation guard then read as a close.
# So anything the stream said within this long before a read was sent is
# treated as possibly newer than the answer.
READ_LAG_S = 1.0


@dataclass
class _Overlay:
    delta: float
    expires_at: float
    label: str
    # When it arrived. An HTTP read keeps the overlays that arrived after it
    # was sent -- it cannot have seen them -- and drops the ones before.
    added_at: float = 0.0


@dataclass
class PositionBook:
    """Per-(account, symbol) signed sizes with a short-lived optimistic layer."""

    overlay_ttl_ms: int = 2000
    _authoritative: dict[Key, float] = field(default_factory=dict)
    _overlays: dict[Key, list[_Overlay]] = field(default_factory=dict)
    # When each key's position was last written -- by a stream position
    # update or a read. An HTTP read does not overwrite a position written
    # after it was sent. Fills do not count here: they are overlays, and a
    # read that skipped a key because a fill had arrived kept the position
    # from BEFORE that fill -- once the overlay expired, the book fell back
    # to it. See `apply_read`.
    _position_at: dict[Key, float] = field(default_factory=dict)
    # When the STREAM last wrote each key. Kept apart from `_position_at`,
    # which reads write too: a read that finished a moment ago says nothing
    # about whether the next one lags, while a stream update a moment ago is
    # exactly what a lagging answer would write over. See `READ_LAG_S`.
    _streamed_at: dict[Key, float] = field(default_factory=dict)
    # Keys whose last read could not be applied because a fill had arrived
    # after it was sent. See `apply_read`.
    _awaiting_read: set[Key] = field(default_factory=set)

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
        now = time.monotonic()
        self._position_at[key] = now
        self._streamed_at[key] = now
        self._awaiting_read.discard(key)

    def apply_snapshot(self, account: str, positions: Iterable) -> None:
        """Replace all of an account's positions from a full snapshot.

        Symbols absent from the snapshot are zeroed -- the exchange omits
        positions it considers closed, and a stale non-zero entry here would
        make the hedger chase a position that no longer exists.
        """
        # Built first and written in one pass. It used to zero every position
        # of the account and then write the real ones, and anything reading
        # in between -- another thread, when this ran from one -- saw the
        # account flat and sized a full hedge the wrong way.
        fresh = {(account, p.symbol): float(p.size) for p in positions}
        for key in [key for key in self._authoritative if key[0] == account]:
            fresh.setdefault(key, 0.0)
        for (acct, symbol), size in fresh.items():
            self.set_authoritative(acct, symbol, size)

    def apply_read(
        self, account: str, positions: Iterable, requested_at: float, force: bool = False
    ) -> list[str]:
        """Apply an HTTP position read, keeping anything newer than it.

        An HTTP read answers with the state at the moment the exchange served
        it, ~320ms after it was sent from here. A fill or position update that
        reached us over the stream in that window is NEWER than the answer,
        and `apply_snapshot` would write the older number over it and drop the
        fill's overlay: the book then reads the fill as never having happened,
        and the reconciler, which runs right after the read, hedges it again.

        Three cases per key:

        * The stream reported the position after `requested_at`. That report
          is newer than this answer, which is left unused.
        * A fill arrived after `requested_at`. The answer may or may not
          include it -- it was sent before the fill and served some time
          after -- so it is not written: writing it and keeping the fill's
          overlay counts the fill twice when the answer had it, and dropping
          the overlay loses the fill when it had not. The overlay is kept
          alive instead, and the key is marked for a confirming read, one
          sent after the fill, which can only include it.

          Merely skipping was not enough. The overlay then expired on its
          ordinary clock and the book fell back to the position from BEFORE
          the fill -- a closing order sized from it went out a second time.
        * Otherwise the answer is written, zeroing what the exchange no longer
          lists, and the overlays it accounts for are dropped.

        `force` writes the answer for every key. "Arrived after the request"
        means "newer" only while the stream keeps up. From a socket running
        tens of seconds behind, a position update or fill that lands during
        the read is OLDER than the answer: keeping the update left the book on
        a position the account no longer held, and keeping the fill counted
        it twice -- a live run hedged one group four times over that way. The
        caller forces it for such a socket, and keeps hedges waiting on reads
        until the socket catches up.

        "After `requested_at`" is widened by `READ_LAG_S` for what the stream
        said: the answer can trail the stream, so an update or fill from just
        before the read was sent may still be missing from it. Reads are not
        widened -- one read finishing just before the next says nothing about
        either lagging.

        Returns the symbols that were skipped, for the log.
        """
        fresh = {(account, p.symbol): float(p.size) for p in positions}
        for key in [key for key in self._authoritative if key[0] == account]:
            fresh.setdefault(key, 0.0)
        skipped = []
        now = time.monotonic()
        stream_cutoff = requested_at - READ_LAG_S
        for key, size in fresh.items():
            # Fills first. A stream position update drops every overlay, so
            # one still here arrived after the stream last spoke and is newer
            # than it too -- skipping on the stream alone would leave it to
            # lapse on its ordinary clock, back to the position before it.
            later = [] if force else [
                o for o in self._overlays.get(key, ()) if o.added_at > stream_cutoff
            ]
            if later:
                skipped.append(key[1])
                # Held well past the ordinary TTL: the confirming read is
                # requested at once, but a slow or failing one must not let
                # the fill lapse back to the older position in the meantime.
                hold_until = now + max(self.overlay_ttl_ms / 1000.0, CONFIRM_HOLD_S)
                for overlay in later:
                    overlay.expires_at = max(overlay.expires_at, hold_until)
                self._awaiting_read.add(key)
                continue
            if not force and (
                self._position_at.get(key, 0.0) > requested_at
                or self._streamed_at.get(key, 0.0) > stream_cutoff
            ):
                skipped.append(key[1])
                self._awaiting_read.discard(key)
                continue
            self._authoritative[key] = size
            self._position_at[key] = now
            self._overlays.pop(key, None)
            self._awaiting_read.discard(key)
        return skipped

    def awaiting_read(self) -> bool:
        """Whether a fill is waiting on a read sent after it to be confirmed."""
        return bool(self._awaiting_read)

    def add_overlay(self, account: str, symbol: str, delta: float, label: str = "") -> None:
        """Optimistically shift a position before the exchange confirms it."""
        if delta == 0:
            return
        key = (account, symbol)
        now = time.monotonic()
        expires_at = now + (self.overlay_ttl_ms / 1000.0)
        self._overlays.setdefault(key, []).append(
            _Overlay(delta=float(delta), expires_at=expires_at, label=label, added_at=now)
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

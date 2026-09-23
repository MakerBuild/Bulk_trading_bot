"""Crash-safe persistence of strategy state.

Written with a temp file plus `os.replace`, which is atomic on both POSIX and
Windows. A crash mid-write therefore leaves the previous good state intact
rather than a truncated file the bot would refuse to start from.

What is persisted is deliberately minimal: the phase, the cycle clock, and the
order IDs the bot believes it owns. Positions are not persisted -- they are read
back from the exchange on startup, which is the only source that can be trusted
after a crash.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field

from enum import Enum
import contextlib

log = logging.getLogger(__name__)

# How hard to try when the destination is locked by another process. Five
# attempts starting at 50ms and doubling covers roughly 1.5s in total, which is
# far longer than a sync client or a virus scanner holds a small file.
REPLACE_ATTEMPTS = 5
REPLACE_RETRY_DELAY_S = 0.05


class Phase(str, Enum):
    IDLE = "IDLE"
    OPEN = "OPEN"
    HOLD = "HOLD"
    EXIT = "EXIT"
    COMPLETE = "COMPLETE"
    HALTED = "HALTED"


@dataclass
class LegState:
    """The bot's belief about one resting limit order.

    `oid` is computed client-side before submission, so it survives a crash that
    happens between sending a transaction and seeing its response -- on restart
    the order can still be recognised and cancelled.
    """

    symbol: str
    # What this leg is filed under. Empty means the market, which is what
    # every leg was until an account pool made two legs on one market
    # possible -- at which point the market stopped being a name and
    # became a property, and two legs trading BTC-USD would have written
    # over each other's phase, order id and cycle count.
    id: str = ""
    oid: str | None = None
    price: float | None = None
    size: float | None = None
    target_size: float = 0.0
    complete: bool = False
    # Each leg runs its own OPEN -> HOLD -> EXIT. They were lockstep once, and
    # that made every leg wait for the slowest: a filled BTC leg sat idle until
    # ETH filled before its hold could start, and a closed ETH leg could not
    # re-open until BTC had closed too. The pair is hedged per symbol, so there
    # was never anything to synchronise -- only an accident of one shared phase.
    phase: Phase = Phase.IDLE
    hold_until: float = 0.0
    # Whether this leg has given up its offset and moved onto the touch.
    # Sticky until the leg completes: letting it spring back would walk
    # the order away from the market again, and it would only have to
    # walk back after the next wait.
    #
    # Held here rather than in a set of symbols inside the chaser. Two
    # legs on one market would have shared that set -- one tightening
    # would tighten the other, which has not yet placed an order --
    # and the flag was lost on restart, so a leg that had already given
    # up its offset went back to waiting out its patience again.
    tightened: bool = False
    # The group that drew this leg, when one did. A restart has to be
    # able to work out who opened what: the positions are on the
    # exchange either way, and without this the accounts holding them
    # cannot be identified and nothing would ever close them.
    #
    # Empty for a leg that came from the config, which is every leg
    # outside pool mode.
    group_id: int = 0
    maker: str = ""
    takers: list = field(default_factory=list)
    shares: list = field(default_factory=list)
    # Which way the group opened, so the exit mirrors it after a restart.
    # Absent from files written before the side was drawn, and True is what
    # those runs did.
    maker_is_buy: bool = True
    # How far inside the touch this cycle rests, drawn when the leg's
    # `offset_bps` is a range. Persisted so a resumed cycle keeps the
    # offset it has been resting at rather than jumping to a new one
    # mid-order. None means the leg follows its market's fixed number.
    offset_bps: float | None = None
    # This cycle's cap on one resting order, drawn with the size. Held per
    # leg for the reason the offset is: the chaser's params are one object
    # per market, so a cap written there is whichever group drew last.
    max_order_size: float | None = None
    cycle_index: int = 0
    # Order IDs that were replaced but whose cancels were never confirmed.
    # Swept on the next chase pass so a failed cancel can't leave a duplicate
    # order resting alongside its replacement.
    stale_oids: list = field(default_factory=list)

    def hold_remaining_s(self) -> float:
        """Seconds left on this leg's hold. Its own clock, not the pair's."""
        return max(0.0, self.hold_until - time.time())

    def remember_stale(self, oid: str | None) -> None:
        if oid and oid != self.oid and oid not in self.stale_oids:
            self.stale_oids.append(oid)


@dataclass
class StrategyState:
    phase: Phase = Phase.IDLE
    cycle_index: int = 0
    cycle_started_at: float = 0.0
    hold_until: float = 0.0
    legs: dict[str, LegState] = field(default_factory=dict)
    halted_reason: str | None = None
    updated_at: float = 0.0
    # Fill-history totals as they stood when this run began. The execution
    # target is measured as the distance from here, so `burn_usd: 3` means "$3
    # this run" and not "$3 since the account was opened" -- which, once passed,
    # ended every later run before it placed an order.
    #
    # Persisted so a crash mid-run resumes the same count rather than starting
    # the goal over; cleared when a run ends, so the next start measures fresh.
    baseline_fees_usd: float = 0.0
    baseline_volume_usd: float = 0.0
    # Self-trade volume as it stood at the baseline. Reported beside progress
    # to say how much of THIS run will not count toward the fee tier, and that
    # is a distance like the other two -- without it the line subtracted a
    # baselined total from a lifetime one and read "$0.00 of which $127,113.97",
    # which cannot be true of any run.
    baseline_self_trade_usd: float = 0.0
    baseline_at: float = 0.0

    @property
    def has_baseline(self) -> bool:
        return self.baseline_at > 0.0

    def clear_baseline(self) -> None:
        self.baseline_fees_usd = 0.0
        self.baseline_volume_usd = 0.0
        self.baseline_self_trade_usd = 0.0
        self.baseline_at = 0.0

    def leg(self, key: str, symbol: str | None = None) -> LegState:
        """The leg filed under `key`, created on first ask.

        `symbol` is the market it trades, and defaults to the key -- which is
        what a leg keyed by its market has always meant, and keeps every
        existing caller and every existing state file reading the same.
        """
        if key not in self.legs:
            self.legs[key] = LegState(symbol=symbol or key, id=key)
        return self.legs[key]

    def hold_remaining_s(self) -> float:
        return max(0.0, self.hold_until - time.time())

    @property
    def summary_phase(self) -> Phase:
        """One phase to show for a pair whose legs may be in different ones.

        The least advanced wins, so a display never claims the cycle is further
        along than its slowest leg. HALTED outranks everything: it is the one
        that must not be hidden behind a leg that happens to be opening.
        """
        phases = [leg.phase for leg in self.legs.values()]
        if not phases:
            return self.phase
        if Phase.HALTED in phases or self.phase == Phase.HALTED:
            return Phase.HALTED
        order = [Phase.IDLE, Phase.OPEN, Phase.HOLD, Phase.EXIT, Phase.COMPLETE]
        return min(phases, key=lambda p: order.index(p) if p in order else 0)

    def to_dict(self) -> dict:
        data = asdict(self)
        # Written from the legs, not from the field. Nothing updates the field
        # while a cycle runs -- the legs carry the phase now -- so persisting it
        # raw records whatever it happened to be at startup. A flatten read that
        # as IDLE and skipped resetting a state whose legs were mid-cycle.
        data["phase"] = self.summary_phase.value
        for leg in data["legs"].values():
            leg["phase"] = Phase(leg["phase"]).value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> StrategyState:
        legs = {}
        for key, stored in (data.get("legs") or {}).items():
            fields = dict(stored)
            # Files written before legs had ids of their own were keyed by
            # market, which is exactly what the id then was.
            fields.setdefault("id", key)
            # Written before legs had phases of their own. Inheriting the
            # cycle's phase is what that file meant.
            fields["phase"] = Phase(
                fields.get("phase") or data.get("phase", Phase.IDLE.value)
            )
            fields.setdefault("hold_until", float(data.get("hold_until", 0.0)))
            fields.setdefault("cycle_index", int(data.get("cycle_index", 0)))
            legs[key] = LegState(**fields)
        return cls(
            phase=Phase(data.get("phase", Phase.IDLE.value)),
            cycle_index=int(data.get("cycle_index", 0)),
            cycle_started_at=float(data.get("cycle_started_at", 0.0)),
            hold_until=float(data.get("hold_until", 0.0)),
            legs=legs,
            halted_reason=data.get("halted_reason"),
            updated_at=float(data.get("updated_at", 0.0)),
            # Absent in files written before the target had a baseline. Zero
            # reads as "not captured", so the next run takes one -- which is
            # the right answer for a file that predates the idea.
            baseline_fees_usd=float(data.get("baseline_fees_usd", 0.0)),
            baseline_volume_usd=float(data.get("baseline_volume_usd", 0.0)),
            baseline_self_trade_usd=float(data.get("baseline_self_trade_usd", 0.0)),
            baseline_at=float(data.get("baseline_at", 0.0)),
        )


class StateStore:
    """Loads and atomically saves `StrategyState` to a JSON file."""

    def __init__(self, path: str):
        self.path = path
        # What was last written, so an unchanged save is a no-op. None means
        # "nothing written yet this process", which forces the first save.
        self._last_signature: str | None = None

    def load(self) -> StrategyState:
        """Read persisted state, or return a fresh IDLE state if none exists.

        A corrupt file is treated as fatal rather than silently discarded: it
        may be the only record that positions are open, and starting a new cycle
        on top of forgotten positions is far worse than refusing to start.
        """
        if not os.path.exists(self.path):
            return StrategyState()
        try:
            with open(self.path, encoding="utf-8") as handle:
                return StrategyState.from_dict(json.load(handle))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"state file {self.path} is unreadable ({exc}). Inspect it manually "
                "and check both accounts for open positions before deleting it."
            ) from exc

    def save(self, state: StrategyState) -> None:
        """Write the state file atomically, and only when it has changed.

        Skipping an unchanged write is not an optimisation. `_drive_leg` saves
        once per chase tick per leg -- twice a second -- while the contents
        change only on a phase transition. Any process that watches the folder
        therefore had a file rewritten under it twice a second, all cycle.
        """
        state.updated_at = time.time()
        payload = json.dumps(state.to_dict(), indent=2)
        # `updated_at` alone is not a change worth a write; it is a timestamp of
        # the write itself, so comparing without it is what makes this work.
        signature = json.dumps(
            {k: v for k, v in state.to_dict().items() if k != "updated_at"},
            sort_keys=True,
        )
        if signature == self._last_signature and os.path.exists(self.path):
            return

        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)

        handle = tempfile.NamedTemporaryFile(
            "w", dir=directory, prefix=".state-", suffix=".tmp", delete=False, encoding="utf-8"
        )
        try:
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._replace_with_retry(handle.name)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(handle.name)
            raise
        self._last_signature = signature

    def _replace_with_retry(self, temp_name: str) -> None:
        """`os.replace`, which on Windows is not reliably atomic in practice.

        The rename fails with a sharing violation whenever another process has
        the destination open -- OneDrive uploading it, an antivirus reading it,
        the search indexer. Seen live as

            PermissionError: [WinError 5] -> app/state/strategy_state.json

        which killed a run mid-HOLD with positions open on both accounts. The
        lock is held for milliseconds, so a few retries clear it; the file is
        fully written and fsynced before the first attempt, so a retry risks
        nothing.
        """
        delay = REPLACE_RETRY_DELAY_S
        for attempt in range(1, REPLACE_ATTEMPTS + 1):
            try:
                os.replace(temp_name, self.path)
                if attempt > 1:
                    log.info("state file written on attempt %d", attempt)
                return
            except PermissionError as exc:
                if attempt == REPLACE_ATTEMPTS:
                    raise
                log.debug(
                    "state file is locked by another process (%s) -- retry %d in %.2fs",
                    exc, attempt, delay,
                )
                time.sleep(delay)
                delay *= 2

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

    def leg(self, symbol: str) -> LegState:
        if symbol not in self.legs:
            self.legs[symbol] = LegState(symbol=symbol)
        return self.legs[symbol]

    def reset_legs(self, targets: dict[str, float]) -> None:
        """Start a fresh set of legs for a new phase."""
        self.legs = {
            symbol: LegState(symbol=symbol, target_size=size)
            for symbol, size in targets.items()
        }

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
        data["phase"] = self.phase.value
        for leg in data["legs"].values():
            leg["phase"] = Phase(leg["phase"]).value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> StrategyState:
        legs = {}
        for symbol, stored in (data.get("legs") or {}).items():
            fields = dict(stored)
            # Written before legs had phases of their own. Inheriting the
            # cycle's phase is what that file meant.
            fields["phase"] = Phase(
                fields.get("phase") or data.get("phase", Phase.IDLE.value)
            )
            fields.setdefault("hold_until", float(data.get("hold_until", 0.0)))
            fields.setdefault("cycle_index", int(data.get("cycle_index", 0)))
            legs[symbol] = LegState(**fields)
        return cls(
            phase=Phase(data.get("phase", Phase.IDLE.value)),
            cycle_index=int(data.get("cycle_index", 0)),
            cycle_started_at=float(data.get("cycle_started_at", 0.0)),
            hold_until=float(data.get("hold_until", 0.0)),
            legs=legs,
            halted_reason=data.get("halted_reason"),
            updated_at=float(data.get("updated_at", 0.0)),
        )


class StateStore:
    """Loads and atomically saves `StrategyState` to a JSON file."""

    def __init__(self, path: str):
        self.path = path

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
        state.updated_at = time.time()
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)

        handle = tempfile.NamedTemporaryFile(
            "w", dir=directory, prefix=".state-", suffix=".tmp", delete=False, encoding="utf-8"
        )
        try:
            with handle:
                json.dump(state.to_dict(), handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(handle.name)
            raise

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
from typing import Dict, Optional

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
    oid: Optional[str] = None
    price: Optional[float] = None
    size: Optional[float] = None
    target_size: float = 0.0
    complete: bool = False
    # Order IDs that were replaced but whose cancels were never confirmed.
    # Swept on the next chase pass so a failed cancel can't leave a duplicate
    # order resting alongside its replacement.
    stale_oids: list = field(default_factory=list)

    def remember_stale(self, oid: Optional[str]) -> None:
        if oid and oid != self.oid and oid not in self.stale_oids:
            self.stale_oids.append(oid)


@dataclass
class StrategyState:
    phase: Phase = Phase.IDLE
    cycle_index: int = 0
    cycle_started_at: float = 0.0
    hold_until: float = 0.0
    legs: Dict[str, LegState] = field(default_factory=dict)
    halted_reason: Optional[str] = None
    updated_at: float = 0.0

    def leg(self, symbol: str) -> LegState:
        if symbol not in self.legs:
            self.legs[symbol] = LegState(symbol=symbol)
        return self.legs[symbol]

    def reset_legs(self, targets: Dict[str, float]) -> None:
        """Start a fresh set of legs for a new phase."""
        self.legs = {
            symbol: LegState(symbol=symbol, target_size=size)
            for symbol, size in targets.items()
        }

    def hold_remaining_s(self) -> float:
        return max(0.0, self.hold_until - time.time())

    def to_dict(self) -> Dict:
        data = asdict(self)
        data["phase"] = self.phase.value
        return data

    @classmethod
    def from_dict(cls, data: Dict) -> "StrategyState":
        legs = {
            symbol: LegState(**leg_data)
            for symbol, leg_data in (data.get("legs") or {}).items()
        }
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
            with open(self.path, "r", encoding="utf-8") as handle:
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
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise

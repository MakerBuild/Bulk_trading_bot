"""Console title as a progress indicator.

A run lasts hours and prints little between phase changes, so a minimised
terminal gives no sign of whether it is mid-cycle, holding, or long since
halted. The title bar carries that without adding log noise.

Windows-only in effect; a no-op elsewhere, so callers need no platform check.
"""

from __future__ import annotations

import ctypes
import logging
import os

log = logging.getLogger(__name__)

_IS_WINDOWS = os.name == "nt"


class WindowTitle:
    """Tracks run progress and reflects it in the console title."""

    def __init__(self, *, cycles: int = 0, name: str = "bulkdn"):
        self.name = name
        self.cycles = cycles
        self.cycle = 0
        self.phase = "starting"
        self.note = ""
        # A control the operator can use right now, kept at the end of the
        # title. The banner that announces it scrolls away; this does not, so
        # someone returning to a window hours later can still see how to stop.
        self.hint = ""
        self.update()

    def set_cycle(self, cycle: int, cycles: int | None = None) -> None:
        self.cycle = cycle
        if cycles is not None:
            self.cycles = cycles
        self.update()

    def set_phase(self, phase: str) -> None:
        self.phase = phase
        self.update()

    def set_note(self, note: str) -> None:
        """Extra detail for the title -- exposure, spend, whatever matters now."""
        self.note = note
        self.update()

    def set_hint(self, hint: str) -> None:
        self.hint = hint
        self.update()

    def halted(self, reason: str) -> None:
        self.phase = "HALTED"
        # The title is a few dozen characters; a long reason would push the
        # phase off the end of the tab.
        self.note = reason[:40]
        self.update()

    def update(self) -> None:
        if not _IS_WINDOWS:
            return
        progress = f"{self.cycle}/{self.cycles}" if self.cycles else str(self.cycle)
        title = f"{self.name} [{progress}] {self.phase}"
        if self.note:
            title += f" | {self.note}"
        if self.hint:
            title += f"  --  {self.hint}"
        try:
            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception as exc:  # noqa: BLE001 - cosmetic only
            log.debug("could not set console title: %s", exc)

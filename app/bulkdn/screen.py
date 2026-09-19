"""A status block that stays at the bottom while the run scrolls above it.

A live run prints about ninety lines of position reads, exposure sums and order
placements every five minutes, and perhaps twenty that anyone would want to
read. Someone who has just started the bot cannot tell from that whether it is
working, and the one number they actually came for -- how far along the goal is
-- scrolls past between two walls of hexadecimal.

So the console gets a status block instead: a few lines, rewritten in place,
carrying what a person watching would ask for. Everything else keeps going to
`logs.txt`, which is where it belongs when something has to be reconstructed
afterwards.

**Anything that is not a console falls back to plain printing.** Redirected
output, a scheduled task, a test -- the escape codes would end up in the file as
literal characters, and a log nobody can grep is worse than an ugly one.
"""

from __future__ import annotations

import os
import shutil
import sys

_IS_WINDOWS = os.name == "nt"
# ENABLE_VIRTUAL_TERMINAL_PROCESSING. Windows 10 understands ANSI once asked;
# older builds return an error, which is why the result is checked rather than
# assumed.
_ENABLE_VT = 0x0004
_STDOUT = -11


def enable_ansi() -> bool:
    """Turn on escape-code handling. True when the console will honour it."""
    if not sys.stdout.isatty():
        return False
    if not _IS_WINDOWS:
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(_STDOUT)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | _ENABLE_VT))
    except Exception:  # noqa: BLE001 - any failure means plain printing
        return False


class StatusBlock:
    """Lines pinned to the bottom of the console, rewritten in place.

    `write_above` is how anything else reaches the screen: it erases the block,
    prints, and draws the block again underneath. Printing straight to stdout
    while the block is up would leave half of it stranded mid-scroll.
    """

    def __init__(self, stream=None, ansi: bool | None = None):
        self.stream = stream or sys.stdout
        self.ansi = enable_ansi() if ansi is None else ansi
        self._lines: list[str] = []
        self._drawn = 0

    @property
    def active(self) -> bool:
        """Whether the block is being drawn rather than printed once."""
        return self.ansi

    def _width(self) -> int:
        try:
            return max(20, shutil.get_terminal_size().columns - 1)
        except Exception:  # noqa: BLE001 - no console, no width
            return 100

    def _clear(self) -> None:
        if not self.ansi or not self._drawn:
            return
        # Up one line and erase it, for each line the block occupies. The
        # cursor sits below the block, so the first move is up into it.
        self.stream.write("\r" + ("\033[A\033[2K" * self._drawn))
        self._drawn = 0

    def update(self, lines: list[str]) -> None:
        """Replace the block's contents and redraw it."""
        self._lines = lines
        if not self.ansi:
            return
        self._clear()
        width = self._width()
        for line in self._lines:
            self.stream.write(line[:width] + "\n")
        self._drawn = len(self._lines)
        self.stream.flush()

    def write_above(self, text: str) -> None:
        """Print `text` where the block is, then put the block back below it."""
        if not self.ansi:
            print(text, file=self.stream, flush=True)
            return
        self._clear()
        self.stream.write(text + "\n")
        for line in self._lines:
            self.stream.write(line[: self._width()] + "\n")
        self._drawn = len(self._lines)
        self.stream.flush()

    def close(self) -> None:
        """Leave the block on screen and stop managing it."""
        self._drawn = 0
        if self.ansi:
            self.stream.flush()


def bar(fraction: float, width: int = 24) -> str:
    """A progress bar in characters every Windows console can draw.

    Deliberately not a block-drawing character: `cmd.exe` on a default code
    page renders those as mojibake, and a progress bar that looks like damage
    is worse than one made of hashes.
    """
    fraction = min(1.0, max(0.0, fraction))
    filled = int(round(fraction * width))
    return "#" * filled + "-" * (width - filled)


def humanise(seconds: float) -> str:
    """`1h 04m`, `4m 12s`, `38s` -- whichever fits what is being measured."""
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds}s"


# One block per process. Logging is configured before anything else exists and
# needs somewhere to write; the run then fills it in.
SCREEN = StatusBlock()

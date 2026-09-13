"""Taking a keypress while the strategy has the terminal.

`run` occupies the console for hours, and until now the only way to stop it was
Ctrl+C or closing the window -- both of which look, to someone who has money on
the exchange, like pulling a plug. People opened a second terminal to run
`flatten` instead, which is a worse answer than the one they wanted: a visible
way to say "stop".

So a background task watches the keyboard while the legs run. It does not read
a line and it does not block: it polls, so pressing the key takes effect at the
next chase tick rather than waiting for Enter.

Windows-only in effect. Elsewhere, and whenever stdin is not a console (piped
output, a test, a scheduled task), the watcher returns immediately and the run
behaves exactly as it did before -- no key, no reader, nothing consuming input.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable

log = logging.getLogger(__name__)

# `ы` is the same physical key on a Russian layout, which is what this is
# usually read on. Q is what people try when S does not occur to them.
STOP_KEYS = frozenset("sSыЫqQйЙ")

# Keys that are not characters (arrows, F-keys) arrive as two reads: a marker,
# then a scan code. The scan code has to be consumed or it is mistaken for a
# keypress of its own -- F2 would otherwise read as `<`.
_PREFIXES = ("\x00", "\xe0")


def _make_reader() -> Callable[[], str | None] | None:
    """A non-blocking read of one character, or None if that is not possible."""
    if sys.platform != "win32":
        return None
    try:
        import msvcrt
    except ImportError:  # pragma: no cover - win32 always has it
        return None
    try:
        if not sys.stdin.isatty():
            return None
    except (AttributeError, ValueError):
        # stdin closed or replaced by something without a file descriptor.
        return None

    def read() -> str | None:
        if not msvcrt.kbhit():
            return None
        char = msvcrt.getwch()
        if char in _PREFIXES:
            msvcrt.getwch()
            return None
        return char

    return read


async def watch_for_stop(
    request_stop: Callable[[str], None],
    *,
    poll_s: float = 0.2,
) -> None:
    """Call `request_stop` when the operator asks for one, then return.

    Returns without doing anything where no console is attached, so callers can
    start it unconditionally.
    """
    read = _make_reader()
    if read is None:
        log.debug("no console keyboard available -- stop key disabled")
        return

    while True:
        char = read()
        if char and char in STOP_KEYS:
            request_stop("keyboard")
            return
        await asyncio.sleep(poll_s)

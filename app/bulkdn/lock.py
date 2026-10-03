"""One copy of the bot at a time on the same accounts.

The menu and the Telegram service are two ways into the same accounts, and
nothing stopped both being used at once: a run started from the menu while the
service was already trading put two strategies on one pool, each hedging the
other's fills as its own. And update.bat reinstalled the SDK underneath a bot
that was running from it.

So every command that trades, closes or reads the accounts -- run, flatten,
status, telegram, and the menu items that call them -- holds this lock while it
works, and the update scripts refuse to touch the install while anyone does.

**The lock is the operating system's, not the file's.** The file only says who
holds it (process id, what it is doing, since when). The lock itself is a byte
range on Windows and an flock on Linux, and both are released by the operating
system the moment the process ends, however it ends. So there is no stale lock
to detect: a bot that crashed, was killed, or lost its machine to a power cut
holds nothing, and the next start simply takes it. A lock judged by "is that
process id still alive" is fooled the day the id is reused by something else;
this cannot be.

Re-entrant within one process: the menu holds it around Telegram control, and
the runs Telegram starts take it again without waiting on themselves.

Standard library only -- update.bat runs this before anything is installed.
"""

from __future__ import annotations

import contextlib
import datetime
import os
import pathlib
import sys
import time

# Beside the state file, in the folder that holds what the bot has open.
LOCK_FILE = "app/state/bot.lock"
# Where the lock byte sits: past anything the file says, so reading who holds
# it is never blocked by the lock itself (Windows locks are mandatory).
_LOCK_OFFSET = 1 << 20

# How long to wait for a lock that is held. Windows releases the lock of a
# process that has just died a moment AFTER it has gone ("depends upon
# available system resources", says the documentation), and a restart right
# after a crash must not be refused for that.
_PATIENCE_S = 2.0

# Exit code of `python -m bulkdn.lock` when another process holds it. Not 1:
# a Python that cannot even start exits 1, and must not read as "running".
HELD_EXIT = 3


class Held(Exception):
    """Another process holds the lock. The message says who and how to stop it."""


_depth = 0
_handle = None


def _try_lock(handle, patience_s: float = 0.0) -> bool:
    """Take the lock if it is free, waiting up to `patience_s` for it to be."""
    deadline = time.monotonic() + patience_s
    while not _lock_once(handle):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def _lock_once(handle) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(_LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle) -> None:
    with contextlib.suppress(OSError):
        if os.name == "nt":
            import msvcrt

            handle.seek(_LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _who(path: pathlib.Path) -> str:
    """What the file says about whoever holds it, or a plain fallback."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    return text or "another copy of the bot"


def _stop_hint() -> str:
    if os.name == "nt":
        return "Close its window (press S first if it is trading), or stop Telegram control."
    return (
        "Stop it first: press S in its window, or if it runs as a service, "
        "`sudo ./service.sh stop`."
    )


def acquire(what: str, path: str | None = None) -> None:
    """Take the lock, or raise Held. Nested calls in one process just count."""
    global _depth, _handle
    if _depth:
        _depth += 1
        return
    target = pathlib.Path(path or LOCK_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = open(target, "a+b")  # noqa: SIM115 - held open for as long as the lock is
    if not _try_lock(handle, _PATIENCE_S):
        handle.close()
        raise Held(
            f"{_who(target)} is already working with these accounts. "
            f"Two at once would trade against each other. {_stop_hint()}"
        )
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    handle.seek(0)
    handle.truncate()
    handle.write(f"process {os.getpid()} ({what}, since {stamp})\n".encode())
    handle.flush()
    _handle, _depth = handle, 1


def release() -> None:
    global _depth, _handle
    if not _depth:
        return
    _depth -= 1
    if _depth or _handle is None:
        return
    handle, _handle = _handle, None
    with contextlib.suppress(OSError):
        handle.seek(0)
        handle.truncate()
    _unlock(handle)
    handle.close()


@contextlib.contextmanager
def hold(what: str, path: str | None = None):
    """Hold the lock for the length of a `with` block."""
    acquire(what, path)
    try:
        yield
    finally:
        release()


def holder(path: str | None = None) -> str | None:
    """Who holds the lock, when another process does; None when it is free."""
    if _depth:
        return None
    target = pathlib.Path(path or LOCK_FILE)
    if not target.exists():
        return None
    try:
        handle = open(target, "a+b")  # noqa: SIM115 - closed just below
    except OSError:
        return None
    try:
        if _try_lock(handle, _PATIENCE_S):
            _unlock(handle)
            return None
    finally:
        handle.close()
    return _who(target)


def main() -> int:
    """`python -m bulkdn.lock`: exit HELD_EXIT, saying who, if the bot is running."""
    who = holder()
    if who is None:
        return 0
    print(f"\n  The bot is running: {who}.", file=sys.stderr)
    print(f"  {_stop_hint()}", file=sys.stderr)
    return HELD_EXIT


if __name__ == "__main__":
    sys.exit(main())

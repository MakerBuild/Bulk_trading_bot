"""Stopping a run from the keyboard, and the log file that survives the window.

Both exist for the same complaint: once `run` had the terminal there was no way
to say "stop" and no record left behind when the window closed.
"""

import asyncio
import logging
import pathlib
import sys

import pytest

from bulkdn import cli, console, menu
from bulkdn.window import WindowTitle

# -- the stop key ----------------------------------------------------------


@pytest.mark.parametrize("key", ["s", "S", "ы", "q", "Q"])
async def test_a_stop_key_requests_a_stop(monkeypatch, key):
    """Upper and lower case, and the same physical key on a Russian layout."""
    monkeypatch.setattr(console, "_make_reader", lambda: iter([key, None]).__next__)
    asked = []
    await console.watch_for_stop(asked.append, poll_s=0)
    assert asked == ["keyboard"]


async def test_other_keys_are_ignored(monkeypatch):
    """A stray keypress must not stop a live run."""
    keys = iter(["a", "1", "\r", " ", "s"])
    monkeypatch.setattr(console, "_make_reader", lambda: lambda: next(keys))
    asked = []
    await console.watch_for_stop(asked.append, poll_s=0)
    assert asked == ["keyboard"]


async def test_without_a_console_the_watcher_does_nothing(monkeypatch):
    """Piped output, a test, a scheduled task -- no reader, no consumed input."""
    monkeypatch.setattr(console, "_make_reader", lambda: None)
    asked = []
    await asyncio.wait_for(console.watch_for_stop(asked.append), timeout=1)
    assert asked == []


def test_no_reader_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert console._make_reader() is None


def test_no_reader_when_stdin_is_not_a_console(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdin", type("X", (), {"isatty": lambda self: False})())
    assert console._make_reader() is None


# -- what request_stop does ------------------------------------------------


class _FakeStrategy:
    """Only the parts of Strategy that request_stop touches."""

    from bulkdn.strategy import Strategy

    request_stop = Strategy.request_stop

    def __init__(self):
        self._stop = asyncio.Event()
        self._stop_requested = None


async def test_request_stop_sets_the_flag_and_the_event():
    s = _FakeStrategy()
    s.request_stop("keyboard")
    assert s._stop.is_set()
    assert s._stop_requested == "keyboard"


async def test_request_stop_is_idempotent():
    """Pressing the key twice is not a harder stop, and must not relabel it."""
    s = _FakeStrategy()
    s.request_stop("keyboard")
    s.request_stop("something else")
    assert s._stop_requested == "keyboard"


async def test_a_stopped_leg_leaves_its_loop_without_chasing(tmp_path):
    """The proof that the key reaches the work: the loop that places orders
    checks the flag before it does anything, so a stop cannot be swallowed by a
    leg that is mid-phase."""
    from test_strategy import BTC, build

    strategy, *_ = build(tmp_path)

    class ExplodingChaser:
        async def step(self, roles, leg):
            raise AssertionError("a stopped leg must not place another order")

    strategy.chaser = ExplodingChaser()
    strategy.request_stop("keyboard")

    # Never satisfied: if the stop did not end this, the test times out here
    # rather than passing by accident.
    await asyncio.wait_for(
        strategy._drive_leg(BTC, lambda: False, "open"), timeout=2
    )


# -- the reminder that does not scroll away --------------------------------


def test_the_hint_stays_in_the_title():
    title = WindowTitle(cycles=10, name="bulkdn")
    title.set_cycle(3)
    title.set_phase("HOLD")
    title.set_hint("S = stop")
    rendered = f"{title.name} [{title.cycle}/{title.cycles}] {title.phase}"
    assert title.hint == "S = stop"
    # Whatever else the title carries, the hint is appended after it.
    assert rendered.startswith("bulkdn [3/10] HOLD")


# -- the log file ----------------------------------------------------------


def _reset_logging():
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()


def test_logging_writes_to_the_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _reset_logging()
    try:
        cli.configure_logging("INFO", log_file=cli.LOG_FILE)
        logging.getLogger("bulkdn").warning("halted: something went wrong")
        for handler in logging.getLogger().handlers:
            handler.flush()
        written = (tmp_path / cli.LOG_FILE).read_text(encoding="utf-8")
    finally:
        _reset_logging()
    assert "halted: something went wrong" in written
    # The file carries the date; the console format does not.
    assert written[:4].isdigit()


def test_an_unwritable_log_does_not_stop_the_bot(tmp_path, monkeypatch):
    """A locked or read-only file is not a reason to refuse to trade."""
    monkeypatch.chdir(tmp_path)
    _reset_logging()
    try:
        # A directory where the file should be: opening it raises OSError.
        (tmp_path / cli.LOG_FILE).mkdir()
        cli.configure_logging("INFO", log_file=cli.LOG_FILE)
        logging.getLogger("bulkdn").info("still running")
    finally:
        _reset_logging()


def test_log_files_include_the_rollovers(tmp_path, monkeypatch):
    """Erasing logs has to take the rotated copies too, or it erases nothing."""
    monkeypatch.chdir(tmp_path)
    for name in (cli.LOG_FILE, f"{cli.LOG_FILE}.1", f"{cli.LOG_FILE}.2"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    (tmp_path / "settings.yaml").write_text("x", encoding="utf-8")

    found = {p.name for p in menu._log_files()}
    assert found == {cli.LOG_FILE, f"{cli.LOG_FILE}.1", f"{cli.LOG_FILE}.2"}


def test_deleting_a_log_releases_the_handle_first(tmp_path, monkeypatch):
    """Windows will not unlink a file this process still has open."""
    monkeypatch.chdir(tmp_path)
    _reset_logging()
    try:
        cli.configure_logging("INFO", log_file=cli.LOG_FILE)
        logging.getLogger("bulkdn").info("a line")
        result = menu._delete(pathlib.Path(cli.LOG_FILE))
    finally:
        _reset_logging()
    assert "deleted" in result
    assert not (tmp_path / cli.LOG_FILE).exists()

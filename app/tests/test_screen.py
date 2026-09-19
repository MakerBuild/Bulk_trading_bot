"""The console someone actually watches.

A live run printed about ninety lines of position reads, exposure sums and
order placements every five minutes against roughly twenty worth reading, plus
one line from the SDK on every HTTP call:

    getting account info for 6r3jPT... from https://mainnet-api1.bulk.trade/...

None of it is addressed to the operator, and the number they started the bot
for scrolled past in the middle of it. So the screen carries a status block and
the rest goes to the file.
"""

import io
import logging

from bulkdn.screen import StatusBlock, bar, humanise


def block(ansi=True):
    out = io.StringIO()
    return StatusBlock(stream=out, ansi=ansi), out


# -- the block redraws rather than scrolls -----------------------------------


def test_the_block_is_drawn():
    b, out = block()
    b.update(["one", "two"])
    assert "one" in out.getvalue() and "two" in out.getvalue()


def test_redrawing_erases_the_previous_block():
    """Otherwise every refresh leaves another copy behind and the console
    fills with the thing meant to stop it filling."""
    b, out = block()
    b.update(["first"])
    out.truncate(0)
    out.seek(0)
    b.update(["second"])

    written = out.getvalue()
    assert "\033[A" in written, "it never moved up to overwrite"
    assert written.count("\033[2K") == 1, "it erased the wrong number of lines"
    assert "second" in written


def test_a_taller_block_erases_all_of_what_it_replaces():
    b, out = block()
    b.update(["a", "b", "c"])
    out.truncate(0)
    out.seek(0)
    b.update(["x"])
    assert out.getvalue().count("\033[2K") == 3


def test_writing_above_puts_the_block_back():
    """A log line printed straight to stdout would strand the block halfway up
    the scrollback."""
    b, out = block()
    b.update(["status here"])
    out.truncate(0)
    out.seek(0)
    b.write_above("13:01:41 INFO  cycle 4")

    written = out.getvalue()
    assert "cycle 4" in written
    assert written.index("cycle 4") < written.index("status here"), "block is above the log"


# -- and gives up gracefully where it cannot ---------------------------------


def test_without_a_console_it_just_prints():
    """Redirected output, a scheduled task, a test: escape codes would land in
    the file as literal characters."""
    b, out = block(ansi=False)
    b.update(["status"])
    b.write_above("a log line")

    written = out.getvalue()
    assert "a log line" in written
    assert "\033[" not in written, "escape codes reached a file"
    assert "status" not in written, "the block was repeated into the log"


def test_without_a_console_the_status_is_not_repeated():
    """It would be a line of noise per second in the log."""
    b, out = block(ansi=False)
    b.update(["status"])
    b.update(["status"])
    assert "status" not in out.getvalue()


# -- the pieces it is made of ------------------------------------------------


def test_the_bar_fills_with_progress():
    assert bar(0.0, 10) == "-" * 10
    assert bar(1.0, 10) == "#" * 10
    assert bar(0.5, 10).count("#") == 5


def test_the_bar_survives_nonsense():
    """A target read before the baseline lands can divide oddly."""
    assert len(bar(-5.0, 10)) == 10
    assert len(bar(99.0, 10)) == 10


def test_the_bar_is_drawable_on_a_default_code_page():
    """cmd.exe renders block-drawing characters as mojibake, and a progress
    bar that looks like damage is worse than one made of hashes."""
    assert set(bar(0.5, 10)) <= {"#", "-"}


def test_durations_read_as_a_person_would_say_them():
    assert humanise(38) == "38s"
    assert humanise(252) == "4m 12s"
    assert humanise(3847) == "1h 04m"
    assert humanise(-1) == "0s"


# -- what reaches the screen -------------------------------------------------


def records():
    from bulkdn.cli import _ConsoleFilter

    f = _ConsoleFilter()

    def passes(name, level, msg):
        return f.filter(logging.LogRecord(name, level, "x", 1, msg, (), None))

    return passes


def test_bookkeeping_stays_off_the_screen():
    passes = records()
    for name in ("bulkdn.chaser", "bulkdn.hedger", "bulkdn.reconcile", "bulkdn.risk"):
        assert not passes(name, logging.INFO, "anything"), name
    assert not passes("bulkdn.strategy", logging.INFO, "fill on master: BUY 1 BTC-USD @ 2")
    assert not passes("bulkdn.strategy", logging.INFO, "reconciler corrected BTC-USD SELL 1")


def test_news_reaches_the_screen():
    passes = records()
    assert passes("bulkdn.strategy", logging.INFO, "=== BTC-USD cycle 4 ===")
    assert passes("bulkdn.strategy", logging.INFO, "volume progress: $1 / $2 qualifying")
    assert passes("bulkdn", logging.INFO, "all cycles complete")


def test_trouble_always_reaches_the_screen():
    """Hiding bookkeeping must never hide a problem -- including from the very
    loggers whose INFO is suppressed."""
    passes = records()
    for name in ("bulkdn.chaser", "bulkdn.hedger", "bulkdn.reconcile", "bulkdn.risk"):
        assert passes(name, logging.WARNING, "something went wrong"), name
        assert passes(name, logging.CRITICAL, "HALT"), name

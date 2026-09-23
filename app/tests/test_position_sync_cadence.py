"""When to ask the exchange for positions, and when not to bother.

The read exists for exactly one failure: a socket that stops delivering
without disconnecting. The position book is fed by those deliveries and the
optimistic fill overlay expires, so a silent stall decays the book toward
empty -- and the hedge rule, derived from an empty book, concludes the pair is
flat while real positions are still open.

It used to run on a timer: one HTTP request per account every five seconds.
Two accounts was already enough to draw a 429 from the exchange, and the pool
this is being prepared for has a hundred. Since the failure it guards against
is measurable, the read now follows the evidence instead of the clock.
"""

import time

import pytest

from bulkdn.config import Config, LegConfig, RiskConfig
from bulkdn.positions import PositionBook
from bulkdn.strategy import Strategy

QUIET = 30.0  # ws_stale_timeout_s; the trigger is half of it


class FakeSession:
    def __init__(self, name, age=0.0):
        self.name = name
        self.last_message_age_s = age


def strategy(ages, sync_interval=60.0):
    obj = object.__new__(Strategy)
    obj.book = PositionBook()
    obj.config = Config(
        markets=[
            LegConfig(symbol="BTC-USD", size=1.0, offset_bps=1.0,
                      max_distance_bps=5.0),
            LegConfig(symbol="ETH-USD", size=1.0, offset_bps=1.0,
                      max_distance_bps=5.0),
        ],
        risk=RiskConfig(ws_stale_timeout_s=QUIET),
        position_sync_interval_s=sync_interval,
        private_key="x",
    )
    obj.sessions = {f"k{i}": FakeSession(f"acct{i}", age) for i, age in enumerate(ages)}
    return obj


# -- a talking socket needs no checking -------------------------------------


def test_a_fresh_socket_is_not_worth_a_request():
    assert strategy([0.0, 0.5])._sync_reason(now=10.0, last_sync=9.0) == ""


def test_nor_is_one_that_is_merely_slow():
    """Half the stale timeout, not a hair past the last message."""
    assert strategy([QUIET / 2 - 1])._sync_reason(now=10.0, last_sync=9.0) == ""


# -- a quiet one does -------------------------------------------------------


def test_a_quiet_socket_is_checked_at_once():
    reason = strategy([0.0, QUIET / 2 + 1])._sync_reason(now=10.0, last_sync=9.9)
    assert "quiet" in reason
    assert "acct1" in reason, "the reason should name which account went quiet"


def test_it_checks_before_the_halt_rather_than_alongside_it():
    """The halt fires at ws_stale_timeout_s. A check that waited that long
    would arrive in the same breath as the thing it was meant to prevent."""
    assert strategy([QUIET / 2 + 0.1])._sync_reason(now=10.0, last_sync=9.9) != ""


def test_one_quiet_session_among_many_is_enough():
    ages = [0.0] * 20 + [QUIET]
    assert strategy(ages)._sync_reason(now=10.0, last_sync=9.9) != ""


def test_a_session_that_cannot_say_is_treated_as_suspect():
    """Not knowing whether the socket is alive is not the same as it being
    alive, and the safe reading of the two is the same."""
    obj = strategy([0.0])

    class Mute:
        name = "mute"

        @property
        def last_message_age_s(self):
            raise RuntimeError("no clock")

    obj.sessions["mute"] = Mute()
    assert obj._sync_reason(now=10.0, last_sync=9.9) != ""


# -- and the backstop underneath --------------------------------------------


def test_a_chatty_socket_is_still_checked_eventually():
    """The staleness clock cannot see a socket that delivers regularly and is
    nonetheless wrong. This is the only thing that would catch it."""
    assert strategy([0.0])._sync_reason(now=100.0, last_sync=0.0) == "periodic"


def test_the_backstop_does_not_fire_early():
    assert strategy([0.0])._sync_reason(now=59.0, last_sync=0.0) == ""


@pytest.mark.parametrize("interval", [5.0, 60.0, 600.0])
def test_the_backstop_follows_its_setting(interval):
    obj = strategy([0.0], sync_interval=interval)
    assert obj._sync_reason(now=interval - 0.1, last_sync=0.0) == ""
    assert obj._sync_reason(now=interval, last_sync=0.0) == "periodic"


def test_the_request_rate_it_replaces():
    """Twelve times fewer reads per account at the shipped defaults, before
    counting the ones the staleness trigger makes unnecessary."""
    obj = strategy([0.0])
    assert obj.config.position_sync_interval_s / obj.config.reconcile_interval_s == 12


def test_a_fill_awaiting_confirmation_is_read_at_once():
    """A read left unused because a fill arrived mid-flight is followed by
    one sent after the fill, not left for the periodic backstop."""
    obj = strategy([0.0])
    obj.book.set_authoritative("acct", "BTC-USD", 0.5)
    time.sleep(0.02)                 # Windows' clock moves in ~15ms steps
    sent = time.monotonic()
    time.sleep(0.02)
    obj.book.apply_fill("acct", "BTC-USD", is_buy=False, size=0.5)

    class P:
        symbol, size = "BTC-USD", 0.5

    obj.book.apply_read("acct", [P()], requested_at=sent)

    assert obj._sync_reason(time.monotonic(), time.monotonic()) != ""

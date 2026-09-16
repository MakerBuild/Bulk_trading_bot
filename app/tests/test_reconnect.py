"""Recovering a dropped WebSocket instead of halting on it.

Two live runs ended mid-cycle with "master WebSocket is not connected", each
after the library gave up on a late pong. Nothing was wrong with the strategy:
the exchange's socket blinked and the kill switch fired. A cycle was therefore
only ever as long as the exchange's least reliable minute.

Reconnecting is safe here for a reason specific to this design: the hedge is
derived from positions, not from the fills that announce them. Whatever filled
while the socket was down appears as a position difference on the next HTTP
read and is corrected once. So these tests care about two things -- that a drop
is retried, and that a retry never swallows a violation that reconnecting
cannot fix.
"""

import pytest

from bulkdn.risk import Violation


class FakeClient:
    """Connects on the attempt given by `succeed_on`; never, if that is 0."""

    def __init__(self, succeed_on=1):
        self.succeed_on = succeed_on
        self.attempts = 0
        self.disconnects = 0
        self.is_connected = False

    async def connect(self):
        self.attempts += 1
        self.is_connected = self.succeed_on and self.attempts >= self.succeed_on
        return self.is_connected

    async def disconnect(self):
        self.disconnects += 1
        self.is_connected = False


class FakeSession:
    def __init__(self, name, succeed_on=1, dry_run=False):
        self.name = name
        self.client = FakeClient(succeed_on)
        self.dry_run = dry_run
        self.reconnects = 0
        # How long this socket has been silent. A live socket reports a small
        # number; a half-open one reports a growing one while still claiming to
        # be connected, which is the case `_healed` has to recognise.
        self.last_message_age_s = 0.0

    @property
    def is_connected(self):
        return self.client.is_connected

    async def reconnect(self, attempts=3, delay=0.0):
        self.reconnects += 1
        from bulkdn.accounts import AccountSession

        return await AccountSession.reconnect(self, attempts=attempts, delay=delay)


class FakeRisk:
    """Only the threshold `_healed` reads."""

    class config:
        ws_stale_timeout_s = 30.0


class FakeStrategy:
    """Only the parts of Strategy that _healed touches."""

    def __init__(self, master, sub1):
        self.master = master
        self.sub1 = sub1
        self.book = object()
        self.resyncs = 0
        self._reconnect_times = []
        self.risk = FakeRisk()

    async def healed(self, violations):
        from bulkdn.strategy import Strategy

        return await Strategy._healed(self, violations)


@pytest.fixture(autouse=True)
def no_http_resync(monkeypatch):
    """The resync reads the exchange; count it instead of calling it."""
    from bulkdn import strategy

    def record(sessions, book):
        for session in sessions:
            if hasattr(session, "_owner"):
                session._owner.resyncs += 1

    monkeypatch.setattr(strategy, "sync_positions_http", record)
    return record


def build(master_succeeds_on=1, sub_connected=True):
    master = FakeSession("master", succeed_on=master_succeeds_on)
    sub1 = FakeSession("sub1", succeed_on=1)
    sub1.client.is_connected = sub_connected
    strategy = FakeStrategy(master, sub1)
    master._owner = strategy
    sub1._owner = strategy
    return strategy, master, sub1


DROPPED = [Violation("disconnected", "master WebSocket is not connected")]


# -- a drop is retried ------------------------------------------------------


async def test_a_dropped_socket_is_reconnected():
    strategy, master, _sub1 = build()
    assert await strategy.healed(DROPPED) is True
    assert master.is_connected


async def test_it_retries_before_giving_up():
    strategy, master, _sub1 = build(master_succeeds_on=3)
    assert await strategy.healed(DROPPED) is True
    assert master.client.attempts == 3


async def test_a_socket_that_stays_down_is_not_healed():
    """And so the caller halts, which is the point of the fallback."""
    strategy, master, _sub1 = build(master_succeeds_on=0)
    assert await strategy.healed(DROPPED) is False
    assert not master.is_connected


async def test_positions_are_re_read_after_a_reconnect():
    """The book stopped updating while the socket was down."""
    strategy, _master, _sub1 = build()
    await strategy.healed(DROPPED)
    assert strategy.resyncs > 0


async def test_nothing_is_re_read_when_the_reconnect_failed():
    strategy, _master, _sub1 = build(master_succeeds_on=0)
    await strategy.healed(DROPPED)
    assert strategy.resyncs == 0


async def test_a_connected_session_is_left_alone():
    strategy, master, sub1 = build()
    master.client.is_connected = True
    await strategy.healed(DROPPED)
    assert master.client.attempts == 0
    assert sub1.client.attempts == 0


# -- a retry never swallows a real halt -------------------------------------


async def test_other_violations_are_not_retried():
    """Exposure over the cap is the strategy misbehaving, not the socket."""
    strategy, master, _sub1 = build()
    breach = [Violation("net_exposure", "BTC-USD net $900 exceeds $500")]
    assert await strategy.healed(breach) is False
    assert master.client.attempts == 0


async def test_a_drop_mixed_with_a_real_breach_is_not_retried():
    """Otherwise a blinking socket would keep clearing a genuine violation."""
    strategy, master, _sub1 = build()
    mixed = DROPPED + [Violation("reject_streak", "sub1 has 5 rejections")]
    assert await strategy.healed(mixed) is False
    assert master.client.attempts == 0


async def test_an_empty_violation_list_does_nothing():
    strategy, master, _sub1 = build()
    assert await strategy.healed([]) is False
    assert master.client.attempts == 0


async def test_a_dry_run_session_is_never_reconnected():
    strategy, master, _sub1 = build()
    master.dry_run = True
    master.client.is_connected = False
    await strategy.healed(DROPPED)
    assert master.client.attempts == 0


# -- a flapping socket is a fault, not a blip -------------------------------


async def test_reconnects_are_capped_within_the_window():
    """Otherwise the cycle never ends: no rejection is recorded, so the reject
    streak never trips, and the log scrolls past unread."""
    from bulkdn.strategy import MAX_RECONNECTS

    strategy, master, _sub1 = build()
    for _ in range(MAX_RECONNECTS):
        master.client.is_connected = False
        assert await strategy.healed(DROPPED) is True

    master.client.is_connected = False
    assert await strategy.healed(DROPPED) is False, "the cap did not hold"


async def test_the_cap_counts_attempts_not_failures():
    """A socket that reconnects every time is still flapping."""
    from bulkdn.strategy import MAX_RECONNECTS

    strategy, master, _sub1 = build()
    for _ in range(MAX_RECONNECTS):
        master.client.is_connected = False
        await strategy.healed(DROPPED)
    assert len(strategy._reconnect_times) == MAX_RECONNECTS


# -- the budget forgives drops that are far apart ----------------------------
#
# The cap was written as "per cycle" and the reset was never implemented, so
# five was the allowance for the entire run. An unlimited run therefore halted
# on its sixth dropped socket however many hours apart they fell -- reported as
# the bot dying overnight with the WebSocket "falling off".


async def test_old_drops_fall_out_of_the_window(monkeypatch):
    """A socket that blips once an hour is not a flapping socket."""
    from bulkdn import strategy as strategy_module
    from bulkdn.strategy import MAX_RECONNECTS, RECONNECT_WINDOW_S

    strategy, master, _sub1 = build()
    clock = [0.0]
    monkeypatch.setattr(strategy_module.time, "monotonic", lambda: clock[0])

    # Spend the whole budget, an hour apart each time.
    for _ in range(MAX_RECONNECTS * 3):
        clock[0] += RECONNECT_WINDOW_S * 6
        master.client.is_connected = False
        assert await strategy.healed(DROPPED) is True, "an isolated drop was refused"

    assert len(strategy._reconnect_times) == 1, "the window did not clear"


async def test_drops_inside_the_window_still_trip_it(monkeypatch):
    """And a socket that blips five times in ten minutes is."""
    from bulkdn import strategy as strategy_module
    from bulkdn.strategy import MAX_RECONNECTS, RECONNECT_WINDOW_S

    strategy, master, _sub1 = build()
    clock = [0.0]
    monkeypatch.setattr(strategy_module.time, "monotonic", lambda: clock[0])

    for _ in range(MAX_RECONNECTS):
        clock[0] += RECONNECT_WINDOW_S / (MAX_RECONNECTS + 2)
        master.client.is_connected = False
        assert await strategy.healed(DROPPED) is True

    clock[0] += 1.0
    master.client.is_connected = False
    assert await strategy.healed(DROPPED) is False, "the cap did not hold"


async def test_the_window_reopens_once_the_burst_ages_out(monkeypatch):
    """A halt is for a fault happening now, not for one that has passed."""
    from bulkdn import strategy as strategy_module
    from bulkdn.strategy import MAX_RECONNECTS, RECONNECT_WINDOW_S

    strategy, master, _sub1 = build()
    clock = [0.0]
    monkeypatch.setattr(strategy_module.time, "monotonic", lambda: clock[0])

    for _ in range(MAX_RECONNECTS):
        master.client.is_connected = False
        await strategy.healed(DROPPED)
    master.client.is_connected = False
    assert await strategy.healed(DROPPED) is False

    clock[0] += RECONNECT_WINDOW_S + 1
    master.client.is_connected = False
    assert await strategy.healed(DROPPED) is True, "still refusing after the burst aged out"


# -- a socket that went quiet without closing --------------------------------
#
# The reported symptom: "the websocket falls off". A peer that vanishes without
# a close frame leaves the client still reporting connected, so silence is the
# only evidence. The watchdog fires at ws_stale_timeout_s (30s) while the
# library's own keepalive needs ping_interval + ping_timeout (80s) to notice --
# so the stale check always won the race, and it halted instead of reconnecting.

STALE = [Violation("stale_stream", "master has received nothing for 31s")]


async def test_a_silent_socket_is_reconnected_not_halted():
    strategy, master, _sub1 = build()
    master.client.is_connected = True          # still claims to be up
    master.last_message_age_s = 31.0           # and has said nothing for 31s

    assert await strategy.healed(STALE) is True, "a stale socket must be retried"
    assert master.reconnects == 1


async def test_a_talking_socket_is_left_alone():
    """Only the silent one is touched, even when the pair is checked together."""
    strategy, master, sub1 = build()
    for session in (master, sub1):
        session.client.is_connected = True
    master.last_message_age_s = 31.0
    sub1.last_message_age_s = 1.0

    await strategy.healed(STALE)
    assert master.reconnects == 1
    assert sub1.reconnects == 0


async def test_staleness_mixed_with_a_real_fault_still_halts():
    """Exposure over the cap is not something a reconnect fixes."""
    strategy, master, _sub1 = build()
    master.client.is_connected = True
    master.last_message_age_s = 31.0

    mixed = STALE + [Violation("exposure", "net exposure $900 over $500")]
    assert await strategy.healed(mixed) is False


async def test_the_stale_threshold_comes_from_the_risk_config():
    """Not a second copy of the number that can drift from the first."""
    strategy, master, _sub1 = build()
    master.client.is_connected = True
    master.last_message_age_s = 20.0

    strategy.risk.config.ws_stale_timeout_s = 60.0
    assert await strategy.healed(STALE) is False, "20s is not stale at a 60s threshold"

    strategy.risk.config.ws_stale_timeout_s = 10.0
    assert await strategy.healed(STALE) is True, "20s is stale at a 10s threshold"
    strategy.risk.config.ws_stale_timeout_s = 30.0

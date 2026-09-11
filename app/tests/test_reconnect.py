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

    @property
    def is_connected(self):
        return self.client.is_connected

    async def reconnect(self, attempts=3, delay=0.0):
        self.reconnects += 1
        from bulkdn.accounts import AccountSession

        return await AccountSession.reconnect(self, attempts=attempts, delay=delay)


class FakeStrategy:
    """Only the parts of Strategy that _healed touches."""

    def __init__(self, master, sub1):
        self.master = master
        self.sub1 = sub1
        self.book = object()
        self.resyncs = 0

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

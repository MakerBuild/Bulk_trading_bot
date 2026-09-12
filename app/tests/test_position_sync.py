"""Sharing one position read between the callers that want it.

Three of them do now: the supervisor on its reconcile tick, and each leg
confirming a phase. They ask on their own schedules, and a live run caught them
fetching the same pair of accounts three times inside one second -- visible in
the log as duplicated "master positions" lines.

Only the request is shared. What each caller gets is still the exchange's
answer, read at most POSITION_FRESHNESS_S ago, which is no staler than the gap
every caller already had between its own read and acting on it.
"""

import asyncio

import pytest

from bulkdn.strategy import POSITION_FRESHNESS_S, Strategy


class Recorder:
    """Stands in for the exchange read, counting how often it is asked."""

    def __init__(self, delay=0.0):
        self.calls = 0
        self.delay = delay

    async def __call__(self, sessions, book):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)


@pytest.fixture
async def strategy(monkeypatch):
    """Async so the lock is created inside the loop the tests run in.

    Built in a sync fixture it binds to whatever loop existed then, and the
    first `async with` waits on a loop that is never going to run again.
    """
    from bulkdn import strategy as module

    s = Strategy.__new__(Strategy)
    s.sessions = {}
    s.book = object()
    s._sync_lock = asyncio.Lock()
    s._synced_at = 0.0

    recorder = Recorder()
    monkeypatch.setattr(module, "sync_positions", recorder)
    s.recorder = recorder
    return s


async def test_a_burst_of_callers_makes_one_read(strategy):
    """The case from the log: supervisor and both legs, at once."""
    await asyncio.gather(*(strategy._sync_positions() for _ in range(3)))
    assert strategy.recorder.calls == 1


async def test_a_second_read_inside_the_window_is_skipped(strategy):
    await strategy._sync_positions()
    await strategy._sync_positions()
    assert strategy.recorder.calls == 1


async def test_a_read_after_the_window_goes_to_the_exchange(strategy):
    await strategy._sync_positions()
    strategy._synced_at -= POSITION_FRESHNESS_S * 2
    await strategy._sync_positions()
    assert strategy.recorder.calls == 2


async def test_a_caller_can_demand_a_fresher_read(strategy):
    """Nothing is forced to accept the shared answer if it needs its own."""
    await strategy._sync_positions()
    await strategy._sync_positions(max_age_s=0.0)
    assert strategy.recorder.calls == 2


async def test_callers_wait_for_the_read_rather_than_racing_past_it(strategy):
    """A caller must not act on the old book while a read is still in flight."""
    strategy.recorder.delay = 0.05

    started = asyncio.get_running_loop().time()
    await asyncio.gather(*(strategy._sync_positions() for _ in range(3)))
    elapsed = asyncio.get_running_loop().time() - started

    assert strategy.recorder.calls == 1
    assert elapsed >= 0.05, "someone returned before the read finished"


async def test_a_failed_read_is_not_recorded_as_fresh(strategy):
    """Otherwise one failure would silence the next half second of reads."""
    from bulkdn import strategy as module

    async def boom(sessions, book):
        raise RuntimeError("exchange down")

    saved = module.sync_positions
    module.sync_positions = boom
    try:
        with pytest.raises(RuntimeError):
            await strategy._sync_positions()
    finally:
        module.sync_positions = saved

    await strategy._sync_positions()
    assert strategy.recorder.calls == 1, "the failure left a fresh timestamp"

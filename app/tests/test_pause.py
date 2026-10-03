"""Holding back new groups in a fast market or at set hours.

Same settings, a day apart: $4.65 per $100k on a quiet weekday, $8.50 through
the Sunday-night CME open. The cost is the price moving between a maker fill
and its hedge, and a fast market is where that happens.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from bulkdn.config import ConfigError, _pause_from_dict
from bulkdn.pause import MoveTracker, PauseConfig, PauseGate, Window

from test_dispatcher import BTC, strategy


def utc(day, hour, minute=0):
    # 2026-09-28 is a Monday; `day` counts from it, and may be negative.
    return datetime(2026, 9, 28, hour, minute, tzinfo=timezone.utc) + timedelta(days=day)


MON, TUE, WED, SAT, SUN = 0, 1, 2, 5, 6


# -- the schedule ------------------------------------------------------------


def test_a_window_past_midnight_belongs_to_the_day_it_starts():
    w = Window.parse("sun 22:00-02:00")
    assert w.covers(utc(SUN - 7, 23))           # Sunday 27.09, 23:00
    assert w.covers(utc(MON, 1, 59))            # Monday small hours
    assert not w.covers(utc(MON, 2, 0)), "the end is not inside"
    assert not w.covers(utc(SAT - 7, 23)), "Saturday night is not Sunday night"
    assert not w.covers(utc(TUE, 1)), "only the night after a listed day"


def test_a_weekday_range():
    w = Window.parse("mon-fri 13:20-14:30")
    assert w.covers(utc(WED, 13, 30))
    assert not w.covers(utc(SAT, 13, 30))
    assert not w.covers(utc(WED, 14, 30))


def test_a_range_can_wrap_the_week():
    w = Window.parse("fri-mon 10:00-11:00")
    assert {d for d in range(7) if w.covers(utc(d, 10, 30))} == {MON, 4, SAT, SUN}


def test_daily_and_a_bare_time_range():
    assert Window.parse("daily 00:00-24:00").covers(utc(WED, 23, 59))
    assert Window.parse("13:00-14:00").covers(utc(SAT, 13, 5))


@pytest.mark.parametrize("bad", ["someday 10:00-11:00", "mon 25:00-26:00", "mon 10-11", "a b c"])
def test_a_schedule_entry_that_does_not_parse_is_refused(bad):
    with pytest.raises(ConfigError, match="pause.schedule"):
        _pause_from_dict({"schedule": [bad]})


# -- the movement window -----------------------------------------------------


def test_the_move_is_the_range_over_the_window_only():
    t = MoveTracker(window_s=60)
    t.add(100.0, 0)
    t.add(101.0, 30)                # +100bps inside the window
    assert t.move_bps() == pytest.approx(100.0)
    t.add(101.0, 100)               # the 100.0 point has aged out
    assert t.move_bps() == pytest.approx(0.0)


class Clock:
    def __init__(self):
        self.now = 1_790_000_000.0   # a Wednesday afternoon UTC

    def __call__(self):
        return self.now


def gate(**kwargs):
    clock = Clock()
    return PauseGate(PauseConfig(**kwargs), clock=clock), clock


def test_a_fast_move_pauses_and_says_so_once():
    g, clock = gate(max_move_bps=30, window_minutes=15, calm_minutes=10)
    assert g.update(84_000) is None
    clock.now += 60
    change = g.update(84_300)        # +35.7bps
    assert change and change.paused and "moved 36bps" in change.reason
    clock.now += 60
    assert g.update(84_300) is None, "still paused: nothing new to announce"
    assert g.paused


def test_it_lifts_only_after_the_calm_period():
    g, clock = gate(max_move_bps=30, window_minutes=1, calm_minutes=10)
    g.update(84_000)
    clock.now += 30
    g.update(84_300)
    assert g.paused
    clock.now += 120                 # the move has left the 1-minute window...
    g.update(84_300)
    assert g.paused, "...but ten calm minutes have not passed"
    clock.now += 9 * 60
    change = g.update(84_300)
    assert change and not change.paused
    assert not g.paused


def test_a_quiet_market_never_pauses():
    g, clock = gate(max_move_bps=30)
    for _ in range(100):
        clock.now += 10
        assert g.update(84_000 + (_ % 3)) is None
    assert not g.paused


def test_a_scheduled_window_pauses_with_no_movement_set():
    g, clock = gate(schedule=[Window.parse("daily 00:00-24:00")])
    change = g.update(84_000)
    assert change and change.paused and "scheduled" in change.reason


def test_off_unless_asked_for():
    assert not PauseConfig().enabled
    assert not _pause_from_dict({}).enabled


def test_a_negative_limit_is_refused():
    from bulkdn.config import Config, LegConfig

    config = Config(
        markets=[LegConfig(symbol=BTC, size=1.0, max_order_size=1.0)],
        pause=PauseConfig(max_move_bps=-1),
        private_key="x",
    )
    with pytest.raises(ConfigError, match="max_move_bps"):
        config.validate(require_credentials=False)


# -- the dispatcher ----------------------------------------------------------


class Feed:
    def reference_price(self, symbol):
        return 84_000.0


class Notes:
    def __init__(self):
        self.sent = []

    async def send(self, text, prefix=""):
        self.sent.append((prefix, text))

    def send_soon(self, coro):
        asyncio.ensure_future(coro)


def drive(obj, ticks=200):
    async def run():
        task = asyncio.create_task(obj._dispatch_groups({BTC: 1.0}))
        for _ in range(ticks):
            await asyncio.sleep(0)
        obj._stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(run())


def test_no_group_is_drawn_while_paused(tmp_path):
    obj = strategy(tmp_path)
    obj.config.pause = PauseConfig(schedule=[Window.parse("daily 00:00-24:00")])
    obj.feed = Feed()
    obj.notifier = Notes()

    async def run_group(group_id, group, size):
        obj.started.append(group_id)

    obj._run_group = run_group
    drive(obj)

    assert obj.started == []
    assert any("pause" in prefix for prefix, _ in obj.notifier.sent), "nobody was told"


def test_groups_are_drawn_when_nothing_is_paused(tmp_path):
    obj = strategy(tmp_path, max_groups=2)
    obj.config.pause = PauseConfig(max_move_bps=30)   # on, but the market is flat
    obj.feed = Feed()
    obj.notifier = Notes()
    held = asyncio.Event()

    async def run_group(group_id, group, size):
        obj.started.append(group_id)
        await held.wait()

    obj._run_group = run_group

    async def run():
        task = asyncio.create_task(obj._dispatch_groups({BTC: 1.0}))
        for _ in range(500):
            if len(obj.started) >= 2:
                break
            await asyncio.sleep(0)
        obj._stop.set()
        held.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(run())
    assert len(obj.started) == 2
    assert obj.notifier.sent == []


def test_groups_already_trading_are_not_touched_by_a_pause(tmp_path):
    """The pause only stops new draws; a running group finishes its cycle."""
    obj = strategy(tmp_path, max_groups=1)
    obj.feed = Feed()
    obj.notifier = Notes()
    finished = []
    release = asyncio.Event()

    async def run_group(group_id, group, size):
        obj.started.append(group_id)
        await release.wait()
        finished.append(group_id)
        obj.pairing.release(group_id)

    obj._run_group = run_group

    async def run():
        task = asyncio.create_task(obj._dispatch_groups({BTC: 1.0}))
        for _ in range(500):
            if obj.started:
                break
            await asyncio.sleep(0)
        # The pause starts while the group is mid-cycle.
        obj.config.pause = PauseConfig(schedule=[Window.parse("daily 00:00-24:00")])
        for _ in range(50):
            await asyncio.sleep(0)
        release.set()
        for _ in range(200):
            await asyncio.sleep(0)
        obj._stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(run())
    assert finished == obj.started == [obj.started[0]], "it was cut off, or a new one opened"

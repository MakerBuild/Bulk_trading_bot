"""Keeping the book of groups full.

Groups are started and not waited on. A group whose market is slow must not
hold up four others -- the same reason, and the same fix, as two configured
legs that stopped waiting on each other.

The failures worth testing are the quiet ones: a draw that comes back empty is
normal and must not be read as a fault, and a group that ends must not take
its accounts out of circulation with it.
"""

import asyncio
import random

import pytest

from bulkdn.config import Config, LegConfig, RiskConfig
from bulkdn.pairing import Pairing
from bulkdn.state import StateStore, StrategyState
from bulkdn.strategy import Strategy

BTC = "BTC-USD"


def strategy(tmp_path, pool_size=8, max_groups=3, cycles_each=None):
    obj = object.__new__(Strategy)
    obj.config = Config(
        markets=[
            LegConfig(symbol=BTC, size=1.0, offset_bps=1.0, max_distance_bps=5.0),
            LegConfig(symbol="ETH-USD", size=1.0, offset_bps=1.0,
                      max_distance_bps=5.0),
        ],
        mode="pool",
        chase_interval_s=0.001,
        risk=RiskConfig(),
        private_key="x",
    )
    obj._groups = {}
    obj._group_ids = {}
    obj._stop = asyncio.Event()
    obj.state = StrategyState()
    obj.store = StateStore(str(tmp_path / "state.json"))
    obj.symbols = [BTC]
    obj._rng = random.Random(11)
    obj.pairing = Pairing(
        pool=[f"acct{n}" for n in range(pool_size)],
        max_groups=max_groups,
        rng=random.Random(3),
    )
    obj._size_for_cycle = lambda symbol, size: size
    obj.started = []

    async def never_reached():
        return None

    obj._target_reached = never_reached
    return obj


def run_until(obj, stop_after):
    """Drive the dispatcher, stopping it once `stop_after` groups have run."""
    async def drive():
        task = asyncio.create_task(obj._dispatch_groups({BTC: 1.0}))
        for _ in range(2000):
            if len(obj.started) >= stop_after:
                break
            await asyncio.sleep(0)
        obj._stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(drive())


# -- it fills the book ------------------------------------------------------


def test_it_starts_groups_up_to_the_cap(tmp_path):
    obj = strategy(tmp_path, max_groups=3)
    held = asyncio.Event()

    async def run_group(group_id, group, size):
        obj.started.append(group_id)
        await held.wait()

    obj._run_group = run_group

    async def drive():
        task = asyncio.create_task(obj._dispatch_groups({BTC: 1.0}))
        for _ in range(2000):
            if len(obj.started) >= 3:
                break
            await asyncio.sleep(0)
        obj._stop.set()
        held.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(drive())
    assert len(obj.started) == 3, "the cap was not the cap"


def test_a_full_book_is_not_a_fault(tmp_path):
    """Every free account already in a group is the normal state of a busy
    run, and the dispatcher waits rather than raising."""
    obj = strategy(tmp_path, pool_size=2, max_groups=5)
    held = asyncio.Event()

    async def run_group(group_id, group, size):
        obj.started.append(group_id)
        await held.wait()

    obj._run_group = run_group

    async def drive():
        task = asyncio.create_task(obj._dispatch_groups({BTC: 1.0}))
        await asyncio.sleep(0.05)
        obj._stop.set()
        held.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(drive())
    assert len(obj.started) == 1, "two accounts make exactly one group"


def test_groups_keep_coming_as_earlier_ones_finish(tmp_path):
    obj = strategy(tmp_path, max_groups=1)

    async def run_group(group_id, group, size):
        obj.started.append(group_id)
        obj.pairing.release(group_id)

    obj._run_group = run_group
    run_until(obj, 5)
    assert len(obj.started) >= 5
    assert len(set(obj.started)) == len(obj.started), "a group id was reused"


# -- and it does not swallow trouble ---------------------------------------


def test_a_group_that_raises_takes_the_run_down(tmp_path):
    """An exception here is the exchange, the network or an invariant, and
    none of those get better by opening more positions."""
    obj = strategy(tmp_path)

    async def run_group(group_id, group, size):
        obj.started.append(group_id)
        raise RuntimeError("exchange said no")

    obj._run_group = run_group

    with pytest.raises(RuntimeError, match="exchange said no"):
        asyncio.run(asyncio.wait_for(obj._dispatch_groups({BTC: 1.0}), timeout=5))


def test_stopping_lets_open_groups_finish_their_cycle(tmp_path):
    """Abandoning a group mid-phase leaves positions with nothing watching."""
    obj = strategy(tmp_path, max_groups=2)
    finished = []
    release = asyncio.Event()

    async def run_group(group_id, group, size):
        obj.started.append(group_id)
        await release.wait()
        finished.append(group_id)
        obj.pairing.release(group_id)

    obj._run_group = run_group

    async def drive():
        task = asyncio.create_task(obj._dispatch_groups({BTC: 1.0}))
        for _ in range(2000):
            if len(obj.started) >= 2:
                break
            await asyncio.sleep(0)
        obj._stop.set()
        await asyncio.sleep(0.01)
        assert not finished, "it gave up on the open groups"
        release.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(drive())
    assert sorted(finished) == sorted(obj.started)


def test_reaching_the_target_stops_drawing(tmp_path):
    obj = strategy(tmp_path, max_groups=5)

    async def reached():
        return "volume target"

    obj._target_reached = reached

    async def run_group(group_id, group, size):
        obj.started.append(group_id)

    obj._run_group = run_group
    asyncio.run(asyncio.wait_for(obj._dispatch_groups({BTC: 1.0}), timeout=5))
    assert obj.started == []

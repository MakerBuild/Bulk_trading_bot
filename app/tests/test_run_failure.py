"""A run that fails, or loses a watcher, does not leave orders behind.

Only `Halted` and a cancellation used to pull resting orders on the way out.
Any other exception left through `finally`, which stops the hedge worker and
cancels nothing -- every other group's order stayed on the book, and a fill on
one landed with no hedge coming. And the supervisor and the hedge worker were
never awaited at all: either could die and trading carried on without it.
"""

import asyncio
import types

import pytest

from bulkdn import strategy as strategy_mod
from bulkdn.strategy import Strategy


def bare():
    obj = object.__new__(Strategy)
    obj._stop = asyncio.Event()
    return obj


async def forever():
    await asyncio.Event().wait()


async def test_a_supervisor_that_dies_fails_the_run():
    obj = bare()

    async def boom():
        raise RuntimeError("HTTP 429")

    leg = asyncio.create_task(forever())
    with pytest.raises(RuntimeError, match="risk supervisor died"):
        await obj._until_legs_finish(
            [leg], asyncio.create_task(boom()), asyncio.create_task(forever())
        )
    leg.cancel()


async def test_a_worker_that_quits_unasked_fails_the_run():
    obj = bare()

    async def quits():
        return None

    leg = asyncio.create_task(forever())
    with pytest.raises(RuntimeError, match="hedge worker stopped on its own"):
        await obj._until_legs_finish(
            [leg], asyncio.create_task(forever()), asyncio.create_task(quits())
        )
    leg.cancel()


async def test_a_supervisor_that_returns_after_a_halt_is_doing_its_job():
    obj = bare()

    async def halts():
        obj._stop.set()

    async def leg_body():
        await obj._stop.wait()

    await obj._until_legs_finish(
        [asyncio.create_task(leg_body())],
        asyncio.create_task(halts()),
        asyncio.create_task(forever()),
    )


async def test_a_leg_that_raises_still_raises():
    obj = bare()

    async def boom():
        raise ValueError("group failed")

    with pytest.raises(ValueError, match="group failed"):
        await obj._until_legs_finish(
            [asyncio.create_task(boom())],
            asyncio.create_task(forever()),
            asyncio.create_task(forever()),
        )


async def test_any_failure_cancels_every_resting_order(monkeypatch):
    obj = bare()
    obj.config = types.SimpleNamespace(active_legs=[])
    obj.symbols = ["BTC-USD"]
    obj.sessions = {"a": object()}
    obj._stop_requested = None
    obj._halt_reason = None
    obj.install_handlers = lambda: None
    obj._hedge_worker = forever
    obj._supervise = forever
    obj._refresh_status = forever
    obj._log_open_positions = lambda: None

    async def recover():
        raise RuntimeError("HTTP 504 in a residual sweep")

    obj._recover = recover
    cancelled = []

    async def cancel_all(sessions, symbols):
        cancelled.append(list(symbols))

    monkeypatch.setattr(strategy_mod, "cancel_all_orders", cancel_all)

    with pytest.raises(RuntimeError, match="504"):
        await obj.run()

    assert cancelled == [["BTC-USD"]], "resting orders were left on the book"

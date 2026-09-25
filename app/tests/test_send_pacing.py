"""Keeping bursts short once each submission stops costing a round trip.

Far from the exchange every submission held its caller ~320ms, and that alone kept a
burst of partial fills down to one or two hedges. Close to the exchange the
same burst becomes one hedge per fill, each with its own cancel -- and a chase
re-price queued behind them all.
"""

import asyncio
import time
import types

import pytest

from bulkdn import accounts
from bulkdn.accounts import SendPacer
from bulkdn.strategy import Strategy


# -- a chase re-price yields to a busy socket --------------------------------


async def test_a_quiet_socket_does_not_hold_a_re_price():
    pacer = SendPacer()
    for _ in range(accounts.CHASE_SENDS_PER_S - 1):
        pacer.note()

    started = time.monotonic()
    await pacer.room_for_chase()
    assert time.monotonic() - started < 0.05


async def test_a_busy_socket_holds_a_re_price_until_it_quietens(monkeypatch):
    monkeypatch.setattr(accounts, "CHASE_SENDS_PER_S", 3)
    pacer = SendPacer()
    for _ in range(3):
        pacer.note()

    started = time.monotonic()
    await pacer.room_for_chase()
    waited = time.monotonic() - started

    assert waited >= 0.9, "it re-priced into a socket already at the limit"


async def test_the_hold_is_bounded(monkeypatch):
    """The limit is a guess at the exchange's. An order left at a stale price
    for good costs more than one submission over it."""
    monkeypatch.setattr(accounts, "CHASE_SENDS_PER_S", 1)
    monkeypatch.setattr(accounts, "CHASE_YIELD_MAX_S", 0.1)
    pacer = SendPacer()

    async def keep_busy():
        for _ in range(20):
            pacer.note()
            await asyncio.sleep(0.01)

    busy = asyncio.create_task(keep_busy())
    await asyncio.sleep(0)
    started = time.monotonic()
    await pacer.room_for_chase()
    waited = time.monotonic() - started
    await busy
    assert waited < 0.2, "a re-price was held past its bound"


async def test_hedges_and_cancels_count_but_never_wait(monkeypatch):
    """Only `place_limit` asks for room. Everything else is only counted."""
    monkeypatch.setattr(accounts, "CHASE_SENDS_PER_S", 1)
    sent = []

    class Client:
        async def submit(self, actions, **kw):
            sent.append(actions)
            return []

    session = accounts.AccountSession(
        name="m", pubkey="m-KEY", client=Client(), http=None,
    )
    started = time.monotonic()
    for _ in range(5):
        await session.submit([types.SimpleNamespace(symbol="BTC-USD")])
    assert time.monotonic() - started < 0.05
    assert len(sent) == 5
    assert accounts.pacer_for(session.client)._recent(time.monotonic()) == 5


def test_subaccounts_on_one_socket_share_its_count():
    client = object()
    assert accounts.pacer_for(client) is accounts.pacer_for(client)


# -- a burst of fills is hedged together --------------------------------------


@pytest.fixture
def hedging(monkeypatch):
    """A Strategy with just enough to run one leg's hedge loop."""
    import bulkdn.strategy as module

    monkeypatch.setattr(module, "HEDGE_COALESCE_S", 0.3)
    s = Strategy.__new__(Strategy)
    s._closing_out = False
    s._halt_reason = None
    s._book_suspect = False
    s._stop = asyncio.Event()
    s.passes = []
    s._roles_for_key = lambda key: object()

    async def hedge_leg(roles):
        s.passes.append(time.monotonic())
        await asyncio.sleep(0)

    s._hedge_leg = hedge_leg
    return s


async def test_the_first_hedge_after_a_fill_is_not_delayed(hedging):
    started = time.monotonic()
    await hedging._hedge_until_quiet("g1", set())
    assert len(hedging.passes) == 1
    assert hedging.passes[0] - started < 0.02


async def test_fills_arriving_meanwhile_are_gathered_into_one_more_pass(hedging):
    again = set()

    async def fills():
        # Ten partial fills, well inside the window even on Windows' ~15ms
        # sleep granularity.
        for _ in range(10):
            again.add("g1")
            await asyncio.sleep(0.003)

    burst = asyncio.create_task(fills())
    await hedging._hedge_until_quiet("g1", again)
    await burst

    assert len(hedging.passes) == 2, (
        f"{len(hedging.passes)} hedge passes for one burst of fills"
    )
    assert hedging.passes[1] - hedging.passes[0] >= 0.29

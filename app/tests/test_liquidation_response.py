"""From a position that shrank to the response, through the real handlers.

The guard's decision is tested piece by piece in test_false_liquidation.py,
against a stand-in that is handed its events. That never showed what went
wrong in between: the position handler noticed the shrink and flagged it, the
worker asked the guard again, and the guard -- which had already lowered its
high-water mark when it answered the handler -- had nothing left to say. A
liquidation seen over the socket was detected and then dropped.

These drive the real Strategy from a position update to the closes.
"""

import asyncio
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_strategy import BTC, MASTER, SUB1, build  # noqa: E402

from bulkdn import strategy as strategy_mod  # noqa: E402
from bulkdn.pairing import Group  # noqa: E402
from bulkdn.state import Phase  # noqa: E402


class Update:
    def __init__(self, symbol, size):
        self.symbol = symbol
        self.size = size


def risk_event(symbol=BTC):
    return {
        "symbol": symbol,
        "eventType": "liquidation",
        "reason": "equity below maintenance margin",
        "timestamp": time.time() * 1e3,
    }


def equip(session):
    """What the guard asks of an account, on test_strategy's FakeSession."""
    session.unconfirmed = {}
    session.closes = []
    session.cancels = []

    def symbols_in_doubt(within_s):
        now = time.monotonic()
        return {s for s, at in session.unconfirmed.items() if now - at <= within_s}

    def settled(symbol):
        session.unconfirmed.pop(symbol, None)

    async def close_market(symbol, is_buy, size):
        session.closes.append((symbol, is_buy, size))

    async def cancel_all(symbols):
        session.cancels.append(tuple(symbols))
        return []

    session.symbols_in_doubt = symbols_in_doubt
    session.settled = settled
    session.close_market = close_market
    session.cancel_all = cancel_all
    return session


def holding_pair(tmp_path, monkeypatch, *, liquidated=True):
    """A real Strategy with one group in HOLD: master long, sub1 short."""
    strategy, book, master, sub1 = build(tmp_path)
    equip(master)
    equip(sub1)
    key = strategy.group_key(1, BTC)
    strategy._groups[key] = Group(BTC, maker=MASTER, takers=(SUB1,), shares=(1.0,))
    strategy._group_ids[key] = 1
    strategy.state.leg(key, BTC).phase = Phase.HOLD
    strategy.notifier = types.SimpleNamespace(
        send_soon=lambda coro: getattr(coro, "close", lambda: None)(),
        halted=lambda reason: reason,
    )
    strategy.title = types.SimpleNamespace(halted=lambda reason: None)
    strategy._persist = lambda: None

    async def no_read(max_age_s=0.0):
        return None

    strategy._sync_positions = no_read
    monkeypatch.setattr(
        strategy_mod, "recent_liquidations",
        lambda http_url, user, **kw: [risk_event()] if liquidated else [],
    )
    monkeypatch.setattr(strategy_mod, "SHRINK_RECHECK_DELAYS_S", (0.0, 0.0))

    handlers = {
        MASTER: strategy._make_position_handler(master),
        SUB1: strategy._make_position_handler(sub1),
    }
    handlers[MASTER](Update(BTC, 0.5))
    handlers[SUB1](Update(BTC, -0.5))
    return strategy, book, master, sub1, handlers


async def until_stopped(strategy, worker, timeout=3.0):
    try:
        await asyncio.wait_for(strategy._stop.wait(), timeout)
    except TimeoutError:
        pass                    # the assertions after say what did not happen
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


# -- a liquidation seen over the socket is answered --------------------------


async def test_a_liquidation_the_position_handler_sees_is_answered_by_the_worker(
    tmp_path, monkeypatch
):
    strategy, book, master, sub1, handlers = holding_pair(tmp_path, monkeypatch)

    handlers[MASTER](Update(BTC, 0.0))            # liquidated, over the socket
    assert strategy._liquidation_seen.is_set(), "the handler did not see it"

    worker = asyncio.create_task(strategy._hedge_worker())
    await until_stopped(strategy, worker)

    assert strategy._halt_reason is not None, "the liquidation was dropped"
    assert "liquidation" in strategy._halt_reason
    assert sub1.closes == [(BTC, True, 0.5)], "the surviving short was left open"


def test_the_guard_keeps_reporting_a_shrink_until_it_is_acknowledged():
    """`check` answering the handler must not use the event up."""
    from bulkdn.liquidation import LiquidationGuard
    from bulkdn.marketdata import MarketSpec
    from bulkdn.positions import PositionBook

    spec = MarketSpec(BTC, tick_size=0.5, lot_size=0.001, min_notional=1.0)
    guard = LiquidationGuard(specs={BTC: spec})
    book = PositionBook()
    book.set_authoritative(MASTER, BTC, 0.5)
    guard.check(book, Phase.HOLD, [MASTER])

    book.set_authoritative(MASTER, BTC, 0.0)
    first = guard.check(book, Phase.HOLD, [MASTER])
    second = guard.check(book, Phase.HOLD, [MASTER])

    assert first and first == second, "the second look found nothing"
    guard.acknowledge(first)
    assert guard.check(book, Phase.HOLD, [MASTER]) == []


async def test_the_worker_and_the_supervisor_do_not_both_answer_one_shrink(
    tmp_path, monkeypatch
):
    """Both call the guard, and a look no longer uses the event up -- so
    without one response at a time, the survivor was closed twice."""
    strategy, book, master, sub1, handlers = holding_pair(tmp_path, monkeypatch)
    handlers[MASTER](Update(BTC, 0.0))

    phases = strategy._phases_by_account()
    await asyncio.gather(
        strategy._guard_liquidation(phases), strategy._guard_liquidation(phases)
    )

    assert sub1.closes == [(BTC, True, 0.5)]

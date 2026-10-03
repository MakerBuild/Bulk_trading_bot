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


# -- the response holds the broken group, and only that group ----------------
#
# It ran inline in the hedge worker: every group's hedges waited behind its
# reads and re-reads -- seconds -- and then behind a cancel-all sent to every
# account in the pool, one after another, before the first close went out.


def two_groups(tmp_path, monkeypatch):
    from test_strategy import FakeFeed, FakeSession, make_config

    from bulkdn.hedger import HedgeResult
    from bulkdn.positions import PositionBook
    from bulkdn.state import StateStore, StrategyState
    from bulkdn.strategy import Strategy

    accounts = [equip(FakeSession(name, f"{name}-KEY")) for name in ("a", "b", "c", "d")]
    a, b, c, d = accounts
    book = PositionBook(overlay_ttl_ms=5000)
    hedged = []

    class Hedger:
        def actionable_hedge(self, roles, price=None):
            return 0.0

        async def hedge(self, roles, mark_price=None, suspended=None):
            if suspended is not None and suspended():
                return HedgeResult(roles.symbol, 0.0, 0.0, False, "suspended")
            hedged.append(roles.key)
            return HedgeResult(roles.symbol, 0.0, 0.0, False, "flat")

    strategy = Strategy(
        config=make_config(), master=a, sub1=b, feed=FakeFeed(), book=book,
        hedger=Hedger(), chaser=None, risk=None,
        store=StateStore(str(tmp_path / "state.json")),
        state=StrategyState(), sessions=accounts,
    )
    strategy.pairing = object()
    for group_id, (maker, taker) in ((1, (a, b)), (2, (c, d))):
        key = strategy.group_key(group_id, BTC)
        strategy._groups[key] = Group(
            BTC, maker=maker.pubkey, takers=(taker.pubkey,), shares=(1.0,)
        )
        strategy.state.leg(key, BTC).phase = Phase.HOLD
        book.set_authoritative(maker.pubkey, BTC, 0.5)
        book.set_authoritative(taker.pubkey, BTC, -0.5)
    strategy.notifier = types.SimpleNamespace(
        send_soon=lambda coro: getattr(coro, "close", lambda: None)(),
        halted=lambda reason: reason,
    )
    strategy.title = types.SimpleNamespace(halted=lambda reason: None)
    strategy._persist = lambda: None

    async def no_read(max_age_s=0.0):
        return None

    strategy._sync_positions = no_read
    strategy.guard.check(book, strategy._phases_by_account(), [s.pubkey for s in accounts])
    return strategy, book, accounts, hedged


async def test_other_groups_hedge_while_the_guard_makes_up_its_mind(tmp_path, monkeypatch):
    strategy, book, (a, b, c, d), hedged = two_groups(tmp_path, monkeypatch)
    deciding, decide = asyncio.Event(), asyncio.Event()

    async def slow_answer(events):
        deciding.set()
        await decide.wait()
        return True

    strategy._liquidation_confirmed = slow_answer
    book.set_authoritative(b.pubkey, BTC, 0.0)        # group 1's hedger is wiped
    strategy._make_position_handler(b)(Update(BTC, 0.0))

    worker = asyncio.create_task(strategy._hedge_worker())
    await asyncio.wait_for(deciding.wait(), 3)
    strategy._hedge_queue.put_nowait("g1:BTC-USD")
    strategy._hedge_queue.put_nowait("g2:BTC-USD")
    for _ in range(50):
        if hedged:
            break
        await asyncio.sleep(0.01)

    assert hedged == ["g2:BTC-USD"], "the guard held every group, or none"

    decide.set()
    await until_stopped(strategy, worker)

    assert strategy._halt_reason is not None
    assert a.closes == [(BTC, False, 0.5)], "the survivor was left open"
    assert c.closes == [] and d.closes == [], "another group was closed by the guard"
    assert a.cancels and b.cancels
    assert c.cancels == [] and d.cancels == [], "every account was cancelled first"


async def test_a_held_group_is_hedged_once_the_guard_lets_it_go(tmp_path, monkeypatch):
    strategy, book, (a, b, c, d), hedged = two_groups(tmp_path, monkeypatch)
    deciding, decide = asyncio.Event(), asyncio.Event()

    async def settled(events):
        deciding.set()
        await decide.wait()
        book.set_authoritative(b.pubkey, BTC, -0.5)   # a stale reading after all
        return True

    strategy._settled_by_a_fresh_read = settled
    book.set_authoritative(b.pubkey, BTC, 0.0)
    strategy._make_position_handler(b)(Update(BTC, 0.0))

    worker = asyncio.create_task(strategy._hedge_worker())
    await asyncio.wait_for(deciding.wait(), 3)
    strategy._hedge_queue.put_nowait("g1:BTC-USD")
    await asyncio.sleep(0.05)
    assert "g1:BTC-USD" not in hedged, "hedged while the guard was deciding"

    decide.set()
    for _ in range(100):
        if "g1:BTC-USD" in hedged:
            break
        await asyncio.sleep(0.01)
    strategy._stop.set()
    await asyncio.gather(worker, return_exceptions=True)

    assert "g1:BTC-USD" in hedged, "the held signal was dropped"
    assert strategy._halt_reason is None

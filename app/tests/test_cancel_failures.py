"""A cancel that did not happen is said so, and acted on.

`cancel_all_orders` used to swallow every failure as a warning, and took any
rejection for "nothing to cancel" at debug level. So the callers' own "COULD
NOT CANCEL -- cancel them by hand" line could never fire, an emergency stop
flattened beside orders still working, and a restart wiped the ids of orders
it had failed to pull, leaving them resting with nothing tracking them.
"""

import asyncio
import logging
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from bulkdn import strategy as strategy_mod  # noqa: E402
from bulkdn.accounts import OrderRejected  # noqa: E402
from bulkdn.reconcile import cancel_all_orders  # noqa: E402
from bulkdn.state import Phase  # noqa: E402
from bulkdn.strategy import Strategy  # noqa: E402

BTC = "BTC-USD"


class Account:
    def __init__(self, name, *answers):
        self.name = name
        self.pubkey = f"{name}-KEY"
        self.answers = list(answers)
        self.calls = 0

    async def cancel_all(self, symbols):
        self.calls += 1
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, BaseException):
            raise answer
        return []


def cancel(sessions, **kw):
    return asyncio.run(cancel_all_orders(sessions, [BTC], retry_delay_s=0.0, **kw))


# -- what cancel_all_orders reports ------------------------------------------


def test_a_refused_cancel_is_not_counted_as_nothing_to_cancel():
    refused = Account("m1", OrderRejected("rate limited"), OrderRejected("rate limited"))

    failures = cancel([refused])

    assert [(s.name, type(e)) for s, e in failures] == [("m1", OrderRejected)]


def test_a_cancel_that_fails_once_is_tried_again():
    flaky = Account("m1", TimeoutError())

    assert cancel([flaky]) == []
    assert flaky.calls == 2


def test_one_account_failing_does_not_stop_the_rest():
    bad = Account("m1", TimeoutError(), TimeoutError())
    good = Account("m1s1")

    failures = cancel([bad, good])

    assert [s.name for s, _e in failures] == ["m1"]
    assert good.calls == 1


def test_concurrent_cancels_go_out_together():
    started = []
    release = asyncio.Event()

    class Slow(Account):
        async def cancel_all(self, symbols):
            started.append(self.name)
            await release.wait()
            return []

    async def drive():
        accounts = [Slow("m1"), Slow("m1s1"), Slow("m1s2")]
        task = asyncio.create_task(
            cancel_all_orders(accounts, [BTC], concurrently=True)
        )
        await asyncio.sleep(0.01)
        both_in_flight = list(started)
        release.set()
        return both_in_flight, await task

    in_flight, failures = asyncio.run(drive())
    assert in_flight == ["m1", "m1s1", "m1s2"], "they went one after another"
    assert failures == []


# -- and the run acts on it --------------------------------------------------


def failing_cancel(monkeypatch, *names):
    """Make `cancel_all_orders` fail on the named accounts; record each call."""
    calls = []

    async def fake(sessions, symbols, **kw):
        sessions = list(sessions)
        calls.append([s.name for s in sessions])
        return [(s, TimeoutError()) for s in sessions if s.name in names]

    monkeypatch.setattr(strategy_mod, "cancel_all_orders", fake)
    return calls


def test_a_failed_cancel_is_named_loudly(tmp_path, monkeypatch, caplog):
    from test_strategy import build

    strategy, _book, master, sub1 = build(tmp_path)
    failing_cancel(monkeypatch, "sub1")

    with caplog.at_level(logging.CRITICAL):
        failed = asyncio.run(strategy._cancel_resting([BTC]))

    assert failed == {sub1.pubkey}
    loud = [r for r in caplog.records if "COULD NOT CANCEL" in r.getMessage()]
    assert loud, "nobody was told"
    assert "sub1" in loud[0].getMessage() and "master" not in loud[0].getMessage()
    assert getattr(loud[0], "alert", False) is True


async def test_recovery_keeps_the_ids_of_orders_it_could_not_cancel(tmp_path, monkeypatch):
    from test_strategy import MASTER, SUB1, build

    strategy, _book, master, sub1 = build(tmp_path)
    failing_cancel(monkeypatch, "master")

    async def no_read(sessions, book):
        return None

    async def no_reconcile(hedger, roles, feed):
        return []

    monkeypatch.setattr(strategy_mod, "sync_positions", no_read)
    monkeypatch.setattr(strategy_mod, "reconcile_net", no_reconcile)
    # BTC is rested by the master during an entry, SOL by the sub-account.
    for symbol, oid in (("BTC-USD", "on-master"), ("SOL-USD", "on-sub1")):
        leg = strategy.state.leg(symbol)
        leg.phase = Phase.OPEN
        leg.oid = oid
        leg.stale_oids = [f"older-{oid}"]
    assert strategy._roles_for_key("BTC-USD").maker == MASTER
    assert strategy._roles_for_key("SOL-USD").maker == SUB1

    await strategy._recover()

    kept = strategy.state.legs["BTC-USD"]
    assert kept.oid == "on-master", "an order it failed to pull was forgotten"
    assert kept.stale_oids == ["older-on-master"]
    cleared = strategy.state.legs["SOL-USD"]
    assert cleared.oid is None and cleared.stale_oids == []


async def test_an_emergency_stop_asks_again_after_the_flatten(tmp_path, monkeypatch):
    from test_strategy import build

    strategy, _book, master, sub1 = build(tmp_path)
    calls = failing_cancel(monkeypatch, "sub1")
    flattened = []

    async def flatten(sessions, book, feed, symbols):
        flattened.append(len(calls))

    monkeypatch.setattr(strategy_mod, "flatten", flatten)

    await strategy._emergency_stop("test")

    assert flattened == [1], "the flatten waited on, or skipped, the cancel"
    assert calls == [["master", "sub1"], ["sub1"]], calls


async def test_a_failed_run_names_the_accounts_it_could_not_cancel(monkeypatch, caplog):
    obj = object.__new__(Strategy)
    obj._stop = asyncio.Event()
    obj.config = types.SimpleNamespace(active_legs=[])
    obj.symbols = [BTC]
    account = types.SimpleNamespace(name="m1s3", pubkey="m1s3-KEY")
    obj.sessions = {account.pubkey: account}
    obj._stop_requested = None
    obj._halt_reason = None
    obj.install_handlers = lambda: None

    async def forever():
        await asyncio.Event().wait()

    obj._hedge_worker = forever
    obj._supervise = forever
    obj._refresh_status = forever
    obj._log_open_positions = lambda: None

    async def nothing():
        return None

    obj._last_hedge_pass = nothing

    async def recover():
        raise RuntimeError("HTTP 504")

    obj._recover = recover
    failing_cancel(monkeypatch, "m1s3")

    with caplog.at_level(logging.CRITICAL), pytest.raises(RuntimeError, match="504"):
        await obj.run()

    assert any(
        "COULD NOT CANCEL" in r.getMessage() and "m1s3" in r.getMessage()
        for r in caplog.records
    ), "the failure path's cancel failed in silence"

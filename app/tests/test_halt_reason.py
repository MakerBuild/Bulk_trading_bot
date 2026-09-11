"""What a halt records about itself.

A halt is read long after the run that caused it -- the console it was logged
to has closed, and nothing else is written to disk. The state file is therefore
the only witness, and until this existed it said only how many rejections there
had been, never what any of them was. That is enough to know a kill switch
fired and not enough to know whether it was safe to restart.

The case that motivated it: a halt reading "sub1 has 5 consecutive rejected
transactions", two hours old, with no way left to find out why.
"""

import asyncio
import json

import pytest
from bulk_api.common import OrderStatus

from bulkdn.accounts import AccountSession, OrderRejected
from bulkdn.config import RiskConfig
from bulkdn.feed import MarketFeed
from bulkdn.positions import PositionBook
from bulkdn.risk import RiskMonitor
from bulkdn.state import Phase, StateStore, StrategyState

BTC = "BTC-USD"


class FakeResponse:
    def __init__(self, status, message=""):
        self.status = status
        self.message = message
        self.order_id = None

    def is_error(self):
        return True


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.is_connected = True
        self.last_message_at = 0.0

    async def submit(self, actions, **kwargs):
        return self.responses

    # The risk monitor prices open positions before it looks at streaks. There
    # are none here, but it still asks.
    def get_book(self, symbol):
        return None

    def get_ticker(self, symbol):
        return None


def session_that_rejects(message):
    return AccountSession(
        name="sub1",
        pubkey="4Fy8FQxz3FYUFqvNgCydRPsBiGzWum5yFaxx68LTWBvU",
        client=FakeClient([FakeResponse(OrderStatus.REJECTED_INVALID, message)]),
        http=None,
        dry_run=True,
    )


def reject_five_times(session):
    for _ in range(5):
        with pytest.raises(OrderRejected):
            asyncio.run(session.submit([]))


def monitor_for(session):
    return RiskMonitor(
        config=RiskConfig(max_reject_streak=5),
        book=PositionBook(overlay_ttl_ms=5000),
        feed=MarketFeed(session, [BTC]),
        sessions={session.name: session},
        symbols=[BTC],
    )


def test_the_violation_names_the_exchanges_own_words():
    session = session_that_rejects("insufficient margin")
    reject_five_times(session)

    violations = [v for v in monitor_for(session).check() if v.kind == "reject_streak"]
    assert len(violations) == 1
    detail = violations[0].detail
    assert "5 consecutive" in detail
    assert "insufficient margin" in detail


def test_the_reason_survives_into_the_state_file(tmp_path):
    """End to end: a rejection at the socket, read back off disk."""
    session = session_that_rejects("minNotional not met")
    reject_five_times(session)

    violations = monitor_for(session).check()
    state = StrategyState(phase=Phase.HALTED)
    state.halted_reason = "; ".join(str(v) for v in violations)

    path = tmp_path / "state.json"
    StateStore(str(path)).save(state)

    written = json.loads(path.read_text(encoding="utf-8"))["halted_reason"]
    assert "reject_streak: sub1 has 5 consecutive" in written
    assert "minNotional not met" in written


def test_a_halt_with_no_recorded_reason_still_reads_cleanly():
    """State files written before this existed carry no reason; no dangling dash."""
    session = session_that_rejects("whatever")
    reject_five_times(session)
    session.last_reject = ""

    detail = next(
        v for v in monitor_for(session).check() if v.kind == "reject_streak"
    ).detail
    assert detail.endswith("consecutive rejected transactions")

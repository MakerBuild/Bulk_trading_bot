"""Rejection accounting in AccountSession.submit.

The reject streak feeds the kill switch, so what counts as a fault decides
whether an active market can halt the bot. Resting orders are ALO, and the
exchange refuses one that would take rather than filling it -- that is the
post-only protection doing its job, not a malfunction.
"""

import asyncio

import pytest
from bulk_api.common import OrderStatus

from bulkdn.accounts import AccountSession, OrderRejected


class FakeResponse:
    def __init__(self, status, message="", order_id=None):
        self.status = status
        self.message = message
        self.order_id = order_id

    def is_error(self):
        return self.status in (
            OrderStatus.REJECTED_CROSSING,
            OrderStatus.REJECTED_INVALID,
            OrderStatus.REJECTED_RISKLIMIT,
            OrderStatus.REJECTED_DUPLICATE,
            OrderStatus.ERROR,
        )


class FakeClient:
    def __init__(self, responses):
        self.responses = responses

    async def submit(self, actions, **kwargs):
        return self.responses


def session(responses):
    return AccountSession(
        name="master",
        pubkey="EXAMPLE-MASTER-ACCOUNT-PUBKEY",
        client=FakeClient(responses),
        http=None,
    )


def test_a_clean_submit_clears_the_streak():
    s = session([FakeResponse(OrderStatus.RESTING, order_id="abc")])
    s.reject_streak = 3
    asyncio.run(s.submit([object()]))
    assert s.reject_streak == 0


def test_crossing_rejection_raises_but_does_not_count_as_a_fault():
    """ALO refusing to take is chase feedback; the kill switch must ignore it."""
    s = session([FakeResponse(OrderStatus.REJECTED_CROSSING)])
    with pytest.raises(OrderRejected):
        asyncio.run(s.submit([object()]))
    assert s.reject_streak == 0


def test_repeated_crossings_never_trip_the_streak():
    s = session([FakeResponse(OrderStatus.REJECTED_CROSSING)])
    for _ in range(20):
        with pytest.raises(OrderRejected):
            asyncio.run(s.submit([object()]))
    assert s.reject_streak == 0


def test_a_real_rejection_still_counts():
    s = session([FakeResponse(OrderStatus.REJECTED_RISKLIMIT, "over the cap")])
    with pytest.raises(OrderRejected):
        asyncio.run(s.submit([object()]))
    assert s.reject_streak == 1


def test_a_crossing_alongside_a_real_fault_still_counts():
    """A batch that also failed for a real reason must not be excused."""
    s = session([
        FakeResponse(OrderStatus.REJECTED_CROSSING),
        FakeResponse(OrderStatus.REJECTED_INVALID, "bad tick"),
    ])
    with pytest.raises(OrderRejected):
        asyncio.run(s.submit([object()]))
    assert s.reject_streak == 1


def test_count_rejects_false_leaves_the_streak_alone():
    """Cancels use this: nothing to cancel is not a malfunction."""
    s = session([FakeResponse(OrderStatus.REJECTED_INVALID, "unknown id")])
    s.reject_streak = 2
    with pytest.raises(OrderRejected):
        asyncio.run(s.submit([object()], count_rejects=False))
    assert s.reject_streak == 2


# -- what the halt record will say ------------------------------------------
#
# The streak says a kill switch fired; `last_reject` says why. It is the only
# copy that outlives the run: the console it was logged to is gone by the time
# anyone reads the state file.


def test_a_counted_rejection_records_what_the_exchange_said():
    s = session([FakeResponse(OrderStatus.REJECTED_RISKLIMIT, "over the cap")])
    with pytest.raises(OrderRejected):
        asyncio.run(s.submit([]))
    assert "over the cap" in s.last_reject
    assert str(OrderStatus.REJECTED_RISKLIMIT) in s.last_reject


def test_a_crossing_rejection_alone_records_nothing():
    """It did not raise the streak, so naming it as the cause would mislead."""
    s = session([FakeResponse(OrderStatus.REJECTED_CROSSING, "would take")])
    with pytest.raises(OrderRejected):
        asyncio.run(s.submit([]))
    assert s.last_reject == ""


def test_only_the_counted_rejections_are_recorded():
    s = session([
        FakeResponse(OrderStatus.REJECTED_CROSSING, "would take"),
        FakeResponse(OrderStatus.REJECTED_INVALID, "bad tick"),
    ])
    with pytest.raises(OrderRejected):
        asyncio.run(s.submit([]))
    assert "bad tick" in s.last_reject
    assert "would take" not in s.last_reject


def test_a_clean_submit_clears_the_recorded_reason():
    s = session([FakeResponse(OrderStatus.RESTING, order_id="abc")])
    s.reject_streak = 3
    s.last_reject = "something from before"
    asyncio.run(s.submit([]))
    assert (s.reject_streak, s.last_reject) == (0, "")


def test_a_cancel_rejection_records_nothing():
    """Cancels pass count_rejects=False: nothing to cancel is not a fault."""
    s = session([FakeResponse(OrderStatus.REJECTED_INVALID, "unknown order")])
    with pytest.raises(OrderRejected):
        asyncio.run(s.submit([], count_rejects=False))
    assert s.last_reject == ""

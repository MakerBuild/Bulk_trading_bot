"""An answer that says no, an answer for someone else, and no answer at all.

The SDK resolves every one of them the same way -- a bare `RuntimeError` on the
pending request -- and `AccountSession.submit` read every `RuntimeError` as "no
answer came back". So a key the exchange refused on every order (a bad
signature, a rate limit) never moved the reject streak: the kill switch built
for exactly that never tripped, and the strategy looped place -> reject ->
cancel, each pass asking for a position read that could only say nothing had
happened.

And one socket carries every account under a key, so the SDK's habit of
failing EVERY pending request when an `error` frame arrives would, with
rejections now counted, have charged one account's refusal to all of them.
"""

import asyncio
import json
import logging

import pytest
from bulk_api.api.bulk_ws import ConnectionState
from bulk_api.common import OrderStatus

from bulkdn.accounts import (
    AccountSession,
    OrderRejected,
    RoutedWsClient,
    SubmissionInDoubt,
    TransactionRejected,
)

MASTER = "MASTER-PUB"
SUB1 = "SUB1-PUB"
BTC = "BTC-USD"


class FakeSigner:
    public_key = MASTER

    def sign_transaction(self, tx, domain):
        return dict(tx, signature="sig")


class FakeAction:
    """Just enough of an SDK action for `RoutedWsClient.submit`."""

    def __init__(self, symbol=BTC):
        self.symbol = symbol

    def to_api(self):
        return {"symbol": self.symbol}


class FakeSocket:
    """Records what is sent and lets the test answer it."""

    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(json.loads(text))


def socket_client():
    """A RoutedWsClient wired to a fake socket, built without __init__."""
    client = RoutedWsClient.__new__(RoutedWsClient)
    client.signer = FakeSigner()
    client.signature_domain = None
    client.account_pubkey = MASTER
    client.accounts = [MASTER, SUB1]
    client.dry_run = False
    client.state = ConnectionState.CONNECTED
    client.ws = FakeSocket()
    client.request_id = 0
    client.pending_requests = {}
    client.default_timeout = 5.0
    client.handlers = {}
    client.logger = logging.getLogger("test.sdk")
    return client


def session_on(client, name, pubkey):
    return AccountSession(name=name, pubkey=pubkey, client=client, http=None)


async def answer_when_sent(client, count, reply_for):
    """Wait until `count` requests are on the wire, then answer them."""
    while len(client.ws.sent) < count:
        await asyncio.sleep(0)
    for frame in reply_for(client.ws.sent):
        await client._handle_message(frame)


def refused(request_id, message="bad signature"):
    return {
        "type": "post",
        "id": request_id,
        "data": {"type": "action", "payload": {"status": "error", "message": message}},
    }


# -- a refusal addressed to us is a rejection --------------------------------


async def test_a_refused_transaction_counts_toward_the_kill_switch():
    client = socket_client()
    master = session_on(client, "m1", MASTER)

    answer = asyncio.create_task(
        answer_when_sent(client, 1, lambda sent: [refused(sent[0]["id"])])
    )
    with pytest.raises(TransactionRejected):
        await master.submit([FakeAction()])
    await answer

    assert master.reject_streak == 1, "a refused key never trips the kill switch"
    assert "bad signature" in master.last_reject
    assert master.symbols_in_doubt(60.0) == set(), (
        "a refusal is an answer: nothing was applied, nothing is in doubt"
    )


async def test_it_is_still_an_order_rejection_to_existing_callers():
    """The chaser and the reconciler catch `OrderRejected` as 'the exchange
    said no'; a whole-transaction refusal is the same kind of answer."""
    assert issubclass(TransactionRejected, OrderRejected)
    assert TransactionRejected("x").responses == []


async def test_repeated_refusals_reach_the_streak_the_risk_layer_trips_on():
    client = socket_client()
    master = session_on(client, "m1", MASTER)

    for n in range(1, 6):
        answer = asyncio.create_task(
            answer_when_sent(client, n, lambda sent: [refused(sent[-1]["id"], "bad signature")])
        )
        with pytest.raises(OrderRejected):
            await master.submit([FakeAction()])
        await answer

    assert master.reject_streak == 5


async def test_being_throttled_is_not_counted_toward_the_kill_switch():
    """The streak halts the run and closes every account at market. A burst of
    rate limiting on a shared socket is not a reason to do that."""
    client = socket_client()
    master = session_on(client, "m1", MASTER)

    for n in range(1, 6):
        answer = asyncio.create_task(
            answer_when_sent(client, n, lambda sent: [refused(sent[-1]["id"], "rate limited")])
        )
        with pytest.raises(OrderRejected):
            await master.submit([FakeAction()])
        await answer

    assert master.reject_streak == 0


async def test_a_refused_cancel_is_not_counted():
    """Cancels pass count_rejects=False; that must hold for this path too."""
    client = socket_client()
    master = session_on(client, "m1", MASTER)

    answer = asyncio.create_task(
        answer_when_sent(client, 1, lambda sent: [refused(sent[0]["id"])])
    )
    with pytest.raises(OrderRejected):
        await master.submit([FakeAction()], count_rejects=False)
    await answer
    assert master.reject_streak == 0


async def test_no_answer_at_all_is_still_in_doubt_and_not_a_rejection():
    client = socket_client()
    master = session_on(client, "m1", MASTER)

    with pytest.raises(asyncio.TimeoutError):
        await master.submit([FakeAction()], timeout=0.01)

    assert master.reject_streak == 0
    assert master.symbols_in_doubt(60.0) == {BTC}


# -- an error frame on a shared socket ---------------------------------------


async def test_an_error_naming_a_request_rejects_that_request_only():
    client = socket_client()
    master = session_on(client, "m1", MASTER)
    sub = session_on(client, "m1s1", SUB1)

    def reply(sent):
        mine = next(r["id"] for r in sent if r["request"]["payload"]["account"] == SUB1)
        return [{"type": "error", "id": mine, "error": {"message": "invalid size"}}]

    answer = asyncio.create_task(answer_when_sent(client, 2, reply))
    master_call = asyncio.create_task(master.submit([FakeAction()], timeout=0.2))
    sub_call = asyncio.create_task(sub.submit([FakeAction()], timeout=5.0))
    await answer

    with pytest.raises(TransactionRejected):
        await sub_call
    # The master's request was nothing to do with it, and is still waiting.
    with pytest.raises(asyncio.TimeoutError):
        await master_call
    assert sub.reject_streak == 1
    assert master.reject_streak == 0


async def test_an_error_naming_nobody_is_doubt_for_everyone_and_a_reject_for_none():
    """Whose it was cannot be told, so nobody's kill switch is charged."""
    client = socket_client()
    master = session_on(client, "m1", MASTER)
    sub = session_on(client, "m1s1", SUB1)

    answer = asyncio.create_task(
        answer_when_sent(
            client, 2, lambda sent: [{"type": "error", "error": {"message": "nope"}}]
        )
    )
    calls = [
        asyncio.create_task(master.submit([FakeAction()])),
        asyncio.create_task(sub.submit([FakeAction("ETH-USD")])),
    ]
    await answer
    results = await asyncio.gather(*calls, return_exceptions=True)

    assert all(isinstance(r, SubmissionInDoubt) for r in results), results
    assert not any(isinstance(r, OrderRejected) for r in results)
    assert (master.reject_streak, sub.reject_streak) == (0, 0)
    assert master.symbols_in_doubt(60.0) == {BTC}
    assert sub.symbols_in_doubt(60.0) == {"ETH-USD"}


async def test_an_error_for_a_request_no_longer_waiting_leaves_the_rest_alone():
    client = socket_client()
    master = session_on(client, "m1", MASTER)

    answer = asyncio.create_task(
        answer_when_sent(
            client, 1, lambda sent: [{"type": "error", "id": 999, "error": {"message": "late"}}]
        )
    )
    call = asyncio.create_task(master.submit([FakeAction()], timeout=0.2))
    await answer
    with pytest.raises(asyncio.TimeoutError):
        await call
    assert master.reject_streak == 0


async def test_an_ok_answer_still_reaches_the_sdk_parser():
    client = socket_client()
    master = session_on(client, "m1", MASTER)

    def ok(sent):
        return [{
            "type": "post",
            "id": sent[0]["id"],
            "data": {"type": "action", "payload": {"status": "ok", "response": {
                "type": "order", "data": {"statuses": [{"resting": {"oid": "abc"}}]},
            }}},
        }]

    answer = asyncio.create_task(answer_when_sent(client, 1, ok))
    responses = await master.submit([FakeAction()], timeout=1.0)
    await answer
    assert responses is not None
    assert master.reject_streak == 0


# -- a cancel+replace whose cancel half lost the race ------------------------


class Response:
    def __init__(self, status, order_id=None, message=""):
        self.status = status
        self.order_id = order_id
        self.message = message

    def is_error(self):
        return self.status in (
            OrderStatus.REJECTED_CROSSING,
            OrderStatus.REJECTED_INVALID,
            OrderStatus.CANCEL_REJECT,
        )


class ScriptedClient:
    def __init__(self, respond):
        self.respond = respond

    async def submit(self, actions, **kwargs):
        return self.respond(actions)


# Real base58, because an order id is a hash over the decoded pubkey. The
# sha256 of "bulkdn-example-master", as in test_subaccounts -- nobody's account.
STAMP_PUBKEY = "DqciofFTMjwbGhwi3ox2kqN1F2P3HLYECDSC5hRytcUo"


def stamped(actions):
    """What `RoutedWsClient.submit` does to each action before signing."""
    for index, action in enumerate(actions):
        action.seqno, action.nonce, action.pubkey = index, 1_700_000_000_000_000_000, STAMP_PUBKEY


async def test_a_rejected_cancel_beside_an_accepted_order_says_the_order_is_placed():
    """The old order filled first, so the cancel was refused -- but the new
    one was accepted and is resting. The chaser has to know its id, or it is
    an order on the book that nothing tracks."""

    def respond(actions):
        stamped(actions)
        return [
            Response(OrderStatus.CANCEL_REJECT, "old"),
            Response(OrderStatus.RESTING, actions[1].order_id()),
        ]

    s = AccountSession(name="m1", pubkey=MASTER, client=ScriptedClient(respond), http=None)
    with pytest.raises(OrderRejected) as caught:
        await s.place_limit(BTC, True, 100.0, 0.001, cancel_oid="old")

    assert caught.value.placed is True
    assert caught.value.order_id
    assert caught.value.order_id != "old"


async def test_a_rejected_order_is_not_placed():
    def respond(actions):
        stamped(actions)
        return [
            Response(OrderStatus.CANCELLED, "old"),
            Response(OrderStatus.REJECTED_CROSSING, actions[1].order_id()),
        ]

    s = AccountSession(name="m1", pubkey=MASTER, client=ScriptedClient(respond), http=None)
    with pytest.raises(OrderRejected) as caught:
        await s.place_limit(BTC, True, 100.0, 0.001, cancel_oid="old")
    assert caught.value.placed is False


async def test_a_refused_transaction_placed_nothing():
    class Refusing:
        async def submit(self, actions, **kwargs):
            stamped(actions)
            raise TransactionRejected("transaction refused: bad signature")

    s = AccountSession(name="m1", pubkey=MASTER, client=Refusing(), http=None)
    with pytest.raises(TransactionRejected) as caught:
        await s.place_limit(BTC, True, 100.0, 0.001, cancel_oid="old")
    assert caught.value.placed is False
    assert caught.value.order_id

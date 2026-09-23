"""One socket, many accounts: reconnecting it, subscribing it, attributing it.

Every account under a key shares one client. Three things assumed otherwise:

* each session on a dead socket spent its own full reconnect budget on it,
  one after another -- eleven accounts, eleven budgets;
* a first connect that died between two account subscriptions left the list
  of subscriptions partial, and every later reconnect replayed only that;
* an update naming an account the socket does not carry was believed, so
  every session on it compared, disagreed, and dropped it in silence.
"""

import asyncio
import logging

import pytest

import bulkdn.accounts as accounts_mod
from bulkdn.accounts import AccountSession, RoutedWsClient, _owner_of

MASTER = "MASTER-PUB"
SUB1 = "SUB1-PUB"
SUB2 = "SUB2-PUB"


class DeadClient:
    """A socket that comes back on attempt `succeed_on`, or never (0)."""

    def __init__(self, succeed_on=0, connect_delay=0.0):
        self.succeed_on = succeed_on
        self.connect_delay = connect_delay
        self.attempts = 0
        self.is_connected = False

    async def connect(self):
        self.attempts += 1
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        self.is_connected = bool(self.succeed_on) and self.attempts >= self.succeed_on
        return self.is_connected

    async def disconnect(self):
        self.is_connected = False


def sessions_on(client, *pubkeys):
    return [
        AccountSession(name=pubkey, pubkey=pubkey, client=client, http=None)
        for pubkey in pubkeys
    ]


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """The backoff is real seconds; the sharing is what is under test."""
    original = asyncio.sleep

    async def fast(seconds, *args, **kwargs):
        await original(0)

    monkeypatch.setattr(accounts_mod.asyncio, "sleep", fast)


# -- one budget per socket ---------------------------------------------------


async def test_sessions_healed_together_share_one_set_of_attempts():
    client = DeadClient(succeed_on=0, connect_delay=0.001)
    sessions = sessions_on(client, MASTER, SUB1, SUB2)

    results = await asyncio.gather(*(s.reconnect(attempts=6) for s in sessions))

    assert results == [False, False, False]
    assert client.attempts == 6, f"{client.attempts} attempts: the budget multiplied"


async def test_a_sibling_asking_right_after_a_failure_does_not_start_again():
    """How the supervisor actually calls it: one session after another."""
    client = DeadClient(succeed_on=0)
    master, sub1, sub2 = sessions_on(client, MASTER, SUB1, SUB2)

    assert await master.reconnect(attempts=6) is False
    assert await sub1.reconnect(attempts=6) is False
    assert await sub2.reconnect(attempts=6) is False
    assert client.attempts == 6


async def test_another_socket_coming_back_earns_a_retry():
    """The supervisor retries failed sockets once any other returns -- proof
    the network did. A shared failure must not refuse that."""
    dead = DeadClient(succeed_on=7)   # fails its first six, then would succeed
    alive = DeadClient(succeed_on=1)
    (master,) = sessions_on(dead, MASTER)
    (other,) = sessions_on(alive, "OTHER-KEY")

    assert await master.reconnect(attempts=6) is False
    assert await other.reconnect(attempts=6) is True
    assert await master.reconnect(attempts=6) is True
    assert dead.attempts == 7


async def test_a_sibling_does_not_tear_down_the_socket_just_restored():
    client = DeadClient(succeed_on=1)
    master, sub1 = sessions_on(client, MASTER, SUB1)

    assert await master.reconnect() is True
    assert await sub1.reconnect() is True
    assert client.attempts == 1, "the second session reconnected it again"


async def test_the_verdict_expires():
    client = DeadClient(succeed_on=0)
    master, sub1 = sessions_on(client, MASTER, SUB1)

    assert await master.reconnect(attempts=2) is False
    ok, generation, at = client._bulkdn_reconnect_outcome
    client._bulkdn_reconnect_outcome = (ok, generation, at - accounts_mod.RECONNECT_SHARE_S - 1)

    assert await sub1.reconnect(attempts=2) is False
    assert client.attempts == 4, "a later heal must try again"


async def test_one_waiter_cancelled_does_not_cancel_it_for_the_rest():
    client = DeadClient(succeed_on=3, connect_delay=0.001)
    master, sub1 = sessions_on(client, MASTER, SUB1)

    first = asyncio.create_task(master.reconnect(attempts=6))
    second = asyncio.create_task(sub1.reconnect(attempts=6))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    first.cancel()

    assert await second is True
    with pytest.raises(asyncio.CancelledError):
        await first


# -- subscribing a socket ------------------------------------------------------


def bare_client(accounts, symbols=("BTC-USD",)):
    client = RoutedWsClient.__new__(RoutedWsClient)
    client.subscriptions = []
    client.accounts = list(accounts)
    client.symbols = list(symbols)
    client.signer = object()
    client.last_message_at = 0.0
    return client


async def test_the_first_connect_records_every_subscription_before_sending(monkeypatch):
    from bulk_api import BulkWebSocketClient

    client = bare_client([MASTER, SUB1, SUB2])
    seen_at_connect = []

    async def base_connect(self):
        # The base class replays `subscriptions` when there are any, and
        # subscribes the SIGNER's account when there are none.
        seen_at_connect.append([s.to_dict() for s in self.subscriptions])
        return False  # dies partway

    monkeypatch.setattr(BulkWebSocketClient, "connect", base_connect)
    assert await client.connect() is False

    wanted = [
        {"type": "account", "user": MASTER},
        {"type": "account", "user": SUB1},
        {"type": "account", "user": SUB2},
        {"type": "ticker", "symbol": "BTC-USD"},
    ]
    assert seen_at_connect == [wanted]
    # And a failed first connect leaves the whole set for the next to replay.
    assert [s.to_dict() for s in client.subscriptions] == wanted


async def test_a_reconnect_replays_rather_than_rebuilding(monkeypatch):
    """Book subscriptions added after the first connect must survive."""
    from bulk_api import BulkWebSocketClient
    from bulk_api.messages import SubscriptionRequest

    client = bare_client([MASTER])
    book = SubscriptionRequest("l2Snapshot", {"symbol": "BTC-USD"})
    client.subscriptions = [SubscriptionRequest("account", {"user": MASTER}), book]

    async def base_connect(self):
        return True

    monkeypatch.setattr(BulkWebSocketClient, "connect", base_connect)
    assert await client.connect() is True
    assert book in client.subscriptions


# -- who an update is about --------------------------------------------------


def test_a_payload_naming_a_stranger_is_not_believed():
    message = {"type": "account", "data": {"user": "SOMEONE-ELSE"}}
    assert _owner_of(message, [MASTER, SUB1]) is None


def test_a_payload_naming_one_of_ours_still_is():
    message = {"type": "account", "data": {"account": SUB1}}
    assert _owner_of(message, [MASTER, SUB1]) == SUB1


async def test_a_stranger_is_logged_and_rate_limited(monkeypatch, caplog):
    from bulk_api import BulkWebSocketClient

    client = bare_client([MASTER, SUB1])
    client._dispatch_owner = None
    client._owner_warned = False
    client._unknown_owner_warned_at = None
    owners = []

    async def base_handle(self, data):
        owners.append(self.message_owner)

    monkeypatch.setattr(BulkWebSocketClient, "_handle_message", base_handle)
    message = {"type": "account", "data": {"u": "SOMEONE-ELSE-ENTIRELY"}}

    with caplog.at_level(logging.WARNING, logger="bulkdn.accounts"):
        await client._handle_message(message)
        await client._handle_message(message)

    assert owners == [None, None], "it was attributed to an account it names wrongly"
    warned = [r for r in caplog.records if "not one of the" in r.getMessage()]
    assert len(warned) == 1, "one warning per interval, not one per frame"

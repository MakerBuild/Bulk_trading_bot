"""Taking positions down when the thing that broke is the socket.

An emergency stop fires exactly when something is wrong, and it is often a
socket. Closes went over the socket alone, and a flatten stopped at the first
account it could not read -- either way one side of a pair could close while
the other stayed open, a lone leg with nothing left to manage it.
"""

import asyncio
import types

import pytest

from bulkdn import accounts, reconcile
from bulkdn.accounts import OrderRejected
from bulkdn.marketdata import MarketSpec
from bulkdn.positions import PositionBook

BTC = "BTC-USD"
SPEC = MarketSpec(BTC, tick_size=0.5, lot_size=0.001, min_notional=1.0)


class Response:
    status_code = 200

    @staticmethod
    def json():
        return {"status": "ok"}


@pytest.fixture
def http_posts(monkeypatch):
    sent = []

    def post(url, json, timeout):
        sent.append((url, json))
        return Response(), False

    monkeypatch.setattr(accounts, "post_signed", post)
    return sent


def session_with(client):
    return accounts.AccountSession(
        name="m1s4", pubkey="SUB", client=client,
        http=types.SimpleNamespace(base_url="https://api.example"),
    )


def signed(actions, account):
    (order,) = actions
    return {"account": account, "reduce_only": order.reduce_only, "size": order.size}


# -- a close finds another way out --------------------------------------------


async def test_a_close_goes_over_http_when_the_socket_is_down(http_posts):
    client = types.SimpleNamespace(is_connected=False, signed_transaction=signed)
    await session_with(client).close_market(BTC, is_buy=False, size=0.01)

    assert http_posts == [(
        "https://api.example/order",
        {"account": "SUB", "reduce_only": True, "size": 0.01},
    )]


async def test_a_close_the_socket_never_answered_is_resent_over_http(http_posts):
    """It may have executed. A second reduce-only close can reach flat, never
    past it -- which is what makes sending it again allowed."""
    async def stalled(*a, **k):
        raise asyncio.TimeoutError()

    client = types.SimpleNamespace(
        is_connected=True, submit=stalled, signed_transaction=signed,
    )
    await session_with(client).close_market(BTC, is_buy=True, size=0.02)

    assert [body for _, body in http_posts] == [
        {"account": "SUB", "reduce_only": True, "size": 0.02}
    ]


async def test_a_close_the_exchange_refused_is_not_resent(http_posts):
    """A refusal is an answer: nothing was applied, and the caller's next read
    decides what is left."""
    async def refused(*a, **k):
        raise OrderRejected("m1s4: rejected")

    client = types.SimpleNamespace(
        is_connected=True, submit=refused, signed_transaction=signed,
    )
    with pytest.raises(OrderRejected):
        await session_with(client).close_market(BTC, is_buy=True, size=0.02)
    assert http_posts == []


# -- one unreadable account does not stop the rest ----------------------------


class FakeSession:
    def __init__(self, name, fails=False):
        self.name = name
        self.pubkey = f"{name}-KEY"
        self.fails = fails
        self.closes = []

    def full_account(self):
        if self.fails:
            raise TimeoutError("429")
        return {"positions": []}

    async def close_market(self, symbol, is_buy, size):
        self.closes.append((symbol, is_buy, size))


async def test_a_flatten_carries_on_past_an_account_it_cannot_read():
    """One 429 on one account ended an emergency stop mid-way."""
    readable = FakeSession("m1s1")
    unreadable = FakeSession("m2s2", fails=True)
    book = PositionBook()
    book.set_authoritative(readable.pubkey, BTC, 0.02)     # the reads find it flat
    book.set_authoritative(unreadable.pubkey, BTC, -0.02)  # last known, cannot re-read
    feed = types.SimpleNamespace(specs={BTC: SPEC})

    await reconcile.flatten(
        {"a": readable, "b": unreadable}, book, feed, [BTC], max_passes=1,
    )

    assert unreadable.closes == [(BTC, True, 0.02)], (
        "the side that could not be read was left open"
    )

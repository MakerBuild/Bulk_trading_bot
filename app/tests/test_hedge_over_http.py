"""Hedging past a socket that is down.

Hedges went over the socket alone. With a hedger's socket down, whatever its
maker filled before the order came off sat unhedged until the reconnect
finished -- minutes, on a slow one -- every attempt refused as not connected.

A hedge opens exposure, so sending one twice is the double hedge this bot has
spent most of its history preventing. These tests pin the three things that
make the HTTP route safe: it is taken only when the socket refused the order
before sending anything, it is never replayed, and what it sent stays
reserved until a position read has seen it -- no fill will come back on a
socket that is down to retire the reservation.
"""

import asyncio
import types

import pytest

from bulkdn import accounts
from bulkdn.accounts import NotConnected
from bulkdn.hedger import Hedger, HedgeInDoubt, LegRoles
from bulkdn.marketdata import MarketSpec
from bulkdn.pairing import Group
from bulkdn.positions import PositionBook
from test_reconnect import build as build_reconnect

BTC = "BTC-USD"
MAKER = "MAKER-PUB"
HEDGER = "HEDGER-PUB"
SPEC = MarketSpec(symbol=BTC, tick_size=0.5, lot_size=0.001, min_notional=10.0)
PRICE = 100_000.0


class Response:
    status_code = 200

    @staticmethod
    def json():
        return {"status": "ok"}


@pytest.fixture
def http_posts(monkeypatch):
    sent = []

    def post(url, json, timeout, **retry):
        sent.append((url, json, retry))
        return Response(), False

    monkeypatch.setattr(accounts, "post_signed", post)
    return sent


def signed(actions, account):
    (order,) = actions
    return {"account": account, "size": order.size, "reduce_only": order.reduce_only}


def session_with(submit):
    client = types.SimpleNamespace(
        is_connected=False, submit=submit, signed_transaction=signed,
    )
    return accounts.AccountSession(
        name="m1s2", pubkey=HEDGER, client=client,
        http=types.SimpleNamespace(base_url="https://api.example"),
    )


# -- the session: which way a hedge goes ---------------------------------------


async def test_a_hedge_the_socket_refused_as_down_goes_over_http_once(http_posts):
    async def down(*a, **k):
        raise NotConnected("not connected to WebSocket")

    went_over_http = await session_with(down).hedge_market(BTC, False, 0.01)

    assert went_over_http is True
    assert http_posts == [(
        "https://api.example/order",
        {"account": HEDGER, "size": 0.01, "reduce_only": False},
        {"attempts": 1},
    )], "sent other than once, or with replays allowed"


async def test_a_hedge_the_socket_never_answered_is_not_sent_again(http_posts):
    """It may have executed. A second copy would be a second hedge."""
    async def stalled(*a, **k):
        raise asyncio.TimeoutError()

    with pytest.raises(asyncio.TimeoutError):
        await session_with(stalled).hedge_market(BTC, False, 0.01)
    assert http_posts == []


async def test_a_hedge_over_a_working_socket_stays_on_it(http_posts):
    async def accepted(*a, **k):
        return []

    assert await session_with(accepted).hedge_market(BTC, True, 0.01) is False
    assert http_posts == []


# -- the hedger: what went over HTTP stays reserved ---------------------------


class Hedging:
    def __init__(self, pubkey, over_http=True, fails=None):
        self.name = pubkey
        self.pubkey = pubkey
        self.over_http = over_http
        self.fails = fails
        self.orders = []

    async def hedge_market(self, symbol, is_buy, size, reduce_only=False):
        self.orders.append((symbol, is_buy, size))
        if self.fails is not None:
            raise self.fails
        return self.over_http


def hedger_with(session, ttl_ms=20):
    book = PositionBook()
    hedger = Hedger(
        book=book,
        sessions={MAKER: Hedging(MAKER), HEDGER: session},
        specs={BTC: SPEC},
        in_flight_ttl_ms=ttl_ms,
    )
    # The maker's fill, as a read found it -- the stream that would have
    # announced it is the one that is down.
    book.set_authoritative(MAKER, BTC, 0.10)
    book.set_authoritative(HEDGER, BTC, 0.0)
    return book, hedger


ROLES = LegRoles(BTC, maker=MAKER, taker=HEDGER, maker_is_buy=True, reduce_only=False)


async def test_a_hedge_over_http_is_not_sent_again_once_its_clock_runs_out():
    """Its fill never comes back on the dropped socket. On the ordinary
    two-second clock the leg read as unhedged right after, and was hedged
    again."""
    hedging = Hedging(HEDGER)
    _book, hedger = hedger_with(hedging, ttl_ms=20)

    result = await hedger.hedge(ROLES, mark_price=PRICE)
    assert result.hedged_size == pytest.approx(0.10)
    assert result.settle_by_read, "nothing told the caller to start a read"

    await asyncio.sleep(0.05)                     # well past the ordinary ttl
    again = await hedger.hedge(ROLES, mark_price=PRICE)

    assert again.hedged_size == 0.0
    assert len(hedging.orders) == 1, "the same exposure was hedged twice"


async def test_a_read_that_shows_the_hedge_releases_its_reservation():
    import time

    hedging = Hedging(HEDGER)
    book, hedger = hedger_with(hedging)
    await hedger.hedge(ROLES, mark_price=PRICE)

    read_started = time.monotonic()
    book.set_authoritative(HEDGER, BTC, -0.10)
    hedger.in_flight.settle_doubtful(read_started)

    assert hedger.in_flight.total(BTC) == 0.0
    assert (await hedger.hedge(ROLES, mark_price=PRICE)).hedged_size == 0.0


async def test_an_http_hedge_that_got_no_answer_is_held_in_doubt():
    """Sent once and unanswered: it may have executed, like any other."""
    hedging = Hedging(HEDGER, fails=RuntimeError("POST /order failed: read timeout"))
    _book, hedger = hedger_with(hedging)

    with pytest.raises(HedgeInDoubt):
        await hedger.hedge(ROLES, mark_price=PRICE)
    assert hedger.in_flight.total(BTC) == pytest.approx(-0.10)


# -- the strategy: a dropped socket's legs are read at once -------------------


async def test_a_dropped_socket_has_its_legs_read_before_the_reconnect_ends():
    """Left to the read after the reconnect, a hedge decided meanwhile came
    from a book that stopped updating when the socket did."""
    strategy, master, sub1 = build_reconnect(master_delay=0.2)
    strategy._groups = {
        "g1:BTC-USD": Group(
            symbol="BTC-USD", maker=master.pubkey, takers=(sub1.pubkey,), shares=(1.0,),
        ),
    }

    strategy._start_heals()
    await asyncio.sleep(0.05)                     # the reconnect is still going

    assert strategy._waiting_for(master.pubkey), "its legs were not held for a read"
    assert strategy.resyncs >= 1, "no read started while the socket was down"

    await asyncio.gather(*strategy._heals.values())


async def test_after_an_http_hedge_the_leg_waits_for_a_read_however_long_it_takes():
    """Its reservation lapses in `doubt_ttl_s`. With reads failing longer than
    that, the leg read as unhedged against a book that never saw the hedge,
    and was hedged again."""
    from bulkdn.hedger import HedgeResult
    from test_self_trade import register, strategy

    obj = strategy()
    roles = register(obj, "g1:" + BTC, "maker-a", maker_is_buy=True)
    hedger_pub = roles.hedgers[0]
    obj.sessions["maker-a"].client = object()       # a socket of its own, up
    obj.sessions[hedger_pub] = types.SimpleNamespace(
        name="hedger", pubkey=hedger_pub, dry_run=False, is_connected=False,
        client=object(),
    )
    sent = []

    async def over_http(roles, mark_price=None, suspended=None):
        sent.append(roles.key)
        return HedgeResult(BTC, 0.1, 0.1, False, settle_by_read=True)

    async def read_fails(max_age_s=0.0, sessions=None):
        raise RuntimeError("HTTP 502")

    obj.hedger.hedge = over_http
    obj._sync_positions = read_fails

    await obj._hedge_leg(roles)
    await asyncio.sleep(0.01)                     # the read is tried, and fails

    assert obj._waiting_for(hedger_pub), "nothing held the leg for a read"
    second = await obj._hedge_leg(roles)
    assert sent == ["g1:" + BTC], "hedged again before any read saw the first"
    assert second.skipped_reason == "waiting for a read"

"""`mode: baskets`: a basket and its mirror, sent to the exchange and the Gauntlet.

What has to hold:

* the settings read as written, and refuse what cannot trade;
* a basket is `per_side` tickers each way, only from books that can take it;
* one transaction per account, the mirror the exact opposite of the basket;
* the close is reduce-only, sized from what each account holds;
* the Gauntlet is sent the very transaction the exchange took, and nothing
  the exchange refused;
* the hedger keeps out while both baskets are on their way;
* a restart mid-hold closes and does not open again.
"""

import asyncio
import random
import types

import pytest
import requests
from bulk_api.common import Side

from bulkdn import baskets
from bulkdn.accounts import AccountSession
from bulkdn.autospread import Seen
from bulkdn.config import Config, ConfigError, HoldTime, LegConfig, RiskConfig, load_config
from bulkdn.marketdata import MarketSpec
from bulkdn.pairing import Group
from bulkdn.positions import PositionBook
from bulkdn.state import Phase, StrategyState
from strategy_double import bare_strategy

NVDA, TSLA, MU, CRCL = "NVDA-USD", "TSLA-USD", "MU-USD", "CRCL-USD"
SPECS = {
    symbol: MarketSpec(symbol=symbol, tick_size=0.01, lot_size=0.001, min_notional=10.0)
    for symbol in (NVDA, TSLA, MU, CRCL)
}


# -- settings ---------------------------------------------------------------------


def write(tmp_path, text):
    path = tmp_path / "settings.yaml"
    path.write_text(
        "markets:\n  - symbol: BTC-USD\n    notional_usd: 100\n" + text, encoding="utf-8"
    )
    return str(path)


def test_the_baskets_block_reads_as_written(tmp_path):
    config = load_config(write(tmp_path, (
        "mode: baskets\n"
        "baskets:\n  markets: crypto\n  notional_usd: 800-1200\n  leverage: 5\n  per_side: 2\n"
    )), require_credentials=False)
    assert config.mode == "baskets"
    assert config.takes_liquidity, "a basket group is a master and its own sub"
    assert config.baskets.markets == "crypto"
    assert (config.baskets.notional_usd.low, config.baskets.notional_usd.high) == (800, 1200)
    assert config.baskets.leverage == 5
    assert config.baskets.per_side == 2


def test_without_the_block_baskets_has_defaults(tmp_path):
    config = load_config(write(tmp_path, "mode: basket\n"), require_credentials=False)
    assert config.mode == "baskets", "basket is another spelling of it"
    assert config.baskets.markets == "tradfi"
    assert config.baskets.per_side == 1


@pytest.mark.parametrize("block, says", [
    ("  per_side: 0\n", "per_side"),
    ("  per_side: 5\n", "per_side"),
    ("  per_side: 1.5\n", "whole number"),
    ("  markets: stocks\n", "baskets.markets"),
    ("  notional_usd: 0\n", "notional_usd"),
    ("  leverage: 80\n", "leverage"),
])
def test_what_cannot_trade_is_refused(tmp_path, block, says):
    with pytest.raises(ConfigError, match=says):
        load_config(write(tmp_path, "baskets:\n" + block), require_credentials=False)


def test_a_leg_is_the_basket_split_over_both_sides():
    settings = Config(markets=[], risk=RiskConfig(), private_key="x").baskets
    settings.per_side = 2
    span = baskets.leg_span(settings)
    assert (span.low, span.high) == (250.0, 250.0)


def test_the_tickers_come_from_auto_spreads_lists():
    config = Config(markets=[], risk=RiskConfig(), private_key="x")
    config.auto_spread.tradfi = ("NVDA", "TSLA")
    config.baskets.markets = "tradfi"
    view = baskets.settings_view(config)
    assert view.markets == "tradfi"
    assert view.tradfi == ("NVDA", "TSLA")


# -- choosing a basket ------------------------------------------------------------


def seen(symbol, spread=0.5, depth=50_000.0, price=100.0):
    return Seen(symbol=symbol, spread_bps=spread, depth_usd=depth, price=price)


def test_a_basket_is_per_side_each_way():
    books = [seen(NVDA), seen(TSLA), seen(MU), seen(CRCL)]
    picked = baskets.choose(books, SPECS, 2, 250.0, max_spread_bps=1.0,
                            depth_fraction=0.5, rng=random.Random(1))
    assert len(picked) == 4
    assert sum(leg.opener_buys for leg in picked) == 2
    assert len({leg.symbol for leg in picked}) == 4
    assert all(leg.size == pytest.approx(2.5) for leg in picked)


def test_a_wide_or_thin_book_is_left_out():
    books = [seen(NVDA), seen(TSLA, spread=3.0), seen(MU, depth=300.0)]
    picked = baskets.choose(books, SPECS, 1, 250.0, max_spread_bps=1.0,
                            depth_fraction=0.5, rng=random.Random(1))
    assert picked is None, "only NVDA passes, and a basket needs two"


def test_a_leg_below_the_minimum_order_is_left_out():
    books = [seen(NVDA), seen(TSLA)]
    assert baskets.choose(books, SPECS, 1, 5.0, max_spread_bps=1.0,
                          depth_fraction=0.5) is None


# -- the Gauntlet ------------------------------------------------------------------


class Answer:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def test_the_signed_transaction_is_what_is_posted(monkeypatch):
    sent = []
    monkeypatch.setattr(baskets.requests, "post",
                        lambda url, json, timeout: sent.append((url, json)) or Answer(200, {"ok": 1}))
    tx = {"actions": [], "nonce": "1", "account": "A", "signer": "A", "signature": "s"}
    assert baskets.register(tx) == (200, {"ok": 1})
    assert sent == [(baskets.GAUNTLET_URL, tx)]


def test_a_refusal_is_returned_and_a_lost_answer_retried(monkeypatch):
    calls = []

    def post(url, json, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise requests.ConnectionError("dropped")
        return Answer(400, {"error": "bad"})

    monkeypatch.setattr(baskets.requests, "post", post)
    assert baskets.register({}) == (400, {"error": "bad"})
    assert len(calls) == 2


def test_no_answer_at_all_raises(monkeypatch):
    def post(url, json, timeout):
        raise requests.Timeout("slow")

    monkeypatch.setattr(baskets.requests, "post", post)
    with pytest.raises(RuntimeError, match="no answer"):
        baskets.register({})


# -- one account's basket ----------------------------------------------------------


class SigningClient:
    def __init__(self):
        self.submitted = []

    async def submit(self, actions, **kwargs):
        self.submitted.append(kwargs)
        return []

    def signed_transaction(self, actions, account, nonce):
        return {"actions": [a.symbol for a in actions], "nonce": f"{nonce}",
                "account": account, "signature": f"sig-{nonce}"}


async def test_the_returned_transaction_carries_the_submitted_nonce():
    client = SigningClient()
    session = AccountSession(name="m1", pubkey="ACC", client=client, http=None)
    orders = baskets.market_orders([(NVDA, True, 1.0), (TSLA, False, 2.0)], reduce_only=False)
    _responses, tx = await session.basket(orders)
    assert client.submitted[0]["nonce"] == int(tx["nonce"])
    assert client.submitted[0]["account"] == "ACC"
    assert tx["actions"] == [NVDA, TSLA]


# -- a basket cycle ----------------------------------------------------------------


class FakeSession:
    """An account whose market orders fill at once, onto the shared book."""

    def __init__(self, name, pubkey, book, *, fails=False, dry_run=False):
        self.name, self.pubkey, self.book = name, pubkey, book
        self.fails, self.dry_run = fails, dry_run
        self.baskets = []
        self.seen_in_flight = None

    async def basket(self, orders):
        self.seen_in_flight = set(self.strategy._basket_flight)
        self.baskets.append(orders)
        if self.fails:
            raise RuntimeError("refused")
        for order in orders:
            signed = order.size if order.side == Side.BUY else -order.size
            held = self.book.authoritative(self.pubkey, order.symbol)
            self.book.set_authoritative(self.pubkey, order.symbol, held + signed)
        return [], {"account": self.pubkey, "basket": len(self.baskets)}


def basket_strategy(*, fails=None, dry_run=False):
    book = PositionBook()
    obj = bare_strategy()
    obj.config = Config(
        markets=[LegConfig(symbol=s, size=1.0) for s in (NVDA, TSLA)],
        risk=RiskConfig(), private_key="x", mode="baskets",
        hold_minutes=HoldTime(0.0, 0.0),
    )
    obj.book = book
    obj.feed = types.SimpleNamespace(
        specs=SPECS,
        quote=lambda symbol: types.SimpleNamespace(best_bid=99.0, best_ask=101.0),
        reference_price=lambda symbol: 100.0,
    )
    obj.sessions = {
        "M": FakeSession("m1", "M", book, fails=fails == "M", dry_run=dry_run),
        "S": FakeSession("m1s1", "S", book, fails=fails == "S", dry_run=dry_run),
    }
    for session in obj.sessions.values():
        session.strategy = obj
    obj.symbols = [NVDA, TSLA]
    obj.pairing = None
    obj._stop = asyncio.Event()
    obj.state = StrategyState()
    obj.guard = types.SimpleNamespace(reset_symbol=lambda *a, **k: None)
    obj.title = types.SimpleNamespace(set_cycle=lambda n: None)
    obj.notifier = types.SimpleNamespace(send_soon=lambda coro: coro.close())

    async def no_read(*args, **kwargs):
        return None

    async def confirmed(is_done, label, key=None):
        return is_done()

    obj._sync_positions = no_read
    obj._confirm_done = confirmed
    obj._leg_is_neutral = lambda key: True
    obj.registered = []

    async def register(group_id, name, tx, label):
        obj.registered.append((name, label, tx))

    obj._register_basket = register
    return obj


def legs(obj, group_id=1):
    return {
        obj.group_key(group_id, NVDA): Group(NVDA, maker="M", takers=("S",), shares=(1.0,),
                                             maker_is_buy=True),
        obj.group_key(group_id, TSLA): Group(TSLA, maker="M", takers=("S",), shares=(1.0,),
                                             maker_is_buy=False),
    }


def sides(orders):
    return {(o.symbol, o.side, o.size, o.reduce_only) for o in orders}


async def test_a_cycle_opens_a_basket_and_its_mirror_then_closes_both():
    obj = basket_strategy()
    drawn = legs(obj)
    await obj._run_basket(1, drawn, dict.fromkeys(drawn, 2.0))
    master, sub = obj.sessions["M"].baskets, obj.sessions["S"].baskets

    assert len(master) == len(sub) == 2, "one basket to open and one to close, each"
    assert sides(master[0]) == {(NVDA, Side.BUY, 2.0, False), (TSLA, Side.SELL, 2.0, False)}
    assert sides(sub[0]) == {(NVDA, Side.SELL, 2.0, False), (TSLA, Side.BUY, 2.0, False)}
    assert sides(master[1]) == {(NVDA, Side.SELL, 2.0, True), (TSLA, Side.BUY, 2.0, True)}
    assert sides(sub[1]) == {(NVDA, Side.BUY, 2.0, True), (TSLA, Side.SELL, 2.0, True)}
    assert all(obj.book.authoritative(p, s) == 0 for p in "MS" for s in (NVDA, TSLA))

    assert all(obj.state.legs[key].phase == Phase.COMPLETE for key in drawn)
    assert not obj._groups, "the finished basket gave its legs back"
    assert obj.state.cycle_index == 1


async def test_every_opening_basket_goes_to_the_gauntlet_and_no_close():
    """The website registers a basket's open and not its close."""
    obj = basket_strategy()
    drawn = legs(obj)
    await obj._run_basket(1, drawn, dict.fromkeys(drawn, 2.0))
    assert sorted((name, label) for name, label, _tx in obj.registered) == [
        ("m1", "open"), ("m1s1", "open"),
    ]


async def test_a_refused_basket_is_not_registered():
    obj = basket_strategy(fails="S")
    drawn = legs(obj)
    await obj._run_basket(1, drawn, dict.fromkeys(drawn, 2.0))
    assert {name for name, _label, _tx in obj.registered} == {"m1"}


async def test_a_dry_run_registers_nothing():
    obj = basket_strategy(dry_run=True)
    obj._register_basket = type(obj)._register_basket.__get__(obj)
    drawn = legs(obj)
    await obj._run_basket(1, drawn, dict.fromkeys(drawn, 2.0))
    assert not obj._registering and obj._gauntlet == {"ok": 0, "refused": 0, "failed": 0}


async def test_the_hedger_keeps_out_while_both_are_on_their_way():
    obj = basket_strategy()
    drawn = legs(obj)
    await obj._run_basket(1, drawn, dict.fromkeys(drawn, 2.0))
    assert obj.sessions["M"].seen_in_flight == set(drawn)
    assert obj.sessions["S"].seen_in_flight == set(drawn)
    assert not obj._basket_flight
    queued = set()
    while not obj._hedge_queue.empty():
        queued.add(obj._hedge_queue.get_nowait())
    assert queued == set(drawn), "each leg is looked at once both have answered"


async def test_a_basket_resumed_mid_hold_closes_and_does_not_open_again():
    obj = basket_strategy()
    drawn = legs(obj)
    for key, group in drawn.items():
        leg = obj.state.leg(key, group.symbol)
        leg.phase, leg.hold_until, leg.target_size, leg.cycle_index = Phase.HOLD, 1.0, 2.0, 1
    obj.book.set_authoritative("M", NVDA, 2.0)
    obj.book.set_authoritative("S", NVDA, -2.0)
    obj.book.set_authoritative("M", TSLA, -2.0)
    obj.book.set_authoritative("S", TSLA, 2.0)

    await obj._run_basket(1, drawn, {})
    assert len(obj.sessions["M"].baskets) == len(obj.sessions["S"].baskets) == 1
    assert all(order.reduce_only for order in obj.sessions["M"].baskets[0])
    assert all(obj.book.authoritative(p, s) == 0 for p in "MS" for s in (NVDA, TSLA))


async def test_the_status_says_what_the_gauntlet_answered():
    obj = basket_strategy()
    obj._gauntlet = {"ok": 3, "refused": 1, "failed": 0}
    obj._basket_last = "long NVDA / short TSLA"
    line = obj._basket_status()
    assert "last long NVDA / short TSLA" in line
    assert "Gauntlet: 3 registered, 1 refused" in line


async def test_a_refusal_from_the_gauntlet_is_counted(monkeypatch):
    obj = basket_strategy()
    obj._register_basket = type(obj)._register_basket.__get__(obj)
    monkeypatch.setattr(baskets, "register", lambda tx: (403, {"error": "no"}))
    await obj._register_basket(1, "m1", {}, "open")
    monkeypatch.setattr(baskets, "register", lambda tx: (200, {"ok": True}))
    await obj._register_basket(1, "m1", {}, "close")
    assert obj._gauntlet == {"ok": 1, "refused": 1, "failed": 0}


async def test_the_exposure_limit_waits_for_the_mirror(monkeypatch):
    obj = basket_strategy()
    assert not obj._basket_settling()
    drawn = legs(obj)
    await obj._run_basket(1, drawn, dict.fromkeys(drawn, 2.0))
    assert obj._basket_settling(), "just answered: the hedger's grace is running"
    monkeypatch.setattr("bulkdn.strategy.time.monotonic", lambda: obj._basket_grace_until + 1)
    assert not obj._basket_settling(), "after the grace a gap halts as any other"

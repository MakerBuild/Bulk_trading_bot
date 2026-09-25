"""Closing out with resting orders instead of taking the spread.

`Close All Positions` only ever closed at market, which pays the spread and a
taker fee on every unit. This is the other half of that choice. Being the
cheaper close, it is also the one that can fail to finish -- so most of what
matters here is that it never crosses, never opens anything, and says so plainly
when it runs out of time.
"""

import asyncio

import pytest

from bulkdn.marketdata import MarketSpec
from bulkdn.positions import PositionBook
from bulkdn.reconcile import flatten_limit

BTC = "BTC-USD"
MASTER = "EXAMPLE-MASTER"
SUB1 = "EXAMPLE-SUB1"
SPEC = MarketSpec(BTC, tick_size=0.5, lot_size=0.001, min_notional=1.0)


class FakeOrder:
    def __init__(self, price, size):
        self.price = price
        self.size = size


class FakeClient:
    def __init__(self):
        self.orders = {}

    def get_order_map(self):
        return self.orders


class FakeSession:
    def __init__(self, name, pubkey, book):
        self.name = name
        self.pubkey = pubkey
        self.client = FakeClient()
        self.book = book
        self.placed = []
        self.reduce_only_flags = []
        self.cancelled = []
        self._next = 0
        # Set to fill whatever is placed, as if the market traded with us.
        self.fills = False

    async def place_limit(self, *, symbol, is_buy, price, size, reduce_only, cancel_oid):
        self.placed.append((symbol, is_buy, price, size, cancel_oid))
        self.reduce_only_flags.append(reduce_only)
        if cancel_oid:
            self.client.orders.pop(cancel_oid, None)
        self._next += 1
        oid = f"{self.name}-{self._next}"
        self.client.orders[oid] = FakeOrder(price, size)
        if self.fills:
            held = self.book.authoritative(self.pubkey, symbol)
            filled = held + (size if is_buy else -size)
            self.book.set_authoritative(
                self.pubkey, symbol, 0.0 if abs(held) <= size else filled
            )
        return oid, []

    async def cancel(self, symbol, oid):
        self.cancelled.append((symbol, oid))
        self.client.orders.pop(oid, None)
        return []


class FakeQuote:
    def __init__(self, bid, ask):
        self.best_bid = bid
        self.best_ask = ask
        self.mark_price = None if bid is None or ask is None else (bid + ask) / 2


class FakeFeed:
    def __init__(self, bid=100_000.0, ask=100_001.0):
        self.specs = {BTC: SPEC}
        self._quote = FakeQuote(bid, ask)

    def quote(self, symbol):
        return self._quote

    def move(self, bid, ask):
        self._quote = FakeQuote(bid, ask)


@pytest.fixture(autouse=True)
def no_http_sync(monkeypatch):
    """Positions come from the fake book, not from the network."""
    from bulkdn import reconcile

    async def noop(sessions, book):
        return None

    monkeypatch.setattr(reconcile, "sync_positions", noop)


def build(master_size=0.01, sub_size=-0.01):
    book = PositionBook()
    master = FakeSession("master", MASTER, book)
    sub1 = FakeSession("sub1", SUB1, book)
    book.set_authoritative(MASTER, BTC, master_size)
    book.set_authoritative(SUB1, BTC, sub_size)
    return {MASTER: master, SUB1: sub1}, book, master, sub1, FakeFeed()


def close(sessions, book, feed, **kwargs):
    kwargs.setdefault("interval_s", 0.02)
    return asyncio.run(flatten_limit(sessions, book, feed, [BTC], **kwargs))


# -- it closes the right way round, passively --------------------------------


def test_a_long_is_closed_by_a_passive_sell():
    sessions, book, master, _sub1, feed = build(master_size=0.01, sub_size=0.0)
    master.fills = True

    assert close(sessions, book, feed, timeout_s=5) is True

    _symbol, is_buy, price, size, _cancel = master.placed[0]
    assert is_buy is False, "a long is closed by selling"
    assert price < feed.quote(BTC).best_ask, "a passive sell rests below the ask"
    assert size == pytest.approx(0.01)


def test_a_short_is_closed_by_a_passive_buy():
    sessions, book, _master, sub1, feed = build(master_size=0.0, sub_size=-0.01)
    sub1.fills = True

    assert close(sessions, book, feed, timeout_s=5) is True

    _symbol, is_buy, price, _size, _cancel = sub1.placed[0]
    assert is_buy is True, "a short is closed by buying"
    assert price > feed.quote(BTC).best_bid, "a passive buy rests above the bid"


@pytest.mark.parametrize("improve_ticks", [0, 1, 5, 50])
@pytest.mark.parametrize("spread_ticks", [1, 2, 40])
def test_it_never_crosses_the_spread(improve_ticks, spread_ticks):
    """The whole point: no taker fee, no spread paid.

    Asking to improve by more ticks than the spread holds is the way a passive
    order turns into a crossing one, so the aggressive settings are checked
    against a one-tick spread that has no room to give.
    """
    sessions, book, master, sub1, _feed = build()
    feed = FakeFeed(100_000.0, 100_000.0 + SPEC.tick_size * spread_ticks)
    quote = feed.quote(BTC)
    close(sessions, book, feed, timeout_s=0.1, improve_ticks=improve_ticks)

    assert master.placed and sub1.placed, "nothing was sent, so nothing was proved"
    for session in (master, sub1):
        for _symbol, is_buy, price, _size, _cancel in session.placed:
            if is_buy:
                assert price < quote.best_ask, "a buy reached the ask"
            else:
                assert price > quote.best_bid, "a sell reached the bid"


def test_every_order_is_reduce_only():
    """Without it a stale position reading flips the account the other way --
    a close that opens, with nothing left watching it."""
    sessions, book, master, sub1, feed = build()
    close(sessions, book, feed, timeout_s=0.1)

    assert master.reduce_only_flags and sub1.reduce_only_flags
    assert all(master.reduce_only_flags) and all(sub1.reduce_only_flags)


def test_nothing_is_placed_when_already_flat():
    sessions, book, master, sub1, feed = build(master_size=0.0, sub_size=0.0)
    assert close(sessions, book, feed, timeout_s=5) is True
    assert master.placed == [] and sub1.placed == []


def test_dust_below_a_lot_is_left_alone():
    """Under one lot cannot be traded, so chasing it would never finish."""
    sessions, book, master, _sub1, feed = build(master_size=1e-7, sub_size=0.0)
    assert close(sessions, book, feed, timeout_s=5) is True
    assert master.placed == []


# -- it follows the market ---------------------------------------------------


def test_the_order_is_re_priced_when_the_touch_moves():
    sessions, book, master, _sub1, feed = build(master_size=0.01, sub_size=0.0)

    async def run():
        async def move_after_a_moment():
            await asyncio.sleep(0.1)
            feed.move(100_010.0, 100_011.0)

        mover = asyncio.create_task(move_after_a_moment())
        await flatten_limit(
            sessions, book, feed, [BTC], timeout_s=0.4, interval_s=0.02
        )
        await mover

    asyncio.run(run())

    prices = [price for _s, _b, price, _sz, _c in master.placed]
    assert len(set(prices)) > 1, "the order stayed put while the market moved"
    assert any(cancel for *_rest, cancel in master.placed), "it replaced without cancelling"


def test_an_unmoved_touch_sends_nothing_further():
    """Otherwise it would burn a transaction every pass for no change."""
    sessions, book, master, _sub1, feed = build(master_size=0.01, sub_size=0.0)
    close(sessions, book, feed, timeout_s=0.3)
    assert len(master.placed) == 1, f"placed {len(master.placed)} times without a move"


# -- and it gives up honestly ------------------------------------------------


def test_it_reports_failure_when_nothing_fills():
    sessions, book, _master, _sub1, feed = build()
    assert close(sessions, book, feed, timeout_s=0.2) is False


def test_orders_are_pulled_when_it_gives_up():
    """Leaving them resting would fill later with nothing watching."""
    sessions, book, master, sub1, feed = build()
    close(sessions, book, feed, timeout_s=0.2)
    assert master.cancelled and sub1.cancelled
    assert master.client.orders == {} and sub1.client.orders == {}


def test_orders_are_pulled_when_it_succeeds():
    sessions, book, master, _sub1, feed = build(master_size=0.01, sub_size=0.0)
    master.fills = True
    assert close(sessions, book, feed, timeout_s=5) is True
    assert master.client.orders == {}


def test_a_missing_price_does_not_place_anything():
    """Right after a reconnect the book can be empty; guessing a price there is
    how a 'passive' order crosses."""
    sessions, book, master, _sub1, feed = build(master_size=0.01, sub_size=0.0)
    feed.move(None, None)

    assert close(sessions, book, feed, timeout_s=0.2) is False
    assert master.placed == []


def test_an_interrupt_still_pulls_the_orders():
    """Ctrl+C during a limit close is an ordinary thing to do -- the menu says
    it cancels the orders. An interrupt that left reduce-only orders resting
    would be the worst case: live orders with nothing watching to hedge them."""
    sessions, book, master, sub1, feed = build()

    async def run():
        task = asyncio.create_task(
            flatten_limit(sessions, book, feed, [BTC], timeout_s=30, interval_s=0.02)
        )
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert master.placed and sub1.placed, "nothing was resting, so nothing was proved"
    assert master.cancelled and sub1.cancelled, "the interrupt left orders on the book"
    assert master.client.orders == {} and sub1.client.orders == {}


# -- and the exit code tells the difference ----------------------------------


class FakeStore:
    def __init__(self, state):
        self.state = state
        self.saved = []

    def load(self):
        return self.state

    def save(self, state):
        self.saved.append(state)


class FakeRuntime:
    """Stands in for the real one, which would open sockets and sign requests."""

    instances = []
    store_override = None
    # Positions the book reports after the close, for the "still open
    # elsewhere" check: {account: {symbol: size}}.
    leftover = {}

    def __init__(self, config, dry_run, symbols=None):
        from bulkdn.state import StrategyState

        self.config = config
        self.dry_run = dry_run
        self.master = object()
        self.sub1 = object()
        self.symbols = symbols or [BTC]
        self.sessions = {}
        self.started_trading = None
        # What the flatten cancels orders on: every account the run holds,
        # which in pool mode is more than the named pair.
        self.pool = [self.master, self.sub1]
        self.book = PositionBook()
        for account, held in FakeRuntime.leftover.items():
            self.sessions[account] = type("S", (), {"name": account})()
            for symbol, size in held.items():
                self.book.set_authoritative(account, symbol, size)
        self.feed = FakeFeed()
        self.store = FakeRuntime.store_override or FakeStore(StrategyState())
        self.stopped = False
        FakeRuntime.instances.append(self)

    async def start(self, verify=True, trading=True):
        self.started_trading = trading

    async def stop(self):
        self.stopped = True


def run_cmd_flatten(monkeypatch, closed, dry_run=False, store=None, markets=None,
                    leftover=None, **kwargs):
    from bulkdn import cli

    FakeRuntime.instances = []
    FakeRuntime.leftover = leftover or {}
    # A real store when the test is about what lands in the file; the fake
    # one otherwise, so nothing touches the disk that does not need to.
    FakeRuntime.store_override = store
    monkeypatch.setattr(cli, "Runtime", FakeRuntime)

    async def no_cancel(sessions, symbols):
        return None

    async def fake_limit(*a, **kw):
        return closed

    async def fake_market(*a, **kw):
        return None

    monkeypatch.setattr(cli, "cancel_all_orders", no_cancel)
    monkeypatch.setattr(cli, "flatten_limit", fake_limit)
    monkeypatch.setattr(cli, "flatten", fake_market)

    class Account:
        improve_ticks = 1

    class Market:
        # The limit close reads its tick improvement from the first market
        # switched on, not from markets[0], which may be switched off.
        improve_ticks = 1

        def __init__(self, symbol, enabled=True):
            self.symbol = symbol
            self.enabled = enabled

    configured = markets or [Market(BTC)]

    class Cfg:
        master_account = Account()
    # Every market in the file, the switched-off ones included.
    Cfg.markets = configured

    return asyncio.run(cli.cmd_flatten(Cfg(), dry_run=dry_run, **kwargs))


def test_a_finished_limit_close_exits_zero(monkeypatch):
    assert run_cmd_flatten(monkeypatch, closed=True, limit=True, timeout_s=1.0) == 0


def test_a_timed_out_limit_close_exits_non_zero(monkeypatch, capsys):
    """Anything scripted on top of this would otherwise read a timeout as
    'you are flat' and stop watching an open position."""
    code = run_cmd_flatten(monkeypatch, closed=False, limit=True, timeout_s=1.0)
    assert code == 1
    assert "STILL OPEN" in capsys.readouterr().out


def test_a_market_close_still_exits_zero(monkeypatch):
    """It has no timeout to run out of, so its exit code must not change."""
    assert run_cmd_flatten(monkeypatch, closed=False) == 0


def test_the_runtime_is_stopped_even_when_the_close_times_out(monkeypatch):
    run_cmd_flatten(monkeypatch, closed=False, limit=True, timeout_s=1.0)
    assert FakeRuntime.instances[0].stopped, "the socket was left open"


# -- and what a dry run is allowed to change --------------------------------


def test_a_dry_flatten_leaves_the_state_alone(monkeypatch, tmp_path):
    """It says it submits nothing, and the state file is something. Clearing
    the halt is the one change that mattered: it is what stops the bot from
    starting, so a "dry" run that cleared it changed the only thing anybody
    was running the command for."""
    from bulkdn.state import Phase, StateStore, StrategyState

    store = StateStore(str(tmp_path / "state.json"))
    store.save(StrategyState(phase=Phase.HALTED, halted_reason="hedge limit"))

    run_cmd_flatten(monkeypatch, closed=True, dry_run=True, store=store)

    after = store.load()
    assert after.phase == Phase.HALTED
    assert after.halted_reason == "hedge limit"


def test_a_live_flatten_clears_it(monkeypatch, tmp_path):
    from bulkdn.state import Phase, StateStore, StrategyState

    store = StateStore(str(tmp_path / "state.json"))
    store.save(StrategyState(phase=Phase.HALTED, halted_reason="hedge limit"))

    run_cmd_flatten(monkeypatch, closed=True, dry_run=False, store=store)

    after = store.load()
    assert after.phase == Phase.IDLE
    assert after.halted_reason is None


# -- the panic button cannot be stopped by what only trading needs -----------


def test_a_close_starts_without_the_trading_checks(monkeypatch):
    """The referral gate, the sizing plan and the leverage change all ran
    first, so the close could fail before a single cancel -- on an indexer
    outage, or with no free margin, which is exactly when you want out."""
    run_cmd_flatten(monkeypatch, closed=True)
    assert FakeRuntime.instances[0].started_trading is False


def test_a_switched_off_market_is_closed_too(monkeypatch):
    """Only the enabled markets were closed, and the state reset after, so a
    market turned off with a position open was left open and forgotten."""
    class Market:
        def __init__(self, symbol, enabled):
            self.symbol = symbol
            self.enabled = enabled

    run_cmd_flatten(
        monkeypatch, closed=True,
        markets=[Market(BTC, True), Market("ETH-USD", False)],
    )
    assert FakeRuntime.instances[0].symbols == [BTC, "ETH-USD"]


def test_a_position_in_a_market_not_in_the_settings_is_reported(monkeypatch, capsys, tmp_path):
    from bulkdn.state import Phase, StateStore, StrategyState

    store = StateStore(str(tmp_path / "state.json"))
    state = StrategyState()
    state.leg("g1:" + BTC, BTC).phase = Phase.EXIT
    store.save(state)

    code = run_cmd_flatten(
        monkeypatch, closed=True, store=store, leftover={"m2s3": {"SOL-USD": -1.5}},
    )

    assert code == 1, "reported flat over an open position"
    assert "m2s3 SOL-USD" in capsys.readouterr().out
    assert store.load().legs, "the state was reset over a position still open"


def test_the_closing_start_skips_the_gate_the_sizing_and_the_leverage(monkeypatch):
    from bulkdn import cli

    async def instant(_s):
        return None

    monkeypatch.setattr(cli.asyncio, "sleep", instant)
    runtime = object.__new__(cli.Runtime)
    runtime.dry_run = False
    runtime.master = runtime.sub1 = type("S", (), {"pubkey": "m1", "client": object()})()
    runtime.symbols = [BTC]
    runtime.config = type("C", (), {"mode": "multi"})()
    connected = []

    async def connect_all():
        connected.append(True)

    async def subscribe():
        return None

    runtime._connect_all = connect_all
    runtime.feed = type("F", (), {"load_specs": lambda self, strict=True: None,
                                  "specs": {BTC: None},
                                  "subscribe": lambda self: subscribe()})()

    def refuse(*_a):
        raise AssertionError("closing ran a trading-only step")

    runtime.check_access = refuse
    runtime.apply_sizing = refuse
    runtime.apply_leverage = refuse

    asyncio.run(runtime.start(verify=False, trading=False))
    assert connected, "it never connected"


# -- one unlisted market does not block the panic button ---------------------


def test_an_unlisted_market_is_skipped_when_closing_not_refused(caplog):
    """Closing and status cover every market in the settings, so one
    delisted or misspelled market that nobody trades stopped the close
    before a single cancel went out."""
    from bulkdn.feed import MarketFeed

    listed = {"symbols": [{"symbol": BTC, "tickSize": 0.1, "lotSize": 0.000001,
                           "minNotional": 1.0}]}
    session = type("S", (), {"http": type("H", (), {
        "get_exchange_info": lambda self: listed})()})()
    feed = MarketFeed(session, [BTC, "ETHH-USD"])

    with caplog.at_level("WARNING"):
        feed.load_specs(strict=False)

    assert feed.symbols == [BTC]
    assert "ETHH-USD is not listed" in caplog.text


def test_a_trading_run_still_refuses_an_unlisted_market():
    from bulkdn.feed import MarketFeed

    listed = {"symbols": [{"symbol": BTC, "tickSize": 0.1, "lotSize": 0.000001,
                           "minNotional": 1.0}]}
    session = type("S", (), {"http": type("H", (), {
        "get_exchange_info": lambda self: listed})()})()
    feed = MarketFeed(session, [BTC, "ETHH-USD"])

    with pytest.raises(RuntimeError, match="ETHH-USD is not listed"):
        feed.load_specs()


# -- a restart pulls the dead run's orders before anything can stop it --------


def test_a_trading_start_cancels_before_the_gate_can_refuse(monkeypatch):
    """A restart after a crash is when the indexer may be down or margin
    short. Those checks ran first, and a refusal left the dead run's orders
    resting, free to fill with nothing hedging them."""
    import pytest

    from bulkdn import cli

    runtime = object.__new__(cli.Runtime)
    runtime.dry_run = False
    runtime.master = runtime.sub1 = type("S", (), {"pubkey": "m1", "client": object()})()
    runtime.pool = ["m1-session"]
    runtime.symbols = [BTC]
    runtime.config = type("C", (), {"mode": "multi"})()
    runtime.feed = type("F", (), {"load_specs": lambda self, strict=True: None,
                                  "specs": {BTC: None}})()
    cancelled = []

    async def cancel_all(sessions, symbols):
        cancelled.append((list(sessions), list(symbols)))

    monkeypatch.setattr(cli, "cancel_all_orders", cancel_all)

    def indexer_down(*_a):
        raise RuntimeError("referral indexer unavailable")

    runtime.check_access = indexer_down

    with pytest.raises(RuntimeError):
        asyncio.run(runtime.start(verify=False, trading=True))
    assert cancelled == [(["m1-session"], [BTC])], "the dead run's orders were left"

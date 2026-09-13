"""Phase machine: role swapping, fill routing, and completion predicates.

The role swap between OPEN and EXIT is what allows a single hedge rule to serve
both directions of the cycle, so it is worth pinning down explicitly.
"""

import time
from dataclasses import dataclass

import pytest

from bulk_api.common import Side, Topic

from bulkdn.config import Config, HoldTime, LegConfig, RiskConfig
from bulkdn.feed import Quote
from bulkdn.hedger import Hedger
from bulkdn.marketdata import MarketSpec
from bulkdn.positions import PositionBook
from bulkdn.state import Phase, StateStore, StrategyState
from bulkdn.strategy import Strategy, build_hedge_ceilings

MASTER = "master-pubkey"
SUB1 = "sub1-pubkey"
BTC = "BTC-USD"
SOL = "SOL-USD"

BTC_SPEC = MarketSpec(symbol=BTC, tick_size=0.5, lot_size=0.001, min_notional=10.0)
SOL_SPEC = MarketSpec(symbol=SOL, tick_size=0.01, lot_size=0.1, min_notional=10.0)


@dataclass
class FakeFill:
    symbol: str
    size: float
    side: object
    price: float = 100_000.0
    # ws_compat attaches this to every parsed fill; production reads it here.
    trade_id: str | None = None


class FakeSession:
    def __init__(self, name, pubkey):
        self.name = name
        self.pubkey = pubkey
        self.dry_run = True
        self.reject_streak = 0
        self.is_connected = True
        self.last_message_age_s = 0.0
        self.handlers = {}
        self.orders = []

    def on(self, topic, handler):
        self.handlers.setdefault(topic, []).append(handler)

    async def market(self, symbol, is_buy, size, reduce_only=False):
        self.orders.append((symbol, is_buy, size, reduce_only))
        return []


class FakeFeed:
    def __init__(self):
        self.specs = {BTC: BTC_SPEC, SOL: SOL_SPEC}

    def quote(self, symbol):
        return Quote(symbol, 100_000.0, 100_010.0, 100_005.0, age_s=0.0)

    def reference_price(self, symbol):
        return 100_000.0


def make_config():
    return Config(
        sub1_pubkey=SUB1,
        master_account=LegConfig(symbol=BTC, size=1.0, offset_bps=1.0, max_distance_bps=5.0, max_order_size=1.0),
        sub_account=LegConfig(symbol=SOL, size=10.0, offset_bps=1.0, max_distance_bps=5.0, max_order_size=10.0),
        hold_minutes=1.0,
        risk=RiskConfig(),
        private_key="x",
    )


def build(tmp_path, phase=Phase.OPEN):
    config = make_config()
    book = PositionBook(overlay_ttl_ms=5000)
    master = FakeSession("master", MASTER)
    sub1 = FakeSession("sub1", SUB1)
    feed = FakeFeed()
    hedger = Hedger(
        book=book,
        sessions={MASTER: master, SUB1: sub1},
        specs=feed.specs,
        max_hedge_size=build_hedge_ceilings(config),
    )
    strategy = Strategy(
        config=config,
        master=master,
        sub1=sub1,
        feed=feed,
        book=book,
        hedger=hedger,
        chaser=None,
        risk=None,
        store=StateStore(str(tmp_path / "state.json")),
        state=StrategyState(phase=phase),
    )
    return strategy, book, master, sub1


# -- role assignment -------------------------------------------------------


def test_open_roles_match_the_strategy(tmp_path):
    strategy, *_ = build(tmp_path)
    roles = {r.symbol: r for r in strategy.roles_for(Phase.OPEN)}

    # Master rests the BTC buy; sub1 hedges it short.
    assert roles[BTC].maker == MASTER
    assert roles[BTC].taker == SUB1
    assert roles[BTC].maker_is_buy is True
    assert roles[BTC].reduce_only is False

    # Sub1 rests the SOL buy; master hedges it short.
    assert roles[SOL].maker == SUB1
    assert roles[SOL].taker == MASTER


def test_exit_roles_swap_and_are_reduce_only(tmp_path):
    strategy, *_ = build(tmp_path)
    roles = {r.symbol: r for r in strategy.roles_for(Phase.EXIT)}

    # Sub1 covers its BTC short, so it becomes the maker; master now hedges.
    assert roles[BTC].maker == SUB1
    assert roles[BTC].taker == MASTER
    # Closing a short means buying.
    assert roles[BTC].maker_is_buy is True
    assert roles[BTC].reduce_only is True

    assert roles[SOL].maker == MASTER
    assert roles[SOL].taker == SUB1
    assert roles[SOL].reduce_only is True


def test_hold_reuses_the_open_roles(tmp_path):
    strategy, *_ = build(tmp_path)
    assert strategy.roles_for(Phase.HOLD) == strategy.roles_for(Phase.OPEN)


# -- hold length, per leg --------------------------------------------------
#
# Each leg holds on its own clock. They ran in lockstep once, which meant a leg
# that filled first waited for the other before its hold could even start.


async def _hold_without_waiting(strategy, symbol) -> None:
    """Run the hold for its bookkeeping, skipping the wait it then does."""

    async def no_wait(*_args, **_kwargs):
        return None

    strategy._drive_leg = no_wait
    await strategy._leg_hold(symbol)


async def test_the_hold_deadline_is_drawn_from_the_configured_range(tmp_path):
    strategy, *_ = build(tmp_path, phase=Phase.HOLD)
    strategy.config.hold_minutes = HoldTime(0.5, 1.0)

    started = time.time()
    await _hold_without_waiting(strategy, BTC)

    # A deadline, not a duration, so the wait survives a restart.
    leg = strategy.state.leg(BTC)
    assert started + 30 <= leg.hold_until <= started + 60 + 1


async def test_each_leg_holds_on_its_own_clock(tmp_path):
    """The point of the change: one leg's hold does not wait on the other."""
    strategy, *_ = build(tmp_path, phase=Phase.HOLD)
    strategy.config.hold_minutes = HoldTime(0.5, 1.0)

    await _hold_without_waiting(strategy, BTC)
    btc_deadline = strategy.state.leg(BTC).hold_until

    assert btc_deadline > 0
    assert strategy.state.leg(SOL).hold_until == 0.0, "the other leg was dragged in"
    assert strategy.state.leg(BTC).phase == Phase.HOLD
    assert strategy.state.leg(SOL).phase != Phase.HOLD


async def test_a_restart_finishes_the_hold_it_was_serving(tmp_path):
    """Re-rolling on restart would let a crash loop extend the hold forever."""
    strategy, *_ = build(tmp_path, phase=Phase.HOLD)
    strategy.config.hold_minutes = HoldTime(0.5, 1.0)
    strategy.state.leg(BTC).hold_until = 1_700_000_000.0

    await _hold_without_waiting(strategy, BTC)

    assert strategy.state.leg(BTC).hold_until == 1_700_000_000.0


# -- fill handling ---------------------------------------------------------


def test_maker_fill_updates_the_book_and_queues_a_hedge(tmp_path):
    strategy, book, master, _sub1 = build(tmp_path)
    strategy.install_handlers()
    book.set_authoritative(MASTER, BTC, 0.0)
    book.set_authoritative(SUB1, BTC, 0.0)

    handler = master.handlers[Topic.FILL][0]
    handler(FakeFill(symbol=BTC, size=0.1, side=Side.BUY))

    assert book.effective(MASTER, BTC) == 0.1
    assert strategy._hedge_queue.get_nowait() == BTC


def test_taker_fill_retires_the_in_flight_reservation(tmp_path):
    strategy, book, _master, sub1 = build(tmp_path)
    strategy.install_handlers()
    strategy.hedger.in_flight.add(BTC, -0.1)

    fill_handler = sub1.handlers[Topic.FILL][0]
    fill_handler(FakeFill(symbol=BTC, size=0.1, side=Side.SELL))

    assert strategy.hedger.in_flight.total(BTC) == 0.0
    assert book.effective(SUB1, BTC) == -0.1


def test_fills_in_unrelated_symbols_are_ignored(tmp_path):
    strategy, book, master, _sub1 = build(tmp_path)
    strategy.install_handlers()

    handler = master.handlers[Topic.FILL][0]
    handler(FakeFill(symbol="ETH-USD", size=1.0, side=Side.BUY))

    assert book.effective(MASTER, "ETH-USD") == 0.0
    assert strategy._hedge_queue.empty()


def test_zero_size_fill_is_ignored(tmp_path):
    strategy, _book, master, _sub1 = build(tmp_path)
    strategy.install_handlers()

    handler = master.handlers[Topic.FILL][0]
    handler(FakeFill(symbol=BTC, size=0.0, side=Side.BUY))

    assert strategy._hedge_queue.empty()


def test_replayed_fill_does_not_move_the_position_twice(tmp_path):
    """A reconnect replay must not inflate the book -- v1.0.17 tradeId dedup."""
    strategy, book, master, _sub1 = build(tmp_path)
    strategy.install_handlers()
    book.set_authoritative(MASTER, BTC, 0.0)

    handler = master.handlers[Topic.FILL][0]
    handler(FakeFill(symbol=BTC, size=0.1, side=Side.BUY, trade_id="12000:3"))
    assert book.effective(MASTER, BTC) == 0.1

    # Same execution delivered again after a reconnect.
    handler(FakeFill(symbol=BTC, size=0.1, side=Side.BUY, trade_id="12000:3"))
    assert book.effective(MASTER, BTC) == 0.1

    # A genuinely new execution still applies.
    handler(FakeFill(symbol=BTC, size=0.1, side=Side.BUY, trade_id="12000:4"))
    assert book.effective(MASTER, BTC) == pytest.approx(0.2)


def test_both_accounts_apply_a_trade_that_crossed_between_them(tmp_path):
    """Maker and taker share a tradeId; both sides genuinely moved."""
    strategy, book, master, sub1 = build(tmp_path)
    strategy.install_handlers()
    book.set_authoritative(MASTER, BTC, 0.0)
    book.set_authoritative(SUB1, BTC, 0.0)

    master.handlers[Topic.FILL][0](
        FakeFill(symbol=BTC, size=0.1, side=Side.BUY, trade_id="12000:9")
    )
    sub1.handlers[Topic.FILL][0](
        FakeFill(symbol=BTC, size=0.1, side=Side.SELL, trade_id="12000:9")
    )

    assert book.effective(MASTER, BTC) == 0.1
    assert book.effective(SUB1, BTC) == -0.1


def test_fills_without_a_trade_id_are_all_applied(tmp_path):
    """Pre-v1.0.17 servers send no tradeId; fills must not be dropped."""
    strategy, book, master, _sub1 = build(tmp_path)
    strategy.install_handlers()
    book.set_authoritative(MASTER, BTC, 0.0)

    handler = master.handlers[Topic.FILL][0]
    handler(FakeFill(symbol=BTC, size=0.1, side=Side.BUY, trade_id=None))
    handler(FakeFill(symbol=BTC, size=0.1, side=Side.BUY, trade_id=None))
    assert book.effective(MASTER, BTC) == pytest.approx(0.2)


# -- completion predicates -------------------------------------------------


def test_a_leg_is_neutral_only_when_its_own_net_is_flat(tmp_path):
    """Each leg is judged alone. One symbol going off-hedge must not be able to
    hold the other's phase open -- that lockstep is what the per-leg rewrite
    removed."""
    strategy, book, *_ = build(tmp_path)
    book.set_authoritative(MASTER, BTC, 1.0)
    book.set_authoritative(SUB1, BTC, -1.0)
    book.set_authoritative(SUB1, SOL, 10.0)
    book.set_authoritative(MASTER, SOL, -10.0)
    assert strategy._leg_is_neutral(BTC) is True
    assert strategy._leg_is_neutral(SOL) is True

    book.set_authoritative(SUB1, BTC, -0.5)
    assert strategy._leg_is_neutral(BTC) is False
    assert strategy._leg_is_neutral(SOL) is True


def test_not_neutral_while_a_hedge_is_in_flight(tmp_path):
    strategy, book, *_ = build(tmp_path)
    book.set_authoritative(MASTER, BTC, 0.0)
    book.set_authoritative(SUB1, BTC, 0.0)
    strategy.hedger.in_flight.add(BTC, -0.1)
    assert strategy._leg_is_neutral(BTC) is False


def test_a_leg_is_flat_only_when_both_accounts_hold_nothing(tmp_path):
    strategy, book, *_ = build(tmp_path)
    book.set_authoritative(MASTER, BTC, 0.0)
    book.set_authoritative(SUB1, BTC, 0.0)
    assert strategy._leg_is_flat(BTC) is True

    # A hedged leg is neutral but decidedly not flat. Keeping the two apart is
    # what stops EXIT declaring itself done on a leg that nets to zero while
    # both sides are still open.
    book.set_authoritative(MASTER, BTC, 1.0)
    book.set_authoritative(SUB1, BTC, -1.0)
    assert strategy._leg_is_neutral(BTC) is True
    assert strategy._leg_is_flat(BTC) is False


# -- end-to-end hedge sequence --------------------------------------------


async def test_full_cycle_keeps_the_pair_neutral(tmp_path):
    """Walk the sequence from the strategy description and check net stays ~0."""
    strategy, book, master, sub1 = build(tmp_path)
    hedger = strategy.hedger
    open_btc = {r.symbol: r for r in strategy.roles_for(Phase.OPEN)}[BTC]
    exit_btc = {r.symbol: r for r in strategy.roles_for(Phase.EXIT)}[BTC]

    book.set_authoritative(MASTER, BTC, 0.0)
    book.set_authoritative(SUB1, BTC, 0.0)

    # Entry fills 0.10 then 0.05, each hedged as it happens.
    for size in (0.10, 0.05):
        book.apply_fill(MASTER, BTC, is_buy=True, size=size)
        result = await hedger.hedge(open_btc, mark_price=100_000.0)
        assert result.hedged_size == pytest.approx(size)
        book.apply_fill(SUB1, BTC, is_buy=False, size=size)
        hedger.note_taker_fill(BTC, -size)
        assert book.net(MASTER, SUB1, BTC) == pytest.approx(0.0)

    # Fully open: master long 0.15, sub1 short 0.15.
    book.set_authoritative(MASTER, BTC, 0.15)
    book.set_authoritative(SUB1, BTC, -0.15)

    # Exit: sub1 buys back 0.05 of its short; master must sell 0.05.
    book.apply_fill(SUB1, BTC, is_buy=True, size=0.05)
    result = await hedger.hedge(exit_btc, mark_price=100_000.0)

    assert result.hedged_size == pytest.approx(0.05)
    assert master.orders[-1] == (BTC, False, 0.05, True)  # sell, reduce-only
    book.apply_fill(MASTER, BTC, is_buy=False, size=0.05)
    hedger.note_taker_fill(BTC, -0.05)
    assert book.net(MASTER, SUB1, BTC) == pytest.approx(0.0)

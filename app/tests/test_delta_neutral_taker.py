"""`mode: delta-neutral`: a master and a random sub of its own, taker orders only.

Nothing rests. The opener takes liquidity with IOC slices, each sized to the
book on BOTH sides -- the side it walks now, and the side its hedge walks a few
milliseconds later -- and no slice goes out until the one before it is hedged.
"""

import random

import pytest

from bulkdn.config import Config, ConfigError, LegConfig, RiskConfig, TakerConfig
from bulkdn.chaser import ChaseParams
from bulkdn.feed import Quote
from bulkdn.hedger import LegRoles
from bulkdn.marketdata import MarketSpec
from bulkdn.pairing import Pairing
from bulkdn.positions import PositionBook
from bulkdn.state import LegState
from bulkdn.taking import SlicePlan, TakerExecutor, Wait, plan_slice, side_depth

BTC = "BTC-USD"
SPEC = MarketSpec(symbol=BTC, tick_size=0.5, lot_size=0.001, min_notional=10.0)
TAKER = TakerConfig(max_slippage_bps=2.0, max_spread_bps=3.0, depth_fraction=0.5)

BIDS = [(100_000.0, 0.2), (99_999.5, 0.3), (99_970.0, 5.0)]
ASKS = [(100_000.5, 0.1), (100_001.0, 0.4), (100_030.0, 5.0)]


# -- reading the book --------------------------------------------------------


def test_depth_stops_at_the_band():
    # 2bps past 100000.5 is ~100020.5: the 100030 level is outside.
    depth = side_depth(ASKS, taker_buys=True, band_bps=2.0)
    assert depth.size == pytest.approx(0.5)
    assert depth.touch == 100_000.5


def test_a_sellers_band_runs_down_the_bids():
    depth = side_depth(BIDS, taker_buys=False, band_bps=2.0)
    assert depth.size == pytest.approx(0.5)


def test_the_slice_is_a_share_of_the_thinner_side():
    plan = plan_slice(
        opener_buys=True, bids=BIDS, asks=ASKS, spec=SPEC,
        remaining=10.0, cap=10.0, settings=TAKER,
    )
    assert isinstance(plan, SlicePlan)
    # Half of 0.5 on either side.
    assert plan.size == pytest.approx(0.25)
    # 0.1 at the touch is not enough; the IOC reaches the next level, no further.
    assert plan.limit_price == 100_001.0


def test_the_hedge_side_limits_the_slice_too():
    thin_bids = [(100_000.0, 0.02), (99_000.0, 9.0)]
    plan = plan_slice(
        opener_buys=True, bids=thin_bids, asks=ASKS, spec=SPEC,
        remaining=10.0, cap=10.0, settings=TAKER,
    )
    assert plan.size == pytest.approx(0.01)


def test_remaining_and_cap_bound_the_slice():
    plan = plan_slice(
        opener_buys=False, bids=BIDS, asks=ASKS, spec=SPEC,
        remaining=0.05, cap=10.0, settings=TAKER,
    )
    assert plan.size == pytest.approx(0.05)
    assert plan.limit_price == 100_000.0
    plan = plan_slice(
        opener_buys=False, bids=BIDS, asks=ASKS, spec=SPEC,
        remaining=10.0, cap=0.03, settings=TAKER,
    )
    assert plan.size == pytest.approx(0.03)


def test_a_wide_spread_waits():
    asks = [(100_050.0, 5.0)]
    plan = plan_slice(
        opener_buys=True, bids=BIDS, asks=asks, spec=SPEC,
        remaining=1.0, cap=1.0, settings=TAKER,
    )
    assert isinstance(plan, Wait) and "spread" in plan.reason


def test_a_book_too_thin_for_the_minimum_waits():
    asks = [(100_000.5, 0.0001)]
    plan = plan_slice(
        opener_buys=True, bids=BIDS, asks=asks, spec=SPEC,
        remaining=1.0, cap=1.0, settings=TAKER,
    )
    assert isinstance(plan, Wait) and "thin book" in plan.reason


def test_an_empty_side_waits():
    plan = plan_slice(
        opener_buys=True, bids=[], asks=ASKS, spec=SPEC,
        remaining=1.0, cap=1.0, settings=TAKER,
    )
    assert isinstance(plan, Wait)


def test_expected_give_up_is_the_spread_at_the_touch():
    bids = [(100_000.0, 5.0)]
    asks = [(100_001.0, 5.0)]
    plan = plan_slice(
        opener_buys=True, bids=bids, asks=asks, spec=SPEC,
        remaining=0.01, cap=1.0, settings=TAKER,
    )
    assert plan.expected_bps == pytest.approx(plan.spread_bps, rel=1e-6)


# -- the executor ------------------------------------------------------------


MASTER = "m1"
SUB = "m1s1"
OPEN = LegRoles(BTC, maker=MASTER, taker=SUB, maker_is_buy=True, reduce_only=False, id="g1")
EXIT = LegRoles(BTC, maker=MASTER, taker=SUB, maker_is_buy=False, reduce_only=True, id="g1")


class FakeSession:
    def __init__(self, name, pubkey):
        self.name = name
        self.pubkey = pubkey
        self.taken = []

    async def take(self, symbol, is_buy, price, size, reduce_only=False):
        self.taken.append((symbol, is_buy, price, size, reduce_only))
        return []


class FakeFeed:
    def __init__(self, bids=BIDS, asks=ASKS, age=0.0):
        self.specs = {BTC: SPEC}
        self.bids, self.asks, self.age = bids, asks, age

    def quote(self, symbol):
        return Quote(
            symbol, best_bid=self.bids[0][0] if self.bids else None,
            best_ask=self.asks[0][0] if self.asks else None,
            mark_price=100_000.25, age_s=self.age,
        )

    def book_age_s(self, symbol):
        return self.age

    def levels(self, symbol, is_buy, n):
        return list(self.bids if is_buy else self.asks)[:n]

    def reference_price(self, symbol):
        return 100_000.25


def build(feed=None, neutral=True):
    book = PositionBook(overlay_ttl_ms=5000)
    master, sub = FakeSession("m1", MASTER), FakeSession("m1s1", SUB)
    executor = TakerExecutor(
        sessions={MASTER: master, SUB: sub},
        feed=feed or FakeFeed(),
        book=book,
        params={BTC: ChaseParams(offset_bps=0, max_distance_bps=5, max_order_size=1.0)},
        settings=TakerConfig(slice_interval_s=0.0, fill_wait_s=60.0),
    )
    state = {"neutral": neutral, "unread": []}
    executor.attach(
        is_neutral=lambda roles: state["neutral"],
        waiting_on_a_read=lambda roles: False,
        mark_unread=lambda session, why: state["unread"].append(why),
    )
    return executor, book, master, state


async def test_a_slice_goes_out_as_an_ioc_sized_to_the_book():
    executor, _book, master, _ = build()
    leg = LegState(symbol=BTC, target_size=1.0)
    outcome = await executor.step(OPEN, leg)
    assert outcome.action == "placed"
    assert master.taken == [(BTC, True, 100_001.0, pytest.approx(0.25), False)]


async def test_nothing_more_goes_out_until_the_fill_lands():
    executor, book, master, _ = build()
    leg = LegState(symbol=BTC, target_size=1.0)
    await executor.step(OPEN, leg)
    await executor.step(OPEN, leg)
    assert len(master.taken) == 1
    book.apply_fill(MASTER, BTC, True, 0.25)
    await executor.step(OPEN, leg)
    assert len(master.taken) == 2


async def test_nothing_more_goes_out_while_the_hedge_is_running():
    executor, book, master, state = build(neutral=False)
    leg = LegState(symbol=BTC, target_size=1.0)
    outcome = await executor.step(OPEN, leg)
    assert outcome.action == "waiting" and master.taken == []
    state["neutral"] = True
    await executor.step(OPEN, leg)
    assert len(master.taken) == 1


async def test_the_open_completes_at_its_target():
    executor, book, master, _ = build()
    leg = LegState(symbol=BTC, target_size=0.25)
    book.apply_fill(MASTER, BTC, True, 0.25)
    outcome = await executor.step(OPEN, leg)
    assert outcome.action == "complete" and leg.complete
    assert master.taken == []


async def test_the_exit_takes_reduce_only_what_is_held():
    executor, book, master, _ = build()
    book.apply_fill(MASTER, BTC, True, 0.05)
    leg = LegState(symbol=BTC, target_size=0.05)
    await executor.step(EXIT, leg)
    assert master.taken == [(BTC, False, 100_000.0, pytest.approx(0.05), True)]


async def test_a_stale_book_takes_nothing():
    executor, _book, master, _ = build(feed=FakeFeed(age=60.0))
    leg = LegState(symbol=BTC, target_size=1.0)
    outcome = await executor.step(OPEN, leg)
    assert outcome.action == "waiting" and master.taken == []


async def test_a_slice_with_no_answer_marks_the_book_behind():
    executor, _book, master, state = build()

    async def lost(*args, **kwargs):
        raise TimeoutError("no answer")

    master.take = lost
    leg = LegState(symbol=BTC, target_size=1.0)
    with pytest.raises(TimeoutError):
        await executor.step(OPEN, leg)
    assert state["unread"]
    # And nothing more goes out until it has landed or been read.
    master.take = FakeSession("x", "x").take
    outcome = await executor.step(OPEN, leg)
    assert outcome.action == "waiting"


def test_nothing_ever_rests():
    executor, *_ = build()
    assert executor.may_be_resting(None, "oid") is False


# -- who trades with whom ----------------------------------------------------


def pool():
    owner = {"m1": 1, "m1s1": 1, "m1s2": 1, "m2": 2, "m2s1": 2}
    return Pairing(
        pool=list(owner), max_groups=5, max_takers=3, owner=owner,
        own_tree=True, tree_roots=frozenset({"m1", "m2"}), rng=random.Random(7),
    )


def test_every_group_is_a_master_and_its_own_sub():
    for seed in range(40):
        pairing = pool()
        pairing.rng = random.Random(seed)
        _id, group = pairing.draw(BTC)
        accounts = set(group.accounts)
        assert len(accounts) == 2 and len(group.takers) == 1
        master = accounts & {"m1", "m2"}
        assert len(master) == 1
        (m,) = master
        (sub,) = accounts - master
        assert sub.startswith(m + "s")


def test_the_sub_is_drawn_at_random_and_either_may_open():
    subs, openers = set(), set()
    for seed in range(60):
        pairing = pool()
        pairing.rng = random.Random(seed)
        pairing.busy.add("m2")
        _id, group = pairing.draw(BTC)
        subs.update(a for a in group.accounts if a != "m1")
        openers.add(group.maker == "m1")
    assert subs == {"m1s1", "m1s2"}
    assert openers == {True, False}


def test_a_busy_master_draws_nothing_more_from_its_tree():
    pairing = pool()
    pairing.draw(BTC)
    pairing.draw(BTC)
    assert pairing.draw(BTC) is None
    assert len(pairing.active) == 2


# -- the setting -------------------------------------------------------------


def make(mode="delta-neutral", **taker):
    return Config(
        markets=[LegConfig(symbol=BTC, size=1.0, offset_bps=0, max_distance_bps=5,
                           max_order_size=1.0)],
        mode=mode, risk=RiskConfig(), private_keys=["k1"],
        taking=TakerConfig(**taker),
    )


def test_delta_neutral_is_a_mode():
    make().validate(require_credentials=False)


def test_a_depth_fraction_over_one_is_refused():
    with pytest.raises(ConfigError, match="depth_fraction"):
        make(depth_fraction=1.5).validate(require_credentials=False)


@pytest.mark.parametrize("spelling", ["delta-neutral", "delta_neutral", "DN", "taker"])
def test_the_file_takes_the_ways_people_write_it(tmp_path, spelling):
    from bulkdn.config import load_config

    path = tmp_path / "settings.yaml"
    path.write_text(
        f"mode: {spelling}\n"
        "markets:\n  - symbol: BTC-USD\n    notional_usd: 100\n"
        "taking:\n  max_slippage_bps: 1.5\n  depth_fraction: 0.3\n",
        encoding="utf-8",
    )
    config = load_config(str(path), require_credentials=False)
    assert config.mode == "delta-neutral"
    assert config.taking.max_slippage_bps == 1.5
    assert config.taking.depth_fraction == 0.3
    assert config.taking.max_spread_bps == TakerConfig().max_spread_bps


# -- a whole cycle through the strategy's own phases ---------------------------


class CycleSession:
    """An account whose orders fill in full, as the stream would report them."""

    def __init__(self, name, pubkey, on_fill):
        self.name, self.pubkey, self.on_fill = name, pubkey, on_fill
        self.taken, self.hedges = [], []
        self.dry_run = True

    async def take(self, symbol, is_buy, price, size, reduce_only=False):
        self.taken.append(size)
        self.on_fill(self, symbol, is_buy, size, opener=True)
        return []

    async def market(self, symbol, is_buy, size, reduce_only=False):
        self.hedges.append((is_buy, size, reduce_only))
        self.on_fill(self, symbol, is_buy, size, opener=False)
        return []

    async def hedge_market(self, symbol, is_buy, size, reduce_only=False):
        await self.market(symbol, is_buy, size, reduce_only=reduce_only)
        return False


async def test_a_cycle_opens_and_closes_neutral_slice_by_slice(monkeypatch):
    import asyncio

    from bulkdn.hedger import Hedger
    from bulkdn.pairing import Group
    from strategy_double import bare_strategy

    book = PositionBook(overlay_ttl_ms=5000)
    feed = FakeFeed()
    obj = bare_strategy()
    key = "g1:BTC-USD"
    tasks = []

    def on_fill(session, symbol, is_buy, size, opener):
        book.apply_fill(session.pubkey, symbol, is_buy, size)
        if opener:
            # The fill handler hands the leg to the hedge worker.
            tasks.append(asyncio.get_running_loop().create_task(
                obj._hedge_leg(obj._roles_for_key(key))
            ))
        else:
            obj.hedger.note_taker_fill(key, size if is_buy else -size)

    master = CycleSession("m1", MASTER, on_fill)
    sub = CycleSession("m1s1", SUB, on_fill)
    sessions = {MASTER: master, SUB: sub}
    hedger = Hedger(book=book, sessions=sessions, specs={BTC: SPEC}, in_flight_ttl_ms=5000)
    executor = TakerExecutor(
        sessions=sessions, feed=feed, book=book,
        params={BTC: ChaseParams(offset_bps=0, max_distance_bps=5, max_order_size=1.0)},
        settings=TakerConfig(slice_interval_s=0.0, poll_interval_s=0.01),
    )
    obj.sessions, obj.book, obj.feed, obj.hedger = sessions, book, feed, hedger
    obj.chaser = executor
    executor.attach(
        is_neutral=lambda roles: obj._leg_is_neutral(roles.key),
        waiting_on_a_read=obj._waiting_on_a_read,
        mark_unread=obj._mark_unread,
    )
    obj.symbols = [BTC]
    obj._groups[key] = Group(BTC, maker=MASTER, takers=(SUB,), shares=(1.0,), maker_is_buy=True)

    async def no_read(*args, **kwargs):
        return None

    monkeypatch.setattr(obj, "_sync_positions", no_read)
    monkeypatch.setattr(obj, "_socket_trouble", lambda session: "")

    await asyncio.wait_for(obj._leg_open(key, 1.0), timeout=10)
    await asyncio.gather(*tasks)
    assert book.effective(MASTER, BTC) == pytest.approx(1.0)
    assert book.effective(SUB, BTC) == pytest.approx(-1.0)
    # Four slices of 0.25 -- half the 0.5 inside the band -- each hedged in full
    # before the next went out.
    assert master.taken == [pytest.approx(0.25)] * 4
    assert [h[1] for h in sub.hedges] == [pytest.approx(0.25)] * 4
    assert all(not is_buy and not reduce for is_buy, _s, reduce in sub.hedges)

    obj.state.leg(key).phase = obj.state.leg(key).phase.__class__.EXIT
    await asyncio.wait_for(obj._leg_exit(key), timeout=10)
    await asyncio.gather(*tasks)
    assert book.effective(MASTER, BTC) == pytest.approx(0.0, abs=1e-12)
    assert book.effective(SUB, BTC) == pytest.approx(0.0, abs=1e-12)
    assert all(reduce for is_buy, _s, reduce in sub.hedges[4:])


async def test_a_slice_that_found_nothing_waits_only_briefly(monkeypatch):
    from bulkdn import taking

    executor, _book, master, _ = build()
    leg = LegState(symbol=BTC, target_size=1.0)
    await executor.step(OPEN, leg)
    assert (await executor.step(OPEN, leg)).action == "waiting"
    # Answered, nothing filled: past the short grace the next slice goes out,
    # well before the full fill_wait_s (60s in this build).
    monkeypatch.setattr(taking.time, "monotonic", lambda: 10**9)
    await executor.step(OPEN, leg)
    assert len(master.taken) == 2


async def test_a_moving_reason_is_logged_once(caplog):
    import logging

    feed = FakeFeed(asks=[(100_050.0, 5.0)])
    executor, *_ = build(feed=feed)
    leg = LegState(symbol=BTC, target_size=1.0)
    with caplog.at_level(logging.INFO, logger="bulkdn.taking"):
        await executor.step(OPEN, leg)
        feed.asks = [(100_060.0, 5.0)]
        await executor.step(OPEN, leg)
    assert sum("spread" in r.getMessage() for r in caplog.records) == 1

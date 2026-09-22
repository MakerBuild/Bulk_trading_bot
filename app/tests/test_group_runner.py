"""Running a drawn group, and giving its accounts back.

A group exists for one cycle: drawn from the pool, traded, disbanded. The part
that has to hold under every ending is the release -- a group that halts, is
stopped, or raises must still hand its accounts back, because a pool that
leaks accounts quietly stops being able to draw and the run winds down to
nothing without ever reporting a fault.
"""

import asyncio
import random

import pytest

from bulkdn.config import Config, LegConfig, RiskConfig
from bulkdn.pairing import Group, Pairing
from bulkdn.state import Phase, StateStore, StrategyState
from bulkdn.strategy import Strategy

BTC = "BTC-USD"


def strategy(tmp_path):
    """A Strategy with only the parts the group runner touches."""
    obj = object.__new__(Strategy)
    obj._groups = {}
    obj._group_ids = {}
    obj._stop = asyncio.Event()
    obj.state = StrategyState()
    obj.store = StateStore(str(tmp_path / "state.json"))
    obj.symbols = [BTC]
    # The runner draws this cycle's offset when it registers the group.
    obj.config = Config(
        markets=[LegConfig(symbol=BTC, size=1.0, offset_bps=2.0,
                           max_distance_bps=5.0)],
        risk=RiskConfig(),
        private_key="x",
    )
    obj.pairing = Pairing(
        pool=[f"acct{n}" for n in range(6)], max_groups=3, rng=random.Random(5)
    )
    return obj


GROUP = Group(BTC, maker="maker-key", takers=("taker-key",), shares=(1.0,))


# -- what a group's leg is called -------------------------------------------


def test_the_key_leads_with_the_group_then_names_the_market(tmp_path):
    """Two groups on one market must sort apart in a log, and every line
    mentioning a leg is read by someone who wants to know the market."""
    assert strategy(tmp_path).group_key(7, BTC) == "g7:BTC-USD"


def test_two_groups_on_one_market_get_different_keys(tmp_path):
    obj = strategy(tmp_path)
    assert obj.group_key(1, BTC) != obj.group_key(2, BTC)


# -- and who makes and who hedges -------------------------------------------


def test_a_groups_roles_come_from_the_group(tmp_path):
    obj = strategy(tmp_path)
    key = obj.group_key(1, BTC)
    obj._groups[key] = GROUP

    roles = obj._roles_for_key(key)
    assert roles.maker == "maker-key"
    assert roles.taker == "taker-key"
    assert roles.symbol == BTC
    assert roles.key == key
    assert roles.reduce_only is False


def test_the_accounts_do_not_swap_on_the_way_out(tmp_path):
    """Where a group differs from a configured pair. Two accounts can swap --
    the one holding the short rests the buy-back. Three hedgers cannot:
    swapping would make one of them the maker and leave the other two holding
    shorts that nothing closes. So the opener rests the close as well, and the
    same hedgers buy back their own shares."""
    obj = strategy(tmp_path)
    key = obj.group_key(1, BTC)
    obj._groups[key] = GROUP
    obj.state.leg(key, BTC).phase = Phase.EXIT

    roles = obj._roles_for_key(key)
    assert roles.maker == "maker-key", "the opener closes what it opened"
    assert roles.hedgers == ("taker-key",)
    assert roles.maker_is_buy is False, "it bought to open, so it sells to close"
    assert roles.reduce_only is True


def test_every_hedger_of_a_split_entry_is_a_hedger_of_the_exit(tmp_path):
    """The failure this prevents is two of three accounts left holding shorts
    that no phase ever closes."""
    obj = strategy(tmp_path)
    key = obj.group_key(1, BTC)
    obj._groups[key] = Group(
        BTC, maker="opener", takers=("t1", "t2", "t3"), shares=(0.5, 0.3, 0.2)
    )

    entry = obj._roles_for_key(key)
    obj.state.leg(key, BTC).phase = Phase.EXIT
    exit_roles = obj._roles_for_key(key)

    assert entry.hedgers == exit_roles.hedgers == ("t1", "t2", "t3")
    assert entry.maker == exit_roles.maker == "opener"
    assert entry.maker_is_buy is True and exit_roles.maker_is_buy is False


def test_an_unknown_key_is_not_a_group(tmp_path):
    assert strategy(tmp_path)._roles_for_key("g99:BTC-USD") is None


# -- the accounts always come back ------------------------------------------


def release_case(tmp_path, ending):
    obj = strategy(tmp_path)
    group_id, group = obj.pairing.draw(BTC)
    before = list(obj.pairing.free)

    async def run_leg(key, size, once=False):
        assert obj._groups[key] is group, "the group was not registered"
        await ending()

    obj._run_leg = run_leg
    return obj, group_id, group, before


def test_a_finished_group_hands_its_accounts_back(tmp_path):
    async def fine():
        return None

    obj, group_id, group, before = release_case(tmp_path, fine)
    asyncio.run(obj._run_group(group_id, group, 1.0))

    assert all(a in obj.pairing.free for a in group.accounts)
    assert sorted(obj.pairing.free) == sorted(before + list(group.accounts))


def test_a_group_that_raises_hands_them_back_too(tmp_path):
    """Otherwise a pool leaks accounts and the run quietly winds down."""
    async def boom():
        raise RuntimeError("exchange said no")

    obj, group_id, group, _before = release_case(tmp_path, boom)
    with pytest.raises(RuntimeError):
        asyncio.run(obj._run_group(group_id, group, 1.0))

    assert all(a in obj.pairing.free for a in group.accounts)


def test_a_cancelled_group_hands_them_back_too(tmp_path):
    """Stopping the run cancels the group tasks."""
    async def forever():
        await asyncio.Event().wait()

    obj, group_id, group, _before = release_case(tmp_path, forever)

    async def drive():
        task = asyncio.create_task(obj._run_group(group_id, group, 1.0))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert all(a in obj.pairing.free for a in group.accounts)


def test_the_registry_is_emptied_with_the_release(tmp_path):
    async def fine():
        return None

    obj, group_id, group, _before = release_case(tmp_path, fine)
    asyncio.run(obj._run_group(group_id, group, 1.0))

    assert obj._groups == {}
    assert obj._group_ids == {}


def test_the_legs_state_survives_the_release(tmp_path):
    """A group that halted mid-cycle has positions, and the state file is
    what a restart reads to find them."""
    async def fine():
        return None

    obj, group_id, group, _before = release_case(tmp_path, fine)
    key = obj.group_key(group_id, group.symbol)
    obj.state.leg(key, group.symbol).target_size = 0.3
    asyncio.run(obj._run_group(group_id, group, 1.0))

    assert obj.state.legs[key].target_size == 0.3


# -- and one group never reaches into another -------------------------------
#
# Three places still resolved a leg by its market after the pool arrived, and
# all three would have crossed between groups trading the same one.


def split_strategy(tmp_path):
    obj = strategy(tmp_path)
    key = obj.group_key(1, BTC)
    obj._groups[key] = Group(
        BTC, maker="opener", takers=("t1", "t2", "t3"), shares=(0.5, 0.3, 0.2)
    )
    return obj, key


def test_a_split_leg_is_not_flat_while_a_later_hedger_holds_something(tmp_path):
    """`roles.taker` is only the FIRST hedger. Reading it alone declared the
    leg flat while the second and third still held the shorts they opened."""
    from bulkdn.marketdata import MarketSpec
    from bulkdn.positions import PositionBook

    obj, key = split_strategy(tmp_path)
    obj.book = PositionBook()
    obj.feed = type("F", (), {"specs": {
        BTC: MarketSpec(symbol=BTC, tick_size=0.5, lot_size=0.001, min_notional=10.0)
    }})()

    obj.book.set_authoritative("opener", BTC, 0.0)
    obj.book.set_authoritative("t1", BTC, 0.0)
    obj.book.set_authoritative("t3", BTC, -0.05)

    assert obj._leg_is_flat(key) is False, "the third hedger's short was ignored"


def test_a_split_leg_is_flat_only_when_every_account_is(tmp_path):
    from bulkdn.marketdata import MarketSpec
    from bulkdn.positions import PositionBook

    obj, key = split_strategy(tmp_path)
    obj.book = PositionBook()
    obj.feed = type("F", (), {"specs": {
        BTC: MarketSpec(symbol=BTC, tick_size=0.5, lot_size=0.001, min_notional=10.0)
    }})()

    assert obj._leg_is_flat(key) is True


def test_the_residual_sweep_touches_only_this_legs_accounts(tmp_path):
    """`self.sessions` is the whole pool. Sweeping it would market-close every
    other group's position in this market, mid-hold, from a cycle that has
    nothing to do with them."""
    import bulkdn.strategy as strategy_mod
    from bulkdn.marketdata import MarketSpec
    from bulkdn.positions import PositionBook

    obj, key = split_strategy(tmp_path)
    obj.sessions = {name: object() for name in
                    ("opener", "t1", "t2", "t3", "stranger-a", "stranger-b")}
    obj.book = PositionBook()
    obj.feed = type("F", (), {"specs": {
        BTC: MarketSpec(symbol=BTC, tick_size=0.5, lot_size=0.001, min_notional=10.0)
    }})()
    obj.book.set_authoritative("t3", BTC, -0.05)
    obj.state.leg(key, BTC).complete = True
    obj.chaser = type("C", (), {})()

    async def straight_through(_key, _is_done, _label):
        return None

    obj._drive_leg = straight_through
    obj._persist = lambda: None

    swept = {}

    async def fake_flatten(sessions, book, feed, symbols):
        swept.update(sessions)

    original = strategy_mod.flatten
    strategy_mod.flatten = fake_flatten
    try:
        asyncio.run(obj._leg_exit(key))
    finally:
        strategy_mod.flatten = original

    assert set(swept) == {"opener", "t1", "t2", "t3"}
    assert "stranger-a" not in swept, "another group's account was swept"

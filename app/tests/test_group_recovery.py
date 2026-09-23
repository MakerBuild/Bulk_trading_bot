"""Picking a group back up after a restart.

The positions a group opened are on the exchange whether or not this process
remembers drawing it. So the thing that must survive a restart is not the
group's convenience but its membership: without knowing which accounts opened
what, nothing can close it, and the exposure sits there hedged-but-unattended
until someone reads the numbers by hand.
"""

import asyncio
import random
import types

import pytest

from bulkdn.marketdata import MarketSpec
from bulkdn.pairing import Group, Pairing
from bulkdn.positions import PositionBook
from bulkdn.state import Phase, StateStore, StrategyState
from bulkdn.strategy import Strategy

BTC = "BTC-USD"
GROUP = Group(BTC, maker="opener", takers=("t1", "t2"), shares=(0.6, 0.4))


def strategy(tmp_path, accounts=("opener", "t1", "t2", "spare")):
    obj = object.__new__(Strategy)
    obj._groups = {}
    obj._group_ids = {}
    obj.state = StrategyState()
    obj.store = StateStore(str(tmp_path / "state.json"))
    obj.sessions = {pubkey: object() for pubkey in accounts}
    obj.pairing = Pairing(pool=list(accounts), max_groups=5, rng=random.Random(2))
    return obj


def remember(obj, phase=Phase.OPEN, group=GROUP, group_id=4):
    key = obj.group_key(group_id, group.symbol)
    leg = obj.state.leg(key, group.symbol)
    leg.group_id = group_id
    leg.maker = group.maker
    leg.takers = list(group.takers)
    leg.shares = list(group.shares)
    leg.maker_is_buy = group.maker_is_buy
    leg.phase = phase
    leg.target_size = 0.25
    return key


# -- what survives ----------------------------------------------------------


def test_the_side_the_group_opened_is_read_back(tmp_path):
    """Without this the exit is guessed, and a guess that comes up wrong
    sells more of a short instead of closing it."""
    selling = Group(
        BTC, maker="opener", takers=("t1", "t2"), shares=(0.6, 0.4),
        maker_is_buy=False,
    )
    obj = strategy(tmp_path)
    remember(obj, group=selling)

    _group_id, restored, _key = obj.restore_groups()[0]
    assert restored.maker_is_buy is False


def test_a_file_written_before_the_side_was_drawn_reads_as_buying(tmp_path):
    """Which is what those runs did, so a cycle resumed out of an older file
    closes the way it opened rather than the other way about."""
    obj = strategy(tmp_path)
    key = obj.group_key(4, BTC)
    remember(obj)

    stored = obj.state.to_dict()
    del stored["legs"][key]["maker_is_buy"]
    obj.state = StrategyState.from_dict(stored)

    assert obj.restore_groups()[0][1].maker_is_buy is True


def test_the_membership_is_written_down_and_read_back(tmp_path):
    obj = strategy(tmp_path)
    key = remember(obj)

    resumed = obj.restore_groups()
    assert len(resumed) == 1
    group_id, group, resumed_key = resumed[0]
    assert (group_id, resumed_key) == (4, key)
    assert group.maker == "opener"
    assert group.takers == ("t1", "t2")
    assert group.shares == (0.6, 0.4)


def test_it_survives_the_state_file_itself(tmp_path):
    """Not just the object: a restart reads JSON, where tuples are lists."""
    obj = strategy(tmp_path)
    remember(obj)
    revived = StrategyState.from_dict(obj.state.to_dict())

    obj.state = revived
    group = obj.restore_groups()[0][1]
    assert group.takers == ("t1", "t2")
    assert group.shares == (0.6, 0.4)


@pytest.mark.parametrize("phase", [Phase.OPEN, Phase.HOLD, Phase.EXIT])
def test_every_unfinished_phase_is_resumed(phase, tmp_path):
    obj = strategy(tmp_path)
    remember(obj, phase=phase)
    assert len(obj.restore_groups()) == 1


@pytest.mark.parametrize("phase", [Phase.IDLE, Phase.COMPLETE])
def test_a_finished_group_is_not_resumed(phase, tmp_path):
    """It holds nothing, and resuming it opens a cycle nobody asked for."""
    obj = strategy(tmp_path)
    remember(obj, phase=phase)
    assert obj.restore_groups() == []


def test_a_configured_leg_is_not_mistaken_for_a_group(tmp_path):
    """single and multi legs have no membership, and must not be resumed as
    though they did."""
    obj = strategy(tmp_path)
    obj.state.leg(BTC).phase = Phase.OPEN
    assert obj.restore_groups() == []


# -- and the accounts are not handed out twice ------------------------------


def test_resumed_accounts_are_busy_again(tmp_path):
    """Re-drawing them would give two groups one set of positions to compute
    their hedges from, and both answers would be wrong."""
    obj = strategy(tmp_path)
    remember(obj)
    obj.restore_groups()

    assert all(a not in obj.pairing.free for a in GROUP.accounts)
    assert "spare" in obj.pairing.free


def test_the_group_id_is_not_reissued(tmp_path):
    """A reused id would make the release of one group free the other's
    accounts."""
    obj = strategy(tmp_path, accounts=("opener", "t1", "t2", "spare", "spare2"))
    remember(obj, group_id=4)
    obj.restore_groups()

    new_id, _group = obj.pairing.draw(BTC)
    assert new_id > 4


def test_the_registry_knows_the_resumed_group(tmp_path):
    obj = strategy(tmp_path)
    key = remember(obj)
    obj.restore_groups()

    assert obj._groups[key].maker == "opener"
    assert obj._group_ids[key] == 4


# -- and exposure that cannot be closed is said out loud --------------------


def test_a_group_whose_key_is_gone_is_reported_not_skipped(tmp_path, caplog):
    """Its position is real and this run cannot sign for it. Quietly ignoring
    it leaves open exposure nobody is told about."""
    obj = strategy(tmp_path, accounts=("opener", "spare"))
    remember(obj)

    with caplog.at_level("ERROR", logger="bulkdn.strategy"):
        assert obj.restore_groups() == []

    message = caplog.text
    assert "still open" in message
    assert "t1" in message or "t2" in message


def test_such_a_group_does_not_reserve_accounts(tmp_path):
    obj = strategy(tmp_path, accounts=("opener", "spare"))
    remember(obj)
    obj.restore_groups()
    assert "opener" in obj.pairing.free


# -- a pool never falls back to the configured pair --------------------------
#
# The "configured pair" of a pool is pool[0] and pool[1] -- in multi mode m1
# and m1s1, which usually sit in different groups. Recovery reconciled it
# before the groups were back, and on a restart mid-cycle that bought or sold
# on an account no group owned: unhedged, and nothing would ever close it.


SPEC = MarketSpec(symbol=BTC, tick_size=0.1, lot_size=0.000001, min_notional=1.0)


class Session:
    def __init__(self, pubkey):
        self.pubkey = pubkey
        self.name = pubkey


class Feed:
    specs = {BTC: SPEC}

    def reference_price(self, symbol):
        return 86_000.0


def pool_strategy(tmp_path, held=None):
    obj = strategy(tmp_path)
    obj.sessions = {pk: Session(pk) for pk in ("opener", "t1", "t2", "spare")}
    obj.symbols = [BTC]
    obj.feed = Feed()
    obj.book = PositionBook(overlay_ttl_ms=5000)
    for pubkey, size in (held or {}).items():
        obj.book.set_authoritative(pubkey, BTC, size)
    return obj


def test_a_key_no_group_owns_resolves_to_nothing(tmp_path):
    obj = pool_strategy(tmp_path)
    assert obj._roles_for_key(BTC) is None


def test_no_group_live_means_nothing_to_reconcile(tmp_path):
    assert pool_strategy(tmp_path)._live_roles() == []


def test_a_bare_market_in_the_queue_means_every_group_on_it(tmp_path):
    obj = pool_strategy(tmp_path)
    key = remember(obj)
    obj.restore_groups()

    assert obj._keys_to_hedge(BTC) == [key]
    assert obj._keys_to_hedge(key) == [key]


async def test_recovery_reconciles_the_restored_groups_not_the_pair(tmp_path, monkeypatch):
    import bulkdn.strategy as strategy_mod

    obj = pool_strategy(tmp_path, held={"opener": 0.02, "t1": -0.012, "t2": -0.008})
    key = remember(obj)
    reconciled = []

    async def reconcile_net(hedger, roles, feed):
        reconciled.append([r.key for r in roles])
        return []

    async def cancel_all(sessions, symbols):
        return None

    monkeypatch.setattr(strategy_mod, "sync_positions_http", lambda s, b: None)
    monkeypatch.setattr(strategy_mod, "cancel_all_orders", cancel_all)
    monkeypatch.setattr(strategy_mod, "reconcile_net", reconcile_net)
    obj.hedger = object()

    await obj._recover()

    assert reconciled == [[key]], "the configured pair was reconciled"
    assert [k for _i, _g, k in obj._restored] == [key]


def test_a_position_no_group_owns_stops_the_run(tmp_path):
    """Nothing in the run would ever close it."""
    obj = pool_strategy(tmp_path, held={"spare": 0.01})
    remember(obj)
    obj.restore_groups()

    with pytest.raises(RuntimeError, match="spare"):
        obj._refuse_unowned_positions()


def test_dust_no_group_owns_is_let_through(tmp_path):
    """Under the minimum order nobody can close it, and every run leaves some."""
    obj = pool_strategy(tmp_path, held={"spare": 0.000002})
    obj._refuse_unowned_positions()


def test_positions_inside_a_restored_group_are_its_own(tmp_path):
    obj = pool_strategy(tmp_path, held={"opener": 0.02, "t1": -0.02})
    remember(obj)
    obj.restore_groups()
    obj._refuse_unowned_positions()


# -- a resumed cycle is finished, not dropped --------------------------------
#
# A group resumed in EXIT matched no branch of `_run_leg` and was marked
# COMPLETE at once; one resumed after its target was met, or past `cycles`,
# returned before doing anything. Either way it was released as finished with
# its positions still open, and nothing would ever close them.



def runner(tmp_path, *, phase, cycles=0, target=None, cycle_index=1):
    obj = strategy(tmp_path)
    key = remember(obj, phase=phase)
    obj.state.leg(key).cycle_index = cycle_index
    obj._stop = asyncio.Event()
    obj.symbols = [BTC]
    obj.config = types.SimpleNamespace(cycles=cycles)
    obj.title = types.SimpleNamespace(set_cycle=lambda n: None)
    obj.guard = types.SimpleNamespace(reset_symbol=lambda *a, **k: None)
    obj.notifier = types.SimpleNamespace(cycle_complete=lambda **k: asyncio.sleep(0))
    obj._roles_for_key = lambda k: None
    ran = []

    async def reached():
        return target

    async def progress():
        return ""

    async def phase_step(name, next_phase):
        async def step(k, *args):
            ran.append(name)
            obj.state.leg(k).phase = next_phase
        return step

    obj._target_reached = reached
    obj.progress_detail = progress
    return obj, key, ran, phase_step


async def _wire(obj, phase_step):
    obj._leg_open = await phase_step("open", Phase.OPEN)
    obj._leg_hold = await phase_step("hold", Phase.HOLD)
    obj._leg_exit = await phase_step("exit", Phase.EXIT)


async def test_a_group_resumed_mid_exit_finishes_its_exit(tmp_path):
    obj, key, ran, phase_step = runner(tmp_path, phase=Phase.EXIT)
    await _wire(obj, phase_step)

    await obj._run_leg(key, 0.25, once=True)

    assert ran == ["exit"], "the close it was in the middle of never ran"


async def test_a_met_target_does_not_drop_a_resumed_hold(tmp_path):
    obj, key, ran, phase_step = runner(
        tmp_path, phase=Phase.HOLD, target="volume reached"
    )
    await _wire(obj, phase_step)

    await obj._run_leg(key, 0.25, once=True)

    assert ran == ["exit"]


async def test_a_spent_cycle_count_does_not_drop_a_resumed_open(tmp_path):
    obj, key, ran, phase_step = runner(tmp_path, phase=Phase.OPEN, cycles=1)
    await _wire(obj, phase_step)

    await obj._run_leg(key, 0.25, once=True)

    assert ran == ["hold", "exit"]


async def test_a_met_target_still_stops_a_new_cycle(tmp_path):
    obj, key, ran, phase_step = runner(
        tmp_path, phase=Phase.COMPLETE, target="volume reached"
    )
    await _wire(obj, phase_step)

    await obj._run_leg(key, 0.25, once=True)

    assert ran == []

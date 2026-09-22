"""Picking a group back up after a restart.

The positions a group opened are on the exchange whether or not this process
remembers drawing it. So the thing that must survive a restart is not the
group's convenience but its membership: without knowing which accounts opened
what, nothing can close it, and the exposure sits there hedged-but-unattended
until someone reads the numbers by hand.
"""

import random

import pytest

from bulkdn.pairing import Group, Pairing
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

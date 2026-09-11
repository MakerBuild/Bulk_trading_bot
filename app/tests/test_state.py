"""State persistence has to survive a crash mid-write."""

import json
import os

import pytest

from bulkdn.state import LegState, Phase, StateStore, StrategyState


def test_missing_file_starts_idle(tmp_path):
    store = StateStore(str(tmp_path / "state.json"))
    state = store.load()
    assert state.phase == Phase.IDLE
    assert state.cycle_index == 0


def test_round_trip(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(str(path))

    state = StrategyState(phase=Phase.HOLD, cycle_index=3, hold_until=123.0)
    state.reset_legs({"BTC-USD": 0.01, "SOL-USD": 1.0})
    state.leg("BTC-USD").oid = "abc123"
    state.leg("BTC-USD").price = 100_000.0
    store.save(state)

    loaded = store.load()
    assert loaded.phase == Phase.HOLD
    assert loaded.cycle_index == 3
    assert loaded.hold_until == 123.0
    assert loaded.leg("BTC-USD").oid == "abc123"
    assert loaded.leg("BTC-USD").target_size == 0.01
    assert loaded.leg("SOL-USD").target_size == 1.0


def test_save_leaves_no_temp_files_behind(tmp_path):
    store = StateStore(str(tmp_path / "state.json"))
    store.save(StrategyState())
    store.save(StrategyState())
    leftovers = [name for name in os.listdir(tmp_path) if name.startswith(".state-")]
    assert leftovers == []


def test_save_replaces_atomically(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(str(path))
    store.save(StrategyState(phase=Phase.OPEN))
    store.save(StrategyState(phase=Phase.EXIT))

    with open(path, encoding="utf-8") as handle:
        assert json.load(handle)["phase"] == "EXIT"


def test_corrupt_state_is_fatal_not_silently_discarded(tmp_path):
    # Discarding it could hide the fact that positions are open, and starting a
    # fresh cycle on top of forgotten positions is the worst outcome.
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unreadable"):
        StateStore(str(path)).load()


def test_creates_missing_directory(tmp_path):
    path = tmp_path / "nested" / "dir" / "state.json"
    StateStore(str(path)).save(StrategyState())
    assert path.exists()


def test_remember_stale_ignores_the_current_order():
    leg = LegState(symbol="BTC-USD", oid="current")
    leg.remember_stale("current")
    leg.remember_stale("old")
    leg.remember_stale("old")  # no duplicates
    assert leg.stale_oids == ["old"]


def test_hold_remaining_never_negative():
    state = StrategyState(hold_until=0.0)
    assert state.hold_remaining_s() == 0.0

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

    state = StrategyState(cycle_index=3)
    for symbol, size in (("BTC-USD", 0.01), ("SOL-USD", 1.0)):
        leg = state.leg(symbol)
        leg.target_size = size
        leg.phase = Phase.HOLD
        leg.hold_until = 123.0
    state.leg("BTC-USD").oid = "abc123"
    state.leg("BTC-USD").price = 100_000.0
    store.save(state)

    loaded = store.load()
    # The persisted pair phase is written from the legs, so it cannot disagree
    # with them the way it did when nothing kept the field in step.
    assert loaded.phase == Phase.HOLD
    assert loaded.leg("BTC-USD").phase == Phase.HOLD
    assert loaded.leg("BTC-USD").hold_until == 123.0
    assert loaded.cycle_index == 3
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


# -- a locked destination must not kill a live run ---------------------------
#
# From a live cycle: the folder was inside OneDrive, which had the state file
# open to upload it, and os.replace raised
#
#     PermissionError: [WinError 5] -> app/state/strategy_state.json
#
# mid-HOLD. The run died with hedged positions open on both accounts and
# nothing left running to close them. The file is a hint about phase; the
# exchange is the ledger. Losing the hint must never cost the positions.


def test_a_transient_lock_is_retried(tmp_path, monkeypatch):
    import os as os_module

    from bulkdn import state as state_module

    store = StateStore(str(tmp_path / "state.json"))
    store.save(StrategyState(phase=Phase.OPEN))

    calls = {"n": 0}
    real_replace = os_module.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(5, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(state_module.os, "replace", flaky)
    monkeypatch.setattr(state_module, "REPLACE_RETRY_DELAY_S", 0.001)

    store.save(StrategyState(phase=Phase.HOLD, cycle_index=4))

    assert calls["n"] == 3, "it should have retried past the lock"
    assert StateStore(str(tmp_path / "state.json")).load().phase == Phase.HOLD


def test_a_permanent_lock_still_raises(tmp_path, monkeypatch):
    """Retrying forever would hide a real problem, like a full disk."""
    from bulkdn import state as state_module

    store = StateStore(str(tmp_path / "state.json"))

    def always_locked(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(state_module.os, "replace", always_locked)
    monkeypatch.setattr(state_module, "REPLACE_RETRY_DELAY_S", 0.001)

    with pytest.raises(PermissionError):
        store.save(StrategyState(phase=Phase.OPEN))


def test_no_temp_files_are_left_behind(tmp_path, monkeypatch):
    from bulkdn import state as state_module

    store = StateStore(str(tmp_path / "state.json"))
    monkeypatch.setattr(
        state_module.os, "replace",
        lambda src, dst: (_ for _ in ()).throw(PermissionError(5, "denied")),
    )
    monkeypatch.setattr(state_module, "REPLACE_RETRY_DELAY_S", 0.001)

    with pytest.raises(PermissionError):
        store.save(StrategyState(phase=Phase.OPEN))

    assert not list(tmp_path.glob(".state-*.tmp")), "a temp file survived the failure"


def test_an_unchanged_save_writes_nothing(tmp_path, monkeypatch):
    """`_drive_leg` saves twice a second while the contents change only on a
    phase transition. Rewriting the file that often is what put it under a sync
    client's nose in the first place."""
    from bulkdn import state as state_module

    store = StateStore(str(tmp_path / "state.json"))
    state = StrategyState(phase=Phase.HOLD)
    store.save(state)

    writes = {"n": 0}
    real_replace = state_module.os.replace

    def counted(src, dst):
        writes["n"] += 1
        return real_replace(src, dst)

    monkeypatch.setattr(state_module.os, "replace", counted)

    for _ in range(20):
        store.save(state)
    assert writes["n"] == 0, "an unchanged state was written to disk"

    state.phase = Phase.EXIT
    store.save(state)
    assert writes["n"] == 1, "a changed state must still be written"


def test_a_deleted_file_is_rewritten_even_if_unchanged(tmp_path):
    """The skip must not leave the bot with no state file at all."""
    path = tmp_path / "state.json"
    store = StateStore(str(path))
    state = StrategyState(phase=Phase.HOLD)
    store.save(state)
    path.unlink()

    store.save(state)
    assert path.exists()


# -- a leg filed under something other than its market ----------------------
#
# Two groups trading one market at once is the point of the account pool, and
# until now a leg WAS its market: both would have written over each other's
# phase, order id and cycle count.


def test_a_leg_is_still_filed_under_its_market_by_default():
    """Every caller and every state file written so far means this."""
    state = StrategyState()
    leg = state.leg("BTC-USD")
    assert leg.symbol == "BTC-USD"
    assert leg.id == "BTC-USD"


def test_two_legs_on_one_market_keep_separate_state():
    state = StrategyState()
    first = state.leg("g1:BTC-USD", symbol="BTC-USD")
    second = state.leg("g2:BTC-USD", symbol="BTC-USD")

    first.oid = "order-one"
    first.cycle_index = 4

    assert second.oid is None, "one leg's order leaked into the other"
    assert second.cycle_index == 0
    assert first.symbol == second.symbol == "BTC-USD"


def test_asking_twice_returns_the_same_leg():
    state = StrategyState()
    assert state.leg("g1:BTC-USD", symbol="BTC-USD") is state.leg("g1:BTC-USD")


def test_a_keyed_leg_survives_a_restart():
    state = StrategyState()
    state.leg("g1:BTC-USD", symbol="BTC-USD").target_size = 0.25

    revived = StrategyState.from_dict(state.to_dict())
    leg = revived.leg("g1:BTC-USD")
    assert leg.target_size == 0.25
    assert leg.symbol == "BTC-USD"
    assert leg.id == "g1:BTC-USD"


def test_a_state_file_written_before_ids_existed_still_loads():
    """It was keyed by market, which is exactly what the id then was."""
    revived = StrategyState.from_dict(
        {"legs": {"BTC-USD": {"symbol": "BTC-USD", "target_size": 0.5}}}
    )
    leg = revived.leg("BTC-USD")
    assert leg.id == "BTC-USD"
    assert leg.target_size == 0.5


# -- a dry run keeps its own file -------------------------------------------


def test_a_dry_run_never_writes_the_live_state_file():
    """A live run resumed three groups a dry run had drawn, "mid-OPEN"."""
    from bulkdn.state import dry_run_path

    live = "./app/state/strategy_state.json"
    assert dry_run_path(live) != live
    assert dry_run_path(live).endswith(".dry.json")

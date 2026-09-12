"""Each leg runs its own OPEN -> HOLD -> EXIT.

They were driven in lockstep by one loop, and that made every leg wait for the
slowest. A BTC leg that had filled on both accounts sat idle until ETH filled
before its hold could even start, and an ETH leg that had closed could not
place its next entry until BTC had closed too.

Nothing required it. The hedge is computed per symbol -- `net = position[maker]
+ position[taker]` -- so the two legs were only ever sharing a phase by
accident of how the loop was written.
"""

import time

import pytest

from bulkdn.liquidation import LiquidationGuard
from bulkdn.marketdata import MarketSpec
from bulkdn.positions import PositionBook
from bulkdn.state import LegState, Phase, StrategyState

BTC = "BTC-USD"
ETH = "ETH-USD"
ACCT = "EXAMPLE-ACCOUNT"

SPECS = {
    BTC: MarketSpec(BTC, tick_size=0.5, lot_size=0.001, min_notional=1.0),
    ETH: MarketSpec(ETH, tick_size=0.01, lot_size=0.001, min_notional=50.0),
}


# -- the state carries a phase per leg ---------------------------------------


def test_legs_can_sit_in_different_phases():
    state = StrategyState(legs={
        BTC: LegState(symbol=BTC, phase=Phase.HOLD),
        ETH: LegState(symbol=ETH, phase=Phase.OPEN),
    })
    assert state.leg(BTC).phase == Phase.HOLD
    assert state.leg(ETH).phase == Phase.OPEN


def test_one_legs_hold_clock_does_not_touch_the_other():
    state = StrategyState(legs={BTC: LegState(symbol=BTC), ETH: LegState(symbol=ETH)})
    state.leg(BTC).hold_until = time.time() + 60

    assert state.leg(BTC).hold_remaining_s() > 0
    assert state.leg(ETH).hold_remaining_s() == 0


def test_legs_count_their_own_cycles():
    """An ETH leg that closes first gets on with its next cycle alone."""
    state = StrategyState(legs={BTC: LegState(symbol=BTC), ETH: LegState(symbol=ETH)})
    state.leg(ETH).cycle_index = 4
    state.leg(BTC).cycle_index = 3
    assert (state.leg(ETH).cycle_index, state.leg(BTC).cycle_index) == (4, 3)


# -- what a display should say about a split pair ----------------------------


@pytest.mark.parametrize("btc,eth,shown", [
    (Phase.OPEN, Phase.OPEN, Phase.OPEN),
    (Phase.HOLD, Phase.OPEN, Phase.OPEN),      # never claim to be further along
    (Phase.EXIT, Phase.HOLD, Phase.HOLD),
    (Phase.COMPLETE, Phase.EXIT, Phase.EXIT),
    (Phase.HALTED, Phase.OPEN, Phase.HALTED),  # a halt is never hidden
])
def test_the_summary_follows_the_slowest_leg(btc, eth, shown):
    state = StrategyState(legs={
        BTC: LegState(symbol=BTC, phase=btc),
        ETH: LegState(symbol=ETH, phase=eth),
    })
    assert state.summary_phase == shown


# -- an old state file still loads -------------------------------------------


def test_a_file_written_before_legs_had_phases():
    """Its legs inherit the cycle's phase, which is what that file meant."""
    state = StrategyState.from_dict({
        "phase": "HOLD",
        "cycle_index": 3,
        "hold_until": 1_700_000_000.0,
        "legs": {BTC: {"symbol": BTC, "target_size": 0.001, "complete": True}},
    })
    leg = state.leg(BTC)
    assert leg.phase == Phase.HOLD
    assert leg.hold_until == 1_700_000_000.0
    assert leg.cycle_index == 3


def test_phases_survive_a_round_trip():
    state = StrategyState(legs={
        BTC: LegState(symbol=BTC, phase=Phase.EXIT, hold_until=5.0, cycle_index=2),
        ETH: LegState(symbol=ETH, phase=Phase.OPEN),
    })
    back = StrategyState.from_dict(state.to_dict())
    assert back.leg(BTC).phase == Phase.EXIT
    assert back.leg(BTC).cycle_index == 2
    assert back.leg(ETH).phase == Phase.OPEN


# -- the liquidation guard has to follow suit --------------------------------


def book_with(btc, eth):
    book = PositionBook(overlay_ttl_ms=5000)
    book.set_authoritative(ACCT, BTC, btc)
    book.set_authoritative(ACCT, ETH, eth)
    return book


def test_an_exiting_leg_shrinking_is_not_a_liquidation():
    """While the other leg, still holding, is watched as closely as ever."""
    guard = LiquidationGuard(specs=SPECS, names={ACCT: "master"})
    phases = {BTC: Phase.EXIT, ETH: Phase.HOLD}

    guard.check(book_with(1.0, 1.0), phases, [ACCT])
    found = guard.check(book_with(0.4, 0.0), phases, [ACCT])

    assert [f.symbol for f in found] == [ETH]


def test_a_single_phase_still_works():
    """Callers that have one phase for the pair are unaffected."""
    guard = LiquidationGuard(specs=SPECS, names={ACCT: "master"})
    guard.check(book_with(1.0, 1.0), Phase.HOLD, [ACCT])
    found = guard.check(book_with(0.0, 1.0), Phase.HOLD, [ACCT])
    assert [f.symbol for f in found] == [BTC]


def test_resetting_one_leg_leaves_the_others_peak():
    """Clearing both would erase the high-water mark of a leg still holding,
    and its next real shrink would then go unnoticed."""
    guard = LiquidationGuard(specs=SPECS, names={ACCT: "master"})
    phases = {BTC: Phase.HOLD, ETH: Phase.HOLD}
    guard.check(book_with(1.0, 1.0), phases, [ACCT])

    guard.reset_symbol(BTC)

    found = guard.check(book_with(0.0, 0.0), phases, [ACCT])
    assert [f.symbol for f in found] == [ETH], "BTC was reset; ETH was not"


# -- the persisted pair phase cannot drift from the legs ---------------------
#
# Nothing updates the pair's own phase field while a cycle runs -- the legs
# carry it now. Persisting the field raw recorded whatever it happened to be at
# startup, and a live `flatten` read that as IDLE, skipped resetting a state
# whose legs still held an order id, and reported nothing wrong.


def test_the_saved_phase_is_written_from_the_legs(tmp_path):
    from bulkdn.state import StateStore

    state = StrategyState(legs={
        BTC: LegState(symbol=BTC, phase=Phase.OPEN, oid="still-resting"),
        ETH: LegState(symbol=ETH, phase=Phase.HOLD),
    })
    # The stale field, exactly as a live run left it.
    state.phase = Phase.IDLE

    path = tmp_path / "state.json"
    StateStore(str(path)).save(state)

    assert StateStore(str(path)).load().phase == Phase.OPEN


def test_a_halt_still_outranks_the_legs(tmp_path):
    from bulkdn.state import StateStore

    state = StrategyState(legs={BTC: LegState(symbol=BTC, phase=Phase.OPEN)})
    state.phase = Phase.HALTED
    state.halted_reason = "something went wrong"

    path = tmp_path / "state.json"
    StateStore(str(path)).save(state)
    loaded = StateStore(str(path)).load()

    assert loaded.phase == Phase.HALTED
    assert loaded.halted_reason == "something went wrong"


def test_a_leg_holding_an_order_is_not_idle():
    """What flatten keys off. An order id with an IDLE pair is the bug."""
    state = StrategyState(legs={BTC: LegState(symbol=BTC, oid="still-resting")})
    assert state.legs, "flatten must see the leg even when the phase reads IDLE"

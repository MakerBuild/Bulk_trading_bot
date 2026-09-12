"""A phase that will not finish.

OPEN and EXIT wait on fills, and a resting order may simply never fill. Nothing
bounded that: `_drive` looped until its completion condition came true, and in
EXIT the sweep that closes whatever the exit legs left runs only *after* the
loop returns. So an exit that could not fill left positions open indefinitely
while the bot went on logging as though it were working.

HOLD is exempt because its condition is a clock it set itself.

Driven by a fake clock rather than by waiting: the check sits at the top of the
loop, so a clock that has already jumped past the budget trips it on the first
pass, before the phase body runs. That keeps the test about the timeout instead
of about everything `_drive` touches.
"""

import asyncio

import pytest

from bulkdn import strategy as strategy_module
from bulkdn.config import ConfigError
from bulkdn.state import Phase
from bulkdn.strategy import Halted, Strategy, phase_budget_s


@pytest.fixture
def clock(monkeypatch):
    """First reading is 0; every reading after it is `clock.now`."""
    def monotonic():
        value = clock.readings
        clock.readings = clock.now
        return value

    clock.readings = 0.0
    clock.now = 0.0
    monkeypatch.setattr(strategy_module.time, "monotonic", monotonic)
    return clock


def build(max_phase_minutes, phase=Phase.EXIT):
    s = Strategy.__new__(Strategy)
    s.config = type("C", (), {
        "max_phase_minutes": max_phase_minutes,
        "chase_interval_s": 0.01,
        "reconcile_interval_s": 999.0,
    })()
    s.risk = type("R", (), {"check": lambda self: []})()
    s.store = type("St", (), {"save": lambda self, _s: None})()
    s._stop = asyncio.Event()
    s._halt_reason = None
    s._reconnects = 0
    s.title = type("T", (), {"halted": lambda self, _r: None})()
    s.notifier = type("N", (), {
        "send_soon": lambda self, _c: None,
        "halted": lambda self, _r: None,
    })()
    s.state = type("S", (), {"phase": phase})()
    s.roles_for = lambda _p: []
    return s


async def drive(s, phase):
    try:
        await Strategy._drive(s, phase, lambda: False, "exit")
    except Halted as exc:
        return str(exc)
    return None


async def test_a_phase_that_cannot_finish_is_halted(clock):
    """And a halt cancels and flattens, which is the point of raising."""
    clock.now = 40 * 60  # forty minutes in
    reason = await drive(build(max_phase_minutes=30), Phase.EXIT)

    assert reason is not None, "the loop ran on"
    assert "did not finish within" in reason
    assert "30" in reason


@pytest.mark.parametrize("phase", [Phase.OPEN, Phase.EXIT])
def test_the_budget_applies_to_the_phases_that_wait_on_fills(phase):
    assert phase_budget_s(phase, 30) == 30 * 60


def test_hold_is_exempt():
    """It ends on a deadline it set itself; a cap could only cut it short."""
    assert phase_budget_s(Phase.HOLD, 30) == 0.0


@pytest.mark.parametrize("minutes", [0, 0.0, -1])
def test_zero_or_less_switches_it_off(minutes):
    assert phase_budget_s(Phase.EXIT, minutes) == 0.0


def test_the_setting_refuses_a_negative():
    from bulkdn.config import Config, LegConfig

    config = Config(
        master_account=LegConfig(symbol="BTC-USD", size=1.0, max_order_size=1.0),
        sub_account=LegConfig(symbol="ETH-USD", size=1.0, max_order_size=1.0),
        max_phase_minutes=-1.0,
        private_key="x",
        sub1_pubkey="y",
    )
    with pytest.raises(ConfigError, match="max_phase_minutes"):
        config.validate(require_credentials=False, require_sub1=False)

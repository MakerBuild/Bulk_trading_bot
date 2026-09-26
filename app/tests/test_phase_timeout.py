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
from bulkdn.strategy import Strategy, phase_budget_s


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


SYMBOL = "BTC-USD"


def build(max_phase_minutes, phase=Phase.EXIT):
    from bulkdn.state import LegState, StrategyState

    s = Strategy.__new__(Strategy)
    s.config = type("C", (), {
        "max_phase_minutes": max_phase_minutes,
        "chase_interval_s": 0.01,
        "reconcile_interval_s": 999.0,
    })()
    s.store = type("St", (), {"save": lambda self, _s: None})()
    s._stop = asyncio.Event()
    s._halt_reason = None
    s.title = type("T", (), {"halted": lambda self, _r: None})()
    s.notifier = type("N", (), {
        "send_soon": lambda self, _c: None,
        "halted": lambda self, _r: None,
    })()
    s.state = StrategyState(legs={SYMBOL: LegState(symbol=SYMBOL, phase=phase)})
    # The leg is marked done so the chase step is never reached: this is about
    # the clock, not about placing orders.
    s.state.leg(SYMBOL).complete = True
    return s


async def drive(s, phase):
    """Returns the recorded halt reason, if the leg gave up.

    `_drive_leg` records rather than raises: the other leg is a separate task
    and has to unwind on its own before `run` turns the reason into a Halted.
    """
    await Strategy._drive_leg(s, SYMBOL, lambda: False, "exit")
    return s._halt_reason


async def test_a_phase_that_cannot_finish_is_halted(clock):
    """And a halt cancels and flattens, which is the point of raising."""
    clock.now = 40 * 60  # forty minutes in
    s = build(max_phase_minutes=30)
    reason = await drive(s, Phase.EXIT)

    assert reason is not None, "the loop ran on"
    assert s._stop.is_set(), "the other leg was never told to stop"
    assert "did not finish within" in reason
    assert "30" in reason
    assert SYMBOL in reason, "a halt should say which leg stalled"


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
        markets=[
            LegConfig(symbol="BTC-USD", size=1.0, max_order_size=1.0),
            LegConfig(symbol="ETH-USD", size=1.0, max_order_size=1.0),
        ],
        max_phase_minutes=-1.0,
        private_key="x",
    )
    with pytest.raises(ConfigError, match="max_phase_minutes"):
        config.validate(require_credentials=False)


@pytest.fixture
def group_clock(monkeypatch):
    """Like `clock`, but only the strategy's clock.

    `clock` replaces `time.monotonic` itself, which the event loop reads too:
    fine for a test that returns on its first pass, a hang for one that has to
    go round the loop, because the loop's own timer never moves.
    """
    import types

    def monotonic():
        value = group_clock.readings
        group_clock.readings = group_clock.now
        return value

    group_clock.readings = 0.0
    group_clock.now = 0.0
    fake = types.SimpleNamespace(monotonic=monotonic, time=strategy_module.time.time)
    monkeypatch.setattr(strategy_module, "time", fake)
    return group_clock


# -- a drawn group is cut short, not the run --------------------------------
#
# Two live runs ended early this way: a quiet market, one
# group 66% of the way to its target at thirty minutes, and the halt then
# closed every other group at market as well. A pool group now ends its own
# phase and the run carries on.


class FakeSpec:
    lot_size = 0.000001
    min_notional = 1.0


class FakeFeed:
    specs = {SYMBOL: FakeSpec()}

    def reference_price(self, symbol):
        return 84_000.0


class FakeSession:
    name = "m1s5"

    def __init__(self, book, pubkey, refuses=0):
        self.book = book
        self.pubkey = pubkey
        self.refuses = refuses
        self.closes = []

    async def close_market(self, symbol, is_buy, size):
        if self.refuses:
            self.refuses -= 1
            raise RuntimeError("no answer from the exchange")
        self.closes.append((symbol, is_buy, size))
        self.book.set_authoritative(self.pubkey, symbol, 0.0)


class FakeChaser:
    def __init__(self):
        self.cancelled = 0
        self.steps = 0

    async def cancel_leg(self, roles, leg):
        self.cancelled += 1
        leg.oid = None

    async def step(self, roles, leg):
        # What the real chaser does with nothing left to trade.
        self.steps += 1
        if leg.target_size - abs(roles_book(roles).effective(roles.maker, SYMBOL)) < 1e-9:
            leg.complete = True


def roles_book(roles):
    return roles._book


def build_group(phase, *, held, target=0.04, refuses=0):
    from bulkdn.positions import PositionBook

    s = build(max_phase_minutes=30, phase=phase)
    # `sleep(0)` only yields. Anything longer waits on the event loop's timer,
    # which reads the same `time.monotonic` the clock fixture has frozen.
    s.config.chase_interval_s = 0
    leg = s.state.leg(SYMBOL)
    leg.complete = False
    leg.group_id = 24
    leg.oid = "resting"
    leg.target_size = target
    s.book = PositionBook()
    s.book.set_authoritative("maker", SYMBOL, held)
    roles = type("R", (), {"maker": "maker", "symbol": SYMBOL, "key": SYMBOL,
                           "_book": s.book})()
    s._roles_for_key = lambda key: roles
    s._being_swept = lambda roles: False

    async def not_paused(key, roles, leg):
        return False

    async def confirmed(is_done, label, key=None):
        return is_done()

    s._paused_for_a_lagging_socket = not_paused
    s._confirm_done = confirmed
    s.feed = FakeFeed()
    s.chaser = FakeChaser()
    s.sessions = {"maker": FakeSession(s.book, "maker", refuses=refuses)}
    s.alerts = []
    s.notifier = type("N", (), {
        "send_soon": lambda self, coro: coro.close() if coro is not None else None,
        "halted": lambda self, _r: None,
        "error": lambda self, text: s.alerts.append(text) or _noop(),
    })()
    return s, leg


async def _noop_coro():
    return None


def _noop():
    return _noop_coro()


async def test_an_overlong_open_keeps_what_it_filled_and_moves_on(group_clock):
    group_clock.now = 40 * 60
    s, leg = build_group(Phase.OPEN, held=0.0266)

    await Strategy._drive_leg(s, SYMBOL, lambda: leg.complete, "open")

    assert s._halt_reason is None, "one slow group must not end the run"
    assert not s._stop.is_set()
    assert s.chaser.cancelled == 1, "the entry order has to come off first"
    assert leg.target_size == pytest.approx(0.0266)
    assert leg.complete
    assert s.sessions["maker"].closes == [], "an open that is cut short trades nothing"
    assert s.alerts and "keeping" in s.alerts[0]


async def test_an_overlong_exit_closes_the_rest_at_market(group_clock):
    group_clock.now = 40 * 60
    s, leg = build_group(Phase.EXIT, held=0.0123)

    await Strategy._drive_leg(s, SYMBOL, lambda: leg.complete, "exit")

    assert s._halt_reason is None
    assert s.chaser.cancelled == 1
    assert s.sessions["maker"].closes == [(SYMBOL, False, pytest.approx(0.0123))]
    assert s.chaser.steps == 0, "no maker order goes back on the book"
    assert leg.complete


async def test_a_short_exit_is_closed_with_a_buy(group_clock):
    group_clock.now = 40 * 60
    s, leg = build_group(Phase.EXIT, held=-0.02)

    await Strategy._drive_leg(s, SYMBOL, lambda: leg.complete, "exit")

    assert s.sessions["maker"].closes == [(SYMBOL, True, pytest.approx(0.02))]


async def test_a_close_that_fails_is_sent_again(group_clock, monkeypatch):
    monkeypatch.setattr(strategy_module, "CUT_SHORT_RETRY_S", 0.0)
    group_clock.now = 40 * 60
    s, leg = build_group(Phase.EXIT, held=0.01, refuses=2)

    await Strategy._drive_leg(s, SYMBOL, lambda: leg.complete, "exit")

    assert s._halt_reason is None
    assert len(s.sessions["maker"].closes) == 1
    assert leg.complete


async def test_a_cut_that_does_not_finish_either_is_still_a_halt(group_clock, monkeypatch):
    """Past the grace period something really is stuck."""
    s, leg = build_group(Phase.EXIT, held=0.01, refuses=10**6)
    calls = []

    def monotonic():
        # Start, then the overrun is seen and the cut made; every reading
        # after that is past the grace period.
        calls.append(None)
        if len(calls) == 1:
            return 0.0
        if len(calls) <= 4:
            return 40 * 60.0
        return 40 * 60.0 + strategy_module.CUT_SHORT_GRACE_S + 1

    strategy_module.time.monotonic = monotonic

    await asyncio.wait_for(
        Strategy._drive_leg(s, SYMBOL, lambda: leg.complete, "exit"), timeout=5
    )

    assert s._stop.is_set()
    assert "cut short" in s._halt_reason


async def test_a_configured_leg_still_halts(group_clock):
    """No group to cut: a configured leg is the whole run."""
    group_clock.now = 40 * 60
    s, leg = build_group(Phase.EXIT, held=0.01)
    leg.group_id = 0

    await Strategy._drive_leg(s, SYMBOL, lambda: leg.complete, "exit")

    assert s._stop.is_set()
    assert s.sessions["maker"].closes == []

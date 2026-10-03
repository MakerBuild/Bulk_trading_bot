"""Recovering a dropped WebSocket instead of halting on it.

Two live runs ended mid-cycle with "master WebSocket is not connected", each
after the library gave up on a late pong. Nothing was wrong with the strategy:
the exchange's socket blinked and the kill switch fired. A cycle was therefore
only ever as long as the exchange's least reliable minute.

Reconnecting is safe here for a reason specific to this design: the hedge is
derived from positions, not from the fills that announce them. Whatever filled
while the socket was down appears as a position difference on the next HTTP
read and is corrected once. So these tests care about three things -- that a
drop is retried, that a retry never swallows a violation that reconnecting
cannot fix, and that the run is still watched while the retry takes its time.
"""

import asyncio
import types

import pytest

from bulkdn.accounts import SharedReconnect
from bulkdn.hedger import LegRoles
from bulkdn.pairing import Group
from bulkdn.risk import Violation
from strategy_double import bare_strategy


class FakeClient:
    """Connects on the attempt given by `succeed_on`; never, if that is 0."""

    def __init__(self, succeed_on=1, connect_delay=0.0):
        self.succeed_on = succeed_on
        self.connect_delay = connect_delay
        self.attempts = 0
        self.disconnects = 0
        self.is_connected = False
        self.reconnect_share = SharedReconnect()

    async def connect(self):
        self.attempts += 1
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        self.is_connected = bool(self.succeed_on) and self.attempts >= self.succeed_on
        return self.is_connected

    async def disconnect(self):
        self.disconnects += 1
        self.is_connected = False


class FakeSession:
    def __init__(self, name, succeed_on=1, dry_run=False, connect_delay=0.0):
        self.name = name
        self.pubkey = name.upper() + "-PUB"
        self.client = FakeClient(succeed_on, connect_delay)
        self.dry_run = dry_run
        self.reconnects = 0
        self.stream_lagging_until = 0.0
        # How long this socket has been silent. A live socket reports a small
        # number; a half-open one reports a growing one while still claiming to
        # be connected, which is the case the heal has to recognise.
        self.last_message_age_s = 0.0

    @property
    def is_connected(self):
        return self.client.is_connected

    async def reconnect(self, attempts=None, delay=0.0):
        # `attempts` follows production unless a test pins it. It used to be
        # pinned at 3 here, which quietly kept the fake on the old patience
        # after the real one grew.
        self.reconnects += 1
        from bulkdn.accounts import AccountSession

        if attempts is None:
            return await AccountSession.reconnect(self, delay=delay)
        return await AccountSession.reconnect(self, attempts=attempts, delay=delay)


class FakeRisk:
    """The threshold the heal reads, and whatever `check` is told to say."""

    def __init__(self):
        self.config = types.SimpleNamespace(ws_stale_timeout_s=30.0)
        self.violations = []
        self.checks = 0
        self.watch = []

    def check(self):
        self.checks += 1
        return list(self.violations)

    def log_exposure(self):
        pass


def build(master_succeeds_on=1, sub_connected=True, master_delay=0.0, sub_delay=0.0):
    master = FakeSession("master", succeed_on=master_succeeds_on, connect_delay=master_delay)
    sub1 = FakeSession("sub1", succeed_on=1, connect_delay=sub_delay)
    sub1.client.is_connected = sub_connected
    strategy = bare_strategy()
    strategy.sessions = {master.pubkey: master, sub1.pubkey: sub1}
    strategy.risk = FakeRisk()
    strategy.resyncs = 0
    strategy.resync_fails = False

    async def sync(max_age_s=0.0):
        """Stands in for the threaded, shared position read."""
        if strategy.resync_fails:
            raise RuntimeError("HTTP 429")
        strategy.resyncs += 1

    strategy._sync_positions = sync
    return strategy, master, sub1


async def heal(strategy):
    """One supervisor pass's worth of healing, waited out.

    True when nothing halted -- every socket that was down is back.
    """
    strategy._start_heals()
    await asyncio.gather(*strategy._heals.values())
    return strategy._halt_reason is None


DROPPED = [Violation("disconnected", "master WebSocket is not connected")]


# -- a drop is retried ------------------------------------------------------


async def test_a_dropped_socket_is_reconnected():
    strategy, master, _sub1 = build()
    assert await heal(strategy) is True
    assert master.is_connected


async def test_it_retries_before_giving_up():
    strategy, master, _sub1 = build(master_succeeds_on=3)
    assert await heal(strategy) is True
    assert master.client.attempts == 3


async def test_a_socket_that_stays_down_halts_the_run():
    """An unattended bot that cannot see its fills must not trade on."""
    strategy, master, _sub1 = build(master_succeeds_on=0)
    assert await heal(strategy) is False
    assert not master.is_connected
    assert "master" in strategy._halt_reason
    assert strategy._stop.is_set()


async def test_positions_are_re_read_after_a_reconnect():
    """The book stopped updating while the socket was down."""
    strategy, _master, _sub1 = build()
    await heal(strategy)
    assert strategy.resyncs > 0


async def test_nothing_is_re_read_when_the_reconnect_failed():
    strategy, _master, _sub1 = build(master_succeeds_on=0)
    await heal(strategy)
    assert strategy.resyncs == 0


async def test_a_connected_session_is_left_alone():
    strategy, master, sub1 = build()
    master.client.is_connected = True
    await heal(strategy)
    assert master.client.attempts == 0
    assert sub1.client.attempts == 0


async def test_a_dry_run_session_is_never_reconnected():
    strategy, master, _sub1 = build()
    master.dry_run = True
    master.client.is_connected = False
    await heal(strategy)
    assert master.client.attempts == 0


async def test_the_legs_on_a_restored_socket_are_hedged_at_once():
    """Anything that filled before their orders came off was seen only by
    the read, and the reconcile tick may be seconds away."""
    strategy, master, sub1 = build()
    strategy._groups = {
        "g1:BTC-USD": Group(
            symbol="BTC-USD", maker=master.pubkey, takers=(sub1.pubkey,), shares=(1.0,),
        ),
    }
    await heal(strategy)
    assert strategy._hedge_queue.get_nowait() == "g1:BTC-USD"


# -- the supervisor: a retry never swallows a real halt ---------------------


async def supervise_briefly(strategy, seconds=0.05):
    strategy.config.chase_interval_s = 0.001
    strategy.config.reconcile_interval_s = 999.0

    async def no_reconcile():
        await strategy._stop.wait()

    strategy._reconcile_loop = no_reconcile
    task = asyncio.create_task(strategy._supervise())
    await asyncio.sleep(seconds)
    strategy._stop.set()
    await task


async def test_other_violations_halt_without_a_reconnect():
    """Exposure over the cap is the strategy misbehaving, not the socket."""
    strategy, master, _sub1 = build()
    strategy.risk.violations = [Violation("net_exposure", "BTC-USD net $900 exceeds $500")]
    await supervise_briefly(strategy)
    assert "net_exposure" in strategy._halt_reason
    assert master.client.attempts == 0


async def test_a_drop_mixed_with_a_real_breach_still_halts():
    """Otherwise a blinking socket would keep clearing a genuine violation."""
    strategy, master, _sub1 = build()
    strategy.risk.violations = DROPPED + [Violation("reject_streak", "sub1 has 5 rejections")]
    await supervise_briefly(strategy)
    assert "reject_streak" in strategy._halt_reason
    assert master.client.attempts == 0


async def test_a_drop_alone_does_not_halt():
    strategy, master, _sub1 = build()
    strategy.risk.violations = DROPPED
    await supervise_briefly(strategy)
    assert strategy._halt_reason is None
    assert master.client.attempts >= 1


async def test_the_risk_checks_go_on_while_a_reconnect_takes_its_time():
    """The supervisor awaited the heal inline, and one reconnect can take
    minutes: the limits, the liquidation guard and the reconciler all stopped
    while makers on the dropped socket could still rest and fill."""
    strategy, master, _sub1 = build(master_succeeds_on=0, master_delay=0.02)
    strategy.risk.violations = DROPPED
    strategy.config.chase_interval_s = 0.001
    strategy.config.reconcile_interval_s = 999.0
    reconciles = 0

    async def reconcile_loop():
        nonlocal reconciles
        while not strategy._stop.is_set():
            reconciles += 1
            await asyncio.sleep(0.001)

    strategy._reconcile_loop = reconcile_loop
    task = asyncio.create_task(strategy._supervise())
    await asyncio.sleep(0.05)
    try:
        assert not strategy._heals[id(master.client)].done(), "the heal finished already"
        checks_mid_heal = strategy.risk.checks
        await asyncio.sleep(0.03)
        assert strategy.risk.checks > checks_mid_heal, "the risk checks stopped"
        assert reconciles > 1, "the reconciler stopped"
        assert strategy._halt_reason is None
    finally:
        strategy._stop.set()
        for heal_task in strategy._heals.values():
            heal_task.cancel()
        await asyncio.gather(task, *strategy._heals.values(), return_exceptions=True)


async def test_a_reconciler_that_dies_takes_the_supervisor_with_it():
    """Run beside the risk checks, it must not be able to stop unnoticed."""
    strategy, _master, _sub1 = build()
    strategy.config.chase_interval_s = 0.001

    async def boom():
        raise RuntimeError("HTTP 504")

    strategy._reconcile_loop = boom
    with pytest.raises(RuntimeError, match="504"):
        await strategy._supervise()


# -- the legs on a dropped socket stop trading -------------------------------


def paused_leg(strategy, master, sub1):
    pulled = []

    async def cancel_all(symbols):
        pulled.append(symbols)

    master.cancel_all = cancel_all
    roles = LegRoles(
        symbol="BTC-USD", maker=master.pubkey, taker=sub1.pubkey,
        maker_is_buy=True, reduce_only=False,
    )
    leg = types.SimpleNamespace(oid="resting")
    return roles, leg, pulled


async def test_a_leg_on_a_dropped_socket_pulls_its_order_and_waits():
    """Nothing that fills on a dropped socket is announced, and its hedger may
    be on it: no new order goes out there until it is back."""
    strategy, master, sub1 = build()
    roles, leg, pulled = paused_leg(strategy, master, sub1)
    sub1.client.is_connected = False  # the HEDGER's socket dropped

    assert await strategy._paused_for_its_sockets("g1", roles, leg) is True
    assert pulled == [["BTC-USD"]] and leg.oid is None

    sub1.client.is_connected = True
    master.client.is_connected = True
    assert await strategy._paused_for_its_sockets("g1", roles, leg) is False


async def test_a_socket_being_healed_still_pauses_its_legs():
    """A silent socket reads as connected until the heal closes it."""
    strategy, master, sub1 = build()
    master.client.is_connected = True
    roles, leg, _pulled = paused_leg(strategy, master, sub1)
    strategy._heals[id(master.client)] = asyncio.get_running_loop().create_future()

    assert await strategy._paused_for_its_sockets("g1", roles, leg) is True
    strategy._heals[id(master.client)].cancel()


async def test_an_order_the_pause_could_not_pull_is_tried_again(monkeypatch):
    from bulkdn import strategy as strategy_module

    strategy, master, sub1 = build()
    clock = [100.0]
    monkeypatch.setattr(strategy_module.time, "monotonic", lambda: clock[0])
    roles, leg, _pulled = paused_leg(strategy, master, sub1)
    tries = []

    async def refuses(symbols):
        tries.append(symbols)
        raise RuntimeError("HTTP 502")

    master.cancel_all = refuses
    master.client.is_connected = False

    await strategy._paused_for_its_sockets("g1", roles, leg)
    await strategy._paused_for_its_sockets("g1", roles, leg)
    assert len(tries) == 1, "it retried on every tick"
    assert leg.oid == "resting", "an order that may be resting was forgotten"

    clock[0] += strategy_module.PAUSED_CANCEL_RETRY_S
    await strategy._paused_for_its_sockets("g1", roles, leg)
    assert len(tries) == 2, "the order was left resting on a dropped socket"


# -- a flapping socket is a fault, not a blip -------------------------------


async def test_reconnects_are_capped_within_the_window():
    """Otherwise the cycle never ends: no rejection is recorded, so the reject
    streak never trips, and the log scrolls past unread."""
    from bulkdn.strategy import MAX_RECONNECTS

    strategy, master, _sub1 = build()
    for _ in range(MAX_RECONNECTS):
        master.client.is_connected = False
        assert await heal(strategy) is True

    master.client.is_connected = False
    assert await heal(strategy) is False, "the cap did not hold"
    assert "dropped" in strategy._halt_reason


async def test_the_cap_counts_attempts_not_failures():
    """A socket that reconnects every time is still flapping."""
    from bulkdn.strategy import MAX_RECONNECTS

    strategy, master, _sub1 = build()
    for _ in range(MAX_RECONNECTS):
        master.client.is_connected = False
        await heal(strategy)
    assert len(strategy._reconnect_times) == MAX_RECONNECTS


# -- the budget forgives drops that are far apart ----------------------------
#
# The cap was written as "per cycle" and the reset was never implemented, so
# five was the allowance for the entire run. An unlimited run therefore halted
# on its sixth dropped socket however many hours apart they fell -- reported as
# the bot dying overnight with the WebSocket "falling off".


async def test_old_drops_fall_out_of_the_window(monkeypatch):
    """A socket that blips once an hour is not a flapping socket."""
    from bulkdn import strategy as strategy_module
    from bulkdn.strategy import MAX_RECONNECTS, RECONNECT_WINDOW_S

    strategy, master, _sub1 = build()
    clock = [0.0]
    monkeypatch.setattr(strategy_module.time, "monotonic", lambda: clock[0])

    # Spend the whole budget, an hour apart each time.
    for _ in range(MAX_RECONNECTS * 3):
        clock[0] += RECONNECT_WINDOW_S * 6
        master.client.is_connected = False
        assert await heal(strategy) is True, "an isolated drop was refused"

    assert len(strategy._reconnect_times) == 1, "the window did not clear"


async def test_drops_inside_the_window_still_trip_it(monkeypatch):
    """And a socket that blips five times in ten minutes is."""
    from bulkdn import strategy as strategy_module
    from bulkdn.strategy import MAX_RECONNECTS, RECONNECT_WINDOW_S

    strategy, master, _sub1 = build()
    clock = [0.0]
    monkeypatch.setattr(strategy_module.time, "monotonic", lambda: clock[0])

    for _ in range(MAX_RECONNECTS):
        clock[0] += RECONNECT_WINDOW_S / (MAX_RECONNECTS + 2)
        master.client.is_connected = False
        assert await heal(strategy) is True

    clock[0] += 1.0
    master.client.is_connected = False
    assert await heal(strategy) is False, "the cap did not hold"


async def test_the_window_reopens_once_the_burst_ages_out(monkeypatch):
    """A halt is for a fault happening now, not for one that has passed."""
    from bulkdn import strategy as strategy_module
    from bulkdn.strategy import MAX_RECONNECTS, RECONNECT_WINDOW_S

    strategy, master, _sub1 = build()
    clock = [0.0]
    monkeypatch.setattr(strategy_module.time, "monotonic", lambda: clock[0])

    for _ in range(MAX_RECONNECTS):
        master.client.is_connected = False
        await heal(strategy)
    assert strategy._charge_reconnect(id(master.client)) is not None

    clock[0] += RECONNECT_WINDOW_S + 1
    master.client.is_connected = False
    assert await heal(strategy) is True, "still refusing after the burst aged out"


# -- a socket that went quiet without closing --------------------------------
#
# The reported symptom: "the websocket falls off". A peer that vanishes without
# a close frame leaves the client still reporting connected, so silence is the
# only evidence. The watchdog fires at ws_stale_timeout_s (30s) while the
# library's own keepalive needs ping_interval + ping_timeout (80s) to notice --
# so the stale check always won the race, and it halted instead of reconnecting.


async def test_a_silent_socket_is_reconnected_not_halted():
    strategy, master, _sub1 = build()
    master.client.is_connected = True          # still claims to be up
    master.last_message_age_s = 31.0           # and has said nothing for 31s

    assert await heal(strategy) is True, "a stale socket must be retried"
    assert master.reconnects == 1


async def test_a_talking_socket_is_left_alone():
    """Only the silent one is touched, even when the pair is checked together."""
    strategy, master, sub1 = build()
    for session in (master, sub1):
        session.client.is_connected = True
    master.last_message_age_s = 31.0
    sub1.last_message_age_s = 1.0

    await heal(strategy)
    assert master.reconnects == 1
    assert sub1.reconnects == 0


async def test_the_stale_threshold_comes_from_the_risk_config():
    """Not a second copy of the number that can drift from the first."""
    strategy, master, _sub1 = build()
    master.client.is_connected = True
    master.last_message_age_s = 20.0

    strategy.risk.config.ws_stale_timeout_s = 60.0
    await heal(strategy)
    assert master.reconnects == 0, "20s is not stale at a 60s threshold"

    strategy.risk.config.ws_stale_timeout_s = 10.0
    await heal(strategy)
    assert master.reconnects == 1, "20s is stale at a 10s threshold"


# -- the keepalive settings have to survive the SDK's own arguments ----------
#
# ws_compat set them with `setdefault`, and the SDK passes ping_timeout=10
# explicitly in its connect(). So the override never applied: every socket the
# bot opened ran on a ten-second pong deadline while the code said sixty, and a
# pong later than that killed the connection. Reported as the WebSocket
# "falling off"; the comment in ws_compat had already recorded the symptom.


async def _captured_kwargs(monkeypatch, **caller_kwargs):
    from bulkdn import ws_compat

    seen = {}

    async def fake_connect(url, **kwargs):
        seen.update(kwargs)

        class Socket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        return Socket()

    monkeypatch.setattr(ws_compat, "_original_ws_connect", fake_connect)
    await ws_compat._connect_verified("wss://example.invalid", **caller_kwargs)
    return seen


async def test_the_sdk_cannot_shorten_the_pong_deadline(monkeypatch):
    """These are the arguments bulk_ws.connect really passes."""
    from bulkdn.ws_compat import _PING_INTERVAL, _PING_TIMEOUT

    seen = await _captured_kwargs(
        monkeypatch, ping_interval=20, ping_timeout=10, close_timeout=10
    )
    assert seen["ping_timeout"] == _PING_TIMEOUT, "the SDK's 10s deadline won again"
    assert seen["ping_interval"] == _PING_INTERVAL


async def test_the_settings_apply_when_nobody_asks(monkeypatch):
    from bulkdn.ws_compat import _OPEN_TIMEOUT, _PING_TIMEOUT

    seen = await _captured_kwargs(monkeypatch)
    assert seen["ping_timeout"] == _PING_TIMEOUT
    assert seen["open_timeout"] == _OPEN_TIMEOUT


async def test_unrelated_arguments_are_passed_through(monkeypatch):
    """Only the keepalive is this module's business."""
    seen = await _captured_kwargs(
        monkeypatch, close_timeout=7, max_size=1234, compression=None
    )
    assert seen["close_timeout"] == 7
    assert seen["max_size"] == 1234
    assert seen["compression"] is None


async def test_the_deadline_outlasts_the_stale_watchdog():
    """Otherwise the two race, and the one that cannot reconnect wins.

    The stale watchdog fires first on purpose: it reconnects, where a keepalive
    timeout drops the socket and leaves the bot to notice.
    """
    from bulkdn.config import RiskConfig
    from bulkdn.ws_compat import _PING_INTERVAL, _PING_TIMEOUT

    detect_s = _PING_INTERVAL + _PING_TIMEOUT
    assert RiskConfig().ws_stale_timeout_s < detect_s


# -- the silence clock belongs to the live socket ---------------------------
#
# A three-hour run ended at 02:29:54 on 2026-09-18 with
#   02:29:52  sub1: WebSocket dropped -- trying to reconnect
#   02:29:53  sub1: WebSocket reconnected on attempt 1
#   02:29:54  HALT: stale_stream: sub1 has received nothing for 31s
# The heal worked. The re-check that follows it measured silence from the dead
# socket's last message, so the repair still looked like the fault.


def routed_client(silent_for=0.0):
    """A RoutedWsClient with its fields set directly.

    Built without __init__ because that reaches for a signer and a URL, and
    connect() only touches the handful of attributes set here.
    """
    import time

    from bulkdn.accounts import RoutedWsClient

    client = RoutedWsClient.__new__(RoutedWsClient)
    client.last_message_at = time.monotonic() - silent_for
    client.subscriptions = []
    client.account_pubkey = None
    # Every account this socket carries. Empty here: the test is about the
    # staleness clock, and a socket with no accounts subscribes to nothing.
    client.accounts = []
    client.signer = None
    client._hidden_signer = None
    return client


def test_a_reconnected_socket_is_not_still_stale(monkeypatch):
    """The field the watchdog reads has to belong to the socket that is live
    now, or a successful reconnect halts on the silence it just repaired."""
    import time

    from bulk_api import BulkWebSocketClient

    client = routed_client(silent_for=31.0)

    async def up(self):
        return True

    monkeypatch.setattr(BulkWebSocketClient, "connect", up, raising=False)
    assert asyncio.run(client.connect()) is True

    silent = time.monotonic() - client.last_message_at
    assert silent < 1.0, f"still reads as silent for {silent:.0f}s"


def test_a_failed_connect_leaves_the_clock_alone(monkeypatch):
    """Otherwise a socket that never came back would look freshly alive, and
    the watchdog meant to catch it would never fire."""
    from bulk_api import BulkWebSocketClient

    client = routed_client(silent_for=31.0)
    stale = client.last_message_at

    async def down(self):
        return False

    monkeypatch.setattr(BulkWebSocketClient, "connect", down, raising=False)
    assert asyncio.run(client.connect()) is False

    assert client.last_message_at == stale, "a failed connect reset the clock"


# -- an outage that ends midway through the heal -----------------------------
#
# The sockets used to be tried one after another, so a network that came back
# in the middle left the earlier one having spent its attempts against a dead
# one. A DNS outage on 2026-09-18 did exactly that:
#
#   15:09:30-35  master: three attempts, "getaddrinfo failed", gave up
#   15:09:42     sub1:   reconnected on attempt 3 -- the network was back
#   15:09:43     HALT: disconnected: master WebSocket is not connected
#
# The run ended on a socket nobody had tried again. They are tried side by
# side now, and the same can happen with their attempts timed differently.


async def test_a_session_that_failed_before_the_network_returned_is_retried():
    """sub1 coming back is proof the network did."""
    from bulkdn.accounts import AccountSession

    budget = AccountSession.reconnect.__defaults__[0]
    strategy, master, sub1 = build(
        master_succeeds_on=budget + 1, sub_connected=False, sub_delay=0.02,
    )

    assert await heal(strategy) is True, "the master was left down and the run halted"
    assert master.is_connected and sub1.is_connected
    assert master.client.attempts == budget + 1, "it was not tried again"


async def test_nothing_is_retried_when_nothing_came_back():
    """With no evidence the network returned, the extra attempts would be
    spent against the same dead network -- and the halt is then correct."""
    from bulkdn.accounts import AccountSession

    strategy, master, sub1 = build(master_succeeds_on=0, sub_connected=False)
    sub1.client.succeed_on = 0

    assert await heal(strategy) is False
    # One round of attempts, not a second round after the first gave up.
    assert master.client.attempts == AccountSession.reconnect.__defaults__[0], (
        "it kept trying a network that was down"
    )


async def test_the_retry_does_not_cost_extra_budget():
    """The budget is charged per incident, not per attempt, so the second go
    is free -- a flapping socket is still caught by the window."""
    from bulkdn.accounts import AccountSession

    budget = AccountSession.reconnect.__defaults__[0]
    strategy, _master, _sub1 = build(
        master_succeeds_on=budget + 1, sub_connected=False, sub_delay=0.02,
    )

    await heal(strategy)
    assert len(strategy._reconnect_times) == 1


# -- a socket that drops while another is being repaired --------------------
#
# Taken from a subscriber's log. sub1's socket closed, the repair began, and
# nine seconds into its reconnect the master's closed too. One flaky link
# carries both sockets, so the second drop landing inside the first repair is
# the common case, not a rare one -- and it is one incident, not two.


async def test_a_drop_during_a_repair_is_repaired_too():
    strategy, master, sub1 = build(sub_connected=False, sub_delay=0.05)
    master.client.is_connected = True
    strategy._start_heals()
    await asyncio.sleep(0.01)

    master.client.is_connected = False  # drops while sub1 is being repaired
    assert await heal(strategy) is True
    assert master.is_connected and sub1.is_connected


async def test_a_drop_during_a_repair_does_not_spend_the_flap_budget():
    """One incident repaired in stages is not two incidents. Charging each
    stage would halt the run for flapping it never did."""
    strategy, master, _sub1 = build(sub_connected=False, sub_delay=0.05)
    master.client.is_connected = True
    strategy._start_heals()
    await asyncio.sleep(0.01)

    master.client.is_connected = False
    await heal(strategy)
    assert len(strategy._reconnect_times) == 1


async def test_the_same_socket_dropping_again_inside_an_incident_pays():
    """That is flapping, and the window has to see it."""
    strategy, master, _sub1 = build()
    await heal(strategy)
    master.client.is_connected = False
    await heal(strategy)
    assert len(strategy._reconnect_times) == 2


# -- and an outage longer than a few seconds --------------------------------


async def test_it_keeps_trying_past_the_first_few_seconds():
    """From a subscriber's log: the socket dropped and all three reconnects
    were refused by the exchange's own front end with HTTP 502 inside seven
    seconds. A gateway is rarely back that quickly, so the run halted on an
    outage it had barely waited out."""
    session = FakeSession("master", succeed_on=5)
    session.client.is_connected = False

    assert await session.reconnect(delay=0.0) is True
    assert session.client.attempts >= 5, "it gave up before the outage ended"


async def test_the_wait_grows_rather_than_repeating():
    """Hammering a gateway that is down every two seconds helps neither of
    us. Six attempts should span about a minute, not seven seconds."""
    import bulkdn.accounts as accounts_mod
    from bulkdn.accounts import AccountSession

    waits = []

    async def record(seconds):
        waits.append(seconds)

    original = accounts_mod.asyncio.sleep
    accounts_mod.asyncio.sleep = record
    try:
        session = FakeSession("master", succeed_on=99)
        session.client.is_connected = False
        # The real method with its real delay: the fake passes 0.0 so tests
        # stay fast, and that is exactly what must not be measured here.
        await AccountSession.reconnect(session)
    finally:
        accounts_mod.asyncio.sleep = original

    assert waits == sorted(waits), "the wait did not grow"
    assert sum(waits) > 30, f"six attempts spanned only {sum(waits):.0f}s"


async def test_a_failed_re_read_does_not_end_the_heal():
    """It ran bare inside `_supervise` once: one 429 ended the supervisor, and
    the exposure limits, the liquidation guard and the reconciler went with it
    while the groups traded on."""
    strategy, _master, _sub1 = build()
    strategy.resync_fails = True

    assert await heal(strategy) is True
    assert strategy._book_suspect, "a book that could not be re-read is suspect"

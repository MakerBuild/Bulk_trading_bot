"""Account reads keep one pace, whoever sends them.

From Tokyo a twelve-account walk went out in a fraction of a second and the
exchange answered with a wall of 429s -- on the fill history, and on the
position reads the hedger relies on. From far away the round trip alone had
spaced the same requests out.
"""

import time

import pytest
import requests
from requests.adapters import BaseAdapter

from bulkdn import ratelimit

API = "https://api.example/api/v1"


class Exchange(BaseAdapter):
    """Answers every request at once, and remembers when each arrived."""

    def __init__(self, status=200):
        super().__init__()
        self.status = status
        self.seen = []

    def send(self, request, **kwargs):
        self.seen.append((time.monotonic(), request.url))
        response = requests.Response()
        response.status_code = self.status
        response._content = b"{}"
        response.url = request.url
        return response

    def close(self):
        pass


@pytest.fixture
def exchange(monkeypatch):
    monkeypatch.setattr(ratelimit, "MIN_INTERVAL_S", 0.05)
    monkeypatch.setattr(ratelimit, "THROTTLE_PAUSE_S", 0.3)
    ratelimit.install(API)
    adapter = Exchange()
    session = requests.Session()
    session.mount("https://", adapter)
    yield session, adapter
    ratelimit.uninstall()


def gaps(adapter):
    times = [t for t, _ in adapter.seen]
    return [b - a for a, b in zip(times, times[1:], strict=False)]


def test_account_reads_are_spaced(exchange):
    session, adapter = exchange
    for _ in range(4):
        session.post(f"{API}/account", json={"type": "fills"})
    assert min(gaps(adapter)) >= 0.045, "account reads went out back to back"


def test_orders_are_never_held_back(exchange):
    """A cancel or a close must not wait behind a history walk."""
    session, adapter = exchange
    started = time.monotonic()
    for _ in range(4):
        session.post(f"{API}/order", json={})
    assert time.monotonic() - started < 0.04


def test_other_hosts_are_left_alone(exchange):
    session, adapter = exchange
    started = time.monotonic()
    for _ in range(4):
        session.post("https://api.telegram.example/account", json={})
    assert time.monotonic() - started < 0.04


def test_a_429_holds_everyone_back(exchange):
    session, adapter = exchange
    adapter.status = 429
    session.post(f"{API}/account", json={})
    adapter.status = 200
    session.post(f"{API}/account", json={})
    assert gaps(adapter)[0] >= 0.28, "the next read walked into the same wall"


def test_module_level_requests_calls_are_paced_too(exchange, monkeypatch):
    """Most callers use requests.post, which makes a Session of its own."""
    _session, adapter = exchange
    original = requests.Session.get_adapter
    monkeypatch.setattr(requests.Session, "get_adapter", lambda self, url: adapter)
    for _ in range(3):
        requests.post(f"{API}/account", json={})
    monkeypatch.setattr(requests.Session, "get_adapter", original)
    assert min(gaps(adapter)) >= 0.045


async def test_never_sleeps_on_the_event_loop(exchange):
    """A read made on the loop's own thread would freeze every hedge for the
    length of the wait. It takes its turn without waiting for it."""
    session, adapter = exchange
    started = time.monotonic()
    for _ in range(4):
        session.post(f"{API}/account", json={})
    assert time.monotonic() - started < 0.04


def test_installing_twice_does_not_stack(exchange):
    session, adapter = exchange
    ratelimit.install(API)
    started = time.monotonic()
    session.post(f"{API}/account", json={})
    session.post(f"{API}/account", json={})
    assert time.monotonic() - started < 0.09, "each request waited twice"


# -- a failed read is not retried every second ------------------------------------


async def test_a_failed_target_read_waits_before_the_next(monkeypatch):
    from bulkdn import strategy as module
    from bulkdn.state import StrategyState
    from bulkdn.strategy import Strategy

    clock = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    s = Strategy.__new__(Strategy)
    s.config = type("C", (), {"target": type("T", (), {
        "measures_fills": True, "burn_usd": 0.0, "volume_usd": 100_000.0,
    })()})()
    s.state = StrategyState()
    s.state.baseline_at = 1.0
    s._target_answer = (0.0, None)
    reads = []

    async def refused():
        reads.append(clock[0])
        raise requests.HTTPError("429 Client Error: Too Many Requests")

    s._read_totals = refused

    assert await s._target_reached() is None
    clock[0] += 1.0                                   # the dispatcher's next pass
    assert await s._target_reached() is None
    assert len(reads) == 1, "a refused read was retried a second later"

    clock[0] += module.TARGET_RETRY_S
    await s._target_reached()
    assert len(reads) == 2, "it never tried again"

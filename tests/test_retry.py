"""Retry policy.

The distinction that matters here is not "does it retry" but *what* it retries:
transport faults yes, exchange decisions no, and a signed transaction only ever
as the same bytes.
"""

import asyncio

import pytest
import requests

from bulkdn.retry import RetryExhausted, post_with_retry, retry


class Boom(Exception):
    """Stands in for a deliberate exchange rejection."""


def test_sync_retries_then_succeeds():
    calls = []

    @retry("probe", attempts=3, delay=0)
    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise requests.ConnectionError("dropped")
        return "ok"

    assert flaky() == "ok"
    assert len(calls) == 3


def test_sync_raises_retry_exhausted_with_the_last_error():
    @retry("probe", attempts=2, delay=0)
    def always_fails():
        raise requests.Timeout("too slow")

    with pytest.raises(RetryExhausted) as excinfo:
        always_fails()
    assert isinstance(excinfo.value.last, requests.Timeout)
    assert excinfo.value.attempts == 2


def test_non_transient_errors_are_not_retried():
    """A rejected order will be rejected again -- retrying only hides it."""
    calls = []

    @retry("probe", attempts=5, delay=0)
    def rejected():
        calls.append(1)
        raise Boom("bad signature")

    with pytest.raises(Boom):
        rejected()
    assert len(calls) == 1


def test_reraise_false_returns_the_default():
    @retry("probe", attempts=2, delay=0, reraise=False, default="fallback")
    def always_fails():
        raise requests.ConnectionError("dropped")

    assert always_fails() == "fallback"


async def test_async_retries_then_succeeds():
    calls = []

    @retry("probe", attempts=3, delay=0)
    async def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise asyncio.TimeoutError()
        return "ok"

    assert await flaky() == "ok"
    assert len(calls) == 2


async def test_async_exhausts():
    @retry("probe", attempts=2, delay=0)
    async def always_fails():
        raise ConnectionError("gone")

    with pytest.raises(RetryExhausted):
        await always_fails()


# -- post_with_retry -------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


def test_post_replays_the_identical_body(monkeypatch):
    """The whole point: a retry must not re-sign under a new nonce.

    Re-signing would turn a lost response into a second live order.
    """
    seen = []

    def fake_post(url, json, timeout):
        seen.append(json)
        if len(seen) < 3:
            raise requests.ConnectionError("dropped")
        return FakeResponse(200)

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr("bulkdn.retry.time.sleep", lambda _: None)

    body = {"nonce": 12345, "signature": "sig"}
    assert post_with_retry("http://x/order", json=body, attempts=3, delay=0).status_code == 200
    assert len(seen) == 3
    # Every attempt carried the same nonce and signature.
    assert all(sent == body for sent in seen)


def test_post_retries_5xx_and_429(monkeypatch):
    statuses = [503, 429, 200]
    monkeypatch.setattr(
        requests, "post", lambda url, json, timeout: FakeResponse(statuses.pop(0))
    )
    monkeypatch.setattr("bulkdn.retry.time.sleep", lambda _: None)

    assert post_with_retry("http://x", json={}, attempts=3, delay=0).status_code == 200
    assert statuses == []


def test_post_does_not_retry_a_4xx(monkeypatch):
    """A 400 is a malformed request; it will be malformed next time too."""
    calls = []

    def fake_post(url, json, timeout):
        calls.append(1)
        return FakeResponse(400)

    monkeypatch.setattr(requests, "post", fake_post)
    assert post_with_retry("http://x", json={}, attempts=3, delay=0).status_code == 400
    assert len(calls) == 1


def test_post_returns_the_final_5xx_rather_than_raising(monkeypatch):
    """The caller reads the body for the exchange's own error message."""
    monkeypatch.setattr(requests, "post", lambda url, json, timeout: FakeResponse(500))
    monkeypatch.setattr("bulkdn.retry.time.sleep", lambda _: None)

    assert post_with_retry("http://x", json={}, attempts=2, delay=0).status_code == 500


def test_post_raises_when_transport_never_recovers(monkeypatch):
    def fake_post(url, json, timeout):
        raise requests.ConnectionError("dropped")

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr("bulkdn.retry.time.sleep", lambda _: None)

    with pytest.raises(RetryExhausted):
        post_with_retry("http://x", json={}, attempts=2, delay=0)

"""Retrying transient failures.

A trading bot cannot retry indiscriminately. Two rules shape this module:

**Only transient faults are retried.** A dropped connection or a 502 is worth
another attempt; a rejected order, a bad signature, or a risk-limit breach is a
decision the exchange already made and will make again. Retrying those wastes
time and hides the real error behind an attempt counter.

**A signed transaction is retried as the same bytes, never re-signed.** The
nonce is what makes a transaction replay-safe: re-POSTing an identical body is
harmless, because the exchange either already applied that nonce or never saw
it. Re-signing with a fresh nonce turns a lost response into a *second order*.
So retry wraps the network call, not the sign-and-submit that surrounds it.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import time
from collections.abc import Callable, Sequence

import requests

log = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 3
DEFAULT_DELAY_S = 2.0

# Faults worth another attempt: the request never produced an answer we can
# trust. Anything the exchange answered deliberately is excluded.
TRANSIENT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    requests.ConnectionError,
    requests.Timeout,
    asyncio.TimeoutError,
    ConnectionError,
    TimeoutError,
    OSError,
)

# 5xx is the server failing to answer; 429 is it asking us to slow down. A 4xx
# other than 429 is a malformed or refused request and will fail identically.
TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


class RetryExhausted(Exception):
    """Every attempt failed. Carries the last underlying error."""

    def __init__(self, source: str, attempts: int, last: BaseException):
        super().__init__(f"{source}: gave up after {attempts} attempts -- {last}")
        self.source = source
        self.attempts = attempts
        self.last = last


def _backoff(delay: float, attempt: int) -> float:
    """Exponential, capped. Keeps a flapping endpoint from being hammered."""
    return min(delay * (2 ** (attempt - 1)), 30.0)


def retry(
    source: str,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    delay: float = DEFAULT_DELAY_S,
    exceptions: Sequence[type[BaseException]] = TRANSIENT_EXCEPTIONS,
    reraise: bool = True,
    default=None,
):
    """Retry a transient-failing callable. Works on sync and async functions.

    `reraise=False` returns `default` once attempts run out, for callers where
    a missing result is recoverable -- a stale price, say -- and an exception
    would take down a loop that could otherwise keep running.
    """
    exception_types = tuple(exceptions)

    def decorator(func: Callable):
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                last: BaseException | None = None
                for attempt in range(1, attempts + 1):
                    try:
                        return await func(*args, **kwargs)
                    except exception_types as exc:
                        last = exc
                        if attempt == attempts:
                            break
                        wait = _backoff(delay, attempt)
                        log.warning(
                            "%s failed (%s: %s) [%d/%d] -- retrying in %.1fs",
                            source, exc.__class__.__name__, exc, attempt, attempts, wait,
                        )
                        await asyncio.sleep(wait)
                return _give_up(source, attempts, last, reraise, default)

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            last: BaseException | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exception_types as exc:
                    last = exc
                    if attempt == attempts:
                        break
                    wait = _backoff(delay, attempt)
                    log.warning(
                        "%s failed (%s: %s) [%d/%d] -- retrying in %.1fs",
                        source, exc.__class__.__name__, exc, attempt, attempts, wait,
                    )
                    time.sleep(wait)
            return _give_up(source, attempts, last, reraise, default)

        return sync_wrapper

    return decorator


def _give_up(source: str, attempts: int, last: BaseException | None, reraise: bool, default):
    if reraise:
        raise RetryExhausted(source, attempts, last or Exception("unknown"))
    log.error("%s gave up after %d attempts -- %s", source, attempts, last)
    return default


def post_with_retry(
    url: str,
    *,
    json: dict,
    timeout: int = 30,
    attempts: int = DEFAULT_ATTEMPTS,
    delay: float = DEFAULT_DELAY_S,
) -> requests.Response:
    """POST an already-signed body, retrying only transport faults and 5xx/429.

    The body is passed in rather than rebuilt, so every attempt carries the same
    nonce and signature. That is what makes a retry a replay rather than a
    second transaction.
    """
    last: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(url, json=json, timeout=timeout)
            if response.status_code not in TRANSIENT_STATUS:
                return response
            last = requests.HTTPError(f"HTTP {response.status_code}")
            if attempt == attempts:
                # Hand back the response rather than raising: the caller reads
                # the body for the exchange's own error message.
                return response
        except TRANSIENT_EXCEPTIONS as exc:
            last = exc
            if attempt == attempts:
                break

        wait = _backoff(delay, attempt)
        log.warning(
            "POST %s failed (%s) [%d/%d] -- replaying same signed body in %.1fs",
            url, last, attempt, attempts, wait,
        )
        time.sleep(wait)

    raise RetryExhausted(f"POST {url}", attempts, last or Exception("unknown"))

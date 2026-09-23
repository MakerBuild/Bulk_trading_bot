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
#
# Named one by one, and `OSError` deliberately not among them. It used to be,
# as a catch-all for socket errors, and it caught far more than that:
# `requests.RequestException` itself subclasses `OSError`, so EVERY requests
# error was "transient" -- `InvalidURL`, `MissingSchema`, `TooManyRedirects`,
# `InvalidJSONError` -- along with `FileNotFoundError` and `PermissionError`
# from anything else inside a retried call. Each of those fails identically on
# every attempt; retrying them only delays the real error behind an attempt
# counter, which is what the module docstring says this must not do.
#
# `TimeoutError` covers `socket.timeout` and `asyncio.TimeoutError`, which are
# the same class on every Python this runs on. The builtin `ConnectionError`
# covers reset, aborted, refused and broken-pipe. requests' own
# `ConnectionError` (which includes its connect timeout and a TLS handshake cut
# off mid-way) is a different class and is listed separately, as is
# `ChunkedEncodingError`: the connection died partway through the body.
TRANSIENT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
    ConnectionError,
    TimeoutError,
)

# 5xx is the server failing to answer; 429 is it asking us to slow down. A 4xx
# other than 429 is a malformed or refused request and will fail identically.
TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


def is_transient(exc: BaseException) -> bool:
    """Whether another attempt could plausibly get a different answer.

    A transport fault from the list above, or an HTTP error carrying a status
    from `TRANSIENT_STATUS` -- which is how `raise_for_status` reports a 502,
    and which the exception list alone cannot see, since `HTTPError` is also
    what a 404 raises.
    """
    if isinstance(exc, TRANSIENT_EXCEPTIONS):
        return True
    if isinstance(exc, requests.HTTPError):
        response = getattr(exc, "response", None)
        return getattr(response, "status_code", None) in TRANSIENT_STATUS
    return False


class RetryExhausted(Exception):
    """Every attempt failed. Carries the last underlying error.

    `uncertain` is set by `post_signed` when at least one attempt may have
    reached the exchange -- a read timeout, a connection cut after sending, a
    gateway 5xx. A signed transaction that ends here may therefore have been
    applied, and must be reported as UNKNOWN rather than as failed.
    """

    def __init__(
        self, source: str, attempts: int, last: BaseException, *, uncertain: bool = False
    ):
        super().__init__(f"{source}: gave up after {attempts} attempts -- {last}")
        self.source = source
        self.attempts = attempts
        self.last = last
        self.uncertain = uncertain


def describe(exc: BaseException) -> str:
    """An exception as something worth reading in a log file.

    `str(exc)` alone is empty for several of the ones that matter most here --
    `asyncio.TimeoutError` chief among them. A live run logged

        ERROR bulkdn.strategy: hedge for BTC-USD failed:

    and that blank was the whole report of a hedge that did not happen. Worse,
    it hid the cause: the SDK had failed to parse an order status, so the
    response never came back and the wait timed out. The class name alone would
    have pointed straight at it.
    """
    text = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {text}" if text else name


def _backoff(delay: float, attempt: int) -> float:
    """Exponential, capped. Keeps a flapping endpoint from being hammered."""
    return min(delay * (2 ** (attempt - 1)), 30.0)


def retry(
    source: str,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    delay: float = DEFAULT_DELAY_S,
    exceptions: Sequence[type[BaseException]] | None = None,
    reraise: bool = True,
    default=None,
):
    """Retry a transient-failing callable. Works on sync and async functions.

    What is retried is `is_transient` unless `exceptions` names the classes
    explicitly, in which case it is exactly those.

    `reraise=False` returns `default` once attempts run out, for callers where
    a missing result is recoverable -- a stale price, say -- and an exception
    would take down a loop that could otherwise keep running.
    """
    if exceptions is None:
        retryable = is_transient
    else:
        exception_types = tuple(exceptions)

        def retryable(exc: BaseException) -> bool:
            return isinstance(exc, exception_types)

    def decorator(func: Callable):
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                last: BaseException | None = None
                for attempt in range(1, attempts + 1):
                    try:
                        return await func(*args, **kwargs)
                    except Exception as exc:
                        if not retryable(exc):
                            raise
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
                except Exception as exc:
                    if not retryable(exc):
                        raise
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
    second transaction. See `post_signed` for what a replay cannot tell you.
    """
    response, _uncertain = post_signed(
        url, json=json, timeout=timeout, attempts=attempts, delay=delay
    )
    return response


def post_signed(
    url: str,
    *,
    json: dict,
    timeout: int = 30,
    attempts: int = DEFAULT_ATTEMPTS,
    delay: float = DEFAULT_DELAY_S,
) -> tuple[requests.Response, bool]:
    """`post_with_retry`, also saying whether the outcome can be trusted.

    Replaying the same signed bytes stops a lost response becoming a second
    transaction -- but it cannot stop the FIRST one having happened. When an
    attempt times out after the request left, the exchange may well have
    applied it; the replay then carries a nonce it has already seen and is
    refused. Reported as-is, that refusal says "failed" about a transfer that
    went through, and an operator retrying by hand moves the money twice.

    So the second value is True when some attempt might have reached the
    exchange without an answer coming back -- a read timeout, a connection
    dropped mid-request, a 5xx from a gateway that may have forwarded it --
    including the last attempt, if it was a 5xx. The caller decides what the
    final answer means; `bulkdn.tx` reports it as UNKNOWN unless the final
    answer was an acceptance.

    A connect timeout, a refused connection and a failed DNS lookup never
    reached anybody and do not count. Neither does 429, which is the exchange
    declining to look at the request at all.
    """
    last: BaseException | None = None
    uncertain = False

    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(url, json=json, timeout=timeout)
            if response.status_code >= 500:
                uncertain = True
            if response.status_code not in TRANSIENT_STATUS:
                return response, uncertain
            last = requests.HTTPError(f"HTTP {response.status_code}")
            if attempt == attempts:
                # Hand back the response rather than raising: the caller reads
                # the body for the exchange's own error message.
                return response, uncertain
        except Exception as exc:
            if not is_transient(exc):
                # Not worth another attempt -- but not necessarily nothing.
                # An earlier attempt may already have reached the exchange,
                # and a reply that came back malformed means this one did.
                # Raised bare, it lost that: the menu counted the transfer as
                # refused and sent the rest of the plan, and an operator
                # retrying by hand moved the money twice.
                if uncertain or isinstance(exc, _ANSWERED_BADLY):
                    raise RetryExhausted(
                        f"POST {url}", attempt, exc, uncertain=True
                    ) from exc
                raise
            last = exc
            if not _never_sent(exc):
                uncertain = True
            if attempt == attempts:
                break

        wait = _backoff(delay, attempt)
        log.warning(
            "POST %s failed (%s) [%d/%d] -- replaying same signed body in %.1fs",
            url, describe(last), attempt, attempts, wait,
        )
        time.sleep(wait)

    raise RetryExhausted(
        f"POST {url}", attempts, last or Exception("unknown"), uncertain=uncertain
    )


# Errors that mean an answer DID come back, only not one that could be read:
# the request reached the other end, so whatever it asked for may be done.
_ANSWERED_BADLY = (
    requests.exceptions.ContentDecodingError,
    requests.exceptions.TooManyRedirects,
)


def _never_sent(exc: BaseException) -> bool:
    """Whether a transport error happened before the request could leave.

    Conservative: anything not positively known to be pre-connection counts as
    possibly sent, because the cost of a wrong "never sent" is a transfer
    reported as failed that was not.
    """
    if isinstance(exc, requests.ConnectTimeout):
        return True
    if isinstance(exc, requests.exceptions.ProxyError):
        # The proxy may have forwarded it before failing; cannot tell.
        return False
    if isinstance(exc, requests.ConnectionError):
        try:
            from urllib3.exceptions import ConnectTimeoutError, NewConnectionError
        except ImportError:  # pragma: no cover - urllib3 ships with requests
            return False
        cause = exc.args[0] if exc.args else None
        reason = getattr(cause, "reason", cause)
        # NewConnectionError covers refused and, as NameResolutionError, DNS.
        return isinstance(reason, (NewConnectionError, ConnectTimeoutError))
    return isinstance(exc, ConnectionRefusedError)

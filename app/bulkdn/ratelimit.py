"""One pace for every account read sent to the exchange.

The exchange rate-limits `/account` hard -- it once answered 429 to two
accounts polling every five seconds -- and the bot reads it from many places:
positions, fill history, fee tier, risk events, the menu. None of them paced
itself. Far from the exchange none needed to: each request held its caller for a
~320ms round trip, which spaced a twelve-account walk over four seconds.

From Tokyo the same walk goes out in a fraction of a second, and the first run
there drew a wall of 429s -- on the fill history, and on the position reads
the hedger depends on through the same endpoint.

So every `/account` request to the exchange's host waits for its turn, at most
one per MIN_INTERVAL_S across the whole process -- the pace the bot kept from
far away, which the exchange accepted for months. A 429 holds everyone back
for THROTTLE_PAUSE_S more, rather than letting the next request walk into the
same wall.

Installed at the transport, once, because half the callers are inside the SDK
where no call site can be edited. Only `/account` is paced: orders and cancels
go to `/order` and must never wait behind a history read.

Never sleeps on the event loop's own thread. A request made there -- none
should be, but a missed one would freeze every hedge for the length of the
wait -- takes its turn without waiting for it, and says so: once per call
site, naming it, because the request itself blocks the loop for its whole
round trip and the place it was sent from is the thing to fix. It used to
skip the wait in silence, so nothing ever pointed at the caller.

A thread that runs a loop of its own, for its own work only, can say so with
`private_loop()`; its requests are then paced like any other worker thread's.

Installed by replacing `requests.Session.send` for the whole process, which is
blunt but the only hook that reaches every caller: the SDK calls the module
functions `requests.get`/`requests.post`, each of which builds a Session of its
own, so no Session or adapter the bot owned would ever see those requests.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import threading
import time
import urllib.parse
from collections.abc import Iterator

import requests

log = logging.getLogger(__name__)

MIN_INTERVAL_S = 0.35
THROTTLE_PAUSE_S = 2.0

_lock = threading.Lock()
_next_at = 0.0
_hosts: set[str] = set()
_original_send = None
# Call sites already warned about, as (file, line). See `_warn_on_loop`.
_warned_sites: set[tuple[str, int]] = set()
# Set by `private_loop` on a thread whose loop serves nothing but that thread.
_private = threading.local()

_HERE = os.path.abspath(__file__)
_PACKAGE = os.path.dirname(_HERE)
_TRANSPORT = (f"{os.sep}requests{os.sep}", f"{os.sep}urllib3{os.sep}")


def _paced(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return parts.hostname in _hosts and parts.path.rstrip("/").endswith("/account")


def _on_event_loop_thread() -> bool:
    if getattr(_private, "active", False):
        return False
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@contextlib.contextmanager
def private_loop() -> Iterator[None]:
    """Treat this thread's event loop as its own, not the bot's.

    For a worker thread that runs `asyncio.run` on work nobody else is waiting
    on -- a status read -- where sleeping for a turn holds up that work alone.
    """
    previous = getattr(_private, "active", False)
    _private.active = True
    try:
        yield
    finally:
        _private.active = previous


def _caller() -> tuple[str, int, str]:
    """Where the request came from: the nearest frame in this bot, if any.

    Frames in `requests`, `urllib3` and this module are the transport, not the
    caller. A request sent by the SDK is attributed to the bot code that
    called into the SDK, which is the line that can be moved off the loop;
    failing that, to the first frame outside the transport.
    """
    frame = sys._getframe(1)
    first = None
    while frame is not None:
        path = os.path.abspath(frame.f_code.co_filename)
        if path != _HERE and not any(part in path for part in _TRANSPORT):
            site = (path, frame.f_lineno, frame.f_code.co_name)
            if first is None:
                first = site
            if os.path.dirname(path) == _PACKAGE:
                return site
        frame = frame.f_back
    return first or ("?", 0, "?")


def _warn_on_loop(skipped_s: float) -> None:
    path, line, function = _caller()
    if (path, line) in _warned_sites:
        return
    _warned_sites.add((path, line))
    waited = f", skipping a {skipped_s:.2f}s wait for its turn" if skipped_s > 0 else ""
    log.warning(
        "an account read was sent from the event loop's own thread, at %s:%d "
        "in %s()%s -- it is not paced there, and it blocks every socket until "
        "it answers. It belongs in asyncio.to_thread.",
        os.path.basename(path), line, function, waited,
    )


def wait_turn() -> None:
    """Take the next slot, sleeping until it comes unless on the loop."""
    global _next_at
    with _lock:
        now = time.monotonic()
        start = max(now, _next_at)
        _next_at = start + MIN_INTERVAL_S
    delay = start - now
    if _on_event_loop_thread():
        _warn_on_loop(delay)
        return
    if delay > 0:
        time.sleep(delay)


def note_throttled() -> None:
    """The exchange said 429: hold every paced request back a while."""
    global _next_at
    with _lock:
        _next_at = max(_next_at, time.monotonic() + THROTTLE_PAUSE_S)
    log.debug("exchange throttled an account read -- pausing account reads %.0fs", THROTTLE_PAUSE_S)


def install(*base_urls: str) -> None:
    """Pace `/account` requests to these hosts. Safe to call more than once."""
    global _original_send
    for url in base_urls:
        host = urllib.parse.urlsplit(url).hostname
        if host:
            _hosts.add(host)
    if _original_send is not None:
        return
    _original_send = requests.Session.send

    def send(self, request, **kwargs):
        paced = _paced(request.url)
        if paced:
            wait_turn()
        response = _original_send(self, request, **kwargs)
        if paced and response.status_code == 429:
            note_throttled()
        return response

    requests.Session.send = send


def uninstall() -> None:
    """Undo `install` -- for tests."""
    global _original_send, _next_at
    if _original_send is not None:
        requests.Session.send = _original_send
        _original_send = None
    _hosts.clear()
    _warned_sites.clear()
    _next_at = 0.0

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
wait -- takes its turn without waiting for it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import urllib.parse

import requests

log = logging.getLogger(__name__)

MIN_INTERVAL_S = 0.35
THROTTLE_PAUSE_S = 2.0

_lock = threading.Lock()
_next_at = 0.0
_hosts: set[str] = set()
_original_send = None


def _paced(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return parts.hostname in _hosts and parts.path.rstrip("/").endswith("/account")


def _on_event_loop_thread() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def wait_turn() -> None:
    """Take the next slot, sleeping until it comes unless on the loop."""
    global _next_at
    with _lock:
        now = time.monotonic()
        start = max(now, _next_at)
        _next_at = start + MIN_INTERVAL_S
    delay = start - now
    if delay > 0 and not _on_event_loop_thread():
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
    _next_at = 0.0

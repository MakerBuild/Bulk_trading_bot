"""WebSocket compatibility patches for the BULK SDK.

Three problems make the stock SDK unusable against the live WebSocket, and all
three are silent -- nothing raises, the data is just wrong or the socket never
opens.

**1. Fill parsing drops every field.** `Fill.from_api` reads only the long field
names (`symbol`, `orderId`, `price`, `size`, `isBuy`, `timestamp`, `maker`), but
the account stream also emits the short forms (`sym`, `oid`, `px`, `sz`, `b`,
`ts`, `mk`). When the short form arrives the parser yields a fill with an empty
symbol and size 0.0 -- which this bot would treat as "nothing filled" and never
hedge. Both spellings are accepted here.

**2. `tradeId` is discarded.** Added in API v1.0.17 and needed to recognise
replayed fills. `Fill` has no such field upstream, so it is attached
dynamically; the dataclass has no `__slots__`, so this is safe.

**3. TLS verification fails against the live endpoints -- on Windows.** This was
read the wrong way round for a while, and the wrong reading is worth recording
because the fix that followed from it was to turn verification off.

The certificates are fine. Measured against all three hosts:

    mainnet-ws1.bulk.trade    system store   REJECTED  certificate has expired
    mainnet-ws1.bulk.trade    certifi        OK        valid to 22 Nov 2026

What has expired is a root in the Windows certificate store, not anything BULK
serves. `requests` never noticed because it ships `certifi` and uses it; the
SDK's WebSocket goes through `ssl.create_default_context()`, which on Windows
means the system store, so it failed where every HTTP call succeeded.

So the socket is opened against certifi's bundle -- the same trust anchors the
HTTP half of this bot has been using all along -- and verification stays on.
The bypass below survives only as a last resort for an operator who cannot
connect at all, and it is no longer reached in normal use.
"""

from __future__ import annotations

import logging
import ssl
from typing import Any

from bulk_api.common import Side
from bulk_api.messages.trade import Fill
from websockets.asyncio.client import connect as _original_ws_connect

log = logging.getLogger(__name__)

_PATCHED = False
_insecure_ssl = False
_auto_bypass = True
_bypass_latched = False

# The SDK's default is short enough that a cold connection can miss it.
_OPEN_TIMEOUT = 30

# How often to ping, and how long to wait for the pong before calling the
# socket dead. See _connect_with_ssl_fallback.
_PING_INTERVAL = 20.0
_PING_TIMEOUT = 60.0


def set_ssl_options(*, insecure: bool = False, auto_bypass: bool = True) -> None:
    """Configure TLS behaviour before connecting.

    `insecure` skips verification outright; `auto_bypass` keeps verification on
    but retries once without it if the certificate is rejected. Auto-bypass is
    the default because the live endpoints have been observed serving
    certificates that fail verification, and a bot that cannot open its account
    stream cannot hedge.
    """
    global _insecure_ssl, _auto_bypass, _bypass_latched
    _insecure_ssl = insecure
    _auto_bypass = auto_bypass
    if insecure:
        _bypass_latched = True


def _insecure_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def verified_context() -> ssl.SSLContext:
    """A verifying context that trusts what `requests` trusts.

    `ssl.create_default_context()` with no arguments reads the operating
    system's trust store, and on Windows that store has an expired root which
    rejects BULK's perfectly valid certificates. certifi ships its own bundle
    and is already installed -- it is how every HTTP call in this bot verifies
    today -- so pointing the socket at the same bundle makes the two halves
    agree instead of one of them giving up.

    Falls back to the system store if certifi is somehow absent, which is no
    worse than the position before this existed.
    """
    try:
        import certifi
    except ImportError:
        log.warning(
            "certifi is not installed -- falling back to the system certificate "
            "store, which on Windows may reject a valid certificate. "
            "`pip install certifi` fixes it."
        )
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


async def _connect_with_ssl_fallback(url: str, **kwargs: Any):
    """Open a socket, verifying against certifi's bundle.

    Only if that is rejected too does the bypass come into play, and then it
    stays latched for the process so reconnects don't pay a failed handshake
    every time. Before certifi was used here the bypass fired on every run on
    Windows, which meant the account stream -- fills and positions, the input
    to every hedge -- came from an endpoint nothing had authenticated.
    """
    global _bypass_latched
    kwargs["open_timeout"] = kwargs.get("open_timeout") or _OPEN_TIMEOUT

    # ASSIGNED, NOT setdefault -- and that is the whole point of these two lines.
    #
    # The SDK passes `ping_timeout=10` explicitly in its own `connect()`, so
    # `setdefault` was a no-op and every socket this bot has ever opened ran on
    # a ten-second pong deadline, not the sixty this file claimed. Any pong from
    # BULK later than ten seconds killed the connection -- which is exactly the
    # "keepalive ping timeout; no close frame received" the comment here already
    # recorded having seen end live cycles. The fix was written and never
    # applied.
    #
    # Overriding a caller's explicit argument is normally wrong. It is the
    # entire job of this module: the SDK's choices are what is being corrected,
    # and it is the only caller. A longer deadline still notices a genuinely
    # dead peer -- the risk layer watches for a silent-but-open socket
    # separately via ws_stale_timeout_s, and that path now reconnects rather
    # than halting.
    kwargs["ping_interval"] = _PING_INTERVAL
    kwargs["ping_timeout"] = _PING_TIMEOUT

    if _insecure_ssl or _bypass_latched:
        kwargs["ssl"] = _insecure_context()
        return await _original_ws_connect(url, **kwargs)

    # Verified against certifi rather than the system store -- see
    # verified_context. Only set when the caller has not chosen for itself.
    kwargs.setdefault("ssl", verified_context())

    try:
        return await _original_ws_connect(url, **kwargs)
    except ssl.SSLCertVerificationError as exc:
        if not _auto_bypass:
            raise
        _bypass_latched = True
        log.warning(
            "TLS verification failed for %s even against certifi's certificate "
            "bundle (%s). Retrying WITHOUT certificate checks: traffic stays "
            "encrypted, but nothing proves the endpoint is BULK, and fills and "
            "positions read from it are what every hedge is based on. If this "
            "persists, check the URL and the system clock before trusting it.",
            url, exc.verify_message or exc,
        )
        kwargs["ssl"] = _insecure_context()
        return await _original_ws_connect(url, **kwargs)


# Every spelling `OrderStatus.from_string` accepts, in its own capitalisation.
# Kept here because the SDK expresses them as a `match` statement, which cannot
# be read back at runtime.
_KNOWN_STATUSES = (
    "resting", "placed", "working", "filled", "partiallyFilled",
    "cancelled", "cancelledRiskLimit", "cancelAllRejected", "cancelOneRejected",
    "cancelledSelfCrossing", "cancelledReduceOnly", "cancelledIOC",
    "rejectedCrossing", "rejectedDuplicate", "rejectedRiskLimit",
    "rejectedInvalid",
)
_STATUS_BY_LOWER = {name.lower(): name for name in _KNOWN_STATUSES}


def _tolerant_status_from_string(original):
    """Accept a status the SDK spells differently, and never drop a response.

    Live, the exchange sends `cancelledIoc` where the SDK matches
    `cancelledIOC`. A pure capitalisation difference, and the enum member it
    needs already exists -- but `from_string` raises on it, and it is called
    from `_handle_post_response`, which is how an order submission's reply gets
    back to the caller. So the reply was dropped, the awaiting `wait_for` ran to
    its timeout, and a hedge failed. Seen four times in one session, once
    directly before `hedge for BTC-USD failed`.

    Matching case-insensitively against the SDK's own list of spellings is
    exact: it never invents a status, it only forgives capitalisation.

    A status on neither list is different. Raising drops the response and costs
    a hedge; guessing could be worse -- reading a new NON-terminal status as
    cancelled would tell the chaser its order had died and it would place a
    second one. So the guess is confined to the two prefixes that are terminal
    by construction, and anything else still raises.
    """

    def parse(cls, s: str):
        try:
            return original(s)
        except ValueError:
            pass

        spelled = _STATUS_BY_LOWER.get(str(s).lower())
        if spelled is not None:
            log.debug("order status %r accepted as %r", s, spelled)
            return original(spelled)

        text = str(s).lower()
        for prefix, fallback in (("cancel", "cancelled"), ("reject", "rejectedInvalid")):
            if text.startswith(prefix):
                log.warning(
                    "unknown order status %r -- treating it as %r so the order "
                    "response is still delivered. Both are terminal, so nothing "
                    "downstream acts on the difference.",
                    s, fallback,
                )
                return original(fallback)

        log.error(
            "unknown order status %r, and it is neither a cancel nor a reject. "
            "Refusing to guess: reading a non-terminal status as terminal would "
            "have the chaser replace an order that is still live.",
            s,
        )
        raise ValueError(f"Unknown order status {s}")

    return classmethod(parse)


def _first(data: dict, *names: str, default: Any = None) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return default


@classmethod
def _robust_fill_from_api(cls, data: dict) -> Fill:
    """Parse a fill from either the long or short field spelling."""
    fill = cls(
        symbol=_first(data, "symbol", "sym", default="") or "",
        order_id=_first(data, "orderId", "oid", default="") or "",
        price=float(_first(data, "price", "px", default=0) or 0),
        size=float(_first(data, "size", "sz", default=0) or 0),
        side=Side.BUY if _first(data, "isBuy", "b", default=False) else Side.SELL,
        timestamp=int(_first(data, "timestamp", "ts", default=0) or 0),
        is_maker=bool(_first(data, "maker", "mk", default=False)),
    )
    # v1.0.17 trade id, used to drop replayed fills. Not a field on Fill
    # upstream, so it is attached here.
    fill.trade_id = _first(data, "tradeId", "tid")
    return fill


def apply_ws_compat(*, insecure_ssl: bool = False, auto_bypass: bool = True) -> None:
    """Install the patches. Idempotent."""
    global _PATCHED
    set_ssl_options(insecure=insecure_ssl, auto_bypass=auto_bypass)
    if _PATCHED:
        return

    Fill.from_api = _robust_fill_from_api  # type: ignore[method-assign]

    from bulk_api.common.enums import OrderStatus

    OrderStatus.from_string = _tolerant_status_from_string(  # type: ignore[method-assign]
        OrderStatus.from_string
    )

    import bulk_api.api.bulk_ws as bulk_ws_module
    import websockets.asyncio.client as websockets_client

    bulk_ws_module.ws_connect = _connect_with_ssl_fallback  # type: ignore[attr-defined]
    websockets_client.connect = _connect_with_ssl_fallback  # type: ignore[attr-defined]

    _PATCHED = True
    log.debug("WebSocket compatibility patches applied")


def fill_trade_id(fill: Any) -> str | None:
    """Read the trade id off a parsed fill, if the patch supplied one."""
    return getattr(fill, "trade_id", None)


# -- the SDK's own printing --------------------------------------------------


def quieten_sdk_prints() -> int:
    """Send the SDK's `print` calls to the debug log. Returns how many modules.

    There are 128 of them across the package, and one fires on every HTTP
    account read:

        getting account info for 6r3jPT... from https://mainnet-api1...

    On a run that reads positions every five seconds that is most of what is on
    screen, and none of it is addressed to the operator. They are `print`, not
    logging, so no logger level reaches them.

    Shadowing the name inside each module is narrower than replacing the
    builtin: nothing outside `bulk_api` changes, and anything the SDK wanted to
    say is still in `logs.txt` at DEBUG rather than thrown away.
    """
    import sys

    quiet = 0
    for name, module in list(sys.modules.items()):
        if not name.startswith("bulk_api") or module is None:
            continue
        if getattr(module, "_bulkdn_quiet", False):
            continue
        module.print = lambda *a, **k: log.debug(
            "sdk: %s", " ".join(str(x) for x in a)
        )
        module._bulkdn_quiet = True
        quiet += 1
    return quiet

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

**3. TLS verification fails against the live endpoints.** Observed on
`mainnet-ws1.bulk.trade`, whose certificate is expired. Without a fallback the socket never
connects and the bot runs blind on HTTP polling alone.
"""

from __future__ import annotations

import logging
import ssl
from typing import Any, Optional

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


async def _connect_with_ssl_fallback(url: str, **kwargs: Any):
    """Open a socket, falling back to unverified TLS if the cert is rejected.

    Once a bypass has been needed it stays latched for the process, so
    reconnects don't pay the failed handshake every time.
    """
    global _bypass_latched
    kwargs.setdefault("open_timeout", _OPEN_TIMEOUT)

    if _insecure_ssl or _bypass_latched:
        kwargs["ssl"] = _insecure_context()
        return await _original_ws_connect(url, **kwargs)

    try:
        return await _original_ws_connect(url, **kwargs)
    except ssl.SSLCertVerificationError:
        if not _auto_bypass:
            raise
        _bypass_latched = True
        log.warning(
            "TLS verification failed for %s -- retrying without certificate "
            "checks. Traffic is still encrypted but the endpoint is unauthenticated.",
            url,
        )
        kwargs["ssl"] = _insecure_context()
        return await _original_ws_connect(url, **kwargs)


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

    import bulk_api.api.bulk_ws as bulk_ws_module
    import websockets.asyncio.client as websockets_client

    bulk_ws_module.ws_connect = _connect_with_ssl_fallback  # type: ignore[attr-defined]
    websockets_client.connect = _connect_with_ssl_fallback  # type: ignore[attr-defined]

    _PATCHED = True
    log.debug("WebSocket compatibility patches applied")


def fill_trade_id(fill: Any) -> Optional[str]:
    """Read the trade id off a parsed fill, if the patch supplied one."""
    return getattr(fill, "trade_id", None)

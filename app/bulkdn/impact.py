"""Size-impact curve, from `GET /impact`.

The endpoint returns the executor's published impact curve for one market:
`minSize`, and a `buyBps` / `sellBps` pair each holding `logMin`, `logRange`
and 250 impact readings in basis points at log-uniform size knots. Reading a
size off it is therefore an interpolation in log space, which is what
`ImpactCurve.bps_for` does.

The strategy hedges with market orders whose size is dictated by the fill it is
answering -- shrinking one would leave the pair directional, so this is not
used to resize anything. It is used to price the hedge before sending it, and
to refuse one whose expected impact is beyond a configured ceiling; a market
that has become too thin to hedge into is a reason to stop, not to trade
through.

**The endpoint answers 404 until a curve has been published for a symbol**, and
at the time of writing no mainnet market has one. Everything here degrades to
"no data" in that case, and a guard with no data does not fire.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import requests

@dataclass
class ImpactCurve:
    """One side of the curve: impact in bps across log-uniform size knots."""

    log_min: float
    log_range: float
    bps: list[float]

    def bps_for(self, size: float) -> float | None:
        """Expected impact in bps for `size`, linearly interpolated in log space.

        Returns None for a non-positive size or an empty curve. Sizes below the
        first knot take the first reading and sizes above the last take the
        last, since the curve says nothing beyond its own range.
        """
        if size <= 0 or not self.bps:
            return None
        if self.log_range <= 0:
            return self.bps[0]

        position = (math.log(size) - self.log_min) / self.log_range
        if position <= 0:
            return self.bps[0]
        if position >= 1:
            return self.bps[-1]

        exact = position * (len(self.bps) - 1)
        low = int(exact)
        high = min(low + 1, len(self.bps) - 1)
        weight = exact - low
        return self.bps[low] * (1 - weight) + self.bps[high] * weight

    @classmethod
    def from_api(cls, data: dict) -> ImpactCurve:
        return cls(
            log_min=float(data.get("logMin") or 0.0),
            log_range=float(data.get("logRange") or 0.0),
            bps=[float(x) for x in (data.get("bps") or [])],
        )


@dataclass
class Impact:
    symbol: str
    timestamp: int
    min_size: float
    buy: ImpactCurve
    sell: ImpactCurve

    def bps_for(self, size: float, is_buy: bool) -> float | None:
        return (self.buy if is_buy else self.sell).bps_for(size)

    @classmethod
    def from_api(cls, data: dict) -> Impact:
        return cls(
            symbol=data.get("symbol", ""),
            timestamp=int(data.get("timestamp") or 0),
            min_size=float(data.get("minSize") or 0.0),
            buy=ImpactCurve.from_api(data.get("buyBps") or {}),
            sell=ImpactCurve.from_api(data.get("sellBps") or {}),
        )


def fetch(http_url: str, symbol: str, timeout: int = 15) -> Impact | None:
    """Latest curve for one market, or None when none has been published."""
    response = requests.get(
        f"{http_url}/impact", params={"market": symbol}, timeout=timeout
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return Impact.from_api(response.json())


class ImpactBook:
    """Cached curves, refreshed on demand.

    Curves are executor-published and change slowly, so re-fetching one per
    hedge would spend a round trip on the latency path for no benefit. A symbol
    with no curve is remembered as absent and not retried until `refresh`.
    """

    def __init__(self, http_url: str):
        self.http_url = http_url
        self._curves: dict[str, Impact | None] = {}

    def refresh(self, symbols: list[str]) -> None:
        for symbol in symbols:
            try:
                self._curves[symbol] = fetch(self.http_url, symbol)
            except Exception:
                # A market-data endpoint being unreachable must not stop
                # trading; the guard simply has nothing to say.
                self._curves[symbol] = None

    def bps_for(self, symbol: str, size: float, is_buy: bool) -> float | None:
        curve = self._curves.get(symbol)
        return curve.bps_for(size, is_buy) if curve else None

    def has(self, symbol: str) -> bool:
        return self._curves.get(symbol) is not None

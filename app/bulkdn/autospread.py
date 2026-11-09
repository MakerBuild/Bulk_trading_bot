"""`mode: auto-spread` -- delta-neutral that picks its own market.

Delta-neutral pays the spread on every slice, twice over a round trip, so the
cheapest market to trade is the one whose spread is narrowest right now --
and which one that is changes through the day: stocks are tight in US hours
and wide outside them, coins the other way round on a quiet weekend.

So nothing here is chosen in advance. A run subscribes to the book of every
listed market of the chosen kind (`auto_spread.markets`: tradfi, crypto or
all), and each new group goes to the one with the narrowest spread at the
moment it is drawn -- the deeper one, between two at the same spread. A
market wider than `taking.max_spread_bps` is never picked; when every market
is, no group opens until one comes back inside it. Groups already trading
finish where they started.

The exchange does not say which of its markets are stocks, so the split is a
list kept here (`TRADFI_BASES`). `auto_spread.tradfi` and `auto_spread.crypto`,
when written, narrow each kind to the tickers they name -- one commented out
is not traded -- and a ticker in `tradfi` counts as a stock, for one listed
after this build.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from dataclasses import dataclass

from .config import AUTO_SPREAD_LABELS as KIND_LABELS  # noqa: F401 - re-exported
from .config import MAX_LEVERAGE, MIN_LEVERAGE, AutoSpreadConfig, Config, LegConfig
from .marketdata import MarketSpec, min_order_size
from .taking import REMAINDER_MARGIN, side_depth

# Stocks, indices and commodities: what the exchange charges the tradfi fee
# on. By base asset, so a new quote currency changes nothing.
TRADFI_BASES = frozenset({
    # Listed on BULK when this was written.
    "CRCL", "MSTR", "MU", "NVDA", "SKHYNIX", "SNDK", "SPACEX", "TSLA", "US500",
    # Likely next: the large US names, and the usual index and metal tickers.
    "AAPL", "AMD", "AMZN", "ARM", "AVGO", "BABA", "COIN", "GOOG", "GOOGL",
    "HOOD", "INTC", "META", "MSFT", "NFLX", "ORCL", "PLTR", "SMCI", "TSM",
    "SAMSUNG", "US100", "US30", "SPX", "NDX", "NAS100", "XAU", "XAG", "GOLD",
    "SILVER", "WTI", "BRENT",
})


def base_of(symbol: str) -> str:
    return symbol.split("-", maxsplit=1)[0].upper()


def asset_class(symbol: str, extra: Iterable[str] = ()) -> str:
    """'tradfi' or 'crypto'. `extra` names symbols or bases counted as tradfi."""
    named = {name.upper() for name in extra}
    if symbol.upper() in named or base_of(symbol) in named or base_of(symbol) in TRADFI_BASES:
        return "tradfi"
    return "crypto"


def named(symbol: str, tickers: Iterable[str]) -> bool:
    """Whether `symbol` is one of `tickers`, written `NVDA` or `NVDA-USD`."""
    names = {name.upper() for name in tickers}
    return symbol.upper() in names or base_of(symbol) in names


def wanted(symbol: str, settings: AutoSpreadConfig) -> bool:
    kind = asset_class(symbol, settings.tradfi or ())
    if settings.markets not in ("all", kind):
        return False
    tickers = settings.tradfi if kind == "tradfi" else settings.crypto
    return tickers is None or named(symbol, tickers)


def not_listed(listed: dict[str, dict], settings: AutoSpreadConfig) -> list[str]:
    """Tickers written in the settings that match no market the exchange lists."""
    written = [*(settings.tradfi or ()), *(settings.crypto or ())]
    return [ticker for ticker in written if not any(named(s, [ticker]) for s in listed)]


def candidates(listed: dict[str, dict], settings: AutoSpreadConfig) -> list[str]:
    """The listed markets auto-spread may pick from, in name order.

    A market the exchange has suspended is left out: it takes no orders, and
    its last book can look as tight as any.
    """
    return sorted(
        symbol for symbol, info in listed.items()
        if str(info.get("status", "TRADING")).upper() == "TRADING" and wanted(symbol, settings)
    )


def auto_legs(config: Config, listed: dict[str, dict], keep: Iterable[str] = ()) -> list[LegConfig]:
    """One market entry per candidate, as the run's `markets` list.

    A market the settings file already has keeps its own numbers there --
    switched on here whether or not it is there. Any other is sized from
    `auto_spread`, at its leverage cut to the market's own ceiling. `keep`
    are markets with a cycle still open, which this run must be able to
    finish whatever the choice of kind.
    """
    settings = config.auto_spread
    symbols = candidates(listed, settings)
    symbols += sorted(set(keep) - set(symbols))
    configured = {leg.symbol: leg for leg in config.markets}
    legs = []
    for symbol in symbols:
        ceiling = float((listed.get(symbol) or {}).get("maxLeverage") or MAX_LEVERAGE)
        if symbol in configured:
            leg = copy.deepcopy(configured[symbol])
            leg.enabled = True
        else:
            span = settings.notional_usd
            leg = LegConfig(
                symbol=symbol,
                notional_usd=span.high,
                notional_span=span,
                max_order_notional_usd=span.high,
                max_order_span=span,
                leverage=settings.leverage,
            )
        if leg.leverage is not None:
            leg.leverage = max(MIN_LEVERAGE, min(leg.leverage, ceiling))
        legs.append(leg)
    return legs


@dataclass(frozen=True)
class Seen:
    """One market's book, as auto-spread weighs it."""

    symbol: str
    spread_bps: float
    # The thinner side's depth inside `taking.max_slippage_bps`, in dollars:
    # what a slice could take there.
    depth_usd: float
    price: float = 0.0

    def describe(self) -> str:
        return f"{self.symbol} {self.spread_bps:.2f}bps (${self.depth_usd:,.0f} deep)"


def deep_enough(seen: Seen, spec: MarketSpec | None, depth_fraction: float) -> bool:
    """Whether a slice of this book could be the market's minimum order.

    The taker never sends less than the minimum, so on a book thinner than
    that it waits -- and a group sent there would sit open until the phase
    timeout. Such a market is passed over however narrow its spread.
    """
    if spec is None or seen.price <= 0:
        return True
    floor_usd = min_order_size(spec, seen.price) * seen.price
    return seen.depth_usd * depth_fraction >= floor_usd * REMAINDER_MARGIN


def measure(feed, symbol: str, band_bps: float, levels: int) -> Seen | None:
    """The spread and depth of one market's book now. None without a book."""
    quote = feed.quote(symbol)
    bid, ask = quote.best_bid, quote.best_ask
    if not bid or not ask or ask <= bid:
        return None
    mid = (bid + ask) / 2
    asks = side_depth(feed.levels(symbol, False, levels), taker_buys=True, band_bps=band_bps)
    bids = side_depth(feed.levels(symbol, True, levels), taker_buys=False, band_bps=band_bps)
    depth = 0.0
    if asks is not None and bids is not None:
        depth = min(asks.size * asks.touch, bids.size * bids.touch)
    return Seen(symbol=symbol, spread_bps=(ask - bid) / mid * 10_000, depth_usd=depth, price=mid)


def pick(seen: Iterable[Seen], max_spread_bps: float) -> Seen | None:
    """The narrowest spread within the limit; the deeper of two equal ones.

    Spreads are compared to a hundredth of a bp: two markets one tick wide
    differ in the last digits by their prices alone, and that is no reason to
    prefer the thinner one.
    """
    inside = [s for s in seen if s.spread_bps <= max_spread_bps + 1e-9 and s.depth_usd > 0]
    if not inside:
        return None
    return min(inside, key=lambda s: (round(s.spread_bps, 2), -s.depth_usd, s.symbol))

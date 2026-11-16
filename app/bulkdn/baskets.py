"""`mode: baskets` -- a dollar-neutral basket and its mirror, for the Basket Gauntlet.

A basket is one transaction of market orders on one account: `per_side`
tickers bought and as many sold, the basket's dollars split evenly between
them. The UI sends exactly that -- a signed transaction whose actions are
market orders -- to the exchange, and then the same signed transaction to the
Basket Gauntlet's own endpoint (`GAUNTLET_URL`), which records it. That
endpoint executes nothing: a basket only counts when it went to the exchange
as well, and the team confirmed that baskets sent this way from a script
count.

Each group here is a master and one of its own subs, as in delta-neutral.
One of the two opens a basket and the other opens its mirror, both at once,
so between them nothing is held; after the hold both close with a basket of
reduce-only market orders. Only the opening baskets are registered: the
website registers the open of a basket, not its close. Whatever a market order leaves uneven between the
two accounts is evened out by the hedger once both baskets have answered,
and whatever the close leaves is swept at market.

The tickers are drawn at random every cycle from the markets of one kind --
`baskets.markets`, read through `auto_spread`'s ticker lists -- among those
whose spread is within `taking.max_spread_bps` and whose book, inside
`taking.max_slippage_bps`, can take the leg's size.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import requests
from bulk_api.common import Side
from bulk_api.messages.trade import MarketOrder

from .autospread import Seen
from .config import AutoSpreadConfig, BasketsConfig, Config, Span
from .marketdata import MarketSpec, tradeable_size
from .retry import describe

log = logging.getLogger(__name__)

# Where the UI registers a basket for the Gauntlet, after sending it to the
# exchange. The body is the signed transaction itself.
GAUNTLET_URL = "https://app.bulk.trade/api/basket-gauntlet/batches"
# Tries at registering one basket. A lost answer is safe to send again: the
# body is the same signed transaction, and the endpoint only records it.
REGISTER_ATTEMPTS = 3


def settings_view(config: Config) -> AutoSpreadConfig:
    """Baskets' markets, spelled the way auto-spread's market choice reads them.

    The kind is baskets' own; the ticker lists are auto-spread's, which the
    menus already edit. The size is one leg's: a basket's dollars split over
    its tickers, both sides.
    """
    settings = config.baskets
    return AutoSpreadConfig(
        markets=settings.markets,
        notional_usd=leg_span(settings),
        leverage=settings.leverage,
        tradfi=config.auto_spread.tradfi,
        crypto=config.auto_spread.crypto,
    )


def legs_per_basket(settings: BasketsConfig) -> int:
    return 2 * settings.per_side


def leg_span(settings: BasketsConfig) -> Span:
    """One ticker's share of the basket, in dollars."""
    n = legs_per_basket(settings)
    return Span(settings.notional_usd.low / n, settings.notional_usd.high / n)


@dataclass(frozen=True)
class BasketLeg:
    """One ticker of a drawn basket, as the opener trades it."""

    symbol: str
    # The opener buys this one; its partner, opening the mirror, sells it.
    opener_buys: bool
    size: float
    price: float

    def describe(self) -> str:
        return f"{'+' if self.opener_buys else '-'}{self.symbol.split('-')[0]}"


def leg_size(spec: MarketSpec, usd: float, price: float) -> float:
    """`usd` of this market in whole lots, or 0.0 below its minimum order."""
    if price <= 0:
        return 0.0
    return tradeable_size(spec, usd / price, price)


def choose(
    seen: Iterable[Seen],
    specs: Mapping[str, MarketSpec],
    per_side: int,
    leg_usd: float,
    *,
    max_spread_bps: float,
    depth_fraction: float,
    rng: random.Random | None = None,
) -> list[BasketLeg] | None:
    """A basket: `per_side` tickers long and as many short, or None.

    Only markets within the spread limit whose book can take the whole leg:
    a market order walks the book, so a leg bigger than the depth inside the
    band would fill deeper than `taking.max_slippage_bps`. None while fewer
    than `2 * per_side` markets qualify -- the caller waits and asks again.
    """
    rng = rng or random.Random()
    fit = []
    for book in seen:
        spec = specs.get(book.symbol)
        if spec is None or book.price <= 0:
            continue
        if book.spread_bps > max_spread_bps + 1e-9:
            continue
        size = leg_size(spec, leg_usd, book.price)
        if not size or book.depth_usd * depth_fraction < size * book.price:
            continue
        fit.append((book, size))
    wanted = 2 * per_side
    if len(fit) < wanted:
        return None
    picked = rng.sample(fit, wanted)
    return [
        BasketLeg(book.symbol, opener_buys=index < per_side, size=size, price=book.price)
        for index, (book, size) in enumerate(picked)
    ]


def describe_basket(legs: Sequence[BasketLeg]) -> str:
    longs = "+".join(leg.symbol.split("-")[0] for leg in legs if leg.opener_buys)
    shorts = "+".join(leg.symbol.split("-")[0] for leg in legs if not leg.opener_buys)
    return f"long {longs} / short {shorts}"


def market_orders(trades: Iterable[tuple[str, bool, float]], *, reduce_only: bool) -> list:
    """One market order per (symbol, buy, size): the actions of one basket."""
    return [
        MarketOrder(
            symbol=symbol,
            side=Side.BUY if buy else Side.SELL,
            size=size,
            reduce_only=reduce_only,
        )
        for symbol, buy, size in trades
    ]


def register(tx: dict, *, url: str = GAUNTLET_URL, timeout: float = 10.0) -> tuple[int, object]:
    """Send a basket's signed transaction to the Gauntlet. (HTTP status, body).

    Raises only when no attempt got an answer at all. A refusal is returned,
    not raised: the basket traded either way, and the caller logs what the
    endpoint said.
    """
    last: Exception | None = None
    for _attempt in range(REGISTER_ATTEMPTS):
        try:
            response = requests.post(url, json=tx, timeout=timeout)
        except requests.RequestException as exc:
            last = exc
            continue
        if response.status_code >= 500:
            last = RuntimeError(f"HTTP {response.status_code}")
            continue
        try:
            body: object = response.json()
        except ValueError:
            body = response.text[:300]
        return response.status_code, body
    raise RuntimeError(f"no answer from the Basket Gauntlet: {describe(last)}")

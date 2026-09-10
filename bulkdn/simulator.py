"""In-process exchange simulator.

BULK publishes no reachable testnet -- every candidate host fails to resolve, so
the only live endpoint is mainnet, where testing costs real money. This module
exists so the full OPEN -> HOLD -> EXIT cycle can be exercised end to end with
no network, no keys, and no funds at risk.

It is a behavioural simulation, not a market model: resting orders fill in
slices, market orders fill instantly and completely, and fills are pushed
through the same handler path the real WebSocket stream uses. That is enough to
exercise routing, hedging, chasing, phase transitions, and the risk checks --
which is where the strategy's bugs actually live.

What it deliberately does not model: queue position, slippage, fees, funding,
rejections, latency, or partial market-order fills.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from bulk_api.common import Side, Topic

from .feed import Quote
from .marketdata import MarketSpec

log = logging.getLogger(__name__)


@dataclass
class SimOrder:
    oid: str
    symbol: str
    is_buy: bool
    price: float
    size: float
    account: str
    reduce_only: bool


@dataclass
class SimExchange:
    """Positions, resting orders, and fill dispatch."""

    positions: Dict[tuple, float] = field(default_factory=dict)
    orders: Dict[str, SimOrder] = field(default_factory=dict)
    handlers: Dict[tuple, list] = field(default_factory=dict)
    market_orders: List[tuple] = field(default_factory=list)
    sessions: Dict[str, "SimSession"] = field(default_factory=dict)
    counter: int = 0
    slot: int = 12_000
    sequence: int = 0
    replay_every: int = 0
    replayed: int = 0

    def position(self, account: str, symbol: str) -> float:
        return self.positions.get((account, symbol), 0.0)

    def _move(self, account: str, symbol: str, delta: float) -> None:
        self.positions[(account, symbol)] = self.position(account, symbol) + delta

    def next_trade_id(self) -> str:
        """Mint a v1.0.17-style `<slot>:<event-sequence>` id."""
        self.sequence += 1
        if self.sequence % 32 == 0:
            self.slot += 1
        return f"{self.slot}:{self.sequence}"

    def emit_fill(
        self,
        account: str,
        symbol: str,
        is_buy: bool,
        size: float,
        price: float,
        trade_id: Optional[str] = None,
    ) -> None:
        """Apply a fill and push it through the account's handlers."""
        trade_id = trade_id or self.next_trade_id()
        self._move(account, symbol, size if is_buy else -size)
        self._dispatch(account, symbol, is_buy, size, price, trade_id)

        # Periodically redeliver a fill, the way a reconnect replay would, so
        # the trade-id deduplication is actually exercised. The position is
        # moved only once; a bot that double-counts will show it immediately.
        if self.replay_every and self.sequence % self.replay_every == 0:
            self.replayed += 1
            self._dispatch(account, symbol, is_buy, size, price, trade_id)

    def _dispatch(self, account, symbol, is_buy, size, price, trade_id) -> None:
        session = self.sessions.get(account)
        if session is not None:
            session.current_trade_id = trade_id
        fill = _SimFill(
            symbol=symbol,
            size=size,
            side=Side.BUY if is_buy else Side.SELL,
            price=price,
        )
        try:
            for handler in self.handlers.get((account, Topic.FILL), []):
                handler(fill)
        finally:
            if session is not None:
                session.current_trade_id = None

    def fill_resting(self, oid: str, size: float, spec: MarketSpec) -> float:
        """Fill part of a resting order. Returns the size actually filled."""
        order = self.orders.get(oid)
        if order is None:
            return 0.0
        size = min(size, order.size)
        if size <= 0:
            return 0.0
        order.size -= size
        if order.size < spec.lot_size:
            del self.orders[oid]
        self.emit_fill(order.account, order.symbol, order.is_buy, size, order.price)
        return size

    def resting_for(self, account: str) -> Dict[str, SimOrder]:
        return {o.oid: o for o in self.orders.values() if o.account == account}

    def net(self, accounts: Sequence[str], symbol: str) -> float:
        return sum(self.position(a, symbol) for a in accounts)


@dataclass
class _SimFill:
    symbol: str
    size: float
    side: object
    price: float


class _SimClient:
    """Stands in for the WebSocket client's order-map and liveness surface."""

    def __init__(self, exchange: SimExchange, account: str):
        self.exchange = exchange
        self.account = account
        self.is_connected = True
        self.last_message_at = 0.0

    def get_order_map(self) -> Dict[str, SimOrder]:
        return self.exchange.resting_for(self.account)


class SimSession:
    """Implements the slice of `AccountSession` the strategy actually uses."""

    def __init__(self, exchange: SimExchange, name: str, pubkey: str, symbols: Sequence[str]):
        self.exchange = exchange
        self.name = name
        self.pubkey = pubkey
        self.symbols = list(symbols)
        self.dry_run = False
        self.reject_streak = 0
        self.client = _SimClient(exchange, pubkey)
        self.current_trade_id: Optional[str] = None
        exchange.sessions[pubkey] = self

    @property
    def is_connected(self) -> bool:
        return True

    @property
    def last_message_age_s(self) -> float:
        return 0.0

    def on(self, topic, handler) -> None:
        self.exchange.handlers.setdefault((self.pubkey, topic), []).append(handler)

    async def place_limit(self, symbol, is_buy, price, size, reduce_only=False, cancel_oid=None):
        if cancel_oid:
            self.exchange.orders.pop(cancel_oid, None)
        self.exchange.counter += 1
        oid = f"sim-{self.exchange.counter}"
        self.exchange.orders[oid] = SimOrder(
            oid, symbol, is_buy, price, size, self.pubkey, reduce_only
        )
        return oid, []

    async def cancel(self, symbol, oid):
        self.exchange.orders.pop(oid, None)
        return []

    async def cancel_all(self, symbols):
        for oid, order in list(self.exchange.orders.items()):
            if order.account == self.pubkey and order.symbol in symbols:
                del self.exchange.orders[oid]
        return []

    async def market(self, symbol, is_buy, size, reduce_only=False):
        price = SimFeed.prices.get(symbol, 1.0)
        self.exchange.market_orders.append((self.name, symbol, is_buy, size, reduce_only))
        self.exchange.emit_fill(self.pubkey, symbol, is_buy, size, price)
        return []

    def full_account(self) -> Dict:
        return {
            "positions": [
                {"symbol": s, "size": self.exchange.position(self.pubkey, s)}
                for s in self.symbols
            ]
        }

    def open_orders(self) -> List[Dict]:
        return [
            {
                "symbol": o.symbol,
                "isBuy": o.is_buy,
                "size": o.size,
                "price": o.price,
                "orderId": o.oid,
            }
            for o in self.exchange.resting_for(self.pubkey).values()
        ]


class SimFeed:
    """Market data with an optional random walk, using real exchange specs."""

    prices: Dict[str, float] = {}

    def __init__(self, specs: Dict[str, MarketSpec], prices: Dict[str, float], volatility_bps: float = 0.0):
        self.specs = specs
        SimFeed.prices = dict(prices)
        self.volatility_bps = volatility_bps

    def step(self, rng: random.Random) -> None:
        """Nudge prices, so the chaser has something to chase."""
        if self.volatility_bps <= 0:
            return
        for symbol in list(SimFeed.prices):
            drift = rng.gauss(0.0, self.volatility_bps / 10_000.0)
            SimFeed.prices[symbol] = max(1e-8, SimFeed.prices[symbol] * (1 + drift))

    def quote(self, symbol: str) -> Quote:
        price = SimFeed.prices[symbol]
        spread = max(self.specs[symbol].tick_size, price * 0.00005)
        return Quote(
            symbol=symbol,
            best_bid=price - spread,
            best_ask=price + spread,
            mark_price=price,
            age_s=0.0,
        )

    def reference_price(self, symbol: str) -> Optional[float]:
        return SimFeed.prices.get(symbol)

"""Close All closes dust too: a remainder under the market's minimum order.

$0.68 of BTC against a $1 floor cannot be closed by an order of its own size,
so Close All reported every account flat and left it there for good. It is
grown past the minimum and closed whole instead.
"""

import types

import pytest

from bulkdn import reconcile
from bulkdn.marketdata import MarketSpec
from bulkdn.positions import PositionBook

BTC = "BTC-USD"
SPEC = MarketSpec(BTC, tick_size=0.01, lot_size=0.00000001, min_notional=1.0)
PRICE = 84_000.0


class Account:
    """Applies its own trades to the position it reports, as the exchange would."""

    def __init__(self, name, size, refuse=None):
        self.name = name
        self.pubkey = f"{name}-KEY"
        self.size = size
        self.refuse = refuse
        self.trades = []
        self.stream_lagging_until = 0.0
        self.dry_run = False
        self.is_connected = True

    def full_account(self):
        return {"positions": [{"symbol": BTC, "size": self.size}] if self.size else []}

    async def market(self, symbol, is_buy, size, reduce_only=False):
        if self.refuse == "grow":
            raise RuntimeError("refused")
        self.trades.append(("grow", is_buy, size))
        self.size = round(self.size + (size if is_buy else -size), 8)

    async def close_market(self, symbol, is_buy, size):
        if self.refuse == "close":
            raise RuntimeError("refused")
        self.trades.append(("close", is_buy, size))
        held = abs(self.size)
        self.size = round(self.size - min(size, held) * (1 if self.size > 0 else -1), 8)


@pytest.fixture
def feed(monkeypatch):
    monkeypatch.setattr(reconcile, "FLATTEN_READ_INTERVAL_S", 0.01)
    monkeypatch.setattr(reconcile, "FLATTEN_SETTLE_S", 0.1)
    return types.SimpleNamespace(specs={BTC: SPEC}, reference_price=lambda s: PRICE)


async def sweep(feed, *accounts):
    return await reconcile.sweep_dust(
        {a.pubkey: a for a in accounts}, PositionBook(), feed, [BTC]
    )


async def test_a_long_remainder_is_grown_then_closed_whole(feed):
    account = Account("m1s1", 0.00000811)                 # $0.68

    left = await sweep(feed, account)

    (grow, close) = account.trades
    assert grow[0] == "grow" and grow[1] is True, "grown the way it already points"
    assert grow[2] * PRICE >= SPEC.min_notional, "grown past the minimum"
    assert close == ("close", False, pytest.approx(0.00000811 + grow[2]))
    assert account.size == 0 and left == []


async def test_a_short_one_lot_remainder_is_closed_too(feed):
    account = Account("m2", -0.00000001)

    left = await sweep(feed, account)

    assert [t[:2] for t in account.trades] == [("grow", False), ("close", True)]
    assert account.size == 0 and left == []


async def test_a_position_large_enough_to_close_is_left_to_flatten(feed):
    account = Account("m1s2", 0.002)                     # $168

    await sweep(feed, account)

    assert account.trades == []


async def test_a_flat_account_is_left_alone(feed):
    account = Account("m1s3", 0.0)
    assert await sweep(feed, account) == [] and account.trades == []


async def test_a_refused_grow_adds_nothing_and_is_reported(feed):
    account = Account("m1s1", 0.00000811, refuse="grow")

    left = await sweep(feed, account)

    assert account.trades == []
    assert left == ["m1s1 BTC-USD=+0.00000811"]


async def test_a_grown_position_that_cannot_be_closed_raises_an_alert(feed, caplog):
    account = Account("m1s1", 0.00000811, refuse="close")

    left = await sweep(feed, account)

    assert left, "reported flat over a position now a dollar larger"
    flagged = [r for r in caplog.records if getattr(r, "alert", False)]
    assert flagged and "MANUAL ACTION REQUIRED" in flagged[0].getMessage()

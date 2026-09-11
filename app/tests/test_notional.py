"""Leg sizes written in dollars instead of in the base coin.

`size: 2.0` means $200 of SOL and $5,200 of ETH. The number carries no unit, so
changing `symbol` and leaving it silently resizes the cycle -- which is the
failure this exists to remove. `notional_usd` says the same thing in any
market, and is converted to a quantity once, at startup, against the live
price.

The conversion is the only place the two units meet: everything downstream
reads `leg.size`, so these tests are mostly about what `leg.size` ends up being.
"""

import pytest

from bulkdn.config import ConfigError, LegConfig, _leg_from_dict
from bulkdn.marketdata import MarketSpec
from bulkdn.sizing import InsufficientMargin, Unpriceable, resolve_notionals

BTC = "BTC-USD"
ETH = "ETH-USD"
SOL = "SOL-USD"

SPECS = {
    BTC: MarketSpec(BTC, tick_size=0.5, lot_size=0.001, min_notional=1.0),
    ETH: MarketSpec(ETH, tick_size=0.01, lot_size=0.001, min_notional=50.0),
    SOL: MarketSpec(SOL, tick_size=0.01, lot_size=0.1, min_notional=50.0),
}
PRICES = {BTC: 80_000.0, ETH: 2_600.0, SOL: 100.0}


def resolve(*legs) -> None:
    resolve_notionals(legs=list(legs), specs=SPECS, prices=PRICES)


# -- conversion ------------------------------------------------------------


def test_dollars_become_a_quantity_at_the_current_price():
    leg = LegConfig(symbol=BTC, notional_usd=80.0, max_order_notional_usd=80.0)
    resolve(leg)
    assert leg.size == 0.001  # $80 / $80,000
    assert leg.max_order_size == 0.001


def test_the_same_dollar_amount_survives_a_symbol_change():
    """The whole point: one number, two markets, the same exposure."""
    btc = LegConfig(symbol=BTC, notional_usd=260.0, max_order_notional_usd=260.0)
    eth = LegConfig(symbol=ETH, notional_usd=260.0, max_order_notional_usd=260.0)
    resolve(btc, eth)

    assert btc.size * PRICES[BTC] == pytest.approx(260.0, abs=PRICES[BTC] * SPECS[BTC].lot_size)
    assert eth.size * PRICES[ETH] == pytest.approx(260.0, abs=PRICES[ETH] * SPECS[ETH].lot_size)
    # Written as a quantity, the same `2.0` would have been $200 and $5,200.


def test_the_quantity_is_rounded_down_to_a_whole_lot():
    """Never up: an order above the target would overshoot the cycle."""
    leg = LegConfig(symbol=SOL, notional_usd=95.0)  # 0.95 SOL, lot 0.1
    resolve(leg)
    assert leg.size == 0.9
    assert leg.size * PRICES[SOL] <= 95.0


def test_a_leg_written_in_the_base_coin_is_left_alone():
    """An existing config must keep behaving exactly as it did."""
    leg = LegConfig(symbol=BTC, size=0.002, max_order_size=0.002)
    resolve(leg)
    assert (leg.size, leg.max_order_size) == (0.002, 0.002)


def test_the_two_units_can_be_mixed_across_legs():
    coins = LegConfig(symbol=BTC, size=0.002, max_order_size=0.002)
    dollars = LegConfig(symbol=ETH, notional_usd=260.0, max_order_notional_usd=260.0)
    resolve(coins, dollars)
    assert coins.size == 0.002
    assert dollars.size == 0.1


# -- the per-order cap -----------------------------------------------------


def test_an_omitted_cap_defaults_to_the_whole_leg():
    leg = _leg_from_dict({"symbol": ETH, "notional_usd": 260.0}, "sub_account")
    assert leg.max_order_notional_usd == 260.0
    resolve(leg)
    assert leg.max_order_size == leg.size


def test_a_cap_below_one_lot_floors_at_one_lot():
    """A cap rounded down to nothing would block every order."""
    leg = LegConfig(symbol=SOL, notional_usd=500.0, max_order_notional_usd=1.0)
    resolve(leg)
    assert leg.max_order_size == SPECS[SOL].lot_size


# -- refusals --------------------------------------------------------------


def test_an_unpriceable_market_is_refused_rather_than_sized_at_zero():
    leg = LegConfig(symbol="DOGE-USD", notional_usd=100.0)
    with pytest.raises(Unpriceable, match="no price available"):
        resolve_notionals(
            legs=[leg],
            specs={"DOGE-USD": MarketSpec("DOGE-USD", 0.001, 1.0, 10.0)},
            prices={},
        )


def test_a_notional_under_one_lot_says_what_one_lot_costs():
    leg = LegConfig(symbol=ETH, notional_usd=1.0)  # a lot is 0.001 ETH = $2.60
    with pytest.raises(InsufficientMargin, match=r"under one lot"):
        resolve(leg)
    with pytest.raises(InsufficientMargin, match=r"\$2\.60"):
        resolve(leg)


def test_setting_both_units_is_refused():
    leg = LegConfig(symbol=BTC, size=0.001, notional_usd=80.0, max_order_size=0.001)
    with pytest.raises(ConfigError, match="sets both `size` and `notional_usd`"):
        leg.validate("master_account")


def test_setting_neither_is_refused():
    leg = LegConfig(symbol=BTC)
    with pytest.raises(ConfigError, match="has no size"):
        leg.validate("master_account")


def test_setting_both_caps_is_refused():
    leg = LegConfig(
        symbol=BTC, notional_usd=80.0, max_order_size=0.001, max_order_notional_usd=80.0
    )
    with pytest.raises(ConfigError, match="max_order_notional_usd"):
        leg.validate("master_account")


def test_a_dollar_leg_validates_without_a_size():
    """Validation runs at load, before any price exists."""
    leg = _leg_from_dict({"symbol": BTC, "notional_usd": 80.0}, "master_account")
    assert leg.size == 0.0
    leg.validate("master_account")  # must not raise

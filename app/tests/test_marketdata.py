"""Reading a market's trading steps from /exchangeInfo."""

import pytest

from bulkdn.marketdata import MarketSpec


# -- /exchangeInfo field names (API v1.0.20) --------------------------------


def test_the_renamed_size_fields_are_read():
    spec = MarketSpec.from_api({
        "symbol": "ETH-USD", "tickSize": 0.001, "sizeIncrement": 0.0001,
        "sizeDecimals": 4, "minNotional": 50.0,
    })
    assert spec.lot_size == 0.0001
    assert spec.size_precision == 4


def test_the_old_size_fields_still_work():
    """Until the upgrade lands, and on any network that has not had it."""
    spec = MarketSpec.from_api({
        "symbol": "ETH-USD", "tickSize": 0.001, "lotSize": 0.0001, "sizePrecision": 4,
    })
    assert spec.lot_size == 0.0001
    assert spec.size_precision == 4


def test_the_new_name_wins_when_both_are_given():
    spec = MarketSpec.from_api({
        "symbol": "BTC-USD", "tickSize": 0.1, "lotSize": 0.000001, "sizeIncrement": 0.00000001,
    })
    assert spec.lot_size == 0.00000001


def test_a_market_without_a_size_step_is_refused_by_name():
    """It used to default to 1e-8: the rename would have made every ETH size a
    non-multiple of its real 0.0001 step, every order refused."""
    with pytest.raises(ValueError, match="ETH-USD"):
        MarketSpec.from_api({"symbol": "ETH-USD", "tickSize": 0.001, "minNotional": 50.0})

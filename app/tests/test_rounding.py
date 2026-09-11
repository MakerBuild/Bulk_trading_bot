"""Rounding must be exact: an off-tick price or sub-lot size is rejected."""

from bulkdn.marketdata import (
    MarketSpec,
    chase_price,
    distance_bps,
    round_price,
    round_price_down,
    round_price_up,
    round_size,
)

BTC = MarketSpec(symbol="BTC-USD", tick_size=0.5, lot_size=0.001, min_notional=10.0)
SOL = MarketSpec(symbol="SOL-USD", tick_size=0.01, lot_size=0.1, min_notional=10.0)


def test_price_rounds_toward_passive_side():
    # A buy must never round up past its target, or it stops being passive.
    assert round_price_down(100_000.4, BTC) == 100_000.0
    assert round_price_up(100_000.4, BTC) == 100_000.5
    assert round_price(100_000.4, BTC, is_buy=True) == 100_000.0
    assert round_price(100_000.4, BTC, is_buy=False) == 100_000.5


def test_price_already_on_tick_is_unchanged():
    assert round_price_down(100_000.5, BTC) == 100_000.5
    assert round_price_up(100_000.5, BTC) == 100_000.5


def test_size_always_rounds_down():
    # Rounding up would overshoot the target or exceed a reduce-only position.
    assert round_size(0.0019, BTC) == 0.001
    assert round_size(0.999, SOL) == 0.9
    assert round_size(0.0009, BTC) == 0.0


def test_accumulated_float_error_does_not_erase_a_whole_lot():
    """Regression: a real one-lot position must never round to zero.

    Positions are accumulated by adding and subtracting fills, so a genuine
    0.1 arrives as 0.09999999999999998. Flooring that naively gives zero, and
    the position gets classified as unclosable dust and silently left open.
    """
    residual = 0.7 - 0.6
    assert residual < 0.1  # genuinely below the lot in binary floating point

    assert round_size(residual, SOL) == 0.1
    assert round_size(residual, SOL) >= SOL.lot_size

    # The same error on the other side of a subtraction.
    assert round_size(0.3 - 0.2, SOL) == 0.1


def test_genuine_sub_lot_amounts_still_round_down():
    # The epsilon snap must not promote real dust into a tradeable size.
    assert round_size(0.09, SOL) == 0.0
    assert round_size(0.0999, SOL) == 0.0
    assert round_size(0.19, SOL) == 0.1


def test_float_error_does_not_leak_into_prices():
    # 0.1 + 0.2 == 0.30000000000000004 in binary floating point; going through
    # Decimal is what keeps this on-tick.
    spec = MarketSpec(symbol="X", tick_size=0.1, lot_size=0.1, min_notional=0.0)
    assert round_price_down(0.1 + 0.2, spec) == 0.3
    assert round_size(0.1 + 0.2, spec) == 0.3


def test_chase_price_rests_inside_the_touch():
    buy = chase_price(
        best_bid=100_000.0, best_ask=100_010.0, mark_price=100_005.0,
        is_buy=True, offset_bps=10.0, spec=BTC,
    )
    # 10 bps below 100_000 is 99_900, already on-tick.
    assert buy == 99_900.0
    assert buy < 100_000.0

    sell = chase_price(
        best_bid=100_000.0, best_ask=100_010.0, mark_price=100_005.0,
        is_buy=False, offset_bps=10.0, spec=BTC,
    )
    assert sell > 100_010.0


def test_chase_price_falls_back_to_mark_without_a_book():
    price = chase_price(
        best_bid=None, best_ask=None, mark_price=100_000.0,
        is_buy=True, offset_bps=0.0, spec=BTC,
    )
    assert price == 100_000.0


def test_chase_price_returns_none_without_any_reference():
    assert chase_price(
        best_bid=None, best_ask=None, mark_price=None,
        is_buy=True, offset_bps=0.0, spec=BTC,
    ) is None


def test_distance_bps():
    assert distance_bps(100.0, 100.0) == 0.0
    assert round(distance_bps(101.0, 100.0), 6) == 100.0
    # Symmetric in magnitude.
    assert round(distance_bps(99.0, 100.0), 6) == 100.0

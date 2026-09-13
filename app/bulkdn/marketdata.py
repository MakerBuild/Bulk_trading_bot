"""Market specifications and price/size rounding.

Rounding goes through Decimal rather than float arithmetic: `0.1 + 0.2` style
error is enough to push a price off-tick or a size below a lot boundary, and
the exchange rejects both.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

BPS = Decimal(10_000)

# Positions and sizes are accumulated by adding and subtracting fills, which
# leaves binary representation error: `0.7 - 0.6` is `0.09999999999999998`, not
# `0.1`. Flooring that against a 0.1 lot yields zero, so a genuine one-lot
# position would be classified as unclosable dust and silently left open.
#
# Snapping the step count to this many decimals before the directional rounding
# absorbs that error while leaving real sub-lot values alone -- no realistic
# tick or lot count needs more than nine decimals of precision.
_STEP_EPSILON = Decimal("1e-9")


@dataclass(frozen=True)
class MarketSpec:
    """Trading constraints for one symbol, as reported by /exchangeInfo."""

    symbol: str
    tick_size: float
    lot_size: float
    min_notional: float
    price_precision: int = 8
    size_precision: int = 8
    max_leverage: float = 1.0

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> MarketSpec:
        return cls(
            symbol=data["symbol"],
            tick_size=float(data.get("tickSize", 0.01)),
            lot_size=float(data.get("lotSize", 0.00000001)),
            min_notional=float(data.get("minNotional", 0.0)),
            price_precision=int(data.get("pricePrecision", 8)),
            size_precision=int(data.get("sizePrecision", 8)),
            max_leverage=float(data.get("maxLeverage", 1.0)),
        )


def _step_count(value: float, step: float) -> Decimal:
    """How many `step`s fit in `value`, with float error snapped out."""
    return (Decimal(str(value)) / Decimal(str(step))).quantize(
        _STEP_EPSILON, rounding=ROUND_HALF_UP
    )


def _quantize(value: float, step: float, rounding: str) -> float:
    """Snap `value` onto a multiple of `step`."""
    if step <= 0:
        return value
    steps = _step_count(value, step).quantize(Decimal(1), rounding=rounding)
    return float(steps * Decimal(str(step)))


def round_price_down(price: float, spec: MarketSpec) -> float:
    """Round to the tick at or below `price` -- correct for resting bids."""
    return _quantize(price, spec.tick_size, ROUND_FLOOR)


def round_price_up(price: float, spec: MarketSpec) -> float:
    """Round to the tick at or above `price` -- correct for resting asks."""
    if spec.tick_size <= 0:
        return price
    steps = -(-_step_count(price, spec.tick_size)).quantize(
        Decimal(1), rounding=ROUND_FLOOR
    )
    return float(steps * Decimal(str(spec.tick_size)))


def round_price(price: float, spec: MarketSpec, is_buy: bool) -> float:
    """Round a limit price so it stays passive on the given side."""
    return round_price_down(price, spec) if is_buy else round_price_up(price, spec)


def round_size(size: float, spec: MarketSpec) -> float:
    """Round a size down to a whole number of lots.

    Always rounds down: rounding up could overshoot the strategy's target size
    or exceed the position being closed on a reduce-only order.
    """
    return _quantize(abs(size), spec.lot_size, ROUND_DOWN)


def chase_price(
    *,
    best_bid: float | None,
    best_ask: float | None,
    mark_price: float | None,
    is_buy: bool,
    offset_bps: float,
    spec: MarketSpec,
    improve_ticks: int = 0,
) -> float | None:
    """Target price for a resting order, `offset_bps` inside the touch.

    A buy rests below the best bid and a sell above the best ask, so the order
    stays passive and earns the maker side. Falls back to the mark price when
    the book is unavailable (e.g. right after a reconnect). Returns None when
    there is no usable reference price at all.

    `improve_ticks` steps that many ticks PAST the touch, into the spread, once
    the offset has been given up. Joining the touch shares a price with everyone
    already queued there and fills last among them; one tick better is alone at
    the front of the book and fills first. It is still passive -- the order is
    posted inside the spread, not across it -- so the fill stays on the maker
    side. It is ignored while an offset is in force, because stepping forward
    from a price deliberately set back from the market is contradictory.
    """
    reference = best_bid if is_buy else best_ask
    if reference is None or reference <= 0:
        reference = mark_price
    if reference is None or reference <= 0:
        return None

    dec_ref = Decimal(str(reference))
    adjustment = dec_ref * Decimal(str(offset_bps)) / BPS
    target = dec_ref - adjustment if is_buy else dec_ref + adjustment
    price = round_price(float(target), spec, is_buy)

    if improve_ticks > 0 and offset_bps <= 0:
        price = _improved(price, best_bid, best_ask, is_buy, improve_ticks, spec)

    return price if price > 0 else None


def _improved(
    price: float,
    best_bid: float | None,
    best_ask: float | None,
    is_buy: bool,
    ticks: int,
    spec: MarketSpec,
) -> float:
    """Step `ticks` into the spread without ever reaching the other side.

    Needs both sides of the book: without an ask there is no way to know how
    much room there is, and guessing is how a "passive" order crosses. A spread
    of one tick has no room at all, and the price is left on the touch.
    """
    if spec.tick_size <= 0 or not best_bid or not best_ask or best_ask <= best_bid:
        return price

    tick = Decimal(str(spec.tick_size))
    step = tick * ticks
    if is_buy:
        # One tick short of the ask is the best a buy can do and stay passive.
        ceiling = Decimal(str(best_ask)) - tick
        improved = min(Decimal(str(price)) + step, ceiling)
        return float(max(improved, Decimal(str(price))))

    floor_price = Decimal(str(best_bid)) + tick
    improved = max(Decimal(str(price)) - step, floor_price)
    return float(min(improved, Decimal(str(price))))


def distance_bps(price_a: float, price_b: float) -> float:
    """Absolute distance between two prices, in basis points of `price_b`."""
    if price_b == 0:
        return float("inf")
    diff = Decimal(str(price_a)) - Decimal(str(price_b))
    return float(abs(diff) / Decimal(str(abs(price_b))) * BPS)


def round_notional(value: float) -> float:
    """Two-decimal USD rounding, for logs and risk comparisons."""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

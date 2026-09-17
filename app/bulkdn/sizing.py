"""Turning configured sizes into sizes the accounts can actually carry.

Two steps, in order.

**Dollars into coins.** A leg written as `notional_usd: 100` has no size until
there is a price, so `resolve_notionals` converts it at startup and writes the
result into `leg.size`. Everything downstream reads `size` and never learns
which way the leg was written.

This is why dollars are the better unit. A size in the base coin is a trap the
moment a symbol changes: swapping `SOL-USD` for `ETH-USD` and leaving
`size: 2.0` asks for 2 ETH, and at $2,600 that is a $5,200 position where the
old one was $200. The exchange answers with a rejection that says nothing about
the cause. `notional_usd: 200` means the same thing in either market.

**Coins into what fits.** The configured size is then treated as a ceiling
rather than an instruction. When
both accounts can carry it, it is used exactly as written. When they cannot,
every leg is scaled by the same factor until the whole cycle fits inside
`max_margin_fraction` of the smaller account's available margin.

**Both accounts are checked, and the smaller one decides.** Each holds one side
of both legs -- the master is long the first and short the second, the sub the
reverse -- so the same total margin is needed on each, and a cycle sized for the
richer account would strand the poorer one mid-entry with a position it cannot
hedge.

**Scaling is proportional.** Halving one leg and leaving the other would change
the balance between them, which is a decision the operator made in the config,
not one to be made here by arithmetic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .marketdata import MarketSpec, round_size

log = logging.getLogger(__name__)


class SizingError(Exception):
    """The cycle cannot be sized as configured."""


class InsufficientMargin(SizingError):
    """Even the scaled-down size cannot be traded."""


class Unpriceable(SizingError):
    """A dollar size cannot be turned into a quantity without a price."""


@dataclass(frozen=True)
class LegSizing:
    symbol: str
    configured: float
    actual: float
    notional_usd: float
    margin_usd: float

    @property
    def scaled(self) -> bool:
        return self.actual < self.configured


@dataclass(frozen=True)
class SizingPlan:
    legs: list[LegSizing]
    budget_usd: float
    required_usd: float
    scale: float

    @property
    def scaled(self) -> bool:
        return self.scale < 1.0

    def describe(self) -> str:
        parts = [
            f"{leg.symbol} {leg.actual:g}"
            + (f" (from {leg.configured:g})" if leg.scaled else "")
            for leg in self.legs
        ]
        return (
            f"{', '.join(parts)} -- margin ${self.required_usd:,.2f} "
            f"of ${self.budget_usd:,.2f} available to this cycle"
        )


# Below this the legs count as comparable, and blaming their ratio for a
# failure would send the operator to fix something that is not wrong.
_LOPSIDED_RATIO = 3.0


def _why_it_did_not_fit(
    leg, legs, prices: dict[str, float], budget: float, spec, scale: float
) -> str:
    """Explain the actual cause, which is one of three quite different things."""
    def notional(other) -> float:
        return (prices.get(other.symbol) or 0.0) * other.size

    biggest = max(legs, key=notional)
    mine = notional(leg)

    if scale >= 1.0:
        # Nothing was scaled: the accounts could afford exactly what was asked
        # for, and what was asked for is below the market's own floor. Money is
        # not the problem, and saying it is sends the operator to deposit funds
        # that will change nothing.
        return (
            f" Nothing was scaled down -- the accounts can afford this cycle. "
            f"The configured size is simply under the market's floor: raise this "
            f"leg to at least ${spec.min_notional:g}, or trade a market with a "
            f"lower minimum."
        )

    if mine > 0 and notional(biggest) / mine >= _LOPSIDED_RATIO:
        # Scaling is proportional, so a lopsided pair starves the smaller leg
        # first. Evening the sizes up fixes it; adding funds mostly does not.
        return (
            f" The legs are {notional(biggest) / mine:.0f}x apart in dollar terms -- "
            f"{biggest.symbol} is ${notional(biggest):,.0f} against ${mine:,.0f} -- "
            f"and scaling keeps that ratio, so the smaller leg hits its floor first. "
            f"Even the two sizes up."
        )

    # Comparable legs: the budget itself is simply too small to clear this
    # market's minimum, whatever the split.
    return (
        f" The legs are already comparable, so this is the budget: ${budget:,.2f} "
        f"split across {len(legs)} legs cannot clear {leg.symbol}'s "
        f"${spec.min_notional:g} minimum. Fund the accounts, raise leverage, or "
        f"trade a market with a lower minimum."
    )


def resolve_notionals(
    *,
    legs: list,
    specs: dict[str, MarketSpec],
    prices: dict[str, float],
) -> None:
    """Convert every dollar-denominated leg into a base-coin size, in place.

    Legs already written in the base coin are left untouched, so a config
    mixing the two units works and an old config behaves exactly as before.

    In place, because everything downstream -- the chaser's targets, the hedge
    ceilings, the exit sizes -- reads `leg.size` from the config, and a second
    source of truth for size is how the two end up disagreeing.
    """
    for leg in legs:
        if not leg.priced_in_usd:
            continue

        price = prices.get(leg.symbol) or 0.0
        if price <= 0:
            raise Unpriceable(
                f"{leg.symbol}: no price available, so a dollar size cannot be "
                f"converted into a quantity. Check the market is listed and "
                f"the symbol is spelled as the exchange spells it."
            )

        spec = specs[leg.symbol]

        if leg.notional_usd > 0:
            leg.size = round_size(leg.notional_usd / price, spec)
            if leg.size < spec.lot_size:
                raise InsufficientMargin(
                    f"{leg.symbol}: ${leg.notional_usd:,.2f} is under one lot "
                    f"({spec.lot_size:g} = ${spec.lot_size * price:,.2f} at "
                    f"${price:,.2f}). Raise notional_usd for this leg."
                )
            log.info(
                "%s: $%s -> %g at $%s",
                leg.symbol, f"{leg.notional_usd:,.2f}", leg.size, f"{price:,.2f}",
            )

        if leg.max_order_notional_usd > 0:
            # A cap rounded down to nothing would stop every order, so it
            # floors at one lot -- the smallest order the market accepts.
            leg.max_order_size = max(
                round_size(leg.max_order_notional_usd / price, spec), spec.lot_size
            )

    # Outside the loop above, because a leg written in the base coin skips it
    # entirely and still needs a cap.
    for leg in legs:
        if leg.max_order_size <= 0:
            # No cap configured means the whole leg goes in one order, which is
            # what `config.LegConfig.validate` documents. It was not what
            # happened: the chaser takes `min(remaining, max_order_size)`, so a
            # zero cap sized every order at zero and the leg never placed
            # anything -- silently, because nothing about it is an error.
            leg.max_order_size = leg.size


def plan_sizes(
    *,
    legs: list,
    specs: dict[str, MarketSpec],
    prices: dict[str, float],
    available_margin: dict[str, float],
    max_margin_fraction: float = 0.25,
) -> SizingPlan:
    """Decide what each leg may actually open.

    `available_margin` is keyed by account; the smallest value binds. Returns
    the configured sizes unchanged when they already fit.

    Raises `InsufficientMargin` when a scaled leg would fall below the
    exchange's minimum order size, since trading it is not possible and a
    rejection at the first order is a worse way to find out.
    """
    if not available_margin:
        raise InsufficientMargin("no account margin could be read")

    smallest = min(available_margin.values())
    if smallest <= 0:
        raise InsufficientMargin(
            "no margin available -- deposit, or move some to the sub-account "
            "from Accounts Management -> Balance Subaccounts"
        )

    def margin_for(leg, size: float) -> float:
        price = prices.get(leg.symbol) or 0.0
        leverage = leg.leverage or 1.0
        return size * price / leverage

    required = sum(margin_for(leg, leg.size) for leg in legs)
    if required <= 0:
        raise InsufficientMargin("could not price the legs -- no market data")

    # The configured size is used as written whenever both accounts can carry
    # it. The fraction is a fallback, not a permanent cap: an operator who
    # sized a cycle deliberately should get that cycle.
    if required <= smallest:
        scale = 1.0
        budget = smallest
    else:
        budget = smallest * max_margin_fraction
        scale = budget / required
        log.warning(
            "configured sizes need $%.2f of margin but only $%.2f is available "
            "-- scaling every leg to %.1f%% so the cycle fits inside $%.2f",
            required, smallest, scale * 100, budget,
        )

    planned: list[LegSizing] = []
    for leg in legs:
        spec = specs[leg.symbol]
        price = prices.get(leg.symbol) or 0.0
        size = round_size(leg.size * scale, spec)

        if size < spec.lot_size:
            raise InsufficientMargin(
                f"{leg.symbol}: ${budget:,.2f} of margin cannot open even one "
                f"lot ({spec.lot_size:g}). Reduce the other leg, raise leverage, "
                f"or fund the accounts."
            )
        notional = size * price
        if notional < spec.min_notional:
            raise InsufficientMargin(
                f"{leg.symbol}: the affordable size {size:g} is ${notional:,.2f}, "
                f"under the ${spec.min_notional:g} minimum the market accepts."
                + _why_it_did_not_fit(leg, legs, prices, budget, spec, scale)
            )
        planned.append(
            LegSizing(
                symbol=leg.symbol,
                configured=leg.size,
                actual=size,
                notional_usd=notional,
                margin_usd=margin_for(leg, size),
            )
        )

    return SizingPlan(
        legs=planned,
        budget_usd=budget,
        required_usd=sum(leg.margin_usd for leg in planned),
        scale=scale,
    )

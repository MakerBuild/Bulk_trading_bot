"""Who trades against whom, drawn fresh for every cycle.

One pair of accounts repeating the same trade is the clearest signal a series
of trades can give about itself: the same two pubkeys, the same size, the same
market, over and over. A pool of accounts removes that if -- and only if -- the
pairing keeps moving.

So a group is drawn per cycle rather than configured: two or more accounts from
the pool, one opening and the rest hedging, disbanded when the cycle ends and
re-drawn from scratch for the next.

Three rules hold the whole thing together:

**An account is in at most one group at a time.** The hedge is derived from
positions -- `net = position[maker] + position[taker]` -- so an account in two
groups at once has one position serving two sums, and both of them are wrong.
This is the rule that makes the arithmetic safe, not a tidiness preference.

**A group never contains an account twice.** Pairing an account with itself
nets to nothing, trades nothing, and still pays two taker fees.

**With more than one master, a group spans two of them.** The maker comes
from one master's tree and every taker from another's. A group's accounts are
not meant to trade with each other -- the hedge goes to the book, and our own
orders are pulled out of its path first -- but when one does anyway, between
two masters it is a trade like any other, and inside one tree it is a
self-trade the fee tier does not count.

**Concurrency is capped.** A hundred accounts allow fifty groups, which is
fifty resting orders being chased and fifty hedges chasing them, against an
exchange that answered 429 to two accounts polling every five seconds. The cap
is a setting rather than a discovery.

Nothing here trades or knows how. It decides who, and is a pure function of the
pool, the cap and the random seed -- which is what makes it testable without an
exchange.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .accounts import short_pubkey


@dataclass(frozen=True)
class Group:
    """One cycle's worth of accounts: who opens, and who covers it.

    `takers` is a list because a hedge may be split across several accounts.
    Splitting is what stops the pairing from being readable even when the
    accounts move: a maker of $4,000 covered by one account of $4,000 is a
    line anyone can draw, and the same $4,000 covered by $1,800, $1,400 and
    $800 is not.

    Shares are fractions of the maker's size and sum to 1. They are decided
    when the group is drawn so that the split is fixed for the cycle: a hedge
    that re-apportioned itself on every fill would chase its own tail.
    """

    symbol: str
    maker: str
    takers: tuple[str, ...]
    shares: tuple[float, ...]
    # Which way the maker opens. Drawn with the group, because a fixed side is
    # the one shape a rotating pool does not remove: every cycle resting on the
    # bid, every hedge selling, for as long as the run lasts. Two groups drawn
    # at once sat at the same price on the same side of the book.
    #
    # Held on the group rather than decided per phase, so the exit is the
    # mirror of whatever this cycle opened. It is persisted with the group for
    # the same reason the accounts are: a restart that guessed the side afresh
    # would "close" a short by selling more of it.
    maker_is_buy: bool = True

    @property
    def accounts(self) -> tuple[str, ...]:
        return (self.maker, *self.takers)

    def share_for(self, taker: str) -> float:
        """This taker's fraction of the maker's size."""
        for pubkey, share in zip(self.takers, self.shares, strict=True):
            if pubkey == taker:
                return share
        return 0.0

    def __str__(self) -> str:
        # `short_pubkey`, not a prefix. Truncating to a few leading characters
        # renders m9s1 and m9s10 identically, and a group line that shows the
        # maker as one of its own takers sends whoever reads it looking for a
        # bug that is not there. Keys sharing a prefix is the normal case for
        # accounts derived from one master.
        side = "BUY" if self.maker_is_buy else "SELL"
        return f"{self.symbol} {short_pubkey(self.maker)} {side} -> " + ", ".join(
            f"{short_pubkey(t)}@{s:.0%}"
            for t, s in zip(self.takers, self.shares, strict=True)
        )


def split_shares(count: int, rng: random.Random, minimum: float = 0.15) -> tuple[float, ...]:
    """`count` fractions summing to 1, none of them negligible.

    The floor matters more than the shape: a hedge share small enough to fall
    under the market's minimum order cannot be sent at all, and the group would
    sit with exactly that much unhedged delta and no way to clear it. BTC-USD
    admits $1 and ETH-USD $50, so a share worth less than the floor is not a
    smaller hedge, it is a missing one.
    """
    if count <= 1:
        return (1.0,)
    if minimum * count >= 1.0:
        return tuple([1.0 / count] * count)
    # Draw the surplus above the floor, then hand every share its floor back.
    cuts = sorted(rng.random() for _ in range(count - 1))
    spare = 1.0 - minimum * count
    previous = 0.0
    shares = []
    for cut in (*cuts, 1.0):
        shares.append(minimum + (cut - previous) * spare)
        previous = cut
    return tuple(shares)


@dataclass
class Pairing:
    """Draws groups from a pool, and remembers which accounts are busy."""

    pool: list[str]
    max_groups: int = 5
    max_takers: int = 1
    # Which master each account belongs to, keyed by pubkey. Empty, or one
    # master for the whole pool, draws from anyone: there is no other tree to
    # cover a maker from, and refusing would mean no groups at all.
    owner: dict[str, object] = field(default_factory=dict)
    rng: random.Random = field(default_factory=random.Random)
    busy: set[str] = field(default_factory=set)
    active: dict[int, Group] = field(default_factory=dict)
    _next_id: int = 0

    @property
    def free(self) -> list[str]:
        """Accounts not currently in a group, in pool order."""
        return [account for account in self.pool if account not in self.busy]

    @property
    def _split(self) -> bool:
        """Whether the pool spans more than one master."""
        return len({self.owner.get(account) for account in self.pool}) > 1

    def _partners(self, maker: str) -> list[str]:
        """Free accounts that may cover this maker: another master's."""
        mine = self.owner.get(maker)
        return [
            account for account in self.free
            if account != maker and self.owner.get(account) != mine
        ]

    def can_draw(self, takers: int = 1) -> bool:
        if len(self.active) >= self.max_groups:
            return False
        if not self._split:
            return len(self.free) >= takers + 1
        return any(len(self._partners(maker)) >= takers for maker in self.free)

    def draw(
        self,
        symbol: str,
        takers: int | None = None,
        maker_is_buy: bool | None = None,
    ) -> tuple[int, Group] | None:
        """One group, or None when the pool or the cap will not allow it.

        Returning None rather than raising: a full book of groups is the normal
        state of a busy run, not a fault, and the caller's answer to it is to
        try again when one finishes.
        """
        wanted = self.max_takers if takers is None else takers
        wanted = max(1, min(wanted, self.max_takers))
        # A group needs a maker and its takers, and the pool may be thin.
        while wanted >= 1 and not self.can_draw(wanted):
            wanted -= 1
        if wanted < 1:
            return None

        if self._split:
            makers = [m for m in self.free if len(self._partners(m)) >= wanted]
            maker = self.rng.choice(makers)
            takers_drawn = tuple(self.rng.sample(self._partners(maker), wanted))
        else:
            chosen = self.rng.sample(self.free, wanted + 1)
            maker, takers_drawn = chosen[0], tuple(chosen[1:])
        group = Group(
            symbol=symbol,
            maker=maker,
            takers=takers_drawn,
            shares=split_shares(len(takers_drawn), self.rng),
            # The caller picks the side when it has a reason to -- it
            # knows which side the other live groups are resting on and
            # this does not. A coin flip is the answer when it does not
            # care, which is what keeps the side unreadable.
            maker_is_buy=(
                self.rng.random() < 0.5 if maker_is_buy is None
                else maker_is_buy
            ),
        )
        self._next_id += 1
        self.active[self._next_id] = group
        self.busy.update(group.accounts)
        return self._next_id, group

    def reserve(self, group_id: int, group: Group) -> None:
        """Put a group back as it was, without drawing it.

        Used on restart: the positions already exist, so the accounts are
        already busy whether or not this process knows it. Re-drawing them
        instead would let a second group form on accounts that are mid-cycle,
        and the two would compute their hedges from the same positions.
        """
        self.active[group_id] = group
        self.busy.update(group.accounts)
        self._next_id = max(self._next_id, group_id)

    def skip_ids_through(self, group_id: int) -> None:
        """Never hand out `group_id` or anything below it.

        Ids started at 1 in every process, while the state file keeps each
        finished group's leg under its id. A new `g1` then inherited the old
        `g1`'s leg: its offset, its order cap and its cycle count -- so a
        changed cap in the settings was ignored for exactly those groups, and
        a leg that had already done its cycles did nothing at all.
        """
        self._next_id = max(self._next_id, group_id)

    def release(self, group_id: int) -> Group | None:
        """Hand a finished group's accounts back to the pool."""
        group = self.active.pop(group_id, None)
        if group is None:
            return None
        self.busy.difference_update(group.accounts)
        return group


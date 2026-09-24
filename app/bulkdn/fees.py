"""Fee schedule and realised spend.

Two sources, both unsigned:

* `GET /feeState` -- the live policy: tier thresholds and maker/taker rates for
  the global scope and each instrument.
* `POST /account {"type": "feeTier"}` -- what this account currently pays,
  including its rolling volume and tier index.

Realised spend is summed from the account's own fills rather than tracked
locally, so it cannot drift from what the exchange recorded.

**Self-trades are counted separately.** The fee documentation is explicit that
"Self-trades between accounts under the same main account do not create
qualifying volume", and this strategy hedges between a master and its own
sub-account, so some fills can cross between them. A fill whose maker and taker
are both inside the tree is real spend but does not move the account toward a
better tier, and a volume target that ignored the distinction would overstate
progress.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

import requests

from .marketdata import epoch_seconds

log = logging.getLogger(__name__)

# Feature tiers come back under differing key spellings across scopes; accept
# both rather than guessing which one a given deployment uses.
_THRESHOLD_KEYS = ("threshold_volume", "thresholdVolume")
_MAKER_KEYS = ("maker_bps", "makerBps")
_TAKER_KEYS = ("taker_bps", "takerBps")


def _first(row: dict, keys: tuple[str, ...], default: float = 0.0) -> float:
    for key in keys:
        if key in row and row[key] is not None:
            return float(row[key])
    return default


@dataclass
class Tier:
    threshold_volume: float
    maker_bps: float
    taker_bps: float


@dataclass
class FeeSchedule:
    """The active policy for one scope (`global` or a market symbol)."""

    scope: str
    window_days: int
    tiers: list[Tier] = field(default_factory=list)

    def tier_for(self, rolling_volume: float) -> Tier | None:
        """Highest tier whose threshold the volume has reached."""
        reached = [t for t in self.tiers if rolling_volume >= t.threshold_volume]
        return max(reached, key=lambda t: t.threshold_volume) if reached else None


def fee_state(http_url: str, timeout: int = 20) -> dict[str, FeeSchedule]:
    """Active fee policy per scope, keyed by `global` or symbol."""
    response = requests.get(f"{http_url}/feeState", timeout=timeout)
    response.raise_for_status()
    body = response.json()

    out: dict[str, FeeSchedule] = {}
    for scope in body.get("scopes") or []:
        policy = scope.get("active_policy") or {}
        out[scope.get("instrument", "global")] = FeeSchedule(
            scope=scope.get("instrument", "global"),
            window_days=int(policy.get("window_days") or 0),
            tiers=[
                Tier(
                    threshold_volume=_first(row, _THRESHOLD_KEYS),
                    maker_bps=_first(row, _MAKER_KEYS),
                    taker_bps=_first(row, _TAKER_KEYS),
                )
                for row in policy.get("tiers") or []
            ],
        )
    return out


@dataclass
class AccountFeeTier:
    scope_instrument: str
    rolling_volume: float
    tier_index: int
    tier_threshold: float
    maker_bps: float
    taker_bps: float
    window_days: int


def account_fee_tier(
    http_url: str, user: str, symbol: str | None = None, timeout: int = 20
) -> AccountFeeTier | None:
    """This account's fee quote, or None when the endpoint has nothing for it.

    Returns None on 404, which is what an account with no settled volume gets.
    """
    body: dict = {"type": "feeTier", "user": user}
    if symbol:
        body["symbol"] = symbol
    response = requests.post(f"{http_url}/account", json=body, timeout=timeout)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict) or not data:
        return None
    # The quote arrives wrapped: `[{"feeTier": {...}}]`. Reading the outer
    # object found none of the fields and every `or 0.0` filled in a zero, so
    # the menu printed "taker 0.0 bps" over a real 3.5 -- a fee schedule that
    # does not exist, stated as fact.
    data = data.get("feeTier") if isinstance(data.get("feeTier"), dict) else data

    # No rate in the payload means no quote, not a free account. A zero here is
    # the difference between "trading costs nothing" and "we could not tell",
    # and only one of those is safe to show someone sizing a burn target.
    if "takerBps" not in data and "makerBps" not in data:
        return None

    return AccountFeeTier(
        # Null is what the global scope looks like on the wire.
        scope_instrument=data.get("scopeInstrument") or "global",
        rolling_volume=float(data.get("rollingVolume") or 0.0),
        tier_index=int(data.get("tierIndex") or 0),
        tier_threshold=float(data.get("tierThreshold") or 0.0),
        maker_bps=float(data.get("makerBps") or 0.0),
        taker_bps=float(data.get("takerBps") or 0.0),
        window_days=int(data.get("windowDays") or 0),
    )


def burned_usd(fees_usd: float) -> float:
    """A signed fee total, as the positive amount it cost.

    BULK reports a charge as a NEGATIVE number on the fill -- a taker fill comes
    back as `"takerFee": -0.035017` -- so a total of -3.0795 means $3.0795 was
    paid. Printing the raw figure put a minus in front of every spend line and
    read as though the account had earned it.

    Maker fills come back as `"makerFee": 0.0`: passive execution is free here,
    not rebated. So a total can reach zero but has no way to go above it, and a
    positive figure would be a shape change rather than a windfall. It is
    floored at zero anyway, because "burned -$0.40" is not something an operator
    can act on.
    """
    return max(0.0, -fees_usd)


@dataclass
class Realised:
    """Totals summed from the exchange's own fill records."""

    fills: int = 0
    fees_usd: float = 0.0
    volume_usd: float = 0.0
    self_trade_volume_usd: float = 0.0
    # True when some account's history was longer than the walk would read,
    # so these totals are a lower bound. See `realised_for_account`.
    truncated: bool = False
    # Money in minus money out on the trades themselves, fees apart: a sell
    # adds its notional, a buy subtracts it. Taken from every row, like fees,
    # because each row is one of OUR accounts' own side of a trade -- a trade
    # between two of ours is in both histories and nets to nothing, which is
    # exactly what it cost.
    cash_usd: float = 0.0
    # How far the rows moved each market's position, summed over accounts.
    # With `cash_usd` and a price this gives the trading result of the walk;
    # see `price_result_usd`.
    base_by_symbol: dict[str, float] = field(default_factory=dict)

    def price_result_usd(self, prices: dict[str, float]) -> float:
        """What the trades made or lost on price alone, fees excluded.

        Cash from the trades, plus whatever position they left valued at
        `prices`. The pool is hedged, so that position is dust and the price
        barely matters -- which is what makes the figure trustworthy mid-run:
        it is the spread and slippage paid, not a directional bet. Negative
        is a cost.
        """
        return self.cash_usd + sum(
            base * prices.get(symbol, 0.0) for symbol, base in self.base_by_symbol.items()
        )

    @property
    def qualifying_volume_usd(self) -> float:
        """Every trade, counted once. What the referral window shows.

        Verified: this returned $958,778.70 against $958.8K on the referral
        screen for a live account, matching to the rounding.

        It used to subtract `self_trade_volume_usd` as well, and that was wrong
        for this figure by exactly the self-trade total. Two separate errors
        were tangled together and only one of them was real arithmetic:

        * A trade between two accounts of the same tree appears in BOTH their
          histories. Summing the two views counted it twice. That is a plain
          bug against any definition, and it is fixed in `from_fills`.
        * Subtracting it on top, so it scored zero. Whether that is right
          depends on which volume is being measured -- see `tier_volume_usd`.
        """
        return self.volume_usd

    @property
    def tier_volume_usd(self) -> float:
        """The same, minus trades between our own accounts.

        The fee documentation says "Self-trades between accounts under the same
        main account do not create qualifying volume", so this is the figure
        the FEE TIER should be read against.

        It is kept separate rather than reconciled because the two cannot both
        be checked yet: the referral window plainly counts such a trade, and
        the tier's own `rollingVolume` reads 0 until the exchange reassesses.
        Reporting one number for both would be picking a winner without
        evidence, and an operator sizing a goal deserves to see which is which.
        """
        return self.volume_usd - self.self_trade_volume_usd

    @classmethod
    def from_fills(
        cls, rows, tree: set[str], seen: set | None = None, user: str | None = None
    ) -> Realised:
        """Total one page of fills.

        `seen` carries trade ids across the whole walk so each trade is counted
        once. A trade between two accounts of the same tree appears in BOTH
        their histories, and summing the two views inflated the total by its
        notional -- 365 of 1465 trades in a live run, 5.7% of the figure the
        goal was measured against.

        Fees are taken from every row regardless, because both sides of such a
        trade are charged and both are ours to pay.

        `tree` is every pubkey under the same master, which is what makes a
        fill identifiable as being between two of them.

        Rows are plain dicts straight from the API. See `fills_page` for why
        they are not the SDK's parsed model.

        `user` is whose history the rows came from. It decides which side's
        fee is theirs when a row carries `makerFee`/`takerFee` rather than a
        single `fee` -- see `_fee_of`.
        """
        total = cls()
        for fill in rows:
            amount = float(fill.get("amount") or 0.0)
            notional = amount * float(fill.get("price") or 0.0)
            total.fills += 1
            total.fees_usd += _fee_of(fill, user)
            bought = _is_buy(fill)
            if bought is not None:
                total.cash_usd += -notional if bought else notional
                symbol = fill.get("symbol") or fill.get("sym") or ""
                total.base_by_symbol[symbol] = (
                    total.base_by_symbol.get(symbol, 0.0) + (amount if bought else -amount)
                )

            trade = _trade_key(fill)
            if seen is not None:
                if trade in seen:
                    continue
                seen.add(trade)

            total.volume_usd += notional
            if fill.get("maker") in tree and fill.get("taker") in tree:
                total.self_trade_volume_usd += notional
        return total

    def __add__(self, other: Realised) -> Realised:
        return Realised(
            fills=self.fills + other.fills,
            fees_usd=self.fees_usd + other.fees_usd,
            volume_usd=self.volume_usd + other.volume_usd,
            self_trade_volume_usd=self.self_trade_volume_usd + other.self_trade_volume_usd,
            truncated=self.truncated or other.truncated,
            cash_usd=self.cash_usd + other.cash_usd,
            base_by_symbol={
                symbol: self.base_by_symbol.get(symbol, 0.0) + other.base_by_symbol.get(symbol, 0.0)
                for symbol in {*self.base_by_symbol, *other.base_by_symbol}
            },
        )


def _is_buy(fill: dict) -> bool | None:
    """Which side THIS account was on, or None when the row does not say.

    `isBuy` is the side of the account whose history it is, which is what a
    cash flow needs. A row without it is left out of the price result rather
    than guessed.
    """
    value = fill.get("isBuy", fill.get("b"))
    if isinstance(value, bool):
        return value
    side = str(fill.get("side") or "").lower()
    if side in ("buy", "b", "bid"):
        return True
    if side in ("sell", "s", "ask"):
        return False
    return None


# Fields that describe one account's VIEW of a trade rather than the trade:
# which side it was on and what it was charged. Two histories showing the same
# trade differ in these, so they are left out of the fallback identity below.
_VIEW_FIELDS = frozenset({"fee", "makerFee", "takerFee", "isBuy", "side", "user", "account"})


def _trade_key(fill: dict):
    """What identifies the trade a row belongs to, for counting it once.

    `slot` and `sequence` together, when both are there -- verified unique
    across a live history: 1830 rows, 1465 ids, no collisions. That is the
    only shape seen so far.

    It used to be those two and nothing else, so a row missing either one got
    the key `(None, None)` -- and so did every other such row. The first was
    counted and every later one was taken for a repeat of it: a history in
    another shape would have totalled one trade's volume, and a volume target
    would never have been reached. A `tradeId` is used instead where there is
    one; failing that, the row's own contents minus the per-account fields, so
    that two rows are only ever merged when they genuinely say the same thing.
    """
    slot, sequence = fill.get("slot"), fill.get("sequence")
    if slot is not None and sequence is not None:
        return ("slot", slot, sequence)
    trade_id = fill.get("tradeId", fill.get("tid"))
    if trade_id is not None:
        return ("id", str(trade_id))
    return ("row",) + tuple(
        sorted((k, repr(v)) for k, v in fill.items() if k not in _VIEW_FIELDS)
    )


def _fee_of(fill: dict, user: str | None) -> float:
    """The fee THIS account paid on a fill row, signed as the API signs it.

    The history rows seen so far carry a single `fee`. The fill stream, and
    `burned_usd`'s own docstring, show the other spelling: `takerFee` and
    `makerFee`. A row in that shape used to read as free -- `fee` absent, so
    zero -- and a burn target measured on it would never have been reached.

    With both side fields present, the one for the side `user` was on is
    theirs; both, if they were somehow on both. Without a `user` to go by, or
    with a row naming neither side as them, whatever side fields are present
    are summed -- which is exact for a row that carries only its own side.
    """
    if fill.get("fee") is not None:
        return float(fill.get("fee") or 0.0)
    maker_fee = fill.get("makerFee")
    taker_fee = fill.get("takerFee")
    if maker_fee is None and taker_fee is None:
        return 0.0
    if user is not None and (fill.get("maker") == user or fill.get("taker") == user):
        total = 0.0
        if fill.get("maker") == user:
            total += float(maker_fee or 0.0)
        if fill.get("taker") == user:
            total += float(taker_fee or 0.0)
        return total
    return float(maker_fee or 0.0) + float(taker_fee or 0.0)


def fills_page(http, user: str, limit: int, cursor: str | None) -> tuple[list[dict], str | None]:
    """Fetch one page of fills as raw dicts.

    Deliberately not `http.get_fills_page`. The SDK parses every row into
    `HistoryFill`, whose `TradeId.from_api` raises

        ValueError: tradeId must be <slot>:<sequence>

    when the field is absent -- and mainnet does not send it. The API returns
    `slot` and `sequence` as separate integers instead, so the SDK's strict
    parser rejects a perfectly good response and takes the whole history with
    it. Nothing here needs a trade id; only amount, price, fee and the two
    sides are read.
    """
    payload: dict = {"type": "fills", "user": user, "limit": limit}
    if cursor:
        payload["cursor"] = cursor

    response = requests.post(f"{http.base_url}/account", json=payload, timeout=30)
    response.raise_for_status()
    body = response.json()

    # A history query answers with {data, page}; a current-state query answers
    # with a bare list. Both are accepted so a shape change degrades to "no
    # rows" rather than an exception.
    if isinstance(body, list):
        return [row for row in body if isinstance(row, dict)], None

    rows = [row for row in (body.get("data") or []) if isinstance(row, dict)]
    page = body.get("page") or {}
    next_cursor = page.get("nextCursor") or page.get("next_cursor")
    return rows, next_cursor


# Accounts already warned about in this process. The walk runs every thirty
# seconds while a target is set; once per account is enough to be seen.
_TRUNCATION_WARNED: set[str] = set()

# Where a history row may carry its time, in milliseconds.
_TIME_KEYS = ("timestamp", "time", "ts")


def _row_time_ms(row: dict) -> float | None:
    """The row's time in epoch milliseconds, whatever unit it was stamped in.

    Fills are stamped in nanoseconds. Read as-is and compared with a cutoff in
    milliseconds, every row was "newer" than any run's start, the walk never
    stopped early, and the target counted the account's whole history.
    """
    for key in _TIME_KEYS:
        value = row.get(key)
        if value is not None:
            try:
                return epoch_seconds(float(value)) * 1000.0
            except (TypeError, ValueError):
                return None
    return None


def realised_for_account(
    http, user: str, tree: set[str], limit: int = 1000, max_pages: int = 20,
    seen: set | None = None, since_ms: float | None = None,
) -> Realised:
    """Walk an account's fill history and total spend and volume.

    `tree` is every pubkey belonging to the same master, which is what makes a
    fill identifiable as a self-trade.

    `max_pages` bounds the walk. History is paginated by cursor, and an account
    with a long life would otherwise make this unbounded.

    **Hitting that bound makes the totals wrong, not merely incomplete.** The
    burn and volume targets are measured as (total now - total at the run's
    start), and both totals come from this walk. Past `max_pages * limit`
    fills -- twenty thousand by default, a day's work for a busy pool -- each
    read sees only the newest twenty thousand, so the difference is the new
    fills minus the old ones that dropped off the far end: roughly zero, and
    the target is never reached. It used to stop there in silence. It now
    warns, once per account per process, and marks the result `truncated`.

    `since_ms` is the fix for that, for a caller that measures from a point in
    time rather than by difference: rows older than it are not counted, and
    when the pages come newest-first the walk stops at the first page that
    reaches back past it, so its length follows the run and not the account's
    lifetime. Pages that come oldest-first cannot be cut short that way and
    are read to the end, still skipping the old rows. A row without a time is
    counted -- there is no way to place it. The execution target passes the
    run's start here (`Strategy._read_totals`).
    """
    total = Realised()
    cursor = None
    for _ in range(max_pages):
        rows, cursor = fills_page(http, user, limit, cursor)
        counted = rows
        reached_back = False
        if since_ms is not None:
            times = [_row_time_ms(row) for row in rows]
            known = [t for t in times if t is not None]
            newest_first = len(known) >= 2 and known[0] >= known[-1]
            reached_back = newest_first and known[-1] < since_ms
            counted = [row for row, t in zip(rows, times, strict=True) if t is None or t >= since_ms]
        total = total + Realised.from_fills(counted, tree, seen, user=user)
        # On the page as served, not as filtered: an oldest-first history has
        # whole pages from before `since_ms`, and there is more after them.
        if not cursor or not rows or reached_back:
            break
    else:
        # Ran out of pages with a cursor still in hand: there was more.
        if cursor:
            total.truncated = True
            if user not in _TRUNCATION_WARNED:
                _TRUNCATION_WARNED.add(user)
                log.warning(
                    "fill history for %s is longer than the %d pages x %d read "
                    "here -- spend and volume totals are a lower bound, and an "
                    "execution target measured from them may never read as "
                    "reached",
                    user, max_pages, limit,
                )
    return total


def realised_for_tree(http, accounts: list[str], **kwargs) -> Realised:
    """Totals across every account in one master tree.

    Each account is walked separately, and one `seen` set spans the walk: a
    trade between two of them appears in both views, and counting it twice is
    what made the goal read 5.7% short of the exchange's own figure. Its fees
    still come from both views, because both sides were charged.
    """
    return realised_for_trees(http, [accounts], **kwargs)


def realised_for_trees(http, trees: Sequence[Sequence[str]], **kwargs) -> Realised:
    """Totals across several master trees, deduplicated as a single walk.

    Self-trades are judged inside each tree rather than across the lot. The
    exchange links a sub-account to the master that created it and nothing
    links two masters to each other, so a trade between accounts under
    different keys is, as far as the fee tier can tell, a trade with a
    stranger -- which is the entire reason for running more than one key.
    Scoring those as self-trades would under-report qualifying volume by
    however much of the trading the pool did with itself.

    `seen` still spans every tree. Deduplication is about a trade appearing
    twice because we can see both sides of it, and we can see both sides of a
    cross-key trade just as plainly as of a cross-account one.
    """
    seen: set = set()
    total = Realised()
    for accounts in trees:
        kin = set(accounts)
        for user in accounts:
            total = total + realised_for_account(http, user, kin, seen=seen, **kwargs)
    return total

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

from dataclasses import dataclass, field

import requests

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

    @property
    def qualifying_volume_usd(self) -> float:
        """Volume that counts toward the fee tier."""
        return self.volume_usd - self.self_trade_volume_usd

    @classmethod
    def from_fills(cls, rows, tree: set[str]) -> Realised:
        """Total one page of fills, splitting out self-trades.

        `tree` is every pubkey under the same master. A fill with both sides
        inside it is real spend that earns no tier credit, which is why the two
        are counted separately rather than netted.

        Rows are plain dicts straight from the API. See `fills_page` for why
        they are not the SDK's parsed model.
        """
        total = cls()
        for fill in rows:
            notional = float(fill.get("amount") or 0.0) * float(fill.get("price") or 0.0)
            total.fills += 1
            total.fees_usd += float(fill.get("fee") or 0.0)
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
        )


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


def realised_for_account(
    http, user: str, tree: set[str], limit: int = 1000, max_pages: int = 20
) -> Realised:
    """Walk an account's fill history and total spend and volume.

    `tree` is every pubkey belonging to the same master, which is what makes a
    fill identifiable as a self-trade.

    `max_pages` bounds the walk. History is paginated by cursor, and an account
    with a long life would otherwise make this unbounded.
    """
    total = Realised()
    cursor = None
    for _ in range(max_pages):
        rows, cursor = fills_page(http, user, limit, cursor)
        total = total + Realised.from_fills(rows, tree)
        if not cursor or not rows:
            break
    return total


def realised_for_tree(http, accounts: list[str], **kwargs) -> Realised:
    """Totals across every account in one master tree.

    Each account is walked separately because a fill that crossed between two
    of them appears once in each view, and both views are real spend.
    """
    tree = set(accounts)
    total = Realised()
    for user in accounts:
        total = total + realised_for_account(http, user, tree, **kwargs)
    return total

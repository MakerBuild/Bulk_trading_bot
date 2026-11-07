"""The weekly report: what one AURA week cost, and the AURA it earned.

An AURA week runs Saturday 13:00 UTC to Saturday 13:00 UTC, and its points
are paid at the end of it. The report takes every account under every key,
reconstructs the total balance at both ends of the week, and says:

    spent = balance at the start + money that came in from outside
            - balance at the end

The exchange keeps no balance history, so both balances are worked back from
today's: everything that moved a balance after a moment -- fees, the cash of
each trade, funding, transfers -- is taken back off. A position open at a
boundary is valued at today's price; the pool is hedged, so across all the
accounts that position is dust and the price barely matters. Transfers
between the operator's own accounts cancel out in the total; only money from
or to a wallet outside the set counts as coming in or going out.

The same week's points are read from the AURA indexer per master: the
`mainnet_week<N>_*` categories, which is how the indexer files a week's
payout. A sub-account's trading is credited to its master.

Both are read from public, unsigned endpoints. Nothing here signs or trades.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import requests

from . import wire
from .fees import Realised
from .marketdata import epoch_seconds
from .referral import INDEXER_URL
from .retry import describe

log = logging.getLogger(__name__)

UTC = dt.timezone.utc
# Mainnet AURA week 1 began here. Counted from the indexer's own week labels:
# week 9 is the week paid on Saturday 2026-11-07, and every earlier label falls
# on the week its trading happened in.
WEEK_ONE_STARTS = dt.datetime(2026, 9, 5, 13, 0, tzinfo=UTC)
WEEK = dt.timedelta(days=7)
# Pages of one history read before it is called incomplete.
MAX_PAGES = 40
PAGE_ROWS = 1000

Post = Callable[[dict], Any]
GetJson = Callable[[str], Any]


def week_window(week: int) -> tuple[dt.datetime, dt.datetime]:
    """Start and end of AURA week `week`, in UTC."""
    start = WEEK_ONE_STARTS + WEEK * (week - 1)
    return start, start + WEEK


def last_finished_week(now: dt.datetime) -> int:
    """The newest week whose payout time has passed."""
    return max(1, (now - WEEK_ONE_STARTS) // WEEK)


@dataclass
class WeekReport:
    week: int
    start: dt.datetime
    end: dt.datetime
    accounts: int = 0
    balance_start: float = 0.0
    balance_end: float = 0.0
    # Money from wallets outside the set, minus money sent to them.
    external_net: float = 0.0
    # Inside the week; each a cost as a positive number.
    fees: float = 0.0
    price: float = 0.0
    funding: float = 0.0
    volume: float = 0.0
    # master pubkey -> {category: points} for this week.
    points: dict[str, dict[str, float]] = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)

    @property
    def spent(self) -> float:
        return self.balance_start + self.external_net - self.balance_end

    @property
    def total_points(self) -> float:
        return sum(sum(cats.values()) for cats in self.points.values())


def _default_post(base_url: str) -> Post:
    def post(body: dict) -> Any:
        response = requests.post(f"{base_url}/account", json=body, timeout=30)
        response.raise_for_status()
        return response.json()

    return post


def _default_get(url: str) -> Any:
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


def _when(row: dict) -> float | None:
    value = row.get("timestamp")
    if value is None:
        return None
    try:
        return epoch_seconds(float(value))
    except (TypeError, ValueError):
        return None


def rows_since(post: Post, kind: str, user: str, since: float) -> tuple[list[dict], bool]:
    """Every `kind` history row of `user` from `since` on. (rows, complete)."""
    rows: list[dict] = []
    cursor = None
    for _ in range(MAX_PAGES):
        body: dict = {"type": kind, "user": user, "limit": PAGE_ROWS}
        if cursor:
            body["cursor"] = cursor
        page, cursor = wire.page(post(body))
        rows += page
        times = [t for t in (_when(row) for row in page) if t is not None]
        if not cursor or not page or (times and min(times) < since):
            break
    else:
        return _since(rows, since), False
    return _since(rows, since), True


def _since(rows: list[dict], since: float) -> list[dict]:
    return [row for row in rows if (_when(row) or 0.0) >= since]


def _between(rows: list[dict], start: float, end: float) -> list[dict]:
    return [row for row in rows if start <= (_when(row) or 0.0) <= end]


def _after(rows: list[dict], moment: float) -> list[dict]:
    return [row for row in rows if (_when(row) or 0.0) > moment]


def _transfers(rows: list[dict], user: str) -> float:
    """What these activity rows added to `user`'s balance."""
    total = 0.0
    for row in rows:
        if str(row.get("status", "completed")).lower() not in ("completed", "complete", "success"):
            continue
        amount = float(row.get("amount") or 0.0)
        if row.get("to") == user:
            total += amount
        if row.get("from") == user:
            total -= amount
    return total


def _equity(account: dict) -> float:
    margin = account.get("margin") or {}
    return float(margin.get("totalMargin") or 0.0) + float(margin.get("unrealizedPnl") or 0.0)


def _price(get_json: GetJson, base_url: str, symbol: str) -> float:
    try:
        ticker = get_json(f"{base_url}/ticker/{symbol}")
        return float(ticker.get("markPrice") or ticker.get("lastPrice") or 0.0)
    except Exception as exc:  # noqa: BLE001 - one price is not worth the report
        log.warning("weekly report: no price for %s: %s", symbol, describe(exc))
        return 0.0


def build_report(
    base_url: str,
    trees: Sequence[Sequence[str]],
    week: int,
    *,
    post: Post | None = None,
    get_json: GetJson = _default_get,
    indexer_url: str = INDEXER_URL,
) -> WeekReport:
    """Read one week's spend and points. `trees`: each master's accounts, master first."""
    post = post or _default_post(base_url)
    start, end = week_window(week)
    t0, t1 = start.timestamp(), end.timestamp()
    report = WeekReport(week=week, start=start, end=end)
    everyone = {pubkey for tree in trees for pubkey in tree}
    report.accounts = len(everyone)

    per_account = []
    symbols: set[str] = set()
    seen: set = set()
    for tree in trees:
        kin = set(tree)
        for user in tree:
            account = wire.unwrap_full_account(post({"type": "fullAccount", "user": user}))
            fills, full_fills = rows_since(post, "fills", user, t0)
            funding, full_funding = rows_since(post, "fundingHistory", user, t0)
            activity, full_activity = rows_since(post, "activityHistory", user, t0)
            if not (full_fills and full_funding and full_activity):
                report.caveats.append(
                    f"{user[:6]}: history longer than this report reads -- figures are a lower bound"
                )
            for row in fills:
                symbol = row.get("symbol") or row.get("sym")
                if symbol:
                    symbols.add(symbol)
            per_account.append((user, kin, account, fills, funding, activity))

            inside = _between(fills, t0, t1)
            week_fills = Realised.from_fills(inside, kin, seen, user=user)
            report.fees += -week_fills.fees_usd
            report.volume += week_fills.volume_usd
            report.funding += -sum(float(r.get("payment") or 0.0) for r in _between(funding, t0, t1))
            for row in _between(activity, t0, t1):
                other = row.get("from") if row.get("to") == user else row.get("to")
                if other not in everyone:
                    report.external_net += _transfers([row], user)

    prices = {symbol: _price(get_json, base_url, symbol) for symbol in sorted(symbols)}
    open_at: dict[str, float] = {}
    for user, kin, account, fills, funding, activity in per_account:
        now = _equity(account)
        for label, moment in (("start", t0), ("end", t1)):
            later = Realised.from_fills(_after(fills, moment), kin, user=user)
            moved = (
                later.fees_usd
                + later.price_result_usd(prices)
                + sum(float(r.get("payment") or 0.0) for r in _after(funding, moment))
                + _transfers(_after(activity, moment), user)
            )
            if label == "start":
                report.balance_start += now - moved
            else:
                report.balance_end += now - moved
            for symbol, base in later.base_by_symbol.items():
                held = _held(account, symbol) - base
                if abs(held) * prices.get(symbol, 0.0) >= 1.0:
                    open_at[label] = open_at.get(label, 0.0) + held * prices.get(symbol, 0.0)

    # Inside the week, the price result alone: cash in minus cash out on the
    # trades, plus what they left open, valued now. A cost is positive here.
    week_price = Realised()
    for user, kin, _account, fills, _funding, _activity in per_account:
        week_price = week_price + Realised.from_fills(_between(fills, t0, t1), kin, user=user)
    report.price = -week_price.price_result_usd(prices)

    for label, net in open_at.items():
        moment = "start" if label == "start" else "end"
        report.caveats.append(
            f"positions were open at the week's {moment} (net ${net:,.2f}); valued at today's price"
        )

    for tree in trees:
        master = tree[0]
        try:
            body = get_json(f"{indexer_url}/{master}")
        except Exception as exc:  # noqa: BLE001 - spend is still worth reporting
            report.caveats.append(f"{master[:6]}: AURA not read ({describe(exc)})")
            continue
        prefix = f"mainnet_week{week}_"
        report.points[master] = {
            name[len(prefix):]: float(value)
            for name, value in (body.get("categories") or {}).items()
            if name.startswith(prefix)
        }
    return report


def _held(account: dict, symbol: str) -> float:
    """This account's position in `symbol` now, signed."""
    for position in account.get("positions") or []:
        if (position.get("symbol") or position.get("sym")) == symbol:
            try:
                return float(position.get("size") or position.get("sz") or 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _money(value: float) -> str:
    """Two decimals, without the "-0.00" a rounding of a tiny negative gives."""
    return f"{round(value, 2) + 0.0:,.2f}"


def render(report: WeekReport, names: dict[str, str] | None = None) -> list[str]:
    """The report as lines of plain text."""
    names = names or {}
    lines = [
        f"Week {report.week}: {report.start:%b %d %H:%M} -> {report.end:%b %d %H:%M} UTC",
        f"Balance   ${report.balance_start:,.2f} -> ${report.balance_end:,.2f}  ({report.accounts} accounts)",
    ]
    if abs(report.external_net) >= 0.01:
        lines.append(f"In/out    {'+' if report.external_net >= 0 else '-'}${abs(report.external_net):,.2f}"
                     "  (from/to wallets outside these accounts)")
    lines.append(f"Spent     ${report.spent:,.2f}")
    lines.append(f"  fees ${_money(report.fees)} + spread/slippage ${_money(report.price)}"
                 f" + funding ${_money(report.funding)}")
    if report.volume:
        bps = report.spent / report.volume * 1e4
        lines.append(f"Volume    ${report.volume:,.0f}  ({bps:.2f} bps, {bps / 100:.4f}%)")
    else:
        lines.append("Volume    $0")
    if report.points:
        each = ", ".join(
            f"{names.get(master, master[:6])} {sum(cats.values()):,.0f}"
            for master, cats in report.points.items()
        )
        lines.append(f"AURA      {report.total_points:,.0f}  ({each})")
        kinds: dict[str, float] = {}
        for cats in report.points.values():
            for name, value in cats.items():
                kinds[name] = kinds.get(name, 0.0) + value
        if kinds:
            lines.append("  " + ", ".join(f"{name} {value:,.0f}" for name, value in sorted(kinds.items())))
        if report.total_points > 0 and report.spent > 0:
            lines.append(f"Cost      ${report.spent / report.total_points:,.4f} per AURA,"
                         f" {report.total_points / report.spent:,.1f} AURA per $1")
    lines += [f"! {caveat}" for caveat in report.caveats]
    return lines

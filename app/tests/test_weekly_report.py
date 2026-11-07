"""📅 The weekly report: an AURA week's spend, from balances worked back from today.

The exchange keeps no balance history, so the balance at each end of the week
is today's minus everything that moved it since. These tests build a small
history by hand and check that the arithmetic closes: what the balances say
was spent is what the fees, the trades and the funding cost.
"""

import asyncio
import datetime as dt
import json
import types

import pytest

from bulkdn import telegram_control as tc
from bulkdn import weekly

from test_telegram_control import Harness, button

UTC = dt.timezone.utc
START, END = weekly.week_window(9)

# Made-up pubkeys. One master with one sub, and a second master alone.
M1, S1, M2 = "Mmmm1111", "Ssss1111", "Mmmm2222"
OUTSIDE = "Xxxx9999"


def ns(moment: dt.datetime) -> int:
    return int(moment.timestamp() * 1e9)


def fill(user, buy, amount, price, fee, when, seq):
    return {"symbol": "NVDA-USD", "amount": amount, "price": price, "isBuy": buy, "fee": fee,
            "maker": user, "taker": OUTSIDE, "slot": 1, "sequence": seq, "timestamp": ns(when)}


DAY = dt.timedelta(days=1)
FILLS = {
    # Inside the week: a hedged pair, opened and closed, each leg $1000.
    M1: [
        fill(M1, False, 5.0, 200.0, -0.15, START + 2 * DAY, 4),   # close: sell
        fill(M1, True, 5.0, 200.1, -0.15, START + DAY, 2),        # open: buy
    ],
    S1: [
        fill(S1, True, 5.0, 200.05, -0.15, START + 2 * DAY, 3),   # close: buy back
        fill(S1, False, 5.0, 199.95, -0.15, START + DAY, 1),      # open: sell
    ],
    # After the week ended: must move the end balance, not the week's figures.
    M2: [fill(M2, True, 1.0, 100.0, -0.02, END + DAY, 9), fill(M2, False, 1.0, 100.0, -0.02, END + DAY, 10)],
}
FUNDING = {M1: [{"payment": -0.05, "timestamp": ns(START + DAY)}], S1: [], M2: []}
ACTIVITY = {
    M1: [
        # Own money moving between own accounts: cancels out.
        {"from": M1, "to": S1, "amount": 50.0, "status": "completed", "timestamp": ns(START + 3 * DAY)},
        # From outside: money in, not earnings.
        {"from": OUTSIDE, "to": M1, "amount": 7.25, "status": "completed", "timestamp": ns(START + 4 * DAY)},
    ],
    S1: [{"from": M1, "to": S1, "amount": 50.0, "status": "completed", "timestamp": ns(START + 3 * DAY)}],
    M2: [],
}
EQUITY = {M1: 300.0, S1: 350.0, M2: 99.96}


def post(body):
    user, kind = body["user"], body["type"]
    if kind == "fullAccount":
        return {"fullAccount": {"margin": {"totalMargin": EQUITY[user], "unrealizedPnl": 0.0},
                                "positions": []}}
    rows = {"fills": FILLS, "fundingHistory": FUNDING, "activityHistory": ACTIVITY}[kind][user]
    return {"data": rows, "page": {"nextCursor": None}}


def get_json(url):
    if "/ticker/" in url:
        return {"markPrice": 200.0}
    wallet = url.rsplit("/", 1)[-1]
    return {"categories": {
        "mainnet_week9_trading": 1500 if wallet == M1 else 0,
        "mainnet_week9_maker": 20 if wallet == M1 else 0,
        "mainnet_week8_trading": 999,
    }}


def report():
    return weekly.build_report("https://api", [[M1, S1], [M2]], 9, post=post, get_json=get_json)


# -- the week ------------------------------------------------------------------


def test_week_nine_is_the_week_paid_on_november_7():
    assert weekly.week_window(9) == (
        dt.datetime(2026, 10, 31, 13, 0, tzinfo=UTC), dt.datetime(2026, 11, 7, 13, 0, tzinfo=UTC),
    )


@pytest.mark.parametrize("now, week", [
    (dt.datetime(2026, 11, 7, 12, 59, tzinfo=UTC), 8),
    (dt.datetime(2026, 11, 7, 13, 0, tzinfo=UTC), 9),
    (dt.datetime(2026, 11, 13, 9, 0, tzinfo=UTC), 9),
])
def test_the_last_finished_week_turns_over_at_saturday_13_utc(now, week):
    assert weekly.last_finished_week(now) == week


# -- the money -------------------------------------------------------------------


def test_the_balances_and_the_trades_agree_on_what_was_spent():
    r = report()
    fees = 0.6
    price = (200.1 - 200.0) * 5 + (200.05 - 199.95) * 5   # 1.0
    funding = 0.05
    assert r.fees == pytest.approx(fees)
    assert r.price == pytest.approx(price)
    assert r.funding == pytest.approx(funding)
    assert r.spent == pytest.approx(fees + price + funding)


def test_money_from_outside_is_not_counted_as_earned():
    r = report()
    assert r.external_net == pytest.approx(7.25)
    # The end balance includes it; the spend does not.
    assert r.balance_end - r.balance_start == pytest.approx(7.25 - r.spent)


def test_trading_after_the_week_moves_only_the_end_balance():
    r = report()
    # M2 traded after the week: 0.04 of fees between the week's end and now.
    assert r.balance_end == pytest.approx(sum(EQUITY.values()) + 0.04)
    assert r.volume == pytest.approx(4 * 1000.0, rel=1e-3)


def test_this_weeks_aura_only():
    r = report()
    assert r.points == {M1: {"trading": 1500.0, "maker": 20.0}, M2: {"trading": 0.0, "maker": 0.0}}
    assert r.total_points == 1520


def test_the_rendered_report():
    lines = "\n".join(weekly.render(report(), {M1: "M1", M2: "M2"}))
    assert "Week 9: Oct 31 13:00 -> Nov 07 13:00 UTC" in lines
    assert "Spent     $1.65" in lines
    assert "AURA      1,520  (M1 1,520, M2 0)" in lines
    assert "per AURA" in lines and "-0.00" not in lines


def test_history_reading_stops_at_the_week_start():
    calls = []
    old = {"timestamp": ns(START - DAY), "amount": 1}
    new = {"timestamp": ns(START + DAY), "amount": 1}

    def paged(body):
        calls.append(body.get("cursor"))
        if body.get("cursor") is None:
            return {"data": [new], "page": {"nextCursor": "c2"}}
        if body["cursor"] == "c2":
            return {"data": [new, old], "page": {"nextCursor": "c3"}}
        raise AssertionError("read past the week start")

    rows, complete = weekly.rows_since(paged, "fills", M1, START.timestamp())
    assert complete and len(rows) == 2 and calls == [None, "c2"]


# -- the Telegram button ------------------------------------------------------------


class Weekly(Harness):
    def __init__(self, tmp_path, now=dt.datetime(2026, 11, 7, 13, 40, tzinfo=UTC), points=1500):
        super().__init__()
        self.now_utc = now
        self.built = []

        def build(config, trees, week):
            self.built.append((trees, week))
            r = report()
            r.points = {M1: {"trading": float(points)}}
            return r

        self.c.trees_fn = lambda config: [
            types.SimpleNamespace(index=1, accounts=(M1, S1)),
            types.SimpleNamespace(index=2, accounts=(M2,)),
        ]
        self.c.report_fn = build
        self.c.weekly_path = str(tmp_path / "weekly_report.json")
        self.c.weekly = self.c._load_weekly()
        self.c.utcnow = lambda: self.now_utc

    async def finished(self):
        while self.c._report is not None and not self.c._report.done():
            await asyncio.sleep(0)


def test_the_keyboard_has_the_weekly_report():
    assert tc.KEYBOARD["📅 Weekly report"] == "/week"
    assert ["📅 Weekly report"] in tc._KEYBOARD_ROWS


async def test_the_button_sends_last_weeks_report(tmp_path):
    h = Weekly(tmp_path)
    reply = await h.say("📅 Weekly report")
    assert "Week 9" in reply.text
    await h.finished()
    assert h.built[0] == ([[M1, S1], [M2]], 9)
    sent = h.sent[-1]
    assert sent.startswith("📅 <b>Week 9</b>") and "Spent" in sent and "M1 1,500" in sent


async def test_an_earlier_week_and_one_not_over_yet(tmp_path):
    h = Weekly(tmp_path)
    reply = await h.say("📅 Weekly report")
    await h.finished()
    await h.c.press(button(reply, "◀️ Week 8"))
    await h.finished()
    assert h.built[-1][1] == 8
    assert "not ended yet" in (await h.say("/week 10")).text


async def test_saturday_reports_are_sent_once_each(tmp_path):
    h = Weekly(tmp_path)
    reply = await h.c.press("wkauto:on")
    assert "every Saturday" in reply.text
    assert json.loads((tmp_path / "weekly_report.json").read_text())["enabled"] is True

    await h.c._weekly_tick()
    assert len(h.built) == 1 and "Week 9" in h.sent[-1]
    await h.c._weekly_tick()
    assert len(h.built) == 1, "the same week was sent twice"

    # A restart keeps the switch and what was sent.
    again = Weekly(tmp_path)
    assert again.c.weekly == {"enabled": True, "last_sent": 9}


async def test_it_waits_half_an_hour_after_the_payout(tmp_path):
    h = Weekly(tmp_path, now=dt.datetime(2026, 11, 7, 13, 10, tzinfo=UTC))
    await h.c.press("wkauto:on")
    await h.c._weekly_tick()
    assert h.built == []


async def test_it_waits_for_the_indexer_then_sends_without_it(tmp_path):
    h = Weekly(tmp_path, points=0)
    await h.c.press("wkauto:on")
    await h.c._weekly_tick()
    assert h.built and len(h.sent) == 0, "sent before the week's AURA was filed"
    h.now_utc = dt.datetime(2026, 11, 7, 19, 30, tzinfo=UTC)
    await h.c._weekly_tick()
    assert "Week 9" in h.sent[-1]


async def test_switched_off_nothing_is_sent(tmp_path):
    h = Weekly(tmp_path)
    await h.c.press("wkauto:on")
    assert "No more" in (await h.c.press("wkauto:off")).text
    await h.c._weekly_tick()
    assert h.built == []

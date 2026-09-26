"""Telling our own unacknowledged fill apart from someone else's close.

A live run on 2026-09-17 halted with "LIQUIDATION: master ETH-USD position
reduced externally: -0.511 -> +0.1022". The exchange had no record of one:
`riskEvents` was empty on both accounts and every position closed with
`closeReason: normal`. What actually happened was an outage -- three hedge
submissions timed out, executed anyway, and the position they moved looked
exactly like one the exchange had force-closed.

Both directions of that mistake are expensive, so both are tested here: calling
a real liquidation our own doing keeps a broken pair trading, and calling our
own fill a liquidation ends a healthy run.
"""

import asyncio
import logging
import time

import pytest

from bulkdn.liquidation import Liquidation, recent_liquidations
from bulkdn.strategy import DOUBT_WINDOW_S, MAX_DOUBT_DEFERRALS

ETH = "ETH-USD"
BTC = "BTC-USD"


# -- the exchange's own answer ----------------------------------------------


class FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def event(ns_ago=0.0, symbol=ETH, kind="liquidation"):
    return {
        "symbol": symbol,
        "eventType": kind,
        "reason": "liquidation due to equity: 20.054 < maintenance margin: 22.389",
        "timestamp": (time.time() - ns_ago) * 1e9,
    }


def answer(monkeypatch, body=None, exc=None):
    def post(*a, **k):
        if exc:
            raise exc
        return FakeResponse(body)

    monkeypatch.setattr("bulkdn.liquidation.requests.post", post)


def test_a_recent_liquidation_is_reported(monkeypatch):
    answer(monkeypatch, {"data": [event(ns_ago=10)]})
    assert len(recent_liquidations("http://x", "ACC")) == 1


def test_an_old_liquidation_is_not_this_one(monkeypatch):
    """Every account that has ever been liquidated would otherwise be treated
    as liquidated forever."""
    answer(monkeypatch, {"data": [event(ns_ago=86_400)]})
    assert recent_liquidations("http://x", "ACC", within_s=300) == []


def test_an_empty_record_means_no_liquidation(monkeypatch):
    answer(monkeypatch, {"data": []})
    assert recent_liquidations("http://x", "ACC") == []


def test_an_unreachable_exchange_raises_rather_than_saying_no(monkeypatch):
    """"Nothing was liquidated" and "I could not ask" must not look the same.
    One of them lets the run continue."""
    answer(monkeypatch, exc=TimeoutError())
    with pytest.raises(TimeoutError):
        recent_liquidations("http://x", "ACC")


def test_a_broken_payload_raises_rather_than_saying_no(monkeypatch):
    answer(monkeypatch, {"error": "nope"})
    with pytest.raises(ValueError):
        recent_liquidations("http://x", "ACC")


# -- doubt tracking on the session ------------------------------------------


def session_like():
    from bulkdn.accounts import AccountSession

    return AccountSession(name="master", pubkey="ACC", client=None, http=None)


def test_a_symbol_is_only_in_doubt_while_the_window_lasts():
    s = session_like()
    s.unconfirmed[ETH] = time.monotonic()
    assert s.symbols_in_doubt(DOUBT_WINDOW_S) == {ETH}

    s.unconfirmed[ETH] = time.monotonic() - DOUBT_WINDOW_S - 1
    assert s.symbols_in_doubt(DOUBT_WINDOW_S) == set()


def test_a_settled_symbol_leaves_doubt():
    s = session_like()
    s.unconfirmed[ETH] = time.monotonic()
    s.settled(ETH)
    assert s.symbols_in_doubt(DOUBT_WINDOW_S) == set()


class FakeAction:
    def __init__(self, symbol):
        self.symbol = symbol


class FakeWsClient:
    def __init__(self, exc=None, responses=None):
        self.exc = exc
        self.responses = responses or []

    async def submit(self, actions, **kwargs):
        if self.exc:
            raise self.exc
        return self.responses


def test_an_unanswered_submission_puts_its_symbol_in_doubt():
    s = session_like()
    s.client = FakeWsClient(exc=TimeoutError())

    with pytest.raises(TimeoutError):
        asyncio.run(s.submit([FakeAction(ETH)]))

    assert s.symbols_in_doubt(DOUBT_WINDOW_S) == {ETH}


def test_an_answered_submission_clears_the_doubt():
    """A rejection is an answer. Only silence leaves the outcome unknown."""
    s = session_like()
    s.unconfirmed[ETH] = time.monotonic()
    s.client = FakeWsClient(responses=[])

    asyncio.run(s.submit([FakeAction(ETH)]))

    assert s.symbols_in_doubt(DOUBT_WINDOW_S) == set()


def test_doubt_is_per_symbol():
    """The other leg is still watched while this one is uncertain."""
    s = session_like()
    s.client = FakeWsClient(exc=TimeoutError())
    with pytest.raises(TimeoutError):
        asyncio.run(s.submit([FakeAction(ETH)]))
    assert BTC not in s.symbols_in_doubt(DOUBT_WINDOW_S)


# -- the decision the guard makes -------------------------------------------


class FakeSession:
    def __init__(self, name):
        self.name = name
        self.pubkey = f"{name}-KEY"
        self.unconfirmed: dict[str, float] = {}
        self.closed: list[tuple] = []

    def symbols_in_doubt(self, within_s):
        now = time.monotonic()
        return {s for s, at in self.unconfirmed.items() if now - at <= within_s}

    def settled(self, symbol):
        self.unconfirmed.pop(symbol, None)

    async def market(self, symbol, is_buy, size, reduce_only=False):
        self.closed.append((symbol, is_buy, size, reduce_only))
        return []

    async def close_market(self, symbol, is_buy, size):
        # Through `market`, which tests replace to watch or refuse the close.
        return await self.market(symbol, is_buy, size, reduce_only=True)


class Bot:
    """Just enough Strategy to exercise the guard's decision."""

    @property
    def all_sessions(self):
        """Stands in for Strategy's own: every account the run trades."""
        return [self.master, self.sub1]

    def __init__(self, *, in_doubt=False, sync_fails=False, liquidated=False,
                 confirm_fails=False):
        from bulkdn.strategy import Strategy

        self.master = FakeSession("master")
        self.sub1 = FakeSession("sub1")
        if in_doubt:
            self.master.unconfirmed[ETH] = time.monotonic()
        self._doubt_deferrals = {}
        self._sync_calls = 0
        self._sync_fails = sync_fails
        self._liquidated = liquidated
        self._confirm_fails = confirm_fails
        self.halted = None
        self.notifier = self
        self._guard_liquidation = Strategy._guard_liquidation.__get__(self)
        self._deferred_to_our_own_orders = (
            Strategy._deferred_to_our_own_orders.__get__(self)
        )
        self._liquidation_confirmed = Strategy._liquidation_confirmed.__get__(self)
        self._settled_by_a_fresh_read = (
            Strategy._settled_by_a_fresh_read.__get__(self)
        )
        self._settled_after_waiting = (
            Strategy._settled_after_waiting.__get__(self)
        )
        # The guard holds both sides as swept while its closes go out.
        self._sweep = Strategy._sweep.__get__(self)

    # the pieces the guard leans on
    async def _sync_positions(self, max_age_s=0.0):
        self._sync_calls += 1
        if self._sync_fails:
            raise TimeoutError()

    def send_soon(self, *a):
        pass

    def halted_message(self, reason):
        return reason

    def _trigger_halt(self, reason):
        self.halted = reason


def bot_with(events, **kw):
    """Wire a Bot whose guard reports `events` and holds one open position."""
    from bulkdn.marketdata import MarketSpec
    from bulkdn.positions import PositionBook

    bot = Bot(**kw)
    spec = MarketSpec(ETH, tick_size=0.01, lot_size=0.0001, min_notional=50.0)
    bot.feed = type("F", (), {"specs": {ETH: spec}})()
    bot.book = PositionBook()
    bot.book.set_authoritative(bot.master.pubkey, ETH, 0.3066)
    bot.guard = type("G", (), {
        "check": lambda self, *a: events,
        "reset_symbol": lambda self, symbol: None,
    })()
    bot.config = type("C", (), {"http_url": "http://x"})()
    return bot


def liquidation_event(symbol=ETH):
    return Liquidation(
        account="master-KEY", account_name="master", symbol=symbol,
        previous=-0.511, current=0.1022,
    )


def patch_confirm(monkeypatch, bot):
    def query(http_url, user, **kw):
        if bot._confirm_fails:
            raise TimeoutError()
        return [event()] if bot._liquidated else []

    monkeypatch.setattr("bulkdn.strategy.recent_liquidations", query)


def no_waiting(monkeypatch):
    monkeypatch.setattr("bulkdn.strategy.SHRINK_RECHECK_DELAYS_S", (0.0, 0.0))


def run_guard(monkeypatch, bot):
    patch_confirm(monkeypatch, bot)
    no_waiting(monkeypatch)
    bot.notifier = type("N", (), {"send_soon": lambda self, m: None,
                                  "halted": lambda self, r: r})()
    return asyncio.run(bot._guard_liquidation({ETH: None}))


def test_our_own_unanswered_order_does_not_end_the_run(monkeypatch):
    """The 2026-09-17 incident: an outage, not a liquidation."""
    bot = bot_with([liquidation_event()], in_doubt=True)
    acted = run_guard(monkeypatch, bot)

    assert acted is False, "a healthy pair was shut down"
    assert bot.halted is None
    assert bot.master.closed == [], "it closed a position it did not need to"
    assert bot._sync_calls == 1, "it carried on without re-reading positions"


def test_an_external_close_still_ends_the_run(monkeypatch):
    """No unanswered order, so this is someone else -- a manual close from the
    web UI reaches here too, and re-hedging it is just as wrong."""
    bot = bot_with([liquidation_event()], in_doubt=False)
    acted = run_guard(monkeypatch, bot)

    assert acted is True
    assert bot.halted is not None
    assert bot.master.closed, "the surviving leg was left open"


def test_a_confirmed_liquidation_says_so(monkeypatch):
    bot = bot_with([liquidation_event()], in_doubt=False, liquidated=True)
    run_guard(monkeypatch, bot)
    assert "liquidation" in bot.halted


def test_an_unconfirmed_close_is_not_called_a_liquidation(monkeypatch):
    """It is what the log will say, and it was wrong once already."""
    bot = bot_with([liquidation_event()], in_doubt=False, liquidated=False)
    run_guard(monkeypatch, bot)
    assert "closed externally" in bot.halted
    assert not bot.halted.startswith("liquidation")


def test_an_unreachable_exchange_is_treated_as_a_liquidation(monkeypatch):
    """Fails safe: closing a healthy pair costs a spread, trading on into a
    real liquidation has no bounded cost."""
    bot = bot_with([liquidation_event()], in_doubt=False, confirm_fails=True)
    run_guard(monkeypatch, bot)
    assert bot.halted is not None and "liquidation" in bot.halted


def test_doubt_does_not_excuse_it_when_positions_cannot_be_re_read(monkeypatch):
    """Deferring means replacing a guess with the exchange's answer. With no
    answer there is nothing to replace it with, and carrying on would be
    trading blind -- which is the thing the guard exists to prevent."""
    bot = bot_with([liquidation_event()], in_doubt=True, sync_fails=True)
    acted = run_guard(monkeypatch, bot)

    assert acted is True, "it carried on while knowing nothing"
    assert bot.halted is not None


def test_the_reprieve_runs_out(monkeypatch):
    """A fault that keeps looking like our own doing is still a fault."""
    bot = bot_with([liquidation_event()], in_doubt=True)
    for _ in range(MAX_DOUBT_DEFERRALS):
        bot.master.unconfirmed[ETH] = time.monotonic()
        assert run_guard(monkeypatch, bot) is False

    bot.master.unconfirmed[ETH] = time.monotonic()
    assert run_guard(monkeypatch, bot) is True, "it deferred forever"


def test_a_doubtful_symbol_does_not_cover_a_clean_one(monkeypatch):
    """One leg being uncertain must not buy the other leg an excuse."""
    bot = bot_with(
        [liquidation_event(ETH), liquidation_event(BTC)], in_doubt=True
    )
    from bulkdn.marketdata import MarketSpec

    bot.feed.specs[BTC] = MarketSpec(BTC, 0.01, 0.00001, 1.0)
    assert run_guard(monkeypatch, bot) is True
    assert bot.halted is not None


# -- and it says why --------------------------------------------------------


def test_a_failed_close_reports_the_reason(monkeypatch, caplog):
    """The operator is told to close by hand. A TimeoutError renders as an
    empty string, so this line printed nothing where the reason belongs."""
    bot = bot_with([liquidation_event()], in_doubt=False)

    async def refuse(*a, **k):
        raise TimeoutError()

    bot.master.market = refuse
    with caplog.at_level(logging.CRITICAL):
        run_guard(monkeypatch, bot)

    hand = [r.getMessage() for r in caplog.records if "close it by hand" in r.getMessage()]
    assert hand, "the operator was never told"
    assert "TimeoutError" in hand[0], f"no reason given: {hand[0]!r}"


def test_incidents_hours_apart_do_not_add_up(monkeypatch):
    """The bound is for a fault that keeps recurring. Counted for the life of
    the process instead, a run lasting hours collects unrelated single
    incidents -- each one correctly excused, each followed by a clean re-read
    and hours of healthy trading -- and the fourth ends the run."""
    from bulkdn.strategy import DEFERRAL_WINDOW_S

    bot = bot_with([liquidation_event()], in_doubt=True)
    for _ in range(MAX_DOUBT_DEFERRALS):
        bot.master.unconfirmed[ETH] = time.monotonic()
        assert run_guard(monkeypatch, bot) is False

    # Same again, a window later.
    aged = time.monotonic() - DEFERRAL_WINDOW_S - 1
    bot._doubt_deferrals[ETH] = [aged] * MAX_DOUBT_DEFERRALS
    bot.master.unconfirmed[ETH] = time.monotonic()
    assert run_guard(monkeypatch, bot) is False, "old incidents still counted"


def test_the_bound_still_holds_inside_the_window(monkeypatch):
    """Ageing them out must not remove the bound for a fault firing steadily."""
    bot = bot_with([liquidation_event()], in_doubt=True)
    for _ in range(MAX_DOUBT_DEFERRALS):
        bot.master.unconfirmed[ETH] = time.monotonic()
        assert run_guard(monkeypatch, bot) is False

    bot.master.unconfirmed[ETH] = time.monotonic()
    assert run_guard(monkeypatch, bot) is True, "it deferred past the bound"


# -- a stale read racing our own fill ---------------------------------------


async def test_a_shrink_that_a_fresh_read_undoes_is_not_a_close(monkeypatch):
    """The book has two sources that do not agree instantly. A periodic HTTP
    read landing seconds after a hedge writes the OLDER number over the newer
    one, and the position then looks smaller by exactly our own fill.

    Live, on a run it ended:

        hedge BTC-USD: net=-0.00230000 -> BUY 0.00230000 across
            m1s1 0.00079300, m1s4 0.00150700
        fill on m1s1: BUY 0.00079300 ... role=taker
        m1s1 positions: BTC-USD=+0.01571200
        POSITION CLOSED EXTERNALLY: m1s1 BTC-USD position reduced
            externally: +0.01650500 -> +0.01571200

    The difference is the hedge fill to the lot, the exchange had confirmed no
    liquidation, and five accounts were closed at market regardless.
    """
    event = Liquidation(
        account="master-KEY", account_name="master", symbol=ETH,
        previous=0.3066, current=0.2066,
    )
    bot = bot_with([event])
    patch_confirm(monkeypatch, bot)
    no_waiting(monkeypatch)
    # What a fresh read finds: the position never shrank.
    bot.book.set_authoritative("master-KEY", ETH, 0.3066)

    acted = await bot._guard_liquidation({ETH: None})

    assert acted is False, "a stale reading must not close five accounts"
    assert bot.halted is None
    assert bot._sync_calls >= 1, "it has to actually ask before deciding"


async def test_a_first_re_read_that_is_still_stale_is_not_the_last_word(monkeypatch):
    """Close to the exchange the immediate re-read can itself be served from
    before our fill and confirm the stale shrink. When the exchange denies a
    liquidation, a later read decides instead."""
    event = Liquidation(
        account="master-KEY", account_name="master", symbol=ETH,
        previous=0.3066, current=0.2066,
    )
    bot = bot_with([event])
    patch_confirm(monkeypatch, bot)
    no_waiting(monkeypatch)
    bot.book.set_authoritative("master-KEY", ETH, 0.2066)   # the stale reading

    async def reads(max_age_s=0.0):
        bot._sync_calls += 1
        if bot._sync_calls >= 2:                            # a later one has it
            bot.book.set_authoritative("master-KEY", ETH, 0.3066)

    bot._sync_positions = reads
    acted = await bot._guard_liquidation({ETH: None})

    assert acted is False, "one stale re-read closed every account"
    assert bot.halted is None
    assert bot._sync_calls == 2


async def test_a_confirmed_liquidation_is_not_kept_waiting(monkeypatch):
    event = Liquidation(
        account="master-KEY", account_name="master", symbol=ETH,
        previous=0.3066, current=0.2066,
    )
    bot = bot_with([event], liquidated=True)
    patch_confirm(monkeypatch, bot)
    monkeypatch.setattr("bulkdn.strategy.SHRINK_RECHECK_DELAYS_S", (60.0,))
    bot.book.set_authoritative("master-KEY", ETH, 0.2066)
    bot.notifier = type("N", (), {"send_soon": lambda self, m: None,
                                  "halted": lambda self, r: r})()

    acted = await asyncio.wait_for(bot._guard_liquidation({ETH: None}), 5)

    assert acted is True
    assert bot._sync_calls == 1, "it re-read a liquidation the exchange confirmed"


# -- nothing hedges while the guard closes out -------------------------------
#
# 2026-09-22 23:23: the guard closed the pair and the hedge worker, still
# running beside it, "hedged" the closes as they filled -- m1s1 and m1s4 ended
# holding fresh shorts they had never had.


def test_hedging_is_suspended_before_the_first_close(monkeypatch):
    bot = bot_with([liquidation_event()], in_doubt=False)
    order = []

    async def cancel_all(sessions, symbols):
        order.append(("cancel", tuple(symbols)))

    real_market = bot.master.market

    async def market(*a, **k):
        order.append(("close", bot._closing_out))
        return await real_market(*a, **k)

    bot.master.market = market
    monkeypatch.setattr("bulkdn.strategy.cancel_all_orders", cancel_all)

    run_guard(monkeypatch, bot)

    assert order[0] == ("cancel", (ETH,)), "resting orders were left to fill mid-close"
    assert ("close", True) in order, "a close went out with hedging still live"

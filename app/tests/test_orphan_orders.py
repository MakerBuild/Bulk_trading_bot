"""An order placed by a submission that was never answered.

A run ended on 2026-09-18 with

    HALT: hedge limit exceeded -- BTC-USD: required hedge 0.09597600
    exceeds ceiling 0.06398400 (maker=0.09597600, taker=0.00000000)

0.09597600 is exactly three times the leg's 0.03199200. Three chase steps had
timed out in the half minute before it; each had placed an order the bot never
learned the id of, all three rested, and all three filled. The leg opened at
three times its size with nothing hedging it.
"""

import asyncio

from bulkdn.strategy import DOUBT_WINDOW_S, Strategy

BTC = "BTC-USD"


class FakeSession:
    def __init__(self, name, in_doubt=(), fails=False):
        self.name = name
        self.pubkey = f"{name}-KEY"
        self._in_doubt = set(in_doubt)
        self.fails = fails
        self.cancelled_all = []

    def symbols_in_doubt(self, within_s):
        assert within_s == DOUBT_WINDOW_S
        return set(self._in_doubt)

    def settled(self, symbol):
        self._in_doubt.discard(symbol)

    async def cancel_all(self, symbols):
        if self.fails:
            raise TimeoutError()
        self.cancelled_all.append(list(symbols))
        return []


class Bot:
    """Only the parts `_clear_orphans` reaches for."""

    def __init__(self, session):
        self.session = session
        self.sessions = {session.pubkey: session}
        self._clear_orphans = Strategy._clear_orphans.__get__(self)

    def leg_roles(self, symbol):
        return type("R", (), {"maker": self.session.pubkey})()


def test_a_possibly_resting_order_is_cancelled():
    """The chaser places again on the next tick, so whatever the timed-out
    submission left behind has to go first."""
    session = FakeSession("master", in_doubt=[BTC])
    asyncio.run(Bot(session)._clear_orphans(BTC))
    assert session.cancelled_all == [[BTC]]


def test_an_answered_submission_cancels_nothing():
    """A rejection is an answer: nothing of ours can be resting unaccounted
    for, and cancelling would throw away queue position for no reason."""
    session = FakeSession("master", in_doubt=[])
    asyncio.run(Bot(session)._clear_orphans(BTC))
    assert session.cancelled_all == []


def test_the_doubt_is_cleared_once_the_book_is_swept():
    """Otherwise every later failure in that leg would cancel again."""
    session = FakeSession("master", in_doubt=[BTC])
    bot = Bot(session)
    asyncio.run(bot._clear_orphans(BTC))
    asyncio.run(bot._clear_orphans(BTC))
    assert session.cancelled_all == [[BTC]], "it swept twice"


def test_only_the_leg_that_failed_is_swept():
    """The other leg may have a perfectly good order resting."""
    session = FakeSession("master", in_doubt=[BTC])
    asyncio.run(Bot(session)._clear_orphans(BTC))
    assert session.cancelled_all == [[BTC]]
    assert "ETH-USD" not in session.cancelled_all[0]


def test_a_failed_sweep_leaves_the_doubt_standing(caplog):
    """The next tick must try again rather than assume it is clean."""
    import logging

    session = FakeSession("master", in_doubt=[BTC], fails=True)
    with caplog.at_level(logging.ERROR):
        asyncio.run(Bot(session)._clear_orphans(BTC))

    assert session.symbols_in_doubt(DOUBT_WINDOW_S) == {BTC}
    assert any("may open larger than its size" in r.getMessage() for r in caplog.records)


def test_the_sweep_does_not_raise_into_the_chase_loop():
    """It runs from an except branch. Raising there would take down the leg
    task over a cleanup that is already best-effort."""
    session = FakeSession("master", in_doubt=[BTC], fails=True)
    asyncio.run(Bot(session)._clear_orphans(BTC))   # must not raise


def test_the_chase_loop_actually_calls_the_sweep():
    """A source check, because reaching this branch for real needs a live leg
    task, two sessions and a feed. What it pins is the wiring: the sweep exists
    and is useless unless the failure path runs it, and that path is one line
    in a long method where it would be easy to lose.
    """
    import inspect

    source = inspect.getsource(Strategy._drive_leg)
    failure = source[source.index("chase step for %s failed"):]
    assert "_clear_orphans" in failure[:300], (
        "the chase-step failure path no longer clears possibly-resting orders"
    )

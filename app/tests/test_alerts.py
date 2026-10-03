"""The log lines that need a person reach Telegram, not only the log.

A crash, "COULD NOT CLOSE ... by hand" and "MANUAL ACTION REQUIRED" were
written to logs.txt alone, on a run meant to be left for hours.
"""

import asyncio
import logging

import pytest

from bulkdn import notify
from bulkdn.notify import AlertHandler, Notifier, TelegramConfig


@pytest.fixture
def sent(monkeypatch):
    monkeypatch.setattr(notify, "_ALERT_BATCH_S", 0.05)
    messages = []

    async def send(self, text="", *, prefix="", plain="", pre=False):
        messages.append(text + plain)

    monkeypatch.setattr(Notifier, "send", send)
    return messages


def wired(enabled=True):
    notifier = Notifier(TelegramConfig(bot_token="t" if enabled else "", user_ids=[1]))
    logger = logging.getLogger("bulkdn.test_alerts")
    handler = AlertHandler(notifier)
    handler.attach(logger)
    return notifier, logger, handler


async def settle(notifier):
    await asyncio.sleep(0.15)
    await notifier.drain()


ALERT = {"alert": True}


async def test_lines_that_need_a_person_arrive_as_one_message(sent):
    notifier, logger, handler = wired()
    try:
        logger.critical("COULD NOT CLOSE BTC-USD on m1s4 -- close it by hand now", extra=ALERT)
        logger.error(
            "flatten did not fully close -- MANUAL ACTION REQUIRED: m2 BTC-USD", extra=ALERT
        )
        await settle(notifier)
    finally:
        handler.detach(logger)

    assert len(sent) == 1, "an emergency arrived as a flood of messages"
    assert "COULD NOT CLOSE" in sent[0]
    assert "MANUAL ACTION REQUIRED" in sent[0]


async def test_a_crash_says_what_it_was(sent):
    notifier, logger, handler = wired()
    try:
        try:
            raise ConnectionResetError("socket gone")
        except ConnectionResetError:
            logger.exception("run failed", extra=ALERT)
        await settle(notifier)
    finally:
        handler.detach(logger)

    assert sent and "ConnectionResetError" in sent[0]


async def test_routine_lines_and_the_halt_are_not_forwarded(sent):
    """The halt already has its own message; forwarding it too sent it twice."""
    notifier, logger, handler = wired()
    try:
        logger.info("fill on m1: BUY")
        logger.warning("throttled by the exchange")
        logger.error("hedge for g1 failed: timeout")
        # Flagged, as strategy.py flags it -- and still not forwarded: the
        # halt sends `Notifier.halted` itself, and forwarding it sent it twice.
        logger.critical("HALT: hedge limit exceeded", extra=ALERT)
        logger.critical("halted: hedge limit exceeded", extra=ALERT)
        await settle(notifier)
    finally:
        handler.detach(logger)

    assert sent == []


async def test_what_is_forwarded_is_decided_by_the_flag_not_the_wording(sent):
    """It used to be read from the text: any CRITICAL line, anything saying
    MANUAL ACTION REQUIRED, a message of exactly "run failed". Rewording a
    line changed what reached the phone."""
    notifier, logger, handler = wired()
    try:
        logger.critical("a critical line nobody marked")
        logger.error("MANUAL ACTION REQUIRED, but not marked")
        logger.warning("a warning that needs a person", extra=ALERT)
        await settle(notifier)
    finally:
        handler.detach(logger)

    assert sent == ["a warning that needs a person"]


async def test_a_line_logged_from_a_worker_thread_arrives(sent):
    """Position reads run in threads and log from there."""
    notifier, logger, handler = wired()
    try:
        await asyncio.to_thread(
            lambda: logger.critical("COULD NOT CANCEL resting orders", extra=ALERT)
        )
        await settle(notifier)
    finally:
        handler.detach(logger)

    assert sent and "COULD NOT CANCEL" in sent[0]


async def test_an_alert_still_gathering_is_sent_at_shutdown(sent, monkeypatch):
    """The process exits right after the line that explains why."""
    monkeypatch.setattr(notify, "_ALERT_BATCH_S", 60.0)
    notifier, logger, handler = wired()
    try:
        logger.critical("COULD NOT CLOSE BTC-USD", extra=ALERT)
        await asyncio.sleep(0)          # handed to the loop
        await notifier.drain()
    finally:
        handler.detach(logger)

    assert sent and "COULD NOT CLOSE" in sent[0]


async def test_a_flood_is_capped(sent):
    notifier, logger, handler = wired()
    try:
        for i in range(40):
            logger.critical("COULD NOT CLOSE %d", i, extra=ALERT)
        await settle(notifier)
    finally:
        handler.detach(logger)

    assert len(sent) == 1
    assert sent[0].count("COULD NOT CLOSE") == notify._ALERT_MAX_LINES
    assert "25 more" in sent[0]


async def test_nothing_is_sent_without_telegram(sent):
    notifier, logger, handler = wired(enabled=False)
    try:
        logger.critical("COULD NOT CLOSE BTC-USD", extra=ALERT)
        await settle(notifier)
    finally:
        handler.detach(logger)

    assert sent == []


# -- the strategy's own lines that need a person carry the flag --------------


def _alerts(caplog):
    return [r.getMessage() for r in caplog.records if getattr(r, "alert", False)]


async def test_a_failure_of_the_liquidation_guard_is_an_alert(caplog):
    from strategy_double import bare_strategy

    obj = bare_strategy()

    async def boom(phases):
        raise RuntimeError("HTTP 504")

    obj._guard_liquidation = boom
    await obj._answer_liquidation()
    assert any("liquidation guard failed" in line for line in _alerts(caplog))


async def test_a_hedge_task_that_dies_is_an_alert(caplog):
    from strategy_double import bare_strategy

    obj = bare_strategy()

    async def boom(leg_key, again):
        raise KeyError("BTC-USD")

    obj._hedge_until_quiet = boom
    await obj._hedge_leg_until_quiet("g1:BTC-USD", set())
    assert any("hedge task for g1:BTC-USD died" in line for line in _alerts(caplog))


async def test_fills_the_stop_could_not_hedge_are_an_alert(caplog):
    from strategy_double import bare_strategy

    obj = bare_strategy()

    async def boom(max_age_s=0.0, sessions=None):
        raise RuntimeError("HTTP 429")

    obj._sync_positions = boom
    await obj._last_hedge_pass()
    assert any("could not hedge fills" in line for line in _alerts(caplog))


def test_a_group_whose_key_is_gone_is_an_alert(caplog):
    from strategy_double import bare_strategy

    from bulkdn.state import Phase

    obj = bare_strategy()
    leg = obj.state.leg("g7:BTC-USD", "BTC-USD")
    leg.group_id, leg.maker, leg.takers, leg.phase = 7, "GONE-PUB", ["ALSO-GONE"], Phase.HOLD
    obj.restore_groups()
    assert any("needs the key that opened it" in line for line in _alerts(caplog))

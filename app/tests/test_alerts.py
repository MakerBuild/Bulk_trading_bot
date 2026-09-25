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

    async def send(self, text, *, prefix=""):
        messages.append(text)

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


async def test_lines_that_need_a_person_arrive_as_one_message(sent):
    notifier, logger, handler = wired()
    try:
        logger.critical("COULD NOT CLOSE BTC-USD on m1s4 -- close it by hand now")
        logger.error("flatten did not fully close -- MANUAL ACTION REQUIRED: m2 BTC-USD")
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
            logger.exception("run failed")
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
        logger.critical("HALT: hedge limit exceeded")
        logger.critical("halted: hedge limit exceeded")
        await settle(notifier)
    finally:
        handler.detach(logger)

    assert sent == []


async def test_a_line_logged_from_a_worker_thread_arrives(sent):
    """Position reads run in threads and log from there."""
    notifier, logger, handler = wired()
    try:
        await asyncio.to_thread(logger.critical, "COULD NOT CANCEL resting orders")
        await settle(notifier)
    finally:
        handler.detach(logger)

    assert sent and "COULD NOT CANCEL" in sent[0]


async def test_an_alert_still_gathering_is_sent_at_shutdown(sent, monkeypatch):
    """The process exits right after the line that explains why."""
    monkeypatch.setattr(notify, "_ALERT_BATCH_S", 60.0)
    notifier, logger, handler = wired()
    try:
        logger.critical("COULD NOT CLOSE BTC-USD")
        await asyncio.sleep(0)          # handed to the loop
        await notifier.drain()
    finally:
        handler.detach(logger)

    assert sent and "COULD NOT CLOSE" in sent[0]


async def test_a_flood_is_capped(sent):
    notifier, logger, handler = wired()
    try:
        for i in range(40):
            logger.critical("COULD NOT CLOSE %d", i)
        await settle(notifier)
    finally:
        handler.detach(logger)

    assert len(sent) == 1
    assert sent[0].count("COULD NOT CLOSE") == notify._ALERT_MAX_LINES
    assert "25 more" in sent[0]


async def test_nothing_is_sent_without_telegram(sent):
    notifier, logger, handler = wired(enabled=False)
    try:
        logger.critical("COULD NOT CLOSE BTC-USD")
        await settle(notifier)
    finally:
        handler.detach(logger)

    assert sent == []

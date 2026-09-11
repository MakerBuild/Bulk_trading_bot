"""Telegram reporting.

The invariant under test throughout: notification is never allowed to affect
trading. A bad token, a dead network, or a malformed recipient must all end in
a log line, not an exception that unwinds into the strategy.
"""

import pytest

from bulkdn.config import ConfigError, _telegram_from_dict
from bulkdn.notify import Notifier, TelegramConfig


def test_disabled_without_a_token():
    assert not TelegramConfig(user_ids=[1]).enabled


def test_disabled_without_recipients():
    assert not TelegramConfig(bot_token="123:abc").enabled


def test_enabled_with_both():
    assert TelegramConfig(bot_token="123:abc", user_ids=[1]).enabled


async def test_send_is_a_noop_when_unconfigured():
    # No session is opened, so this cannot raise even with no network at all.
    await Notifier().send("anything")


async def test_send_never_raises_when_the_network_fails(monkeypatch):
    from bulkdn import notify

    class ExplodingSession:
        def __init__(self, *args, **kwargs):
            raise OSError("no network")

    monkeypatch.setattr(notify.aiohttp, "ClientSession", ExplodingSession)

    notifier = Notifier(TelegramConfig(bot_token="123:abc", user_ids=[1]))
    await notifier.send("hello")  # must not raise


async def test_event_helpers_do_not_raise_when_disabled():
    notifier = Notifier()
    await notifier.run_started(endpoint="http://x", master="M", sub1="S", dry_run=True)
    await notifier.cycle_complete(cycle=1, of=3, detail="burn $1")
    await notifier.halted("net exposure exceeded")
    await notifier.run_finished(cycles=2)
    await notifier.error("boom")


async def test_send_soon_closes_the_coroutine_when_disabled():
    """Must not leak an un-awaited coroutine warning on the disabled path."""
    notifier = Notifier()
    notifier.send_soon(notifier.send("ignored"))


def test_long_messages_are_split():
    from bulkdn.notify import _CHUNK, _split

    chunks = _split("x" * (_CHUNK * 2 + 10))
    assert len(chunks) == 3
    assert all(len(chunk) <= _CHUNK for chunk in chunks)
    assert "".join(chunks) == "x" * (_CHUNK * 2 + 10)


def test_short_message_is_one_chunk():
    from bulkdn.notify import _split

    assert _split("short") == ["short"]


# -- config parsing --------------------------------------------------------


def test_config_accepts_a_single_unwrapped_id():
    cfg = _telegram_from_dict({"bot_token": "123:abc", "user_id": 42})
    assert cfg.user_ids == [42]
    assert cfg.enabled


def test_config_accepts_string_ids():
    assert _telegram_from_dict({"bot_token": "t", "user_ids": ["1", "2"]}).user_ids == [1, 2]


def test_config_empty_is_disabled():
    assert not _telegram_from_dict({}).enabled


def test_token_without_recipients_is_an_error():
    """Silently sending nowhere is worse than refusing to start."""
    with pytest.raises(ConfigError, match="user_ids is empty"):
        _telegram_from_dict({"bot_token": "123:abc"})


def test_non_numeric_id_is_an_error():
    with pytest.raises(ConfigError, match="must be integers"):
        _telegram_from_dict({"bot_token": "t", "user_ids": ["not-a-number"]})


def test_non_mapping_is_an_error():
    with pytest.raises(ConfigError, match="must be a mapping"):
        _telegram_from_dict(["nope"])

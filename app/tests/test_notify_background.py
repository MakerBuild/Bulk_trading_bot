"""Telegram in the background, and through the bot's own proxy.

A cycle report used to be awaited between cycles, so a slow or blocked
api.telegram.org held up the next cycle for as long as the send took to time
out. And every send went direct, ignoring proxy.local -- in a country that
blocks BULK, often blocked too, which reads as "the bot has gone quiet".
"""

import asyncio

from bulkdn import notify
from bulkdn.notify import Notifier, TelegramConfig


def enabled_notifier():
    return Notifier(TelegramConfig(bot_token="123:abc", user_ids=[1]))


async def test_a_cycle_report_does_not_wait_for_telegram(monkeypatch):
    notifier = enabled_notifier()
    release = asyncio.Event()
    sent = []

    async def slow_send(text, *, prefix=""):
        await release.wait()
        sent.append(text)

    monkeypatch.setattr(notifier, "send", slow_send)

    await asyncio.wait_for(notifier.cycle_complete(cycle=1, of=3), timeout=0.5)

    assert sent == [], "it waited for the send"
    assert len(notifier._pending) == 1, "the background send is not held onto"
    release.set()
    await notifier.drain(timeout_s=1.0)
    assert sent and "cycle <b>1/3</b>" in sent[0]
    assert not notifier._pending


async def test_drain_gives_up_on_a_send_that_never_finishes(monkeypatch):
    notifier = enabled_notifier()

    async def never(text, *, prefix=""):
        await asyncio.Event().wait()

    monkeypatch.setattr(notifier, "send", never)
    notifier.send_soon(notifier.send("stuck"))

    await asyncio.wait_for(notifier.drain(timeout_s=0.05), timeout=1.0)


def test_an_http_proxy_is_used_for_telegram(monkeypatch):
    for name in notify._PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("https_proxy", "http://user:pw@proxy.example:8080")
    assert notify._telegram_proxy() == "http://user:pw@proxy.example:8080"


def test_a_socks_proxy_is_skipped_with_one_warning(monkeypatch, caplog):
    for name in notify._PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("https_proxy", "socks5h://proxy.example:1080")
    monkeypatch.setattr(notify, "_socks_warned", False)

    assert notify._telegram_proxy() is None
    assert notify._telegram_proxy() is None
    warnings = [m for m in caplog.messages if "not applied to Telegram" in m]
    assert len(warnings) == 1


def test_no_proxy_configured_goes_direct(monkeypatch):
    for name in notify._PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    assert notify._telegram_proxy() is None


def test_the_token_stays_out_of_a_repr():
    assert "abc" not in repr(TelegramConfig(bot_token="123:abc", user_ids=[1]))

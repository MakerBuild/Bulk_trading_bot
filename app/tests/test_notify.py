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


def test_long_text_is_split_into_pieces_that_fit():
    from bulkdn.notify import _CHUNK, _pieces

    pieces = _pieces("x" * (_CHUNK * 2 + 10), _CHUNK)
    assert len(pieces) == 3
    assert all(len(piece) <= _CHUNK for piece in pieces)
    assert "".join(pieces) == "x" * (_CHUNK * 2 + 10)


def test_short_text_is_one_piece():
    from bulkdn.notify import _pieces

    assert _pieces("short", 100) == ["short"]


def test_pieces_are_measured_escaped_and_break_at_lines():
    """`&` becomes five characters once escaped; measuring the raw text let an
    escaped piece run past the limit."""
    import html

    from bulkdn.notify import _pieces

    text = "\n".join(["a & b < c"] * 400)
    pieces = _pieces(text, 300)
    assert "".join(pieces) == text
    assert all(len(html.escape(piece)) <= 300 for piece in pieces)
    assert all(piece.endswith("\n") for piece in pieces[:-1]), "a line was cut in two"


# -- long messages arrive whole and parseable ------------------------------


class Telegram:
    """A fake Bot API that refuses a message the way Telegram does when its
    HTML does not parse -- an unclosed tag or a cut entity."""

    def __init__(self, refuse_html=False):
        self.posted = []
        self.refuse_html = refuse_html

    def session(self, *args, **kwargs):
        telegram = self

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def post(self, url, json=None, **kw):
                telegram.posted.append(json)

                class Response:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *exc):
                        return False

                    async def json(self):
                        return telegram.answer(json)

                return Response()

        return Session()

    def answer(self, body):
        if body.get("parse_mode") == "HTML" and (self.refuse_html or not parses(body["text"])):
            return {"ok": False, "error_code": 400,
                    "description": "Bad Request: can't parse entities: unclosed tag"}
        return {"ok": True}


def parses(text):
    """Every <pre>/<b> closed in the same message, and no entity cut."""
    import re

    for tag in ("pre", "b", "code"):
        if text.count(f"<{tag}>") != text.count(f"</{tag}>"):
            return False
    return not re.search(r"&[a-z#0-9]*\Z", text) and not re.match(r"[a-z#0-9]*;", text)


@pytest.fixture
def telegram(monkeypatch):
    from bulkdn import notify

    fake = Telegram()
    monkeypatch.setattr(notify.aiohttp, "ClientSession", fake.session)
    return fake


async def test_a_long_error_arrives_in_pieces_that_each_parse(telegram):
    """The finished HTML was cut every 1900 characters, through a <pre> and
    through `&amp;`; Telegram refused every such piece and the alert was lost."""
    import html

    from bulkdn.notify import _CHUNK

    lines = [f"COULD NOT CLOSE m{i} BTC-USD & ETH-USD <by hand>" for i in range(120)]
    notifier = Notifier(TelegramConfig(bot_token="123:abc", user_ids=[1]))
    await notifier.error("\n".join(lines))

    texts = [body["text"] for body in telegram.posted]
    assert len(texts) > 1
    assert all(body.get("parse_mode") == "HTML" for body in telegram.posted), (
        "a piece needed the plain-text fallback, so it did not parse"
    )
    assert all(len(text) <= _CHUNK for text in texts)
    assert all(parses(text) for text in texts), "a piece was cut through a tag or an entity"
    shown = html.unescape("".join(t.replace("<pre>", "").replace("</pre>", "") for t in texts))
    for line in lines:
        assert line in shown, "part of the alert never arrived"


async def test_a_long_halt_reason_is_not_lost(telegram):
    notifier = Notifier(TelegramConfig(bot_token="123:abc", user_ids=[1]))
    reason = "net exposure exceeded on " + ", ".join(f"m{i} BTC-USD" for i in range(400))
    await notifier.halted(reason)

    texts = [body["text"] for body in telegram.posted]
    assert len(texts) > 1
    assert "trading stopped" in texts[0]
    assert all(parses(text) for text in texts)
    assert "m399 BTC-USD" in "".join(texts)


async def test_html_telegram_refuses_is_sent_again_as_plain_text(monkeypatch):
    from bulkdn import notify

    fake = Telegram(refuse_html=True)
    monkeypatch.setattr(notify.aiohttp, "ClientSession", fake.session)
    notifier = Notifier(TelegramConfig(bot_token="123:abc", user_ids=[1]))
    await notifier.send("<b>cycle</b> 3 &amp; counting")

    assert len(fake.posted) == 2
    retry = fake.posted[1]
    assert "parse_mode" not in retry
    assert "<b>" not in retry["text"] and "cycle 3 & counting" in retry["text"]


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

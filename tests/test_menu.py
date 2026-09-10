"""Menu behaviour.

The menu is the surface that fires live orders, so what matters here is that
nothing spends money without an explicit `yes`, and that navigation always
terminates -- a menu that cannot be left is worse than one that is ugly.
"""

import builtins

import pytest

from bulkdn import menu


class StubConfig:
    """Only what run_menu's header touches."""

    http_url = "https://mainnet-api1.bulk.trade/api/v1"
    cycles = 1
    hold_minutes = 5.0


def feed(monkeypatch, answers):
    """Drive the menu from a scripted list of keystrokes."""
    it = iter(answers)
    monkeypatch.setattr(builtins, "input", lambda *_: next(it))


def test_box_renders_a_closed_frame():
    rendered = menu._box("TITLE", ["1. One", "2. Two"])
    lines = rendered.splitlines()
    assert len(set(len(line) for line in lines)) == 1, "ragged frame"
    assert lines[0].startswith("+") and lines[0].endswith("+")
    assert "TITLE" in lines[1]
    assert lines[-1] == lines[0]


def test_confirm_requires_the_literal_word(monkeypatch):
    for answer in ("y", "Y", "yes please", "", "no", "YES"):
        feed(monkeypatch, [answer])
        assert menu._confirm("do a thing") is False, f"{answer!r} must not confirm"
    feed(monkeypatch, ["yes"])
    assert menu._confirm("do a thing") is True


def test_confirm_treats_eof_as_refusal(monkeypatch):
    def raise_eof(*_):
        raise EOFError

    monkeypatch.setattr(builtins, "input", raise_eof)
    assert menu._confirm("do a thing") is False


def test_exit_returns_immediately(monkeypatch):
    feed(monkeypatch, ["0"])
    assert menu.run_menu(StubConfig(), "config.yaml") == 0


def test_unknown_choice_reprompts_then_exits(monkeypatch):
    feed(monkeypatch, ["99", "", "0"])
    assert menu.run_menu(StubConfig(), "config.yaml") == 0


def test_menu_survives_an_action_that_raises(monkeypatch):
    """A failing item must return to the menu, not kill the process."""
    def boom(_config):
        raise RuntimeError("exchange down")

    monkeypatch.setattr(menu, "_active_strategy", boom)
    feed(monkeypatch, ["2", "", "0"])
    assert menu.run_menu(StubConfig(), "config.yaml") == 0


def test_no_account_tree_is_reported_as_guidance(monkeypatch):
    def missing(_config):
        raise menu.NoAccountTree("deposit first")

    monkeypatch.setattr(menu, "_history", missing)
    feed(monkeypatch, ["3", "", "0"])
    assert menu.run_menu(StubConfig(), "config.yaml") == 0


def test_start_declined_does_not_run_the_strategy(monkeypatch):
    """`2` picks live, then anything but `yes` must abort before submitting."""
    calls = []
    monkeypatch.setattr(menu.asyncio, "run", lambda coro: calls.append(coro))

    feed(monkeypatch, ["2", "n", ""])
    menu._start(StubConfig())
    assert calls == [], "a declined confirmation must not start a cycle"


def test_short_leaves_small_keys_alone():
    assert menu._short("abc") == "abc"
    long = "BR4SV1CRKygGWCsb1zF3g38Xc68b31WkEagdk8hVedB8"
    assert menu._short(long) == "BR4SV1..edB8"

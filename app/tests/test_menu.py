"""Menu behaviour.

The menu is the surface that fires live orders, so what matters here is that
nothing spends money without an explicit `yes`, and that navigation always
terminates -- a menu that cannot be left is worse than one that is ugly.
"""

import builtins

from bulkdn import menu
from bulkdn.config import ExecutionTarget, HoldTime


class StubLeg:
    def __init__(self, symbol):
        self.symbol = symbol


class StubConfig:
    """Only what run_menu's header and the start screen touch."""

    http_url = "https://mainnet-api1.bulk.trade/api/v1"
    cycles = 1
    # The start screen states the real stop conditions, all of which live here.
    target = ExecutionTarget(cycles=1)
    hold_minutes = HoldTime(5.0, 5.0)
    # The start screen names the markets before asking, because the mode can
    # come from the command line and the settings file is then not proof of
    # what the run will trade.
    mode = "multi"
    master_account = StubLeg("BTC-USD")
    sub_account = StubLeg("ETH-USD")
    active_legs = [master_account, sub_account]


def feed(monkeypatch, answers):
    """Drive the menu from a scripted list of keystrokes."""
    it = iter(answers)
    monkeypatch.setattr(builtins, "input", lambda *_: next(it))


def test_box_renders_a_closed_frame():
    rendered = menu._box("TITLE", ["1. One", "2. Two"])
    lines = rendered.splitlines()
    assert len({len(line) for line in lines}) == 1, "ragged frame"
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
    monkeypatch.setattr(menu.asyncio, "run", calls.append)

    feed(monkeypatch, ["2", "n", ""])
    menu._start(StubConfig())
    assert calls == [], "a declined confirmation must not start a cycle"


def test_short_pubkey_leaves_small_keys_alone():
    from bulkdn.accounts import short_pubkey

    assert short_pubkey("abc") == "abc"
    assert short_pubkey(None) == "?"
    long = "EXAMPLE-MASTER-ACCOUNT-PUBKEY"
    assert short_pubkey(long) == "EXAMPL..BKEY"


# -- switching between one market and two -----------------------------------
#
# The flag exists on the command line, but the menu is where this actually
# gets used, and a setting you have to remember a flag for is a setting that
# gets forgotten. Writing it back has to keep the file's comments: they are
# most of the file and the only documentation an operator reads.


SETTINGS_WITH_COMMENTS = """\
# ---------------------------------------------------------------------------
#  WHAT TO TRADE
# ---------------------------------------------------------------------------
legs:
  master_account:
    symbol: BTC-USD      # keep this comment
  sub_account:
    symbol: ETH-USD
"""


def test_the_mode_is_written_above_the_legs_it_describes(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(SETTINGS_WITH_COMMENTS, encoding="utf-8")

    menu._write_mode(str(path), "single")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert "mode: single" in lines
    assert lines.index("mode: single") < lines.index("legs:")
    assert "#  WHAT TO TRADE" in lines, "the file's comments were lost"
    assert "    symbol: BTC-USD      # keep this comment" in lines


def test_writing_it_twice_replaces_rather_than_repeats(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(SETTINGS_WITH_COMMENTS, encoding="utf-8")

    menu._write_mode(str(path), "single")
    menu._write_mode(str(path), "multi")

    body = path.read_text(encoding="utf-8")
    assert body.count("mode:") == 1
    assert "mode: multi" in body


def test_a_nested_mode_key_is_not_mistaken_for_the_top_level_one(tmp_path):
    """`legs` could gain a key ending in the same word. Only a line starting
    at column zero is this setting."""
    path = tmp_path / "settings.yaml"
    path.write_text("legs:\n  master_account:\n    mode: whatever\n", encoding="utf-8")

    menu._write_mode(str(path), "single")

    body = path.read_text(encoding="utf-8")
    assert "    mode: whatever" in body, "it edited the nested key"
    assert "mode: single" in body.splitlines()


def test_a_long_entry_does_not_break_the_frame():
    """`ljust` pads but never truncates, so before this the box simply came
    apart the day an entry outgrew it -- which is how the markets line, with
    two symbols in it, first appeared."""
    rendered = menu._box("TITLE", ["short", "an entry far wider than the frame has ever been"])
    lines = rendered.splitlines()
    assert len({len(line) for line in lines}) == 1, "ragged frame"
    assert all(line.startswith(("+", "|")) and line.endswith(("+", "|")) for line in lines)


def test_a_short_menu_keeps_the_width_it_always_had():
    assert len(menu._box("T", ["1. One"]).splitlines()[0]) == menu.BOX_WIDTH

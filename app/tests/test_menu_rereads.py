"""The menu reads settings.yaml for every action, and opens whatever state it is in.

It used to read the file once, before anything else -- before logging existed,
before the proxy -- so a mistake in it printed one line and closed the window,
and the menu that could have helped never opened. The message was nowhere
afterwards: not in logs.txt, and run.bat did not pause. And the one Config it
held for its whole life meant an edit made while it was open did nothing until
a restart, while a run's resized legs became the next run's.
"""

import builtins
import logging
import pathlib

import pytest

from bulkdn import cli, menu
from bulkdn import config as config_module

GOOD = """\
mode: multi
markets:
  - symbol: BTC-USD
    notional_usd: 100
    max_order_notional_usd: 100
  - symbol: ETH-USD
    notional_usd: 100
    max_order_notional_usd: 100
"""

SIDEWAYS = GOOD.replace("mode: multi", "mode: sideways")


def feed(monkeypatch, answers):
    it = iter(answers)
    monkeypatch.setattr(builtins, "input", lambda *_: next(it))


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """A bot folder with good settings and a key that needs no password."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "settings.yaml").write_text(GOOD, encoding="utf-8")
    reads = []

    def keys(required=True):
        if required:
            reads.append(required)
        return ["KEY-ONE"] if required else []

    monkeypatch.setattr(config_module, "load_private_keys", keys)
    monkeypatch.setattr(menu, "_pause", lambda: None)
    return tmp_path, reads


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)


# -- every action reads the file as it is now ---------------------------------


def test_each_start_gets_the_file_as_it_is_now(folder, monkeypatch):
    path, _ = folder
    seen = []

    async def fake_run(config, dry_run):
        seen.append((config, config.markets[0].notional_usd))
        # What a run does to the config it is given.
        config.markets[0].notional_usd = 1.0
        return 0

    monkeypatch.setattr(menu, "cmd_run", fake_run)
    settings = menu.Settings(str(path / "settings.yaml"))

    feed(monkeypatch, ["1", "1", "1", "1", "0"])
    # Start -> dry run (the file is edited by hand while the screen is up),
    # Start -> dry run, exit.
    real_ask = menu._ask
    asked = []

    def ask(prompt):
        asked.append(prompt)
        if len(asked) == 2:
            text = GOOD.replace("notional_usd: 100\n    max", "notional_usd: 250\n    max", 1)
            (path / "settings.yaml").write_text(text, encoding="utf-8")
        return real_ask(prompt)

    monkeypatch.setattr(menu, "_ask", ask)
    assert menu.run_menu(settings) == 0

    (first, _), (second, size_seen) = seen
    assert first is not second
    assert size_seen == 250.0, "the edit was not read, or the last run leaked into this one"


def test_the_key_password_is_asked_for_once(folder, monkeypatch):
    path, reads = folder
    monkeypatch.setattr(menu, "_logs", lambda: None)
    feed(monkeypatch, ["7", "7", "7", "0"])
    assert menu.run_menu(menu.Settings(str(path / "settings.yaml"))) == 0
    assert len(reads) == 1


def test_erasing_the_key_makes_the_next_action_read_it_again(folder):
    path, reads = folder
    settings = menu.Settings(str(path / "settings.yaml"))
    settings.load()
    settings.forget_keys()
    settings.load()
    assert len(reads) == 2


# -- a broken file opens the menu rather than closing the window --------------


def test_a_broken_file_shows_the_problem_and_waits_for_a_fix(folder, monkeypatch, capsys):
    path, _ = folder
    (path / "settings.yaml").write_text(SIDEWAYS, encoding="utf-8")
    answers = iter(["1", "0", "0"])

    def ask(prompt):
        answer = next(answers)
        if answer == "1":
            # The operator fixes the file, then chooses Try again.
            (path / "settings.yaml").write_text(GOOD, encoding="utf-8")
        return answer

    monkeypatch.setattr(menu, "_ask", ask)
    assert menu.run_menu(menu.Settings(str(path / "settings.yaml"))) == 0
    out = capsys.readouterr().out
    assert "THE BOT CANNOT START YET" in out
    assert "sideways" in out
    assert "DELTA-NEUTRAL BOT" in out, "the menu never opened after the fix"


def test_reset_keeps_a_copy_of_what_was_there(folder, monkeypatch):
    path, _ = folder
    broken = SIDEWAYS
    (path / "settings.yaml").write_text(broken, encoding="utf-8")
    template = path / config_module.SETTINGS_TEMPLATE
    template.parent.mkdir(parents=True)
    template.write_text(GOOD, encoding="utf-8")

    answers = iter(["3", "0"])
    monkeypatch.setattr(menu, "_ask", lambda _p: next(answers))
    assert menu.run_menu(menu.Settings(str(path / "settings.yaml"))) == 0

    backups = list(path.glob("settings.yaml.bak-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == broken
    assert (path / "settings.yaml").read_text(encoding="utf-8") == GOOD


def test_a_key_problem_points_at_the_key_file(folder, monkeypatch, capsys):
    path, _ = folder
    monkeypatch.setattr(config_module, "load_private_keys", lambda required=True: [])
    monkeypatch.setattr(menu, "_ask", lambda _p: "0")
    assert menu.run_menu(menu.Settings(str(path / "settings.yaml"))) == 1
    out = capsys.readouterr().out
    assert f"Open {config_module.PRIVATE_KEY_FILE}" in out
    assert "Reset to the shipped settings" not in out, "the settings are not the fault"


def test_the_cli_opens_the_menu_over_a_broken_file_and_logs_why(
    folder, monkeypatch, restore_logging
):
    path, _ = folder
    (path / "settings.yaml").write_text(SIDEWAYS, encoding="utf-8")
    monkeypatch.setattr(menu, "_ask", lambda _p: "0")

    assert cli.main([]) == 1
    logged = (path / cli.LOG_FILE).read_text(encoding="utf-8")
    assert "sideways" in logged, "the reason is not in logs.txt"


def test_a_command_that_cannot_start_logs_why_too(folder, monkeypatch, restore_logging, capsys):
    path, _ = folder
    (path / "settings.yaml").write_text(SIDEWAYS, encoding="utf-8")

    assert cli.main(["status"]) == 1
    assert "configuration error" in capsys.readouterr().err
    assert "sideways" in (path / cli.LOG_FILE).read_text(encoding="utf-8")


def test_settings_warnings_reach_the_log_file(folder, monkeypatch, restore_logging):
    """They went to Python's last-resort handler: the console only, unformatted."""
    path, _ = folder
    (path / "settings.yaml").write_text(GOOD + "notional_ust: 5\n", encoding="utf-8")
    monkeypatch.setattr(menu, "_ask", lambda _p: "0")
    assert cli.main([]) == 0
    assert "notional_ust" in (path / cli.LOG_FILE).read_text(encoding="utf-8")


# -- run.bat keeps the window open on a failure -------------------------------


def test_run_bat_pauses_when_the_bot_exits_with_an_error():
    root = pathlib.Path(__file__).resolve().parents[2]
    text = (root / "run.bat").read_text(encoding="utf-8")
    after = text[text.index("-m bulkdn %*"):]
    assert "pause" in after.lower(), "the window closes on the error"

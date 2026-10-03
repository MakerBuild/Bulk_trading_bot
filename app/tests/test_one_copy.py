"""One copy of the bot on the same accounts at a time.

The menu and the Telegram service could both trade the same pool at once, each
hedging the other's fills as its own, and update.bat reinstalled the SDK under
a bot running from it. Every command that trades, closes or reads the accounts
now holds an operating-system lock, which the process's death releases -- so
there is no stale lock left behind by a crash for anyone to clear by hand.
"""

import pathlib
import subprocess
import sys
import textwrap
import time

import pytest

from bulkdn import cli, lock, menu

APP = pathlib.Path(__file__).resolve().parents[1]


def other_process(path, seconds=30):
    """A second Python holding the lock, the way a running bot would."""
    code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(APP)!r})
        from bulkdn import lock
        lock.acquire("telegram", {str(path)!r})
        print("held", flush=True)
        time.sleep({seconds})
    """)
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "held"
    return child


@pytest.fixture
def held(tmp_path, monkeypatch):
    # Refusing need not wait the full patience a just-crashed copy is given.
    monkeypatch.setattr(lock, "_PATIENCE_S", 0.2)
    path = tmp_path / "bot.lock"
    child = other_process(path)
    yield path
    child.kill()
    child.wait()


def test_a_second_copy_is_refused_and_told_who_holds_it(held):
    with pytest.raises(lock.Held, match=r"process \d+ \(telegram"):
        lock.acquire("run", str(held))
    assert "telegram" in lock.holder(str(held))


def test_a_copy_that_died_holds_nothing(tmp_path):
    """No stale lock: the operating system lets go when the process ends."""
    path = tmp_path / "bot.lock"
    child = other_process(path)
    child.kill()
    child.wait()
    assert lock.holder(str(path)) is None
    with lock.hold("run", str(path)):
        assert path.read_text(encoding="utf-8").startswith("process ")


def test_one_process_can_take_it_inside_itself(tmp_path):
    """The menu holds it around Telegram control, whose runs take it again."""
    path = str(tmp_path / "bot.lock")
    with lock.hold("menu", path), lock.hold("run", path):
        pass
    with lock.hold("again", path):
        pass


def test_the_cli_refuses_a_trading_command_while_held(held, monkeypatch, capsys):
    monkeypatch.setattr(lock, "LOCK_FILE", str(held))
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: object())
    monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(cli.proxy, "configure", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ready", lambda *a, **k: None)
    ran = []
    monkeypatch.setattr(cli, "cmd_status", ran.append)

    assert cli.main(["status"]) == cli.EXIT_ALREADY_RUNNING
    assert ran == []
    assert "already running" in capsys.readouterr().err


def test_the_menu_refuses_to_start_while_held(held, monkeypatch, capsys):
    monkeypatch.setattr(lock, "LOCK_FILE", str(held))
    monkeypatch.setattr(menu, "_pause", lambda: None)
    started = []
    guarded = menu._locked("menu: start", started.append)
    guarded(object())
    assert started == []
    assert "already working with these accounts" in capsys.readouterr().out


def test_the_menu_lets_go_after_each_item(tmp_path, monkeypatch):
    """Held for the length of an item, not for as long as the menu is open:
    an idle menu must not keep the Telegram service from working."""
    monkeypatch.setattr(lock, "LOCK_FILE", str(tmp_path / "bot.lock"))
    menu._locked("menu: status", lambda _c: None)(object())
    child = other_process(tmp_path / "bot.lock", seconds=0)
    child.wait()
    assert child.returncode == 0


def test_the_update_check_exits_with_its_own_code(held, monkeypatch):
    monkeypatch.setattr(lock, "LOCK_FILE", str(held))
    assert lock.main() == lock.HELD_EXIT


def test_the_update_check_is_quiet_when_nothing_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(lock, "LOCK_FILE", str(tmp_path / "none.lock"))
    started = time.monotonic()
    assert lock.main() == 0
    assert time.monotonic() - started < 5

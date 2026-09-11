"""Erasing local data from Accounts Management.

The only irreversible thing this bot can do to the operator's own machine, so
what matters is that nothing is deleted without an explicit instruction to
delete it, and that the private key needs more than the usual `yes` -- losing
the only copy of a signing key loses the account from every tool, not just
this one.
"""

import builtins
import pathlib

import pytest

from bulkdn import menu


class StubConfig:
    def __init__(self, state_file):
        self.state_file = str(state_file)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A project folder with a key, a state file and a bytecode cache."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "app" / "state").mkdir(parents=True)
    state = tmp_path / "app" / "state" / "strategy_state.json"
    state.write_text('{"phase": "IDLE"}', encoding="utf-8")
    key = tmp_path / "private_key.local"
    key.write_text("not a real key", encoding="utf-8")
    cache = tmp_path / "app" / "bulkdn" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "menu.cpython-314.pyc").write_bytes(b"\x00" * 64)
    return tmp_path, state, key, cache


def feed(monkeypatch, answers):
    it = iter(answers)
    monkeypatch.setattr(builtins, "input", lambda *_: next(it))


def run(monkeypatch, state, answers):
    feed(monkeypatch, answers)
    menu._erase_data(StubConfig(state))


# -- nothing happens by accident -------------------------------------------


def test_backing_out_deletes_nothing(workspace, monkeypatch):
    _root, state, key, cache = workspace
    run(monkeypatch, state, ["0"])
    assert state.exists() and key.exists() and cache.exists()


def test_a_refused_confirmation_deletes_nothing(workspace, monkeypatch):
    _root, state, key, _cache = workspace
    run(monkeypatch, state, ["1", "no", ""])
    assert state.exists() and key.exists()
    assert "not a real key" in key.read_text(encoding="utf-8")


def test_the_key_is_not_wiped_by_the_ordinary_yes(workspace, monkeypatch):
    """`yes` clears every other confirmation in this menu. Not this one."""
    _root, state, key, _cache = workspace
    run(monkeypatch, state, ["2", "yes", ""])
    assert "not a real key" in key.read_text(encoding="utf-8")


def test_the_key_needs_its_own_words(workspace, monkeypatch):
    _root, state, key, _cache = workspace
    run(monkeypatch, state, ["2", "DELETE KEY", ""])
    assert "not a real key" not in key.read_text(encoding="utf-8")
    # And only the key.
    assert state.exists()


# -- it deletes what was asked for, and nothing else ------------------------


def test_state_only_leaves_the_key_alone(workspace, monkeypatch):
    _root, state, key, cache = workspace
    run(monkeypatch, state, ["1", "yes", ""])
    assert not state.exists()
    assert key.exists() and cache.exists()


def test_caches_only_leave_the_data_alone(workspace, monkeypatch):
    _root, state, key, cache = workspace
    run(monkeypatch, state, ["3", "yes", ""])
    assert not cache.exists()
    assert state.exists() and key.exists()


def test_all_of_the_above_still_needs_the_key_phrase(workspace, monkeypatch):
    _root, state, key, cache = workspace
    run(monkeypatch, state, ["9", "yes", ""])
    # 'yes' is not the key phrase, so the whole batch aborts rather than
    # deleting the harmless parts and stopping at the key.
    assert state.exists() and key.exists() and cache.exists()


def test_all_of_the_above_clears_everything_when_confirmed(workspace, monkeypatch):
    _root, state, key, cache = workspace
    run(monkeypatch, state, ["9", "DELETE KEY", ""])
    assert not state.exists() and not cache.exists()
    assert key.exists(), "the key file itself must survive"
    assert "not a real key" not in key.read_text(encoding="utf-8")


# -- the venv is not a cache ------------------------------------------------


def test_the_virtualenv_is_never_offered(workspace, monkeypatch):
    """Deleting it would break the install; it is not the operator's data."""
    root, _state, _key, _cache = workspace
    venv_cache = root / "app" / ".venv" / "Lib" / "site-packages" / "__pycache__"
    venv_cache.mkdir(parents=True)
    (venv_cache / "x.pyc").write_bytes(b"\x00")

    found = menu._cache_dirs(pathlib.Path(root))
    assert venv_cache not in found
    assert all(".venv" not in d.parts for d in found)


# -- a warning when the file may still matter -------------------------------


def test_an_open_cycle_is_called_out(workspace):
    _root, state, _key, _cache = workspace
    state.write_text('{"phase": "OPEN"}', encoding="utf-8")
    warning = menu._live_cycle_warning(state)
    assert warning and "OPEN" in warning
    assert "Close All Positions" in warning


def test_an_idle_state_warns_about_nothing(workspace):
    _root, state, _key, _cache = workspace
    assert menu._live_cycle_warning(state) is None


def test_a_missing_state_file_warns_about_nothing(tmp_path):
    assert menu._live_cycle_warning(tmp_path / "absent.json") is None


def test_the_screen_fits_inside_the_frame(workspace, monkeypatch, capsys):
    """_box does not wrap, so a line that outgrows it splits the border."""
    _root, state, _key, _cache = workspace
    run(monkeypatch, state, ["0"])

    lines = capsys.readouterr().out.splitlines()
    border = next(line for line in lines if set(line) == {"+", "-"})
    framed = [line for line in lines if line.startswith("|")]
    assert framed, "no framed lines rendered"
    assert {len(line) for line in framed} == {len(border)}


def test_paths_are_shown_relative_to_the_project(workspace, monkeypatch, capsys):
    root, state, _key, cache = workspace
    run(monkeypatch, state, ["0"])

    out = capsys.readouterr().out
    assert "app" in out
    assert str(root) not in out, "an absolute path leaked into the listing"
    assert menu._shown(cache).startswith("app")


# -- the key file is emptied, never removed ---------------------------------
#
# It is where the next key gets pasted. Deleting it leaves no obvious place to
# put one, and getting it back means re-running install.bat -- which is not
# what "erase my data" should cost.


def test_the_key_file_survives_with_its_instructions(workspace, monkeypatch):
    from bulkdn.config import PRIVATE_KEY_TEMPLATE

    _root, state, key, _cache = workspace
    run(monkeypatch, state, ["2", "DELETE KEY", ""])

    assert key.exists()
    assert key.read_text(encoding="utf-8") == PRIVATE_KEY_TEMPLATE
    assert "Paste your BULK master account" in key.read_text(encoding="utf-8")


def test_the_emptied_file_is_not_read_as_a_key():
    """The template is all comments, so nothing mistakes it for a key."""
    from bulkdn.config import PRIVATE_KEY_TEMPLATE

    lines = [
        line for line in PRIVATE_KEY_TEMPLATE.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines == []


def test_install_bat_writes_the_same_template():
    """Two copies of this text exist -- batch cannot read a Python constant --
    so they are checked against each other rather than trusted to stay equal."""
    from bulkdn.config import PRIVATE_KEY_TEMPLATE

    batch = pathlib.Path("install.bat").read_text(encoding="utf-8")
    echoed = []
    for line in batch.splitlines():
        stripped = line.strip()
        if "private_key.local echo" not in stripped:
            continue
        text = stripped.split("private_key.local echo", 1)[1]
        # `echo.` is batch for a blank line, and `^>` escapes a redirect.
        echoed.append("" if text.strip() == "." else text.strip().replace("^>", ">"))

    expected = [line.strip() for line in PRIVATE_KEY_TEMPLATE.splitlines()]
    assert echoed == expected, "install.bat and PRIVATE_KEY_TEMPLATE have drifted"

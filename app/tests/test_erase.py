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


SHIPPED_SETTINGS = "hold_minutes: 0.5-1.5\n"
EDITED_SETTINGS = "hold_minutes: 9-9   # mine\nnotional_usd: 5000\n"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A project folder as an operator's really looks: key, state, cache,
    settings they have edited, and the template those were made from."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "app" / "state").mkdir(parents=True)
    state = tmp_path / "app" / "state" / "strategy_state.json"
    state.write_text('{"phase": "IDLE"}', encoding="utf-8")
    key = tmp_path / "private_key.local"
    key.write_text("not a real key", encoding="utf-8")
    cache = tmp_path / "app" / "bulkdn" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "menu.cpython-314.pyc").write_bytes(b"\x00" * 64)

    from bulkdn.config import SETTINGS_TEMPLATE

    template = tmp_path / SETTINGS_TEMPLATE
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text(SHIPPED_SETTINGS, encoding="utf-8")
    settings = tmp_path / "settings.yaml"
    settings.write_text(EDITED_SETTINGS, encoding="utf-8")
    return tmp_path, state, key, cache, settings


def feed(monkeypatch, answers):
    it = iter(answers)
    monkeypatch.setattr(builtins, "input", lambda *_: next(it))


def run(monkeypatch, state, answers, config_path="settings.yaml"):
    feed(monkeypatch, answers)
    menu._erase_data(StubConfig(state), config_path)


# -- nothing happens by accident -------------------------------------------


def test_backing_out_deletes_nothing(workspace, monkeypatch):
    _root, state, key, cache, _settings = workspace
    run(monkeypatch, state, ["0"])
    assert state.exists() and key.exists() and cache.exists()


def test_a_refused_confirmation_deletes_nothing(workspace, monkeypatch):
    _root, state, key, _cache, _settings = workspace
    run(monkeypatch, state, ["1", "no", ""])
    assert state.exists() and key.exists()
    assert "not a real key" in key.read_text(encoding="utf-8")


def test_the_key_is_not_wiped_by_the_ordinary_yes(workspace, monkeypatch):
    """`yes` clears every other confirmation in this menu. Not this one."""
    _root, state, key, _cache, _settings = workspace
    run(monkeypatch, state, ["2", "yes", ""])
    assert "not a real key" in key.read_text(encoding="utf-8")


def test_the_key_needs_its_own_words(workspace, monkeypatch):
    _root, state, key, _cache, _settings = workspace
    run(monkeypatch, state, ["2", "DELETE KEY", ""])
    assert "not a real key" not in key.read_text(encoding="utf-8")
    # And only the key.
    assert state.exists()


# -- it deletes what was asked for, and nothing else ------------------------


def test_state_only_leaves_the_key_alone(workspace, monkeypatch):
    _root, state, key, cache, _settings = workspace
    run(monkeypatch, state, ["1", "yes", ""])
    assert not state.exists()
    assert key.exists() and cache.exists()


def test_caches_only_leave_the_data_alone(workspace, monkeypatch):
    _root, state, key, cache, _settings = workspace
    run(monkeypatch, state, ["3", "yes", ""])
    assert not cache.exists()
    assert state.exists() and key.exists()


def test_all_of_the_above_still_needs_the_key_phrase(workspace, monkeypatch):
    _root, state, key, cache, _settings = workspace
    run(monkeypatch, state, ["9", "yes", ""])
    # 'yes' is not the key phrase, so the whole batch aborts rather than
    # deleting the harmless parts and stopping at the key.
    assert state.exists() and key.exists() and cache.exists()


def test_all_of_the_above_clears_everything_when_confirmed(workspace, monkeypatch):
    _root, state, key, cache, _settings = workspace
    run(monkeypatch, state, ["9", "DELETE KEY", ""])
    assert not state.exists() and not cache.exists()
    assert key.exists(), "the key file itself must survive"
    assert "not a real key" not in key.read_text(encoding="utf-8")


# -- the venv is not a cache ------------------------------------------------


def test_the_virtualenv_is_never_offered(workspace, monkeypatch):
    """Deleting it would break the install; it is not the operator's data."""
    root, _state, _key, _cache, _settings = workspace
    venv_cache = root / "app" / ".venv" / "Lib" / "site-packages" / "__pycache__"
    venv_cache.mkdir(parents=True)
    (venv_cache / "x.pyc").write_bytes(b"\x00")

    found = menu._cache_dirs(pathlib.Path(root))
    assert venv_cache not in found
    assert all(".venv" not in d.parts for d in found)


# -- a warning when the file may still matter -------------------------------


def test_an_open_cycle_is_called_out(workspace):
    _root, state, _key, _cache, _settings = workspace
    state.write_text('{"phase": "OPEN"}', encoding="utf-8")
    warning = menu._live_cycle_warning(state)
    assert warning and "OPEN" in warning
    assert "Close All Positions" in warning


def test_an_idle_state_warns_about_nothing(workspace):
    _root, state, _key, _cache, _settings = workspace
    assert menu._live_cycle_warning(state) is None


def test_a_missing_state_file_warns_about_nothing(tmp_path):
    assert menu._live_cycle_warning(tmp_path / "absent.json") is None


def test_the_screen_fits_inside_the_frame(workspace, monkeypatch, capsys):
    """_box does not wrap, so a line that outgrows it splits the border."""
    _root, state, _key, _cache, _settings = workspace
    run(monkeypatch, state, ["0"])

    lines = capsys.readouterr().out.splitlines()
    border = next(line for line in lines if set(line) == {"+", "-"})
    framed = [line for line in lines if line.startswith("|")]
    assert framed, "no framed lines rendered"
    assert {len(line) for line in framed} == {len(border)}


def test_paths_are_shown_relative_to_the_project(workspace, monkeypatch, capsys):
    root, state, _key, cache, _settings = workspace
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

    _root, state, key, _cache, _settings = workspace
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


# -- what the menu says about the key ---------------------------------------
#
# An empty password encrypts under a constant published in keystore.py. The
# menu used to call that "encrypted", which tells an operator they are
# protected when anyone holding this repository can open the file.


def _write(path, seed, password=None):
    import json

    from bulkdn import keystore

    fast = keystore.KdfParams(ops=1, mem=8 * 1024 * 1024)
    if password is None:
        path.write_text(seed, encoding="utf-8")
    else:
        path.write_text(json.dumps(keystore.encrypt(seed, password, fast)), encoding="utf-8")


def test_a_bare_key_reads_as_plaintext(tmp_path):
    from bulkdn import keystore, menu

    f = tmp_path / "k.local"
    _write(f, "EXAMPLE-SEED")
    assert menu._key_state(str(f)) == "PLAINTEXT"
    assert keystore.is_encrypted(str(f)) is False


def test_the_default_password_is_called_out(tmp_path):
    from bulkdn import keystore, menu

    f = tmp_path / "k.local"
    _write(f, "EXAMPLE-SEED", keystore.DEFAULT_PASSWORD)
    # is_encrypted alone cannot tell these apart -- that was the bug.
    assert keystore.is_encrypted(str(f)) is True
    assert menu._key_state(str(f)) == "default password"


def test_a_real_password_reads_as_encrypted(tmp_path):
    from bulkdn import menu

    f = tmp_path / "k.local"
    _write(f, "EXAMPLE-SEED", "a real password")
    assert menu._key_state(str(f)) == "encrypted"


def test_a_missing_file_is_not_reported_as_protected(tmp_path):
    from bulkdn import menu

    assert menu._key_state(str(tmp_path / "absent.local")) == "PLAINTEXT"


# -- settings go back to the shipped ones, not away -------------------------
#
# "Erase my data" has to leave a copy that still runs. A bot with no settings
# file does not start at all, so the sizes and the leverage are what go -- by
# rewriting the file from the template install.bat used, which is exactly the
# file a fresh install produces.


def test_settings_are_reset_to_the_template(workspace, monkeypatch):
    _root, state, _key, _cache, settings = workspace
    run(monkeypatch, state, ["5", "yes", ""])

    assert settings.exists(), "the bot cannot start without it"
    assert settings.read_text(encoding="utf-8") == SHIPPED_SETTINGS
    assert "5000" not in settings.read_text(encoding="utf-8")


def test_resetting_settings_leaves_everything_else(workspace, monkeypatch):
    _root, state, key, cache, _settings = workspace
    run(monkeypatch, state, ["5", "yes", ""])
    assert state.exists() and cache.exists()
    assert "not a real key" in key.read_text(encoding="utf-8")


def test_all_of_the_above_resets_the_settings_too(workspace, monkeypatch):
    """The whole point of the button: a freshly installed copy."""
    _root, state, key, cache, settings = workspace
    run(monkeypatch, state, ["9", "DELETE KEY", ""])

    assert not state.exists() and not cache.exists()
    assert settings.read_text(encoding="utf-8") == SHIPPED_SETTINGS
    assert key.exists() and "not a real key" not in key.read_text(encoding="utf-8")


def test_a_missing_template_is_reported_not_guessed(workspace, monkeypatch, capsys):
    """Better to say so than to delete the settings and leave nothing to run."""
    from bulkdn.config import SETTINGS_TEMPLATE

    root, state, _key, _cache, settings = workspace
    (root / SETTINGS_TEMPLATE).unlink()

    run(monkeypatch, state, ["5", "yes", ""])
    out = capsys.readouterr().out
    assert "FAILED" in out and "update.bat" in out
    assert settings.read_text(encoding="utf-8") == EDITED_SETTINGS


# -- what it cannot reach, it says so about ---------------------------------


def test_the_virtualenv_is_reported_as_a_manual_step(workspace, monkeypatch, capsys):
    """It holds the Windows username, and a running program cannot delete its
    own executable -- a rmtree from in here fails partway and leaves a broken
    environment. So it is named, with what to do, rather than attempted."""
    root, state, _key, _cache, _settings = workspace
    (root / "app" / ".venv" / "Scripts").mkdir(parents=True)
    (root / "app" / ".venv" / "pyvenv.cfg").write_text(
        "home = C:/Users/example", encoding="utf-8"
    )

    run(monkeypatch, state, ["0"])
    out = capsys.readouterr().out
    assert ".venv" in out
    assert "install.bat" in out


def test_no_venv_no_note(workspace, monkeypatch, capsys):
    _root, state, _key, _cache, _settings = workspace
    run(monkeypatch, state, ["0"])
    assert "Not removable from here" not in capsys.readouterr().out


# -- update.bat tells the truth about why it failed --------------------------
#
# It ran `git pull` and reported every failure as a hand-edited file. github.com
# answers on several addresses and one of them was unreachable from the
# operator's network, so the update failed on about every other run while
# telling them to throw away edits they had never made:
#
#     fatal: unable to access '...': Failed to connect to github.com port 443
#     Update failed. Almost always this means a file that ships with the bot
#     was edited by hand...


def update_bat() -> str:
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    return (root / "update.bat").read_text(encoding="utf-8")


def test_fetching_and_applying_are_separate_steps():
    """One `git pull` cannot report which of the two went wrong."""
    import re

    text = update_bat()
    assert "git fetch" in text
    assert "git merge --ff-only" in text
    # As a command, not as the comment explaining why it is gone.
    assert not re.search(r"^\s*git pull", text, re.M), (
        "a pull cannot tell the two failures apart"
    )


def test_an_unreachable_network_is_named_as_one():
    text = update_bat()
    fetch = text[text.index("git fetch"):text.index("git merge")]
    assert "Could not reach GitHub" in fetch
    assert "edited by hand" not in fetch, "it still blames the operator's files"


def test_a_local_edit_is_named_as_one():
    text = update_bat()
    merge = text[text.index("git merge"):]
    assert "edited by hand" in merge
    assert "git checkout -- ." in merge, "no way out is offered"


def test_the_fetch_is_retried_before_giving_up():
    """A single attempt is not evidence. The same GitHub address timed out
    after 21s and then answered in 1.3s, so one failure means nothing and
    making the operator re-run by hand asks them to do what the script can."""
    text = update_bat()
    fetch = text[text.index("Fetching..."):text.index("git merge")]
    assert "for /L" in fetch, "it still gives up on the first try"
    assert "git fetch" in fetch
    assert "Trying again" in fetch, "a silent retry looks like a hang"


def test_the_retry_stops_once_it_works():
    """Otherwise it fetches three times on every successful update."""
    text = update_bat()
    fetch = text[text.index("Fetching..."):text.index("git merge")]
    # Twice: once guarding the loop body so later passes are skipped, once
    # after it to decide whether to give up. Only the first is the early exit,
    # and only counting both distinguishes it from the failure check.
    assert fetch.count("if not defined FETCHED") == 2, "the loop has no early exit"
    assert 'set "FETCHED=1"' in fetch


def test_the_download_is_retried_too():
    """The unzipped copy clones instead of fetching, over the same network."""
    text = update_bat()
    start = text.index("Downloading")
    # The command, not the comment further up that mentions it by name.
    section = text[start:text.index('robocopy "', start)]
    assert "for /L" in section, "the download still gives up on the first try"
    assert "git clone" in section
    assert "CLONED" in section


def test_a_failed_fetch_changes_nothing():
    """It must not leave a half-applied update behind."""
    text = update_bat()
    fetch = text[text.index("git fetch"):text.index("git merge")]
    assert "Nothing was changed" in fetch


def test_the_block_is_still_one_parenthesised_unit():
    """cmd.exe reads this file by byte offset while it runs, and the update
    rewrites the file underneath it. The block is what makes that safe, and a
    stray goto or label would abandon it."""
    import re

    text = update_bat()
    body = text[text.index("\n(\n"):]
    assert body.count("(") == body.count(")"), "unbalanced -- the block is broken"
    assert not re.search(r"^\s*goto ", body, re.M), "a goto abandons the block"
    assert not re.search(r"^:[a-zA-Z]", body, re.M), "a label abandons the block"


# -- install.bat has the same two problems -----------------------------------
#
# pip fetches the SDK by cloning it from GitHub, over the same unreliable
# route, and the failure said:
#
#     SDK install failed. It needs git on PATH
#
# git is checked at the top of that script, so by the time this fires it is
# provably present. The operator was told to install something they had.


def install_bat() -> str:
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    return (root / "install.bat").read_text(encoding="utf-8")


def sdk_section() -> str:
    text = install_bat()
    return text[text.index("Installing the BULK SDK"):text.index("Installing dependencies")]


def test_the_sdk_fetch_is_retried():
    sdk = sdk_section()
    assert "for /L" in sdk, "it still gives up on the first try"
    loop = sdk[sdk.index("for /L"):]
    # Counting the guard was the old check, and it broke the moment the
    # section grew a second way to install. What it was standing in for is
    # this: the guard has to come before the attempt, or a loop that has
    # already succeeded runs the install twice more.
    assert "if not defined SDK_OK" in loop, "no early exit from the loop"
    assert loop.index("if not defined SDK_OK") < loop.index("pip install"), \
        "the guard falls after the attempt, so success is retried anyway"


def test_the_sdk_comes_from_the_folder_before_the_network():
    """github.com is unreachable from some of the networks this is handed out
    on -- three pip clones in a row failed at 21 seconds each while PyPI
    answered throughout. Shipping the wheel only helps if it is tried first."""
    sdk = sdk_section()
    assert sdk.index("SDK_WHEEL") < sdk.index("git+https://github.com/Bulk-trade"), \
        "GitHub is tried before the copy that ships with the bot"
    assert "--force-reinstall" in sdk, (
        "the SDK's version string does not change between commits, so without "
        "this pip keeps whatever an earlier run installed"
    )


def test_the_pinned_wheel_is_where_the_installer_says_it_is():
    """A version bump that edits the pin and forgets to commit the file would
    otherwise be found by the first operator to install, not here."""
    import pathlib
    import re

    match = re.search(r'set "SDK_WHEEL=([^"]+)"', install_bat())
    assert match, "the installer no longer pins a wheel by name"
    root = pathlib.Path(__file__).resolve().parents[2]
    wheel = root / match.group(1).replace("\\", "/")
    assert wheel.is_file(), f"{match.group(1)} is pinned but not present"
    assert wheel.stat().st_size > 1024, "the wheel is there but empty"


def test_the_dependency_install_is_retried_too():
    """PyPI is a network too."""
    text = install_bat()
    deps = text[text.index("Installing dependencies"):]
    assert "for /L" in deps
    assert "DEPS_OK" in deps


def test_it_no_longer_blames_a_missing_git():
    """It is checked at the top of the same script, so it cannot be the cause
    down here."""
    text = install_bat()
    top = text.index("git is not installed")
    sdk_failure = text.index("Could not install the BULK SDK")
    assert top < sdk_failure, "the up-front check moved"
    after = text[sdk_failure:sdk_failure + 600]
    assert "git-scm.com" not in after, "it still sends them to install git"
    assert "connection" in after, "it does not name the likely cause"


def test_delayed_expansion_is_on():
    """The retry flags are set inside a block and read in the same one."""
    text = install_bat()
    assert "setlocal enabledelayedexpansion" in text


# -- both scripts can go through the bot's own proxy -------------------------
#
# GitHub is unreachable from the networks proxy.local exists for: measured on
# one of them, four direct attempts to github.com failed while the exchange
# answered in 0.8s. An update that cannot fetch leaves the operator as stuck as
# a bot that cannot trade, and they have already configured a proxy that works.


def test_both_scripts_read_the_proxy_the_bot_uses():
    """Not a second place to configure it. There is already one file."""
    for text in (update_bat(), install_bat()):
        assert 'if exist "proxy.local"' in text
        assert "BOT_PROXY" in text


def test_the_proxy_line_is_never_echoed():
    """It usually carries a password."""
    import re

    for text in (update_bat(), install_bat()):
        for line in text.splitlines():
            if line.strip().lower().startswith("echo") and "PROXY" in line.upper():
                assert not re.search(r"![A-Z_]*PROXY[A-Z_]*!", line), line


def test_comments_are_skipped_when_reading_it():
    """The shipped file is all comments, and reading one as an address would
    point every fetch at a sentence."""
    for text in (update_bat(), install_bat()):
        assert '"!LINE:~0,1!"=="#"' in text


def test_git_gets_the_address_as_written():
    """It works through SOCKS and aborts the CONNECT on an HTTP proxy."""
    for text in (update_bat(), install_bat()):
        assert 'set "ALL_PROXY=!BOT_PROXY!"' in text


def test_pip_gets_an_http_address_instead():
    """Any socks address makes pip's vendored urllib3 raise PoolKey.__new__()
    got an unexpected keyword argument key_proxy_ssl_context."""
    text = install_bat()
    assert 'if /I "!BOT_PROXY:~0,5!"=="socks" set "PIP_PROXY=http://!BOT_PROXY:*//=!"' in text
    assert "--proxy !PIP_PROXY!" in text


def test_every_pip_call_is_given_it():
    """One left out is one that cannot reach PyPI on such a network."""
    text = install_bat()
    calls = text.count('-m pip install')
    assert calls >= 3
    assert text.count("!PIPARG!") == calls, "a pip call was left without the proxy"


def test_no_proxy_configured_passes_nothing():
    """The shipped file configures none, which is the normal case, and an
    empty --proxy is not a thing pip accepts."""
    text = install_bat()
    assert 'set "PIPARG="' in text
    assert "if defined BOT_PROXY (" in text

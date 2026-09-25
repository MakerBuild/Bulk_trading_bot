"""Running on Linux: the scripts, the stop key, and the names in messages.

The bot ran and was tested on Windows only. The shell scripts are its Linux
launchers, and a script that breaks there breaks before anything else can
say why -- so their shape is checked here on every platform, and what can only
be exercised on Linux is exercised there.
"""

import asyncio
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

from bulkdn import console, scripts

ROOT = pathlib.Path(__file__).resolve().parents[2]
SHELL_SCRIPTS = ["install.sh", "run.sh", "update.sh", "service.sh"]


# -- the scripts themselves -------------------------------------------------------


@pytest.mark.parametrize("name", SHELL_SCRIPTS)
def test_every_script_is_shipped_with_lf_and_a_shebang(name):
    """A carriage return makes bash read `then\\r`, and the script dies on its
    first line with an error that shows nothing wrong."""
    data = (ROOT / name).read_bytes()
    assert data.startswith(b"#!/usr/bin/env bash\n")
    assert b"\r" not in data, f"{name} has CRLF line endings"


@pytest.mark.parametrize("name", SHELL_SCRIPTS)
def test_every_script_parses(name):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash here")
    result = subprocess.run(
        [bash, "-n", str(ROOT / name)], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", ["install.sh", "update.sh", "service.sh"])
def test_scripts_that_may_be_replaced_while_running_are_read_whole(name):
    """update.sh replaces files, itself included, and bash reads a script as
    it goes. Everything inside one function is read before any of it runs."""
    text = (ROOT / name).read_text(encoding="utf-8")
    assert re.search(r"^main\(\) \{$", text, re.M)
    assert text.rstrip().endswith('main "$@"\nexit $?')


def test_scripts_are_executable_in_git():
    """Committed from Windows, a new file is 100644 -- and `./run.sh` then
    answers "Permission denied" on the server."""
    if not (ROOT / ".git").exists() or shutil.which("git") is None:
        pytest.skip("not a git checkout")
    listing = subprocess.run(
        ["git", "ls-files", "-s", *SHELL_SCRIPTS], cwd=ROOT, capture_output=True, text=True,
        check=False,
    ).stdout
    modes = {line.split()[3]: line.split()[0] for line in listing.splitlines()}
    if not modes:
        pytest.skip("scripts not committed yet")
    assert modes == dict.fromkeys(SHELL_SCRIPTS, "100755")


def test_git_keeps_shell_scripts_lf_on_every_checkout():
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"^\*\.sh\s+text\s+eol=lf$", attributes, re.M)


def test_both_installers_install_the_same_sdk():
    """The two are kept by hand; the SDK they install must not drift apart."""
    bat = (ROOT / "install.bat").read_text(encoding="utf-8")
    sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    wheel = re.search(r"app\\vendor\\(bulk_client-[^\"]+\.whl)", bat).group(1)
    commit = re.search(r"bulk-client\.git@([0-9a-f]+)", bat).group(1)
    assert f"app/vendor/{wheel}" in sh
    assert f"bulk-client.git@{commit}" in sh
    assert "app/docs/requirements.txt" in sh


def test_the_installer_writes_the_bots_own_templates():
    """Rather than a second copy of their text, to drift out of step."""
    sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "PRIVATE_KEY_TEMPLATE" in sh and "PROXY_TEMPLATE" in sh


def test_the_service_only_listens():
    """It runs the Telegram listener; a run starts when someone asks for it."""
    sh = (ROOT / "service.sh").read_text(encoding="utf-8")
    exec_line = re.search(r"^ExecStart=(.*)$", sh, re.M).group(1)
    assert exec_line.split()[1:2] == ["telegram"]
    assert "RestartPreventExitStatus=1" in sh, "a setup error would restart forever"
    assert "KillSignal=SIGINT" in sh, "a stop would cut a run off mid-order"


# -- names in messages ------------------------------------------------------------------


def test_messages_name_the_script_that_exists(monkeypatch):
    monkeypatch.setattr(scripts, "WINDOWS", True)
    assert scripts.script("install") == "install.bat"
    assert scripts.venv_python() == r"app\.venv\Scripts\python.exe"
    monkeypatch.setattr(scripts, "WINDOWS", False)
    assert scripts.script("install") == "./install.sh"
    assert scripts.venv_python() == "app/.venv/bin/python"


# -- the stop key on a real terminal ---------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="a pseudo-terminal needs POSIX")
async def test_s_stops_on_a_linux_terminal_and_the_terminal_is_put_back(monkeypatch):
    import pty
    import termios

    master, slave = pty.openpty()
    terminal = os.fdopen(slave, "r")
    before = termios.tcgetattr(slave)
    monkeypatch.setattr(sys, "stdin", terminal)
    try:
        asked = []
        watching = asyncio.create_task(console.watch_for_stop(asked.append, poll_s=0.01))
        await asyncio.sleep(0.05)
        os.write(master, "ы".encode())            # the S key on a Russian layout
        await asyncio.wait_for(watching, timeout=2)

        assert asked == ["keyboard"]
        assert termios.tcgetattr(slave) == before, "the terminal was left in cbreak mode"
    finally:
        terminal.close()
        os.close(master)


@pytest.mark.skipif(sys.platform == "win32", reason="a pseudo-terminal needs POSIX")
async def test_arrow_keys_are_not_letters(monkeypatch):
    import pty

    master, slave = pty.openpty()
    terminal = os.fdopen(slave, "r")
    monkeypatch.setattr(sys, "stdin", terminal)
    try:
        read = console._make_reader()
        os.write(master, b"\x1b[A")                # up arrow
        await asyncio.sleep(0.05)
        assert read() is None
        read.close()
    finally:
        terminal.close()
        os.close(master)

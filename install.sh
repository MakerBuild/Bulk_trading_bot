#!/usr/bin/env bash
# First-time setup on Linux (Ubuntu 24.04 and the like): builds the virtualenv
# and installs everything. Safe to re-run; it upgrades an existing install in
# place. The Linux twin of install.bat -- the comments there say why each step
# is the way it is; the ones here are only about what differs.
#
# Wrapped in a function and run from the last line, so bash has read the whole
# file before any of it executes. update.sh calls this right after replacing
# it, and bash otherwise reads a script as it goes.

main() {
    cd "$(dirname "$0")" || exit 1

    say() { printf '  %s\n' "$@"; }
    fail() { echo; say "$@"; echo; exit 1; }
    suitable() {
        "$@" -c 'import sys, struct; sys.exit(0 if sys.version_info >= (3, 12) and struct.calcsize("P") == 8 else 1)' >/dev/null 2>&1
    }

    local venv_py="app/.venv/bin/python"
    # The bot's own package answers the questions below; see bulkdn/scripts.py.
    export PYTHONPATH="$PWD/app"

    # Not while the bot is running from this folder: the steps below reinstall
    # the libraries it has loaded. bulkdn/lock.py exits 3 when it holds its lock.
    if [ -x "$venv_py" ]; then
        "$venv_py" -m bulkdn.lock
        if [ $? -eq 3 ]; then
            fail "Stop the bot first, then run this again. Nothing was changed." \
                 "If it runs as a service:  sudo ./service.sh stop"
        fi
    fi

    # The Python that builds the virtualenv, when there is none yet. Once
    # app/.venv exists every script runs the one inside it.
    local py_new=""
    if [ ! -x "$venv_py" ]; then
        for candidate in python3 python3.14 python3.13 python3.12 python; do
            if command -v "$candidate" >/dev/null 2>&1 && suitable "$candidate"; then
                py_new="$candidate"
                break
            fi
        done
        if [ -z "$py_new" ]; then
            fail "This needs 64-bit Python 3.12 or newer." \
                 "On Ubuntu 24.04 it is already there; on anything older:" \
                 "" \
                 "    sudo apt update && sudo apt install -y python3 python3-venv"
        fi
    fi

    command -v git >/dev/null 2>&1 || fail \
        "git is not installed. It is needed to fetch updates:" \
        "" \
        "    sudo apt update && sudo apt install -y git"

    if [ ! -x "$venv_py" ]; then
        echo
        say "Creating the virtualenv (with $py_new)..."
        if ! "$py_new" -m venv app/.venv; then
            # A failed attempt leaves a half-built folder that the next run
            # would take for a finished one.
            rm -rf app/.venv
            fail "Could not create app/.venv -- see the error above." \
                 "On Ubuntu and Debian the venv module is a separate package:" \
                 "" \
                 "    sudo apt install -y python3-venv" \
                 "" \
                 "then run ./install.sh again."
        fi
    fi

    # proxy.local, read by the bot's own reader -- the same address the bot
    # uses, in the form pip accepts for pip (see install.bat).
    local bot_proxy="" pip_proxy=""
    local -a pip_arg=()
    if [ -f proxy.local ]; then
        bot_proxy="$("$venv_py" -m bulkdn.scripts proxy)" || fail \
            "proxy.local cannot be used -- the reason is above. Fix it, or empty it."
        pip_proxy="$("$venv_py" -m bulkdn.scripts pip-proxy 2>/dev/null)"
    fi
    if [ -n "$bot_proxy" ]; then
        echo
        say "Using the proxy from proxy.local."
        export ALL_PROXY="$bot_proxy"
        pip_arg=(--proxy "$pip_proxy")
    fi

    echo
    say "Updating pip..."
    if ! "$venv_py" -m pip install "${pip_arg[@]}" --quiet --upgrade pip; then
        say "Could not update pip. Carrying on with the one the virtualenv came" \
            "with, which is normally fine."
    fi

    local sdk_wheel="app/vendor/bulk_client-0.1.2+bulkdn.1-py3-none-any.whl"
    local sdk_ok=""
    if [ -f "$sdk_wheel" ]; then
        # Only when the wheel changed -- forcing it every time replaced the SDK
        # under a running bot on every update. See install.bat.
        if "$venv_py" -m bulkdn.scripts sdk-current "$sdk_wheel" >/dev/null 2>&1; then
            say "The BULK SDK is up to date."
            sdk_ok=1
        else
            say "Installing the BULK SDK..."
            if "$venv_py" -m pip install "${pip_arg[@]}" --quiet --no-deps --force-reinstall "$sdk_wheel"; then
                sdk_ok=1
                "$venv_py" -m bulkdn.scripts sdk-stamp "$sdk_wheel"
            else
                say "The bundled copy would not install. Trying GitHub instead."
            fi
        fi
    fi
    if [ -z "$sdk_ok" ]; then
        say "Installing the BULK SDK (from GitHub -- the PyPI build cannot sign)..."
        for attempt in 1 2 3; do
            if [ "$attempt" -gt 1 ]; then
                say "That did not come through. Trying again ($attempt of 3)..."
                sleep 2
            fi
            if "$venv_py" -m pip install "${pip_arg[@]}" --quiet --no-deps \
                "bulk-client @ git+https://github.com/Bulk-trade/bulk-client.git@3a6506e#subdirectory=crates/api-python"; then
                sdk_ok=1
                break
            fi
        done
    fi
    [ -n "$sdk_ok" ] || fail "Could not install the BULK SDK -- see the error above." \
        "A connection or timeout message there is the whole problem." \
        "Check the network and run ./install.sh again."

    say "Installing dependencies..."
    say "(pip will report bulk-keychain and solders as missing. That is expected:" \
        " the SDK declares them and never imports them. The signing check at the" \
        " end is what tells you the install works.)"
    local deps_ok=""
    for attempt in 1 2 3; do
        if [ "$attempt" -gt 1 ]; then
            say "That did not come through. Trying again ($attempt of 3)..."
            sleep 2
        fi
        if "$venv_py" -m pip install "${pip_arg[@]}" --quiet -r app/docs/requirements.txt; then
            deps_ok=1
            break
        fi
    done
    [ -n "$deps_ok" ] || fail "Could not install the dependencies after three tries -- see the" \
        "error above. A connection or timeout message there means the" \
        "download failed; run ./install.sh again."

    if [ ! -f settings.yaml ]; then
        cp app/settings.default.yaml settings.yaml
        say "Created settings.yaml from the defaults -- edit that one."
    fi

    # The two templates are written from the constants the bot itself uses,
    # rather than a second copy of their text here to drift out of step.
    "$venv_py" -m bulkdn.scripts templates \
        || fail "Could not write the key and proxy templates -- see the error above."

    # Owner-only. The key, the proxy's password and the Telegram token are in
    # these, and on a server other accounts may exist.
    chmod 600 private_key.local proxy.local settings.yaml 2>/dev/null
    chmod +x run.sh update.sh service.sh 2>/dev/null

    echo
    # A real check: a test transaction signed, its domain byte checked, the
    # signature verified. It used to import one name and call that OK.
    if ! "$venv_py" -m bulkdn.scripts signing-check; then
        fail "WARNING: the SDK cannot sign. Do not trade with this install."
    fi

    echo
    say "Done. Next:"
    say "  1. Put your base58 private key on one line in private_key.local"
    say "  2. Edit settings.yaml -- the only settings file you touch"
    say "  3. Run ./run.sh"
    echo
    return 0
}

main "$@"
exit $?

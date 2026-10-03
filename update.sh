#!/usr/bin/env bash
# Update the bot to the latest published version, then refresh its
# dependencies. The Linux twin of update.bat.
#
# Works whether this folder was cloned or unzipped. A clone fetches and
# fast-forwards; an unzipped copy is first turned into a clone in place, then
# is one. Your own files -- settings.yaml, private_key.local, proxy.local,
# app/state, logs.txt -- are not part of the published code, so there is
# nothing to overwrite them with.
#
# Wrapped in a function and run from the last line: this script may replace
# itself as it updates, and bash reads a script as it goes -- a function is
# read whole before any of it runs.

REPO="https://github.com/MakerBuild/Bulk_trading_bot.git"

main() {
    cd "$(dirname "$0")" || exit 1

    say() { printf '  %s\n' "$@"; }
    fail() { echo; say "$@"; echo; exit 1; }

    command -v git >/dev/null 2>&1 || fail \
        "git is not installed:" "" "    sudo apt update && sudo apt install -y git"

    # The bot's own package answers the questions below; see bulkdn/scripts.py.
    export PYTHONPATH="$PWD/app"
    local tool_py=""
    if [ -x app/.venv/bin/python ]; then
        tool_py="app/.venv/bin/python"
    elif command -v python3 >/dev/null 2>&1; then
        tool_py="python3"
    fi

    # Not while the bot is running from this folder: an update replaces the
    # code under it, and install.sh the libraries it has loaded. bulkdn/lock.py
    # exits 3 when the bot holds its lock.
    if [ -n "$tool_py" ]; then
        "$tool_py" -m bulkdn.lock
        if [ $? -eq 3 ]; then
            fail "Stop the bot first, then run this again. Nothing was changed." \
                 "If it runs as a service:  sudo ./service.sh stop   then update," \
                 "then:  sudo ./service.sh start"
        fi
    fi

    # proxy.local, read by the bot's own reader so git uses exactly the
    # address the bot does. This took the FIRST usable line where install.bat
    # took the last and the bot refused a file with two.
    local bot_proxy=""
    if [ -f proxy.local ] && [ -n "$tool_py" ]; then
        bot_proxy="$("$tool_py" -m bulkdn.scripts proxy)" || fail \
            "proxy.local cannot be used -- the reason is above. Fix it, or empty it."
    fi
    if [ -n "$bot_proxy" ]; then
        say "Using the proxy from proxy.local."
        export ALL_PROXY="$bot_proxy"
    fi

    if [ -d .git ]; then
        echo
        say "Fetching..."
        local fetched=""
        for attempt in 1 2 3; do
            if [ "$attempt" -gt 1 ]; then
                say "No answer. Trying again ($attempt of 3)..."
                sleep 2
            fi
            if git fetch --quiet origin; then
                fetched=1
                break
            fi
        done
        [ -n "$fetched" ] || fail \
            "Could not reach GitHub after three tries, so there is nothing to" \
            "update from. Nothing was changed. The bot itself is unaffected."

        if ! git merge --ff-only FETCH_HEAD; then
            local ahead behind
            ahead="$(git rev-list --count FETCH_HEAD..HEAD 2>/dev/null || echo 0)"
            behind="$(git rev-list --count HEAD..FETCH_HEAD 2>/dev/null || echo 0)"
            echo
            say "Downloaded, but could not apply it. Nothing was changed." \
                "" \
                "Your own files are never the cause: settings.yaml, your key," \
                "app/state and logs.txt are not tracked." ""
            if [ "$ahead" -gt 0 ] && [ "$behind" -gt 0 ]; then
                say "This copy has $ahead commit(s) of its own that GitHub does not have," \
                    "and GitHub has $behind new one(s). To keep yours on a branch and" \
                    "move this folder to the published version:" "" \
                    "    git branch my-changes" \
                    "    git reset --keep FETCH_HEAD" \
                    "    ./update.sh" ""
            fi
            if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
                say "These files that ship with the bot were edited by hand, or are" \
                    "new and in the way of the update:" ""
                git status --short
                echo
                say "To set your edits aside, update, and then put them back:" "" \
                    "    git stash push --include-untracked" \
                    "    ./update.sh" \
                    "    git stash pop" ""
            fi
            exit 1
        fi
    else
        # An unzipped copy becomes a clone in place -- see update.bat for why
        # the old download-and-copy-over did not work. `checkout -f` writes the
        # shipped files and leaves everything else alone; the operator's own
        # files are copied to update-backup first all the same.
        echo
        say "This copy was unzipped rather than cloned. Turning it into a clone," \
            "so this and every later update works the same way..."
        mkdir -p update-backup
        for name in settings.yaml private_key.local proxy.local settings.default.yaml; do
            [ -f "$name" ] && cp -p "$name" "update-backup/$name"
        done
        [ -d app/state ] && cp -rp app/state update-backup/
        say "Your settings, key and state are also copied to update-backup."

        git init --quiet || fail "git could not make this folder a clone."
        git remote remove origin >/dev/null 2>&1
        git remote add origin "$REPO"
        echo "/update-backup/" >> .git/info/exclude
        local fetched=""
        for attempt in 1 2 3; do
            if [ "$attempt" -gt 1 ]; then
                say "No answer. Trying again ($attempt of 3)..."
                sleep 2
            fi
            if git fetch --quiet origin main; then
                fetched=1
                break
            fi
        done
        [ -n "$fetched" ] || fail \
            "Could not reach GitHub after three tries. Your files were not changed."
        git checkout --quiet -f -B main FETCH_HEAD || fail \
            "Downloaded, but could not apply it -- see the message above." \
            "Your settings, key and state are in update-backup."
        git config branch.main.remote origin
        git config branch.main.merge refs/heads/main
        # Modules an unzipped copy kept after they were removed upstream.
        git clean -fdq -- app/bulkdn app/tests app/dev
        rm -f release.bat settings.default.yaml
    fi

    chmod +x install.sh run.sh update.sh service.sh 2>/dev/null
    echo
    say "Updating dependencies..."
    if ! ./install.sh; then
        fail "The code was updated, but installing its dependencies FAILED --" \
             "the reason is above. Fix what it says, then run ./install.sh again."
    fi
    echo
    say "Up to date. Your settings and key were left alone." \
        "If app/settings.default.yaml gained options you want, copy them across."
    echo
    return 0
}

main "$@"
exit $?

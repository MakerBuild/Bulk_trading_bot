#!/usr/bin/env bash
# Update the bot to the latest published version, then refresh its
# dependencies. The Linux twin of update.bat.
#
# Works whether this folder was cloned or unzipped. A clone pulls; an unzipped
# copy fetches a fresh one into a temp folder and copies it over. Your own
# files -- settings.yaml, private_key.local, proxy.local, app/state, logs.txt --
# are not part of the published code, so there is nothing to overwrite them
# with.
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

    local bot_proxy=""
    if [ -f proxy.local ]; then
        bot_proxy="$(grep -v '^[[:space:]]*#' proxy.local | grep -v '^[[:space:]]*$' | head -n 1 | tr -d '\r')"
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
        local tmpdir
        tmpdir="$(mktemp -d)" || fail "Could not make a temporary folder."
        echo
        say "Downloading the latest version..."
        local cloned=""
        for attempt in 1 2 3; do
            if [ "$attempt" -gt 1 ]; then
                say "No answer. Trying again ($attempt of 3)..."
                sleep 2
                rm -rf "$tmpdir" && mkdir -p "$tmpdir"
            fi
            if git clone --depth 1 --quiet "$REPO" "$tmpdir"; then
                cloned=1
                break
            fi
        done
        if [ -z "$cloned" ]; then
            rm -rf "$tmpdir"
            fail "Could not reach the repository. Check the connection, then try again."
        fi
        say "Installing it..."
        # Everything but git's own folder, over the top of this one. Files
        # that are not in the published code are left where they are.
        if ! (cd "$tmpdir" && tar --exclude=./.git -cf - .) | tar -xf -; then
            rm -rf "$tmpdir"
            fail "Copy failed."
        fi
        rm -rf "$tmpdir"
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

#!/bin/bash
# macOS: double-click this in Finder to install the bot in this folder, and
# again whenever there is a new version.
#
# The work is update.sh's: it turns an unzipped copy into a clone, fetches the
# latest published version, then runs install.sh. This adds only what a
# double-click needs:
#
#   * the download quarantine taken off the folder, so "Mac - Start.command"
#     opens without the dialog this one may have needed;
#   * in a freshly unzipped folder, the settings, keys and state of a bot
#     folder already on this Mac brought across, by dragging that folder in;
#   * the version in the archive installed when GitHub cannot be reached;
#   * the window kept open at the end, so the result can be read.
#
# Wrapped in a function and run from the last line: update.sh may replace this
# file as it updates, and bash reads a script as it goes.

say() { printf '  %s\n' "$@"; }
finish() {
    echo
    say "Press Enter to close this window."
    read -r _
    exit "$1"
}

# A dragged-in folder as Terminal types it: a backslash before each space, a
# space after it, sometimes in quotes.
unescape_path() {
    local path="$1"
    path="${path%"${path##*[![:space:]]}"}"
    path="${path#"${path%%[![:space:]]*}"}"
    path="${path#\'}"; path="${path%\'}"
    path="${path#\"}"; path="${path%\"}"
    path="${path//\\/}"
    [ "$path" = "/" ] || path="${path%/}"
    printf '%s' "$path"
}

bring_settings_across() {
    echo
    say "If the bot is already set up in another folder on this Mac, drag that" \
        "folder into this window and press Enter: its settings, keys and state" \
        "are copied here, and the old folder is left as it is." \
        "" \
        "Setting up for the first time? Just press Enter."
    echo
    local typed old
    read -r -p "  > " typed
    old="$(unescape_path "$typed")"
    [ -n "$old" ] || return 0

    if [ ! -d "$old" ]; then
        say "Not a folder: $old" "Nothing was copied."
        return 0
    fi
    if [ "$(cd "$old" && pwd -P)" = "$(pwd -P)" ]; then
        say "That is this folder. Nothing was copied."
        return 0
    fi
    if [ ! -f "$old/settings.yaml" ] && [ ! -f "$old/private_key.local" ]; then
        say "There is no settings.yaml or private_key.local in" "  $old" \
            "Nothing was copied."
        return 0
    fi
    # Two copies of the bot on one account each hedge the other's fills as
    # their own. bulkdn/lock.py exits 3 while the old one runs.
    if [ -x "$old/app/.venv/bin/python" ]; then
        (cd "$old" && PYTHONPATH="$PWD/app" app/.venv/bin/python -m bulkdn.lock)
        if [ $? -eq 3 ]; then
            echo
            say "The bot is running in that folder. Stop it there first (press S," \
                "close its positions, quit the menu), then double-click this again." \
                "Nothing was copied."
            finish 1
        fi
    fi

    local name copied=""
    for name in settings.yaml private_key.local proxy.local; do
        if [ -f "$old/$name" ] && cp -p "$old/$name" "$name"; then
            copied="$copied $name"
        fi
    done
    mkdir -p app/state
    for name in "$old"/app/state/*; do
        [ -e "$name" ] || continue
        # Only says who held the lock; the lock itself is the system's.
        [ "$(basename "$name")" = bot.lock ] && continue
        if cp -Rp "$name" app/state/; then
            copied="$copied app/state/$(basename "$name")"
        fi
    done
    echo
    say "Copied:$copied" \
        "" \
        "From now on run the bot from THIS folder only. Delete the old one once" \
        "this one works -- it holds a copy of your keys."
}

main() {
    cd "$(dirname "$0")" || exit 1

    # Python and git from python.org or Homebrew, for a shell profile that
    # does not add them. After what is already there, so a PATH that picks
    # one still does.
    export PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"

    # Everything that came in the download carries the quarantine flag, and
    # every script here would be stopped by the same dialog. This one is
    # already open.
    xattr -dr com.apple.quarantine . 2>/dev/null
    chmod +x install.sh run.sh update.sh service.sh ./*.command 2>/dev/null

    if [ ! -f settings.yaml ] && [ ! -f private_key.local ]; then
        bring_settings_across
    fi

    was_clone=""
    [ -d .git ] && was_clone=1

    ./update.sh
    status=$?

    # An unzipped copy that could not reach GitHub: update.sh made a .git
    # with nothing in it. Take that back and install the version in this
    # folder, which is a release too; the next update fetches the latest.
    if [ $status -ne 0 ] && [ -z "$was_clone" ] && [ -d .git ] \
            && ! git rev-parse --verify --quiet HEAD >/dev/null 2>&1 \
            && [ ! -x app/.venv/bin/python ]; then
        rm -rf .git
        echo
        say "Installing the version in this folder instead. Double-click this" \
            "again later to update it."
        ./install.sh
        status=$?
    fi

    if [ $status -eq 0 ]; then
        say "Next time, double-click this file again to update."
    fi
    finish $status
}

main "$@"
exit $?

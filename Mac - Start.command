#!/bin/bash
# macOS: double-click this in Finder to open the bot's menu.
#
# caffeinate keeps the Mac awake while the bot runs: a Mac that goes to sleep
# stops the bot mid-cycle, with a position open on one side. On battery it can
# still sleep, and closing the lid always sleeps it.

cd "$(dirname "$0")" || exit 1

say() { printf '  %s\n' "$@"; }

if [ ! -x app/.venv/bin/python ]; then
    echo
    say "Not installed yet. Double-click \"Mac - Install or Update.command\" first."
    echo
    say "Press Enter to close this window."
    read -r _
    exit 1
fi

caffeinate -is ./run.sh "$@"
status=$?

echo
say "The bot has exited. Press Enter to close this window."
read -r _
exit $status

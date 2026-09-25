#!/usr/bin/env bash
# Launch the bot with the project's own interpreter. The Linux twin of run.bat.
#
# Works from any directory and passes arguments through, so `./run.sh status`
# and `./run.sh telegram --live` behave like the CLI. With no arguments the
# menu opens.

cd "$(dirname "$0")" || exit 1

VENV_PY="app/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
    echo
    echo "  Not installed yet. Run ./install.sh first."
    echo
    exit 1
fi

# The package lives in app/, which is not where Python looks by default.
export PYTHONPATH="$PWD/app"
exec "$VENV_PY" -m bulkdn "$@"

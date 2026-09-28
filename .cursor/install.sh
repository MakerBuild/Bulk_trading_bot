#!/usr/bin/env bash
# Cloud Agent bootstrap. The Linux counterpart of install.bat: the shipped
# setup is Windows-only (.bat), so this reproduces it for the agent's Ubuntu
# host. Idempotent -- safe to re-run against a warm checkout or snapshot.
set -euo pipefail

# Project root is the parent of this .cursor/ directory, regardless of cwd.
cd "$(dirname "$0")/.."

# The stock python3 on the base image ships without the venv/ensurepip module,
# so `python3 -m venv` fails until python3-venv is present. This is the one
# system package the setup needs; everything else comes from pip.
if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
    echo "  Installing python3-venv..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3-venv
fi

# The bot expects its interpreter at app/.venv, the same path install.bat uses.
VENV="app/.venv"
if [ ! -x "$VENV/bin/python" ]; then
    echo "  Creating the virtualenv..."
    python3 -m venv "$VENV"
fi
PY="$VENV/bin/python"

echo "  Updating pip..."
"$PY" -m pip install --quiet --upgrade pip

# The BULK SDK ships with the bot as a local wheel (app/vendor). Installed with
# --no-deps because it declares bulk-keychain/solders, which it never imports
# and which have no wheels for current Pythons; its real deps follow below.
# --force-reinstall because the SDK's version string never changes between
# builds, so pip would otherwise keep a stale copy.
echo "  Installing the BULK SDK..."
"$PY" -m pip install --quiet --no-deps --force-reinstall \
    app/vendor/bulk_client-0.1.2-py3-none-any.whl

# The SDK's real dependencies plus the bot's own (PyYAML). pip will note
# bulk-keychain and solders as missing -- expected, they are declared and never
# imported. The signing check at the end is what proves the install works.
echo "  Installing dependencies..."
"$PY" -m pip install --quiet \
    pandas numpy numba websockets pynacl base58 sortedcontainers \
    aiohttp requests PyYAML certifi python-socks PySocks

# install.bat leaves these out because an operator does not need them, but a
# development environment does: run-tests.bat installs exactly this set.
echo "  Installing the test tools..."
"$PY" -m pip install --quiet pytest pytest-asyncio ruff

# The operator's settings are a copy of the shipped defaults, kept apart so an
# update stays a plain `git pull`. Mirrors install.bat.
if [ ! -f settings.yaml ]; then
    cp app/settings.default.yaml settings.yaml
    echo "  Created settings.yaml from the defaults."
fi

# Signing check: the SDK is only usable if it can build a signature domain.
# This is the same check install.bat ends on.
"$PY" -c "from bulk_api.common import SignatureDomain; print('  Signing check: OK')"

echo "  Done. Run the bot with: PYTHONPATH=app app/.venv/bin/python -m bulkdn --help"

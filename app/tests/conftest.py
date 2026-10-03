"""Shared test setup.

Only one thing lives here: the one-copy lock (bulkdn/lock.py) is taken by the
menu items and the CLI commands under test, and its real place is the bot's
own app/state. The suite gets a lock file of its own instead, so the suite
neither writes into the folder it runs from nor fails because the bot happens
to be running from the same checkout.
"""

import pytest

from bulkdn import lock


@pytest.fixture(scope="session")
def _lock_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("lock")


@pytest.fixture(autouse=True)
def _own_lock_file(_lock_dir, monkeypatch):
    # One folder for the session rather than tmp_path per test: creating a
    # fresh directory for each of fifteen hundred tests costs real time on
    # Windows, and the lock is let go after every use anyway.
    monkeypatch.setattr(lock, "LOCK_FILE", str(_lock_dir / "bot.lock"))

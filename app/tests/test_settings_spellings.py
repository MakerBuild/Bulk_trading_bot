"""Older spellings keep working, say what to rename, and say it once.

The shipped settings used the oldest shape there is -- a `legs:` mapping of two
blocks named after accounts that no longer choose anything -- while the menu
wrote the current `markets:` list. A file holding both read `markets:` and
ignored `legs:` without a word, so an edit to the ignored block did nothing.
And every alias the loader still accepts was accepted in silence, so nobody
ever learned the current name.
"""

import logging
import pathlib

import pytest

from bulkdn import referral
from bulkdn.config import ConfigError, load_config

SHIPPED = pathlib.Path(__file__).resolve().parents[1] / "settings.default.yaml"

MARKETS = "markets:\n  - symbol: BTC-USD\n    notional_usd: 100\n"
LEGS = "legs:\n  btc:\n    symbol: BTC-USD\n    notional_usd: 100\n"


def write(tmp_path, text, name="settings.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def warnings_from(caplog, path, **kwargs):
    with caplog.at_level(logging.WARNING, logger="bulkdn.config"):
        caplog.clear()
        load_config(path, require_credentials=False, **kwargs)
    return list(caplog.messages)


def test_the_shipped_file_uses_the_current_spelling():
    text = SHIPPED.read_text(encoding="utf-8")
    assert "\nmarkets:" in "\n" + text
    assert "\nlegs:" not in "\n" + text


def test_both_blocks_at_once_is_refused_rather_than_one_ignored(tmp_path):
    with pytest.raises(ConfigError, match="both `markets:` and `legs:`"):
        load_config(write(tmp_path, MARKETS + LEGS), require_credentials=False)


@pytest.mark.parametrize(
    "text,old,new",
    [
        (LEGS, "legs:", "markets:"),
        (MARKETS + "mode: pool\n", "mode: pool", "mode: multi"),
        (MARKETS + "cycles: 3\n", "cycles:", "execution_target:"),
        (MARKETS + "pool:\n  single_master: 1\n", "pool.single_master", "single_master:"),
        (MARKETS + "telegram:\n  bot_token: '1:x'\n  user_id: 5\n", "user_id", "user_ids"),
    ],
)
def test_an_old_spelling_still_loads_and_names_the_new_one(tmp_path, caplog, text, old, new):
    messages = warnings_from(caplog, write(tmp_path, text))
    assert any(old in m and new in m for m in messages), messages


def test_the_old_legs_block_still_trades_what_it_says(tmp_path):
    config = load_config(write(tmp_path, LEGS), require_credentials=False)
    assert [m.symbol for m in config.markets] == ["BTC-USD"]


def test_a_warning_is_said_once_per_version_of_the_file(tmp_path, caplog):
    """The menu re-reads the file for every action. Saying the same thing on
    every keypress would bury the one warning that matters under copies."""
    path = write(tmp_path, LEGS)
    assert warnings_from(caplog, path)
    assert warnings_from(caplog, path) == []
    # An edit is a new file, and gets its own say.
    pathlib.Path(path).write_text(LEGS + "mode: pool\n", encoding="utf-8")
    assert warnings_from(caplog, path)


def test_a_quiet_load_says_nothing(tmp_path, caplog):
    assert warnings_from(caplog, write(tmp_path, LEGS), warn=False) == []


# -- a sealed build does not read `access:` at all ---------------------------


ACCESS = MARKETS + "access:\n  require_referral: maybe\n"


def test_a_sealed_build_ignores_the_access_block_and_says_so(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(referral, "SEALED_WALLETS", ("owner",))
    messages = warnings_from(caplog, write(tmp_path, ACCESS))
    assert any("access" in m and "ignored" in m for m in messages), messages


def test_an_unsealed_build_still_checks_it(tmp_path, monkeypatch):
    monkeypatch.setattr(referral, "SEALED_WALLETS", ())
    monkeypatch.setattr(referral, "SEALED_CODES", ())
    with pytest.raises(ConfigError, match="access.require_referral"):
        load_config(write(tmp_path, ACCESS), require_credentials=False)

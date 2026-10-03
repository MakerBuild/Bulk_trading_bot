"""The menu's edits to settings.yaml: found where they belong, saved or not at all.

There were four line-walking writers with four ideas of what a `key:` line is.
The worst disagreement was silent: `execution_target:  # note` was not
recognised, so the target writer appended a second block, PyYAML kept the last
of the two, and every later edit landed in the first block and was ignored.
They now share one matcher and one save, which checks the result with the same
loader the bot starts with before it replaces the file.
"""

import pathlib

import pytest

from bulkdn import menu
from bulkdn.config import ConfigError, ExecutionTarget, load_config

MARKETS = """\
markets:
  - symbol: BTC-USD
    notional_usd: 100
  - symbol: ETH-USD
    notional_usd: 100
"""


def write(tmp_path, text, newline="\n"):
    path = tmp_path / "settings.yaml"
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return path


def load(path):
    return load_config(str(path), require_credentials=False)


# -- the execution target ---------------------------------------------------


def test_a_commented_target_block_is_edited_not_duplicated(tmp_path):
    path = write(tmp_path, MARKETS + "execution_target:   # when to stop\n  burn_usd: 3\n")

    menu._write_target(str(path), "burn_usd", 7.0, ExecutionTarget())
    menu._write_target(str(path), "burn_usd", 9.0, ExecutionTarget())

    body = path.read_text(encoding="utf-8")
    assert body.count("execution_target:") == 1, "a second block was appended"
    assert load(path).target.burn_usd == 9.0, "the edit did not take"
    assert "# when to stop" in body


def test_a_value_keeps_its_comment(tmp_path):
    path = write(tmp_path, MARKETS + "execution_target:\n  cycles: 0   # groups\n")
    menu._write_target(str(path), "cycles", 4, ExecutionTarget())
    assert "  cycles: 4  # groups" in path.read_text(encoding="utf-8").splitlines()


def test_a_top_level_value_keeps_its_comment_too(tmp_path):
    path = write(tmp_path, "mode: multi   # who trades\n" + MARKETS)
    menu._write_mode(str(path), "single")
    assert "mode: single  # who trades" in path.read_text(encoding="utf-8").splitlines()


def test_a_target_written_on_one_line_is_refused_not_mangled(tmp_path):
    original = MARKETS + "execution_target: {cycles: 0, burn_usd: 3}\n"
    path = write(tmp_path, original)
    with pytest.raises(ConfigError, match="one line"):
        menu._write_target(str(path), "burn_usd", 5.0, ExecutionTarget())
    assert path.read_text(encoding="utf-8") == original


# -- the file is never left half-written or unreadable -------------------------


def test_an_edit_that_would_not_load_is_not_saved(tmp_path):
    original = MARKETS
    path = write(tmp_path, original)
    with pytest.raises(ConfigError, match="not saved"):
        menu._write_mode(str(path), "sideways")
    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.iterdir()) == [path], "a temporary file was left behind"


def test_a_file_that_already_has_a_key_twice_is_refused(tmp_path):
    original = MARKETS + "mode: multi\nmode: single\n"
    path = write(tmp_path, original)
    with pytest.raises(ConfigError, match="mode"):
        menu._write_mode(str(path), "single")
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("newline", ["\r\n", "\n"])
def test_line_endings_are_kept_as_the_file_had_them(tmp_path, newline):
    path = write(tmp_path, MARKETS + "execution_target:\n  burn_usd: 3\n", newline)
    menu._write_target(str(path), "cycles", 2, ExecutionTarget())
    menu._write_market_enabled(str(path), "ETH-USD", False)
    data = path.read_bytes()
    lines = data.count(b"\n")
    assert data.count(b"\r\n") == (lines if newline == "\r\n" else 0)


def test_a_byte_order_mark_does_not_hide_the_first_line(tmp_path):
    """Notepad can save UTF-8 with a BOM, which sits in front of the first key."""
    path = tmp_path / "settings.yaml"
    path.write_bytes(b"\xef\xbb\xbf" + ("mode: multi\n" + MARKETS).encode("utf-8"))
    menu._write_mode(str(path), "single")
    data = path.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")
    assert data.count(b"mode:") == 1
    assert load(path).mode == "single"


# -- finding a market ---------------------------------------------------------


def test_a_symbol_outside_the_markets_block_is_not_a_market(tmp_path):
    text = (
        "notes:\n  - symbol: ETH-USD\n"  # somebody's own block, not a market
        + MARKETS
    )
    lines = text.splitlines()
    start, _end = menu._block_at(lines, "ETH-USD")
    assert start > lines.index("markets:")


def test_a_market_whose_symbol_is_not_its_first_line_is_found_whole(tmp_path):
    path = write(
        tmp_path,
        "markets:\n"
        "  - symbol: BTC-USD\n    notional_usd: 100\n"
        "  - enabled: true\n    notional_usd: 100\n    symbol: ETH-USD\n",
    )
    menu._write_market_enabled(str(path), "ETH-USD", False)
    body = path.read_text(encoding="utf-8")
    assert body.count("enabled:") == 1, "a second `enabled:` was added to the entry"
    assert [m.enabled for m in load(path).markets] == [True, False]


def test_a_new_market_arrives_switched_off_in_one_write(tmp_path):
    """Written on and then switched off, it was briefly a market that trades."""
    path = write(tmp_path, MARKETS)
    template = load(path).markets[0]
    menu._append_market(str(path), "SOL-USD", template, enabled=False)
    sol = load(path).legs["SOL-USD"]
    assert sol.enabled is False


def test_writes_go_through_one_save():
    """A writer opening the file itself is how the four drifted apart."""
    source = pathlib.Path(menu.__file__).read_text(encoding="utf-8")
    assert source.count('open(config_path, "w"') == 0
    assert source.count("_save_settings(config_path") >= 4

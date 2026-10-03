"""One copy of every default, and the shipped file agreeing with it.

The defaults were written three times -- the dataclasses, the loader and
app/settings.default.yaml -- and had drifted apart: `cycles` was 1 in the
dataclass and 0 in the loader, `hold_minutes` five minutes in code against the
shipped 0.5-1.5, `burn_usd` 0 against 3. Which one applied depended on whether
a Config came from a file or was built in code. The dataclasses are now the
only copy, the loader reads them, and the shipped file is held to them here --
it is what every installed copy started from, so it is what "the default"
means to everyone running the bot.
"""

import dataclasses
import pathlib

import pytest
import yaml

from bulkdn.config import Config, ExecutionTarget, LegConfig, load_config

SHIPPED = pathlib.Path(__file__).resolve().parents[1] / "settings.default.yaml"

# What a file says about its markets is the operator's choice, not a default,
# and the keys are filled in at load time.
NOT_DEFAULTS = {"markets", "market_names", "private_keys", "private_key"}


def markets_only(tmp_path) -> Config:
    """The shipped file's markets and nothing else, so every other setting
    falls through to the loader's defaults."""
    raw = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    kept = {key: raw[key] for key in ("markets", "legs") if key in raw}
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(kept), encoding="utf-8")
    return load_config(str(path), require_credentials=False)


def comparable(config: Config) -> dict:
    return {
        f.name: getattr(config, f.name)
        for f in dataclasses.fields(Config)
        if f.name not in NOT_DEFAULTS
    }


def test_the_shipped_file_says_what_the_code_defaults_to(tmp_path):
    shipped = load_config(str(SHIPPED), require_credentials=False)
    assert comparable(shipped) == comparable(markets_only(tmp_path))


def test_the_loader_takes_its_defaults_from_the_dataclasses(tmp_path):
    loaded = markets_only(tmp_path)
    built = Config(markets=loaded.markets)
    assert comparable(loaded) == comparable(built)


def test_a_market_left_unsaid_gets_the_dataclass_defaults(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("markets:\n  - symbol: BTC-USD\n    notional_usd: 100\n", encoding="utf-8")
    loaded = load_config(str(path), require_credentials=False).markets[0]
    default = LegConfig(symbol="BTC-USD")
    for name in ("max_distance_bps", "chase_patience_s", "improve_ticks",
                 "join_depth_usd", "offset_bps", "leverage", "enabled"):
        assert getattr(loaded, name) == getattr(default, name), name


def test_cycles_is_the_execution_target_under_its_older_name():
    config = Config(markets=[], target=ExecutionTarget(cycles=7))
    assert config.cycles == 7
    config.target.cycles = 2
    assert config.cycles == 2, "the two can no longer disagree"


def test_cycles_cannot_be_set_on_its_own():
    """It used to be a field the menu had to remember to set as well."""
    config = Config(markets=[])
    with pytest.raises(AttributeError):
        config.cycles = 3

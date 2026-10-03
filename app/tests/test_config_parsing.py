"""Every value in the settings file goes through the file's own readers.

config.py says so at the top of its `_as_*` helpers, and four places did not:
Telegram ids went through a bare `int()`, which truncates `12.7` to 12 and
reads `true` as user 1; `hold_minutes: null` was an error where every other
null means "the default"; a range was split on its first `-`, so `-5` was
reported as "not a number" rather than as negative; and the leverage bounds
were written out twice, once here and once beside the request that enforces
them.
"""

import pytest

from bulkdn import settings as leverage_settings
from bulkdn.config import (
    MAX_LEVERAGE,
    MIN_LEVERAGE,
    ConfigError,
    HoldTime,
    LegConfig,
    Span,
    load_config,
)

BASE = """\
markets:
  - symbol: BTC-USD
    notional_usd: 100
{top}
"""


def load(tmp_path, top=""):
    path = tmp_path / "settings.yaml"
    path.write_text(BASE.format(top=top), encoding="utf-8")
    return load_config(str(path), require_credentials=False)


# -- telegram ids -----------------------------------------------------------


def test_a_fractional_telegram_id_is_refused_not_truncated(tmp_path):
    top = 'telegram:\n  bot_token: "1:x"\n  user_ids: [12.7]'
    with pytest.raises(ConfigError, match="telegram.user_ids"):
        load(tmp_path, top)


def test_true_is_not_a_telegram_id(tmp_path):
    top = 'telegram:\n  bot_token: "1:x"\n  user_ids: [true]'
    with pytest.raises(ConfigError, match="telegram.user_ids"):
        load(tmp_path, top)


def test_a_telegram_id_is_read_exactly_however_long_it_is(tmp_path):
    """Through a float, an id past 2**53 would come back one off."""
    big = 2**53 + 1
    top = f'telegram:\n  bot_token: "1:x"\n  user_ids: [{big}, "-1001234567890"]'
    assert load(tmp_path, top).telegram.user_ids == [big, -1001234567890]


# -- hold_minutes -----------------------------------------------------------


def test_an_empty_hold_minutes_is_the_default_like_every_other_empty_setting(tmp_path):
    from bulkdn.config import Config

    config = load(tmp_path, "hold_minutes:")
    assert config.hold_minutes == Config(markets=[]).hold_minutes


# -- ranges -----------------------------------------------------------------


@pytest.mark.parametrize("written", ["-5", "-5-10", -5])
def test_a_negative_number_is_called_negative(written):
    with pytest.raises(ConfigError, match=">= 0"):
        Span.parse(written, "offset_bps")


def test_a_number_in_scientific_notation_is_a_number_not_a_range():
    assert Span.parse("1e-3", "offset_bps") == Span(0.001, 0.001)


@pytest.mark.parametrize("written", ["nan", "inf", "1-inf", [0, float("nan")]])
def test_a_range_must_be_finite(written):
    with pytest.raises(ConfigError, match="finite"):
        Span.parse(written, "notional_usd")


def test_hold_time_still_names_itself():
    with pytest.raises(ConfigError, match="hold_minutes"):
        HoldTime.parse("-1")


# -- leverage ---------------------------------------------------------------


def test_the_leverage_bounds_are_written_once():
    assert leverage_settings.MIN_LEVERAGE is MIN_LEVERAGE
    assert leverage_settings.MAX_LEVERAGE is MAX_LEVERAGE


@pytest.mark.parametrize("value", [MIN_LEVERAGE - 0.5, MAX_LEVERAGE + 1])
def test_a_leverage_outside_the_bounds_is_refused_by_the_loader(value):
    with pytest.raises(ConfigError, match="leverage"):
        LegConfig(symbol="BTC-USD", notional_usd=100, leverage=value).validate("m")

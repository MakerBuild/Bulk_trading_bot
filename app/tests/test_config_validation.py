"""What the settings loader refuses, and what it warns about.

The operator edits settings.yaml by hand, in Notepad, often from a message
someone pasted. Every mistake that can make in it has to end in a sentence
naming the setting -- not a traceback, and above all not a silent reading of
the opposite of what was written: `enabled: "false"` used to switch a market
ON, because `bool("false")` is True.
"""

import logging

import pytest

from bulkdn.config import Config, ConfigError, LegConfig, RiskConfig, load_config

BASE = """\
mode: multi
markets:
  - symbol: BTC-USD
    notional_usd: 100
    max_order_notional_usd: 100
{market_extra}
risk:
  max_net_exposure_usd: 500
{risk_extra}
{top_extra}
"""


def settings(tmp_path, market_extra="", risk_extra="", top_extra="", text=None):
    path = tmp_path / "settings.yaml"
    body = text if text is not None else BASE.format(
        market_extra=market_extra, risk_extra=risk_extra, top_extra=top_extra
    )
    path.write_text(body, encoding="utf-8")
    return str(path)


def load(path):
    return load_config(path, require_credentials=False)


# -- booleans mean what they say --------------------------------------------


@pytest.mark.parametrize("word", ['"false"', "'no'", '"off"', '"0"', "false", "0"])
def test_a_quoted_false_switches_a_market_off(tmp_path, word):
    config_path = settings(
        tmp_path,
        market_extra=f"    enabled: {word}\n  - symbol: ETH-USD\n    notional_usd: 100",
    )
    config = load(config_path)
    assert config.markets[0].enabled is False, f"{word} read as on"


@pytest.mark.parametrize("word", ['"true"', '"yes"', '"On"', '"1"', "true"])
def test_a_quoted_true_is_true(tmp_path, word):
    config = load(settings(tmp_path, market_extra=f"    enabled: {word}"))
    assert config.markets[0].enabled is True


def test_a_word_that_is_neither_is_refused(tmp_path):
    with pytest.raises(ConfigError, match=r"markets\[0\]\.enabled must be true or false"):
        load(settings(tmp_path, market_extra='    enabled: "maybe"'))


def test_the_tls_switches_are_parsed_the_same_way(tmp_path):
    config = load(settings(tmp_path, top_extra='ws_ssl_auto_bypass: "false"'))
    assert config.ws_ssl_auto_bypass is False
    with pytest.raises(ConfigError, match="ws_insecure_ssl"):
        load(settings(tmp_path, top_extra='ws_insecure_ssl: "sure"'))


# -- numbers ----------------------------------------------------------------


def test_a_unit_suffix_names_the_setting_instead_of_a_traceback(tmp_path):
    with pytest.raises(ConfigError, match=r"markets\[0\]\.max_distance_bps"):
        load(settings(tmp_path, market_extra='    max_distance_bps: "5bps"'))


def test_a_fraction_where_a_count_belongs_is_refused_not_truncated(tmp_path):
    with pytest.raises(ConfigError, match=r"pool\.max_groups must be a whole number"):
        load(settings(tmp_path, top_extra="pool:\n  max_groups: 2.5"))


def test_a_whole_float_is_still_a_count(tmp_path):
    config = load(settings(tmp_path, top_extra="pool:\n  max_groups: 3.0"))
    assert config.max_groups == 3


def test_infinity_is_not_a_number_here(tmp_path):
    with pytest.raises(ConfigError, match="risk.max_position_usd"):
        load(settings(tmp_path, risk_extra="  max_position_usd: .inf"))


@pytest.mark.parametrize("key", ["ws_stale_timeout_s", "price_stale_timeout_s"])
def test_a_negative_stale_timeout_is_refused(tmp_path, key):
    with pytest.raises(ConfigError, match=f"risk.{key}"):
        load(settings(tmp_path, risk_extra=f"  {key}: -5"))


def test_a_negative_overlay_ttl_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="overlay_ttl_ms"):
        load(settings(tmp_path, top_extra="overlay_ttl_ms: -1"))


# -- the file itself --------------------------------------------------------


def test_a_file_that_is_not_a_mapping_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="top level"):
        load(settings(tmp_path, text="- just\n- a list\n"))


def test_a_notepad_cp1251_file_says_to_save_as_utf8(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_bytes("# рынки\nmode: multi\n".encode("cp1251"))
    with pytest.raises(ConfigError, match="UTF-8"):
        load(str(path))


def test_a_yaml_syntax_error_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not valid YAML"):
        load(settings(tmp_path, text="mode: [unclosed\n"))


def test_a_numeric_log_level_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="log_level"):
        load(settings(tmp_path, top_extra="log_level: 10"))


# -- typos are named --------------------------------------------------------


def test_a_misspelled_key_is_warned_about_with_the_nearest_real_one(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="bulkdn.config"):
        load(settings(tmp_path, market_extra="    chase_patiense_s: 1"))
    warned = " ".join(caplog.messages)
    assert "markets[0].chase_patiense_s" in warned
    assert "chase_patience_s" in warned.split("did you mean")[1]


@pytest.mark.parametrize(
    "extra, where",
    [
        ({"top_extra": "hold_minuts: 3"}, "hold_minuts"),
        ({"risk_extra": "  max_net_exposure: 3"}, "risk.max_net_exposure"),
        ({"top_extra": "pool:\n  max_group: 3"}, "pool.max_group"),
        ({"top_extra": "execution_target:\n  burn: 3"}, "execution_target.burn"),
    ],
)
def test_every_level_is_checked(tmp_path, caplog, extra, where):
    with caplog.at_level(logging.WARNING, logger="bulkdn.config"):
        load(settings(tmp_path, **extra))
    assert any(where in message for message in caplog.messages), caplog.messages


def test_a_retired_key_says_it_can_be_deleted(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="bulkdn.config"):
        load(settings(tmp_path, top_extra='sub1_pubkey: ""'))
    assert any("sub1_pubkey" in m and "deleted" in m for m in caplog.messages)


def test_the_shipped_settings_load_without_a_single_warning(caplog):
    import pathlib

    shipped = pathlib.Path(__file__).resolve().parents[1] / "settings.default.yaml"
    with caplog.at_level(logging.WARNING, logger="bulkdn.config"):
        config = load_config(str(shipped), require_credentials=False)
    assert caplog.messages == []
    assert config.ws_ssl_auto_bypass is False, "TLS bypass must ship switched off"


# -- the exposure limit and the order cap are a pair ------------------------


def test_an_exposure_limit_under_the_order_cap_is_warned_about(tmp_path, caplog):
    text = BASE.format(market_extra="", risk_extra="", top_extra="").replace(
        "max_net_exposure_usd: 500", "max_net_exposure_usd: 50"
    )
    with caplog.at_level(logging.WARNING, logger="bulkdn.config"):
        load(settings(tmp_path, text=text))
    assert any("max_net_exposure_usd" in m and "per-order" in m for m in caplog.messages)


# -- secrets stay out of a repr ---------------------------------------------


def test_a_config_repr_never_shows_the_keys():
    config = Config(
        markets=[LegConfig(symbol="BTC-USD", notional_usd=100.0)],
        risk=RiskConfig(),
        private_keys=["SECRET-KEY-ONE", "SECRET-KEY-TWO"],
    )
    config.telegram.bot_token = "123:SECRET-TOKEN"
    shown = repr(config)
    assert "SECRET" not in shown


def test_the_tls_bypass_defaults_off():
    assert Config.ws_ssl_auto_bypass is False
    assert Config.ws_insecure_ssl is False

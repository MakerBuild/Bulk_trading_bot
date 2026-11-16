"""Baskets in the start-up, the terminal menu and Telegram.

The exchange's market list and the accounts are made up here.
"""

import types

import pytest

from bulkdn import cli
from bulkdn import menu
from bulkdn import tg_settings
from bulkdn.config import Config, LegConfig, RiskConfig, Span, load_config
from bulkdn.sizing import InsufficientMargin

from test_telegram_control import button
from test_telegram_settings import Live, all_labels, settings_file  # noqa: F401 - fixture

LISTED = {
    "BTC-USD": {"symbol": "BTC-USD", "status": "TRADING", "maxLeverage": 50},
    "NVDA-USD": {"symbol": "NVDA-USD", "status": "TRADING", "maxLeverage": 10},
    "TSLA-USD": {"symbol": "TSLA-USD", "status": "TRADING", "maxLeverage": 5},
    "MU-USD": {"symbol": "MU-USD", "status": "SUSPENDED", "maxLeverage": 10},
}


def basket_config(tmp_path, **baskets):
    config = Config(
        markets=[LegConfig(symbol="NVDA-USD", notional_usd=5000.0, leverage=3)],
        risk=RiskConfig(), private_key="x", mode="baskets",
    )
    config.state_file = str(tmp_path / "state.json")
    for name, value in baskets.items():
        setattr(config.baskets, name, value)
    return config


# -- the run's markets and sizes ---------------------------------------------------


def test_the_runs_markets_are_the_kinds_tickers_all_one_size(tmp_path, monkeypatch):
    config = basket_config(tmp_path, notional_usd=Span(1000.0, 1000.0), leverage=8.0)
    monkeypatch.setattr(cli, "listed_markets", lambda _config: LISTED)
    cli.choose_auto_markets(config)
    legs = {leg.symbol: leg for leg in config.active_legs}
    assert sorted(legs) == ["NVDA-USD", "TSLA-USD"], "TradFi, and never a suspended one"
    assert legs["NVDA-USD"].notional_usd == 500.0, "a basket's own size, not the market's"
    assert legs["NVDA-USD"].leverage == 8.0
    assert legs["TSLA-USD"].leverage == 5.0, "cut to the market's own ceiling"


class Sizing:
    _size_baskets = cli.Runtime._size_baskets

    def __init__(self, config):
        self.config = config


def test_a_basket_too_big_for_the_thinnest_account_is_cut(tmp_path):
    config = basket_config(tmp_path, notional_usd=Span(1000.0, 3000.0))
    config.max_margin_fraction = 0.25
    legs = [LegConfig(symbol="NVDA-USD", leverage=10.0), LegConfig(symbol="TSLA-USD", leverage=4.0)]
    Sizing(config)._size_baskets(legs, {"m1": 2000.0, "m1s1": 1000.0})
    # 1000 * 0.25 * 4x, the lowest leverage among the basket's markets.
    assert (config.baskets.notional_usd.low, config.baskets.notional_usd.high) == (1000.0, 1000.0)


def test_an_account_with_no_margin_stops_the_start(tmp_path):
    config = basket_config(tmp_path)
    with pytest.raises(InsufficientMargin, match="baskets"):
        Sizing(config)._size_baskets([LegConfig(symbol="NVDA-USD", leverage=5.0)], {"m1": 0.0})


# -- the terminal menu --------------------------------------------------------------


def test_the_terminal_menu_offers_baskets_and_writes_its_kind(settings_file, monkeypatch):  # noqa: F811
    assert "baskets" in menu.MODE_CHOICES
    config = load_config(str(settings_file), require_credentials=False)
    monkeypatch.setattr(menu, "_ask", lambda _prompt: "3")
    assert menu._pick_auto_kind(config, str(settings_file), "baskets")
    again = load_config(str(settings_file), require_credentials=False)
    assert again.baskets.markets == "all"
    assert again.auto_spread.markets == config.auto_spread.markets, "auto-spread's kind is its own"


# -- Telegram -------------------------------------------------------------------------


def wired(path):
    h = Live(path)
    h.c.write_setting = menu.write_setting
    return h


async def test_telegram_saves_baskets_with_its_kind(settings_file):  # noqa: F811
    h = wired(settings_file)
    modes = await h.say("🎛 Mode")
    kinds = await h.c.press(button(modes, "baskets"))
    assert all_labels(kinds)[:3] == ["TradFi", "Crypto", "All"]
    assert "Basket Gauntlet" in kinds.text
    assert h.c.config.mode == "multi", "the mode was saved before a kind was chosen"

    saved = await h.c.press(button(kinds, "Crypto"))
    assert "Saved" in saved.text and "baskets (Crypto)" in saved.text
    again = load_config(str(settings_file), require_credentials=False)
    assert (again.mode, again.baskets.markets) == ("baskets", "crypto")
    assert "✅ Crypto" in all_labels(await h.c.press("setmode:baskets"))


async def test_the_settings_have_a_baskets_section(settings_file):  # noqa: F811
    h = Live(settings_file)
    section = await h.c.press("cfg:baskets")
    labels = all_labels(section)
    assert "Basket size $: 1000" in labels
    assert "Tickers per side: 1" in labels


def test_the_basket_settings_read_what_they_write():
    config = Config(markets=[], risk=RiskConfig(), private_key="x")
    config.baskets.per_side = 3
    assert tg_settings.shown(tg_settings.BY_ID["bside"].read(config)) == "3"
    assert tg_settings.parse(tg_settings.BY_ID["bsize"], "800-1200") == "800-1200"
    with pytest.raises(ValueError):
        tg_settings.parse(tg_settings.BY_ID["bside"], "1.5")


def test_the_status_line_needs_nothing_from_the_network():
    from bulkdn.strategy import Strategy

    obj = types.SimpleNamespace(
        config=types.SimpleNamespace(baskets=types.SimpleNamespace(markets="tradfi")),
        _gauntlet={"ok": 0, "refused": 0, "failed": 2},
        _basket_waiting="1 of 9 markets within 1bps, 2 needed with $500 of depth each",
        _basket_last=None,
        symbols=[],
    )
    line = Strategy._basket_status(obj)
    assert line.startswith("baskets TradFi: waiting -- 1 of 9 markets")
    assert "2 unanswered" in line

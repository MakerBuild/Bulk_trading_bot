"""Adding a market copies a size that means the same thing in the new one.

`size:` is a quantity of one coin, so it cannot be copied into another market.
It used to be copied as `notional_usd: 0` -- a market with no size, which the
next load refused, taking the whole bot down over a line the menu wrote. And
the template's leverage was copied past the new market's own ceiling, which
the next start refused as well.
"""

import builtins

from bulkdn import menu
from bulkdn.config import LegConfig, load_config

COIN_SIZED = """\
mode: multi
markets:
  - symbol: BTC-USD
    size: 0.001
    leverage: 40
    offset_bps: 2.0
    max_distance_bps: 5.0
    chase_patience_s: 3
    improve_ticks: 1
"""

DOLLAR_SIZED = """\
mode: multi
markets:
  - symbol: BTC-USD
    notional_usd: 100
    leverage: 40
    max_order_notional_usd: 100
"""


class StubConfig:
    http_url = "https://x/api/v1"

    def __init__(self, markets):
        self.markets = markets


def exchange(monkeypatch, entries):
    class Http:
        def get_exchange_info(self):
            return entries

    monkeypatch.setattr(menu, "_http", lambda _config: Http())


def pick(monkeypatch, answer="1"):
    monkeypatch.setattr(builtins, "input", lambda *_: answer)


def test_a_coin_sized_template_still_writes_a_loadable_market(monkeypatch, tmp_path, capsys):
    path = tmp_path / "settings.yaml"
    path.write_text(COIN_SIZED, encoding="utf-8")
    config = StubConfig([LegConfig(symbol="BTC-USD", size=0.001, leverage=40.0)])
    exchange(monkeypatch, [{"symbol": "BTC-USD"}, {"symbol": "ETH-USD", "minNotional": 50}])
    pick(monkeypatch)

    menu._add_market(config, str(path))

    loaded = load_config(str(path), require_credentials=False)
    eth = loaded.legs["ETH-USD"]
    assert eth.notional_usd >= 50, "wrote a size the market refuses, or none at all"
    assert eth.size == 0, "a BTC quantity was copied into ETH"
    assert eth.enabled is False, "a guessed size must not start trading on its own"
    assert "sized in coins" in capsys.readouterr().out


def test_leverage_is_clamped_to_the_new_markets_ceiling(monkeypatch, tmp_path, capsys):
    path = tmp_path / "settings.yaml"
    path.write_text(DOLLAR_SIZED, encoding="utf-8")
    config = StubConfig([LegConfig(
        symbol="BTC-USD", notional_usd=100.0, max_order_notional_usd=100.0, leverage=40.0,
    )])
    exchange(monkeypatch, [
        {"symbol": "BTC-USD", "maxLeverage": 50},
        {"symbol": "ETH-USD", "minNotional": 1, "maxLeverage": 25},
    ])
    pick(monkeypatch)

    menu._add_market(config, str(path))

    loaded = load_config(str(path), require_credentials=False)
    assert loaded.legs["ETH-USD"].leverage == 25.0
    assert "maximum of 25" in capsys.readouterr().out


def test_leverage_under_the_ceiling_is_copied_as_it_is(monkeypatch, tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(DOLLAR_SIZED, encoding="utf-8")
    config = StubConfig([LegConfig(
        symbol="BTC-USD", notional_usd=100.0, max_order_notional_usd=100.0, leverage=10.0,
    )])
    exchange(monkeypatch, [{"symbol": "ETH-USD", "minNotional": 1, "maxLeverage": 25}])
    pick(monkeypatch)

    menu._add_market(config, str(path))

    loaded = load_config(str(path), require_credentials=False)
    assert loaded.legs["ETH-USD"].leverage == 10.0
    assert loaded.legs["ETH-USD"].enabled is True


def test_a_dollar_market_is_preferred_over_a_coin_one_listed_first(monkeypatch, tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(
        COIN_SIZED + "  - symbol: SOL-USD\n    notional_usd: 250\n", encoding="utf-8"
    )
    config = StubConfig([
        LegConfig(symbol="BTC-USD", size=0.001),
        LegConfig(symbol="SOL-USD", notional_usd=250.0, max_order_notional_usd=250.0),
    ])
    exchange(monkeypatch, [{"symbol": "ETH-USD", "minNotional": 1}])
    pick(monkeypatch)

    menu._add_market(config, str(path))

    loaded = load_config(str(path), require_credentials=False)
    assert loaded.legs["ETH-USD"].notional_usd == 250.0
    assert "copied from SOL-USD" in path.read_text(encoding="utf-8")

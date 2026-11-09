"""🔀 Auto-spread tickers in the Telegram menu.

The same lists the terminal menu switches -- `auto_spread.tradfi:` and
`crypto:` in settings.yaml -- written by the same writer, against a real
settings file. The exchange's market list is made up here.
"""

import types

import pytest

from bulkdn import config as config_mod
from bulkdn import menu
from bulkdn.config import load_config

from test_telegram_settings import DEFAULT, Live, all_labels


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    monkeypatch.delenv(config_mod.PRIVATE_KEY_ENV, raising=False)
    path = tmp_path / "settings.yaml"
    path.write_bytes(DEFAULT.read_bytes())
    return path


LISTED = {
    symbol: {"symbol": symbol, "status": "TRADING"}
    for symbol in ("NVDA-USD", "TSLA-USD", "SPACEX-USD", "BTC-USD", "ETH-USD")
}


def files(listed=LISTED):
    def exchange_markets(_config):
        if isinstance(listed, Exception):
            raise listed
        return listed

    return types.SimpleNamespace(
        **{name: getattr(menu, name) for name in (
            "write_setting", "write_block_setting", "write_ticker_list", "shipped_tickers",
        )},
        exchange_markets=exchange_markets,
    )


def loaded(path):
    return load_config(str(path), require_credentials=False).auto_spread


def labels(reply):
    return set(all_labels(reply))


async def test_settings_and_the_auto_spread_screen_lead_to_it(settings_file):
    h = Live(settings_file, files=files())
    assert "🔀 Auto-spread tickers" in labels(await h.c.press("cfg:"))
    assert "🔀 Tickers" in labels(await h.c.press("setmode:auto-spread"))

    home = await h.c.press("tick:")
    assert {"TradFi: 9 on", "Crypto: 23 on"} <= labels(home)


async def test_a_ticker_is_switched_off_and_on_again(settings_file):
    h = Live(settings_file, files=files())
    screen = await h.c.press("tick:tradfi")
    assert "✅ SPACEX" in labels(screen)
    for row in screen.buttons:
        for _, data in row:
            assert len(data.encode()) <= 64

    off = await h.c.press("tickt:tradfi.SPACEX")
    assert "SPACEX switched off" in off.text and "⬜ SPACEX" in labels(off)
    assert "SPACEX" not in loaded(settings_file).tradfi
    assert "#   - SPACEX" in h.text
    assert "SPACEX" not in h.c.config.auto_spread.tradfi, "the controller kept the old list"

    on = await h.c.press("tickt:tradfi.SPACEX")
    assert "SPACEX switched on" in on.text
    assert "SPACEX" in loaded(settings_file).tradfi
    assert h.text.count("SPACEX") == 1


async def test_every_listed_takes_the_list_out_and_all_on_writes_it_back(settings_file):
    h = Live(settings_file, files=files())
    gone = await h.c.press("tickx:crypto")
    assert "every listed market" in gone.text
    assert loaded(settings_file).crypto is None
    assert loaded(settings_file).tradfi, "the other list went with it"

    await h.c.press("tickt:crypto.ETH")
    assert loaded(settings_file).crypto == ("BTC",), "only what the exchange lists is offered"

    await h.c.press("ticka:crypto")
    assert loaded(settings_file).crypto == ("BTC", "ETH")


async def test_the_last_one_on_stays_on(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(
        "markets:\n  - symbol: BTC-USD\n    notional_usd: 100\n"
        "auto_spread:\n  tradfi:\n    - NVDA\n",
        encoding="utf-8",
    )
    h = Live(path, files=files())
    before = h.text
    reply = await h.c.press("tickt:tradfi.NVDA")
    assert "last TradFi ticker" in reply.text
    assert h.text == before


async def test_a_ticker_no_longer_offered_redraws_the_list(settings_file):
    h = Live(settings_file, files=files())
    before = h.text
    reply = await h.c.press("tickt:tradfi.AAPL")
    assert "out of date" in reply.text and h.text == before


async def test_without_the_exchange_the_shipped_list_is_offered(settings_file):
    h = Live(settings_file, files=files(ConnectionError("no network")))
    screen = await h.c.press("tick:tradfi")
    assert "Could not read the exchange" in screen.text
    assert "✅ SNDK" in labels(screen)


async def test_without_a_settings_file_nothing_is_written(settings_file):
    h = Live(settings_file, files=files())
    h.c.config_path = None
    before = h.text
    reply = await h.c.press("tickt:tradfi.SPACEX")
    assert "only when control was started with its settings file" in reply.text
    assert h.text == before

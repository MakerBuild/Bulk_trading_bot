"""Auto-spread's tickers, switched on and off from the terminal menu.

Configuration -> Markets & Accounts -> Auto-spread tickers writes
`auto_spread.tradfi:` / `crypto:` in settings.yaml: a ticker switched off
stays in the file as a comment, so the file reads the way the screen does.
The exchange's market list is made up here.
"""

import pathlib

import pytest

from bulkdn import config as config_mod
from bulkdn import menu
from bulkdn.config import ConfigError, load_config

DEFAULT = pathlib.Path(__file__).resolve().parents[1] / "settings.default.yaml"

LISTED = {
    symbol: {"symbol": symbol, "status": "TRADING"}
    for symbol in ("NVDA-USD", "TSLA-USD", "SPACEX-USD", "BTC-USD", "ETH-USD")
}

OLD = "markets:\n  - symbol: BTC-USD\n    notional_usd: 100\n"


@pytest.fixture
def path(tmp_path, monkeypatch):
    monkeypatch.delenv(config_mod.PRIVATE_KEY_ENV, raising=False)
    monkeypatch.setattr(menu, "_exchange_markets", lambda _config: LISTED)
    monkeypatch.setattr(menu, "_pause", lambda: None)
    target = tmp_path / "settings.yaml"
    target.write_bytes(DEFAULT.read_bytes())
    return target


def loaded(path):
    return load_config(str(path), require_credentials=False, private_keys=["KEY-ONE"])


def answers(monkeypatch, *typed):
    it = iter(typed)
    monkeypatch.setattr(menu, "_ask", lambda _prompt: next(it))


# -- the writer ------------------------------------------------------------------


def test_a_ticker_switched_off_is_kept_as_a_comment(path):
    menu.write_ticker_list(str(path), "tradfi", ["NVDA", "TSLA"], ["SPACEX"])
    text = path.read_text(encoding="utf-8")
    assert "  tradfi:\n    - NVDA\n    - TSLA\n#   - SPACEX\n  crypto:\n" in text
    auto = loaded(path).auto_spread
    assert auto.tradfi == ("NVDA", "TSLA")
    assert "BTC" in auto.crypto and auto.markets == "all", "the rest of the block moved"


def test_writing_again_replaces_the_list_and_its_comments(path):
    menu.write_ticker_list(str(path), "tradfi", ["NVDA"], ["SPACEX", "TSLA"])
    menu.write_ticker_list(str(path), "tradfi", ["SPACEX", "TSLA"], ["NVDA"])
    text = path.read_text(encoding="utf-8")
    assert text.count("SPACEX") == 1 and text.count("- NVDA") == 1
    assert loaded(path).auto_spread.tradfi == ("SPACEX", "TSLA")


def test_no_list_means_every_listed_market(path):
    menu.write_ticker_list(str(path), "crypto", None)
    auto = loaded(path).auto_spread
    assert auto.crypto is None and auto.tradfi, "the other list went with it"
    assert "  crypto:" not in path.read_text(encoding="utf-8")


def test_a_file_without_the_lists_gets_one(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(OLD + "auto_spread:\n  markets: tradfi\n  leverage: 5\n", encoding="utf-8")
    menu.write_ticker_list(str(path), "tradfi", ["NVDA"], ["TSLA"])
    auto = load_config(str(path), require_credentials=False).auto_spread
    assert (auto.markets, auto.leverage, auto.tradfi, auto.crypto) == ("tradfi", 5, ("NVDA",), None)


def test_a_file_without_the_block_gets_one(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(OLD, encoding="utf-8")
    menu.write_ticker_list(str(path), "crypto", ["BTC"])
    assert load_config(str(path), require_credentials=False).auto_spread.crypto == ("BTC",)


def test_crlf_is_kept(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_bytes((OLD + "auto_spread:\n  markets: all\n").replace("\n", "\r\n").encode())
    menu.write_ticker_list(str(path), "tradfi", ["NVDA"], ["TSLA"])
    data = path.read_bytes()
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")


def test_a_one_line_block_is_refused_and_left_alone(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(OLD + "auto_spread: {markets: all}\n", encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ConfigError, match="one line"):
        menu.write_ticker_list(str(path), "tradfi", ["NVDA"])
    assert path.read_text(encoding="utf-8") == before


# -- the screen ----------------------------------------------------------------------


def test_switching_one_off_from_the_screen(path, monkeypatch, capsys):
    config = loaded(path)
    # 3 = Auto-spread tickers, 1 = TradFi; the list is CRCL MSTR MU NVDA
    # SKHYNIX SNDK SPACEX TSLA US500 (the shipped list and the listed ones).
    universe = sorted({"CRCL", "MSTR", "MU", "NVDA", "SKHYNIX", "SNDK", "SPACEX", "TSLA", "US500"})
    spacex = str(universe.index("SPACEX") + 1)
    answers(monkeypatch, "3", "1", spacex, "0", "0", "0")
    menu._markets(config, str(path))
    assert "SPACEX" not in loaded(path).auto_spread.tradfi
    assert "SPACEX" not in config.auto_spread.tradfi, "the screen kept the old list"
    assert "[off] SPACEX" in capsys.readouterr().out


def test_with_no_list_every_listed_one_shows_on_and_one_off_writes_the_rest(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(menu, "_exchange_markets", lambda _config: LISTED)
    path = tmp_path / "settings.yaml"
    path.write_text(OLD + "auto_spread:\n  markets: tradfi\n", encoding="utf-8")
    config = load_config(str(path), require_credentials=False)
    # NVDA SPACEX TSLA: switch TSLA (3) off.
    answers(monkeypatch, "3", "0")
    menu._ticker_list_screen(config, str(path), "tradfi")
    out = capsys.readouterr().out
    assert "[on ] NVDA" in out and "every listed market" in out
    auto = load_config(str(path), require_credentials=False).auto_spread
    assert auto.tradfi == ("NVDA", "SPACEX")
    assert "#   - TSLA" in path.read_text(encoding="utf-8")


def test_all_on_add_and_back_to_no_list(path, monkeypatch):
    config = loaded(path)
    answers(monkeypatch, "n", "aapl-usd", "0")
    menu._ticker_list_screen(config, str(path), "tradfi")
    assert "AAPL" in loaded(path).auto_spread.tradfi

    answers(monkeypatch, "x", "0")
    menu._ticker_list_screen(config, str(path), "tradfi")
    assert loaded(path).auto_spread.tradfi is None


def test_the_last_one_on_is_not_switched_off(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(menu, "_exchange_markets", lambda _config: LISTED)
    path = tmp_path / "settings.yaml"
    path.write_text(OLD + "auto_spread:\n  tradfi:\n    - NVDA\n", encoding="utf-8")
    config = load_config(str(path), require_credentials=False)
    before = path.read_text(encoding="utf-8")
    answers(monkeypatch, "1", "0")
    menu._ticker_list_screen(config, str(path), "tradfi")
    assert path.read_text(encoding="utf-8") == before
    assert "last TradFi ticker" in capsys.readouterr().out


def test_without_the_exchange_the_shipped_list_is_offered(tmp_path, monkeypatch, capsys):
    def down(_config):
        raise ConnectionError("no network")

    monkeypatch.setattr(menu, "_exchange_markets", down)
    path = tmp_path / "settings.yaml"
    path.write_text(OLD, encoding="utf-8")
    config = load_config(str(path), require_credentials=False)
    answers(monkeypatch, "0")
    menu._ticker_list_screen(config, str(path), "tradfi")
    out = capsys.readouterr().out
    assert "could not ask the exchange" in out and "[on ] SNDK" in out


def test_the_markets_screen_says_what_the_lists_are(path, monkeypatch, capsys):
    answers(monkeypatch, "0")
    menu._markets(loaded(path), str(path))
    assert "TradFi 9 ticker(s), Crypto 23 ticker(s)" in capsys.readouterr().out

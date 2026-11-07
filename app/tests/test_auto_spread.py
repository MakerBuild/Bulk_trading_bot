"""`mode: auto-spread`: delta-neutral that sends each new group to the market
with the narrowest spread at that moment, among TradFi, Crypto or All.

The books, the exchange's market list and the accounts are all made up here;
nothing is read from the exchange.
"""

import types

import pytest

from bulkdn import autospread
from bulkdn import cli
from bulkdn import menu
from bulkdn import telegram_control as tc
from bulkdn.config import AutoSpreadConfig, Config, ConfigError, LegConfig, Span, load_config
from bulkdn.feed import Quote
from bulkdn.marketdata import MarketSpec
from bulkdn.sizing import InsufficientMargin
from bulkdn.strategy import Strategy

from test_telegram_control import button
from test_telegram_settings import Live, all_labels, settings_file  # noqa: F401 - fixture

LISTED = {
    "BTC-USD": {"symbol": "BTC-USD", "status": "TRADING", "maxLeverage": 50},
    "ETH-USD": {"symbol": "ETH-USD", "status": "TRADING", "maxLeverage": 50},
    "NVDA-USD": {"symbol": "NVDA-USD", "status": "TRADING", "maxLeverage": 10},
    "TSLA-USD": {"symbol": "TSLA-USD", "status": "TRADING", "maxLeverage": 5},
    "US500-USD": {"symbol": "US500-USD", "status": "SUSPENDED", "maxLeverage": 20},
    "DOGE-USD": {"symbol": "DOGE-USD", "status": "SUSPENDED", "maxLeverage": 10},
}


def write(tmp_path, text):
    path = tmp_path / "settings.yaml"
    path.write_text("markets:\n  - symbol: BTC-USD\n    notional_usd: 100\n" + text, encoding="utf-8")
    return load_config(str(path), require_credentials=False)


# -- the settings ------------------------------------------------------------------


def test_the_mode_and_its_block_are_read(tmp_path):
    config = write(tmp_path, "mode: auto\nauto_spread:\n  markets: TradFi\n"
                             "  notional_usd: 800-1200\n  leverage: 5\n  tradfi: [paxg-usd]\n")
    assert config.mode == "auto-spread" and config.takes_liquidity
    assert config.auto_spread.markets == "tradfi"
    assert (config.auto_spread.notional_usd.low, config.auto_spread.notional_usd.high) == (800, 1200)
    assert config.auto_spread.leverage == 5
    assert config.auto_spread.tradfi == ("PAXG-USD",)


def test_without_the_block_it_picks_from_everything(tmp_path):
    config = write(tmp_path, "mode: auto-spread\n")
    assert config.auto_spread == AutoSpreadConfig()
    assert config.auto_spread.markets == "all"


@pytest.mark.parametrize("block, says", [
    ("  markets: stocks\n", "tradfi"),
    ("  notional_usd: 0\n", "notional_usd"),
    ("  leverage: 80\n", "leverage"),
])
def test_a_wrong_value_is_refused_by_name(tmp_path, block, says):
    with pytest.raises(ConfigError, match=says):
        write(tmp_path, "mode: auto-spread\nauto_spread:\n" + block)


def test_delta_neutral_still_takes_and_multi_does_not():
    assert Config(markets=[], mode="delta-neutral").takes_liquidity
    assert not Config(markets=[], mode="multi").takes_liquidity


# -- which markets -------------------------------------------------------------------


@pytest.mark.parametrize("symbol, kind", [
    ("NVDA-USD", "tradfi"), ("US500-USD", "tradfi"), ("BTC-USD", "crypto"), ("PAXG-USD", "crypto"),
])
def test_asset_class(symbol, kind):
    assert autospread.asset_class(symbol) == kind


def test_a_stock_the_list_does_not_know_can_be_named():
    assert autospread.asset_class("NEWCO-USD", extra=["NEWCO-USD"]) == "tradfi"
    assert autospread.asset_class("NEWCO-USD", extra=["newco"]) == "tradfi"


@pytest.mark.parametrize("kind, expected", [
    ("tradfi", ["NVDA-USD", "TSLA-USD"]),
    ("crypto", ["BTC-USD", "ETH-USD"]),
    ("all", ["BTC-USD", "ETH-USD", "NVDA-USD", "TSLA-USD"]),
])
def test_candidates_are_the_kind_asked_for_and_never_suspended(kind, expected):
    assert autospread.candidates(LISTED, AutoSpreadConfig(markets=kind)) == expected


def config_for(kind="all", markets=None):
    return Config(
        markets=markets or [LegConfig(symbol="NVDA-USD", notional_usd=1200, enabled=False,
                                      notional_span=Span(800, 1200), leverage=10)],
        mode="auto-spread",
        auto_spread=AutoSpreadConfig(markets=kind, notional_usd=Span(100, 150), leverage=20),
    )


def test_a_configured_market_keeps_its_size_and_any_other_gets_the_auto_size():
    legs = {leg.symbol: leg for leg in autospread.auto_legs(config_for(), LISTED)}
    assert set(legs) == {"BTC-USD", "ETH-USD", "NVDA-USD", "TSLA-USD"}
    nvda = legs["NVDA-USD"]
    assert nvda.enabled, "a market switched off in the file is still one to pick from"
    assert (nvda.notional_span.low, nvda.notional_span.high) == (800, 1200)
    btc = legs["BTC-USD"]
    assert (btc.notional_span.low, btc.notional_span.high) == (100, 150)
    assert btc.notional_usd == 150 and btc.leverage == 20
    btc.validate("btc")


def test_leverage_is_cut_to_the_markets_own_ceiling():
    legs = {leg.symbol: leg for leg in autospread.auto_legs(config_for(), LISTED)}
    assert legs["TSLA-USD"].leverage == 5
    assert legs["NVDA-USD"].leverage == 10


def test_a_market_with_a_cycle_open_is_kept_whatever_the_kind():
    legs = autospread.auto_legs(config_for("tradfi"), LISTED, keep={"BTC-USD"})
    assert [leg.symbol for leg in legs] == ["NVDA-USD", "TSLA-USD", "BTC-USD"]


def test_the_runs_markets_become_the_candidates(tmp_path, monkeypatch):
    config = config_for("crypto")
    config.state_file = str(tmp_path / "state.json")
    monkeypatch.setattr(cli, "listed_markets", lambda _config: LISTED)
    cli.choose_auto_markets(config)
    assert [leg.symbol for leg in config.active_legs] == ["BTC-USD", "ETH-USD"]


def test_no_market_of_the_kind_is_said_plainly(tmp_path, monkeypatch):
    config = config_for("tradfi")
    config.state_file = str(tmp_path / "state.json")
    monkeypatch.setattr(cli, "listed_markets", lambda _config: {"BTC-USD": LISTED["BTC-USD"]})
    with pytest.raises(ConfigError, match="no TradFi market"):
        cli.choose_auto_markets(config)


# -- the narrowest spread -----------------------------------------------------------


class Books:
    """A feed with a book per market: (bid, ask, size on each level)."""

    def __init__(self, books):
        self.books = books

    def quote(self, symbol):
        bid, ask, _size = self.books.get(symbol, (None, None, 0))
        return Quote(symbol=symbol, best_bid=bid, best_ask=ask, mark_price=bid, age_s=0.0)

    def levels(self, symbol, is_buy, n):
        bid, ask, size = self.books.get(symbol, (None, None, 0))
        price = bid if is_buy else ask
        return [] if price is None else [(price, size)]


def test_the_narrowest_spread_is_picked():
    feed = Books({"A-USD": (100.0, 100.03, 10), "B-USD": (100.0, 100.01, 10), "C-USD": (None, None, 0)})
    seen = [s for s in (autospread.measure(feed, sym, 2.0, 20) for sym in feed.books) if s]
    assert [s.symbol for s in seen] == ["A-USD", "B-USD"], "a market with no book is skipped"
    assert autospread.pick(seen, 3.0).symbol == "B-USD"
    assert autospread.pick(seen, 3.0).spread_bps == pytest.approx(1.0, abs=0.01)


def test_between_two_equal_spreads_the_deeper_wins():
    feed = Books({"A-USD": (100.0, 100.01, 5), "B-USD": (200.0, 200.02, 50)})
    seen = [autospread.measure(feed, sym, 2.0, 20) for sym in feed.books]
    assert autospread.pick(seen, 3.0).symbol == "B-USD"


def test_nothing_is_picked_while_every_spread_is_too_wide():
    feed = Books({"A-USD": (100.0, 100.05, 10)})
    assert autospread.pick([autospread.measure(feed, "A-USD", 2.0, 20)], 3.0) is None


class Dispatcher:
    """The strategy's market choice, on a made-up feed."""

    _next_market = Strategy._next_market
    _note_auto_pick = Strategy._note_auto_pick
    _auto_status = Strategy._auto_status

    def __init__(self, books, paused=()):
        self.config = types.SimpleNamespace(
            mode="auto-spread",
            taking=types.SimpleNamespace(max_slippage_bps=2.0, max_spread_bps=3.0, levels=20,
                                         depth_fraction=0.5),
            auto_spread=AutoSpreadConfig(markets="tradfi"),
        )
        self.feed = Books(books)
        self.symbols = list(books)
        self.paused = set(paused)

    def _market_paused(self, symbol):
        return symbol in self.paused


def test_each_new_group_goes_to_the_narrowest_market_now():
    run = Dispatcher({"NVDA-USD": (200.0, 200.04, 10), "TSLA-USD": (400.0, 400.02, 10)})
    assert run._next_market() == "TSLA-USD"
    assert "TSLA-USD" in run._auto_status() and "TradFi" in run._auto_status()
    run.feed.books["NVDA-USD"] = (200.0, 200.005, 10)
    assert run._next_market() == "NVDA-USD", "the choice follows the book"


def test_a_paused_market_is_not_picked():
    run = Dispatcher({"NVDA-USD": (200.0, 200.01, 10), "TSLA-USD": (400.0, 400.08, 10)},
                     paused={"NVDA-USD"})
    assert run._next_market() == "TSLA-USD"


def test_a_book_too_thin_for_the_minimum_order_is_passed_over():
    # NVDA is the narrowest, but half its depth is $50 against a $100 minimum:
    # the taker would wait there for ever.
    run = Dispatcher({"NVDA-USD": (200.0, 200.01, 0.5), "TSLA-USD": (400.0, 400.04, 10)})
    run.feed.specs = {"NVDA-USD": spec("NVDA-USD", 100), "TSLA-USD": spec("TSLA-USD", 100)}
    assert run._next_market() == "TSLA-USD"


def test_no_group_opens_while_every_spread_is_too_wide():
    run = Dispatcher({"NVDA-USD": (200.0, 200.2, 10)})
    assert run._next_market() is None
    status = run._auto_status()
    assert "waiting" in status and "NVDA-USD" in status


# -- sizing ---------------------------------------------------------------------------


class Sizing:
    _size_each = cli.Runtime._size_each

    def __init__(self, legs, specs):
        self.config = types.SimpleNamespace(max_margin_fraction=0.25)
        self.feed = types.SimpleNamespace(specs=specs)
        self.legs = legs


def spec(symbol, min_notional):
    return MarketSpec(symbol=symbol, tick_size=0.01, lot_size=0.0001, min_notional=min_notional)


def test_every_market_is_sized_on_its_own_and_one_too_big_is_left_out():
    small = LegConfig(symbol="NVDA-USD", size=1.0, max_order_size=1.0, leverage=10)
    big = LegConfig(symbol="BTC-USD", size=0.0002, max_order_size=0.0002, leverage=10)
    run = Sizing([small, big], {"NVDA-USD": spec("NVDA-USD", 10), "BTC-USD": spec("BTC-USD", 500)})
    run._size_each([small, big], {"NVDA-USD": 200.0, "BTC-USD": 100_000.0}, {"m1": 100.0, "s1": 100.0})
    assert small.size == 1.0, "sized as if every market traded at once"
    assert small.enabled and not big.enabled


def test_with_no_market_affordable_the_run_says_so():
    big = LegConfig(symbol="BTC-USD", size=0.0002, max_order_size=0.0002, leverage=10)
    run = Sizing([big], {"BTC-USD": spec("BTC-USD", 500)})
    with pytest.raises(InsufficientMargin, match="auto-spread"):
        run._size_each([big], {"BTC-USD": 100_000.0}, {"m1": 100.0})


def wired(path):
    """The controller as `serve` builds it: settings written by the menu's writers."""
    h = Live(path)
    h.c.write_setting = menu.write_setting
    return h


# -- Telegram and the terminal menu ------------------------------------------------------


async def test_telegram_offers_tradfi_crypto_and_all(settings_file):  # noqa: F811
    h = wired(settings_file)
    modes = await h.say("🎛 Mode")
    assert "auto-spread" in all_labels(modes)
    kinds = await h.c.press(button(modes, "auto-spread"))
    assert all_labels(kinds)[:3] == ["TradFi", "Crypto", "All"]
    assert h.c.config.mode == "multi", "the mode was saved before a kind was chosen"

    saved = await h.c.press(button(kinds, "TradFi"))
    assert "Saved" in saved.text and "auto-spread (TradFi)" in saved.text
    assert h.c.config.mode == "auto-spread" and h.c.config.auto_spread.markets == "tradfi"
    again = load_config(str(settings_file), require_credentials=False)
    assert (again.mode, again.auto_spread.markets) == ("auto-spread", "tradfi")

    run = await h.say("▶️ Run")
    assert "narrowest-spread TradFi market" in run.text


async def test_switching_kind_keeps_auto_spread(settings_file):  # noqa: F811
    h = wired(settings_file)
    await h.c.press("setauto:tradfi")
    await h.c.press("setauto:all")
    again = load_config(str(settings_file), require_credentials=False)
    assert (again.mode, again.auto_spread.markets) == ("auto-spread", "all")
    assert "✅ All" in all_labels(await h.c.press("setmode:auto-spread"))


async def test_the_settings_have_an_auto_spread_section(settings_file):  # noqa: F811
    h = Live(settings_file)
    section = await h.c.press("cfg:auto")
    assert "Size per cycle $: 100" in all_labels(section)
    assert "Leverage: 10" in all_labels(section)


def test_the_modes_are_the_same_in_both_menus():
    assert list(tc.TRADING_MODES) == ["multi", "single", "delta-neutral", "auto-spread"]
    assert set(menu.MODE_CHOICES) == set(tc.TRADING_MODES)


def test_the_terminal_menu_writes_the_kind(settings_file, monkeypatch):  # noqa: F811
    config = load_config(str(settings_file), require_credentials=False)
    monkeypatch.setattr(menu, "_ask", lambda _prompt: "2")
    assert menu._pick_auto_kind(config, str(settings_file))
    assert load_config(str(settings_file), require_credentials=False).auto_spread.markets == "crypto"

"""Choosing markets and accounts from the menu.

Two things are being guarded. One is that the screens write what they say
they write -- a menu that reports a change it did not make is worse than one
that refuses.

The other is the file itself. It is mostly comments, and those comments are
the documentation the operator actually reads: the note about the order cap
having to sit under the exposure limit lives inside a market block. A writer
that loaded the YAML and dumped it back would throw every one of them away,
so these tests check the comments are still there afterwards.
"""

import builtins

import pytest

from bulkdn import menu
from bulkdn.config import LegConfig, Span

SETTINGS = """\
# ===========================================================================
#  settings
# ===========================================================================

mode: multi

# ---------------------------------------------------------------------------
#  WHAT TO TRADE
# ---------------------------------------------------------------------------
legs:
  master_account:
    symbol: BTC-USD
    notional_usd: 50-100       # total per cycle
    leverage: 25
    offset_bps: 2.0
    max_distance_bps: 5.0
    chase_patience_s: 3
    improve_ticks: 1
    max_order_notional_usd: 25-100  # cap on any single resting order.
                            # THIS NUMBER AND risk.max_net_exposure_usd ARE
                            # A PAIR -- raise one without the other and the
                            # first clean fill trips the kill switch.
  sub_account:
    symbol: ETH-USD
    notional_usd: 50-100
    leverage: 25
    offset_bps: 3.0
    max_distance_bps: 8.0
    chase_patience_s: 3
    improve_ticks: 1
    max_order_notional_usd: 50-100

# ---------------------------------------------------------------------------
#  HOW LONG TO HOLD
# ---------------------------------------------------------------------------
hold_minutes: 0.5-1.5
"""

LANDMARK = "# THIS NUMBER AND risk.max_net_exposure_usd ARE"


def feed(monkeypatch, answers):
    it = iter(answers)
    monkeypatch.setattr(builtins, "input", lambda *_: next(it))


def market(symbol, low=50.0, high=100.0, enabled=True):
    return LegConfig(
        symbol=symbol,
        notional_usd=high,
        notional_span=Span(low, high),
        max_order_notional_usd=high,
        max_order_span=Span(25.0, high),
        offset_bps=2.0,
        max_distance_bps=5.0,
        chase_patience_s=3.0,
        improve_ticks=1,
        leverage=25.0,
        enabled=enabled,
    )


class StubConfig:
    http_url = "https://x/api/v1"
    max_groups = 5
    max_takers = 1

    def __init__(self, mode="multi", markets=None, keys=("k1", "k2")):
        self.mode = mode
        self.markets = markets or [market("BTC-USD"), market("ETH-USD")]
        self.private_keys = list(keys)
        self.single_master = 1

    @property
    def active_legs(self):
        return [leg for leg in self.markets if leg.enabled]


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(SETTINGS, encoding="utf-8")
    return path


# -- turning a market on and off --------------------------------------------


def test_the_screen_lists_every_market_with_its_state(monkeypatch, capsys, settings):
    config = StubConfig(markets=[market("BTC-USD"), market("ETH-USD", enabled=False)])
    feed(monkeypatch, ["0"])

    menu._markets_screen(config, str(settings))

    printed = capsys.readouterr().out
    assert "[on ] BTC-USD" in printed
    assert "[off] ETH-USD" in printed
    assert "1 of 2 switched on" in printed


def test_turning_one_off_writes_it(monkeypatch, capsys, settings):
    config = StubConfig()
    feed(monkeypatch, ["2", "0"])

    menu._markets_screen(config, str(settings))

    body = settings.read_text(encoding="utf-8")
    assert "enabled: false" in body
    assert config.markets[1].enabled is False
    assert "ETH-USD is now off" in capsys.readouterr().out


def test_turning_it_back_on_rewrites_the_same_line(monkeypatch, settings):
    config = StubConfig(markets=[market("BTC-USD"), market("ETH-USD", enabled=False)])
    menu._write_market_enabled(str(settings), "ETH-USD", False)

    feed(monkeypatch, ["2", "0"])
    menu._markets_screen(config, str(settings))

    body = settings.read_text(encoding="utf-8")
    assert body.count("enabled:") == 1, "it added a second line instead of editing"
    assert "enabled: true" in body


def test_the_comments_survive_the_write(settings):
    """They are the documentation the operator reads, and a YAML round trip
    would silently delete all of them."""
    menu._write_market_enabled(str(settings), "BTC-USD", False)

    body = settings.read_text(encoding="utf-8")
    assert LANDMARK in body
    assert "#  HOW LONG TO HOLD" in body
    assert "hold_minutes: 0.5-1.5" in body


def test_the_flag_lands_in_the_right_market(settings):
    menu._write_market_enabled(str(settings), "ETH-USD", False)

    body = settings.read_text(encoding="utf-8")
    eth = body.index("symbol: ETH-USD")
    assert body.index("enabled: false") > eth, "it marked the wrong market"


def test_the_last_market_cannot_be_turned_off(monkeypatch, capsys, settings):
    """Every market off is a config that will not load, and the menu should
    not be able to write one."""
    config = StubConfig(markets=[market("BTC-USD"), market("ETH-USD", enabled=False)])
    feed(monkeypatch, ["1", "0"])

    menu._markets_screen(config, str(settings))

    assert "nothing left to trade" in capsys.readouterr().out
    assert config.markets[0].enabled is True
    assert "enabled:" not in settings.read_text(encoding="utf-8")


# -- adding one -------------------------------------------------------------


def exchange(monkeypatch, symbols, min_notional=1.0):
    class Http:
        base_url = "https://x/api/v1"

        def get_exchange_info(self):
            return [
                {"symbol": s, "minNotional": min_notional} for s in symbols
            ]

    monkeypatch.setattr(menu, "_http", lambda _config: Http())


def test_a_new_market_copies_the_first_one(monkeypatch, settings):
    config = StubConfig()
    exchange(monkeypatch, ["BTC-USD", "ETH-USD", "SOL-USD"])
    feed(monkeypatch, ["1"])

    menu._add_market(config, str(settings))

    body = settings.read_text(encoding="utf-8")
    assert "symbol: SOL-USD" in body
    assert "notional_usd: 50-100" in body.split("symbol: SOL-USD")[1]
    assert "copied from BTC-USD" in body


def test_markets_already_in_the_file_are_not_offered(monkeypatch, capsys, settings):
    config = StubConfig()
    exchange(monkeypatch, ["BTC-USD", "ETH-USD", "SOL-USD"])
    feed(monkeypatch, ["0"])

    menu._add_market(config, str(settings))

    printed = capsys.readouterr().out
    assert "SOL-USD" in printed
    assert "BTC-USD" not in printed, "it offered a market that is already there"


def test_a_market_whose_minimum_beats_the_copied_size_arrives_switched_off(
    monkeypatch, capsys, settings
):
    """The copied size would be rejected by the exchange on every order, and
    a run finds that out as a halt. Better to say it now."""
    config = StubConfig()
    exchange(monkeypatch, ["BTC-USD", "ETH-USD", "SOL-USD"], min_notional=500.0)
    feed(monkeypatch, ["1"])

    menu._add_market(config, str(settings))

    printed = capsys.readouterr().out
    assert "will not accept an order under $500" in printed
    body = settings.read_text(encoding="utf-8")
    assert "enabled: false" in body.split("symbol: SOL-USD")[1]


def test_the_exchange_being_unreachable_does_not_kill_the_menu(
    monkeypatch, capsys, settings
):
    class Http:
        def get_exchange_info(self):
            raise RuntimeError("timed out")

    monkeypatch.setattr(menu, "_http", lambda _config: Http())

    menu._add_market(StubConfig(), str(settings))

    assert "could not read the market list" in capsys.readouterr().out


# -- which accounts ---------------------------------------------------------


def test_both_modes_are_offered_with_what_each_means(monkeypatch, capsys):
    feed(monkeypatch, ["0"])

    menu._accounts_screen(StubConfig(), "settings.yaml")

    printed = capsys.readouterr().out
    assert "1. multi" in printed and "2. single" in printed
    assert "every master and every sub" in printed
    assert "one master and its own sub-accounts" in printed


def test_the_mode_in_force_is_marked(monkeypatch, capsys):
    feed(monkeypatch, ["0"])

    menu._accounts_screen(StubConfig(mode="single"), "settings.yaml")

    assert "* 2. single" in capsys.readouterr().out


def test_choosing_single_asks_which_master(monkeypatch, capsys, settings):
    config = StubConfig(mode="multi")
    monkeypatch.setattr(menu, "_trees", lambda _c: [
        menu.Tree(1, "k1", "MASTER-ONE", ("s1",)),
        menu.Tree(2, "k2", "MASTER-TWO", ("s2",)),
    ])
    feed(monkeypatch, ["2", "2"])

    menu._accounts_screen(config, str(settings))

    body = settings.read_text(encoding="utf-8")
    assert config.mode == "single" and config.single_master == 2
    assert "mode: single" in body
    assert "single_master: 2" in body


def test_a_master_with_no_subs_cannot_be_picked(monkeypatch, capsys, settings):
    """Single mode draws both accounts of a group from that one tree, and a
    lone master has nothing to pair with."""
    config = StubConfig(mode="multi")
    monkeypatch.setattr(menu, "_trees", lambda _c: [
        menu.Tree(1, "k1", "MASTER-ONE", ()),
    ])
    feed(monkeypatch, ["2", "1"])

    menu._accounts_screen(config, str(settings))

    assert "owns no sub-accounts" in capsys.readouterr().out
    assert config.mode == "multi", "it switched anyway"
    assert "mode: multi" in settings.read_text(encoding="utf-8")


def test_single_is_refused_without_keys(monkeypatch, capsys, settings):
    config = StubConfig(mode="multi", keys=())
    feed(monkeypatch, ["2"])

    menu._accounts_screen(config, str(settings))

    assert "no keys in private_key.local" in capsys.readouterr().out
    assert config.mode == "multi"


def test_leaving_the_screen_changes_nothing(monkeypatch, settings):
    config = StubConfig(mode="multi")
    feed(monkeypatch, ["0"])

    menu._accounts_screen(config, str(settings))

    assert config.mode == "multi"
    assert settings.read_text(encoding="utf-8") == SETTINGS


def test_a_number_that_is_not_a_mode_is_refused(monkeypatch, capsys, settings):
    config = StubConfig(mode="multi")
    feed(monkeypatch, ["9"])

    menu._accounts_screen(config, str(settings))

    assert "not one of the choices" in capsys.readouterr().out
    assert config.mode == "multi"


# -- and the screen that holds both -----------------------------------------


def test_the_top_screen_says_what_is_traded_and_by_whom(monkeypatch, capsys):
    config = StubConfig(markets=[market("BTC-USD"), market("ETH-USD", enabled=False)])
    feed(monkeypatch, ["3"])

    menu._markets(config, "settings.yaml")

    printed = capsys.readouterr().out
    assert "BTC-USD" in printed and "ETH-USD" not in printed.split("Markets")[1][:40]
    assert "multi -- every master (2 key(s))" in printed
    assert "up to 5 group(s) at once" in printed

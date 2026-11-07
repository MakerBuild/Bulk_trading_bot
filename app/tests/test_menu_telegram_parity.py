"""The terminal menu changes what Telegram's buttons change.

Mode, accounts on or off, every ⚙️ Settings value and the weekly report are
reachable from Configuration, written by the same writers and refused by the
same loader. Everything runs against a real settings file in a temp folder;
the accounts and the week's figures are made up.
"""

import pathlib

import pytest

from bulkdn import config as config_mod
from bulkdn import menu
from bulkdn import tg_settings
from bulkdn import weekly
from bulkdn.config import load_config

DEFAULT = pathlib.Path(__file__).resolve().parents[1] / "settings.default.yaml"

M1 = "Mmmm1111111111111111111111111111111111111111"
S1 = "Ssss1111111111111111111111111111111111111111"
M2 = "Mmmm2222222222222222222222222222222222222222"


@pytest.fixture
def path(tmp_path, monkeypatch):
    monkeypatch.delenv(config_mod.PRIVATE_KEY_ENV, raising=False)
    target = tmp_path / "settings.yaml"
    target.write_bytes(DEFAULT.read_bytes())
    monkeypatch.setattr(menu, "_pause", lambda: None)
    return target


def loaded(path):
    return load_config(str(path), require_credentials=False, private_keys=["KEY-ONE"])


def answers(monkeypatch, *typed):
    it = iter(typed)
    monkeypatch.setattr(menu, "_ask", lambda _prompt: next(it))


def trees(_config):
    return [
        menu.Tree(index=1, private_key="k1", master=M1, subs=(S1,)),
        menu.Tree(index=2, private_key="k2", master=M2, subs=()),
    ]


def test_configuration_lists_what_telegram_has(path, monkeypatch, capsys):
    answers(monkeypatch, "0")
    menu._configuration(loaded(path), str(path))
    screen = capsys.readouterr().out
    for item in ("6. Mode", "7. Accounts on/off", "8. Settings", "9. Weekly report",
                 "4. Markets & Accounts", "5. Progress"):
        assert item in screen
    assert "[multi]" in screen and "[all on]" in screen


def test_mode_is_chosen_from_configuration(path, monkeypatch):
    config = loaded(path)
    # 6 = Mode, 4 = auto-spread, 1 = TradFi, then back out.
    answers(monkeypatch, "6", "4", "1", "0")
    menu._configuration(config, str(path))
    again = loaded(path)
    assert (again.mode, again.auto_spread.markets) == ("auto-spread", "tradfi")


def test_every_telegram_section_is_in_the_settings_screen(path, monkeypatch, capsys):
    answers(monkeypatch, "0")
    menu._settings_screen(loaded(path), str(path))
    screen = capsys.readouterr().out
    assert "1. Markets and sizes" in screen
    for number, (title, _) in enumerate(tg_settings.SECTIONS.values(), start=2):
        assert f"{number}. {menu._plain(title)}" in screen


def test_a_value_is_typed_and_saved(path, monkeypatch, capsys):
    config = loaded(path)
    # 2 = the first section (run targets), 1 = cycles.
    answers(monkeypatch, "2", "1", "3", "0", "0")
    menu._settings_screen(config, str(path))
    assert loaded(path).target.cycles == 3
    assert config.target.cycles == 3, "the screen kept showing the old value"
    assert "Cycles: 0 -> 3" in capsys.readouterr().out


def test_a_value_the_bot_would_refuse_leaves_the_file_alone(path, monkeypatch, capsys):
    before = path.read_text(encoding="utf-8")
    groups = list(tg_settings.SECTIONS).index("pool") + 2
    answers(monkeypatch, str(groups), "1", "0", "0", "0")
    menu._settings_screen(loaded(path), str(path))
    assert path.read_text(encoding="utf-8") == before
    assert "max_groups must be at least 1" in capsys.readouterr().out


def test_nonsense_and_enter_change_nothing(path, monkeypatch, capsys):
    before = path.read_text(encoding="utf-8")
    answers(monkeypatch, "2", "1", "lots", "1", "", "0", "0")
    menu._settings_screen(loaded(path), str(path))
    assert path.read_text(encoding="utf-8") == before
    assert "not a number" in capsys.readouterr().out


def test_one_markets_size_changes(path, monkeypatch):
    # 1 = markets and sizes, 1 = BTC-USD, 1 = size per cycle.
    answers(monkeypatch, "1", "1", "1", "300-600", "0", "0", "0")
    menu._settings_screen(loaded(path), str(path))
    legs = {leg.symbol: leg for leg in loaded(path).markets}
    assert (legs["BTC-USD"].notional_span.low, legs["BTC-USD"].notional_span.high) == (300, 600)
    assert legs["ETH-USD"].notional_usd == 100


def test_auto_spreads_size_is_set_here(path, monkeypatch):
    section = list(tg_settings.SECTIONS).index("auto") + 2
    answers(monkeypatch, str(section), "1", "800-1200", "2", "5", "0", "0")
    menu._settings_screen(loaded(path), str(path))
    auto = loaded(path).auto_spread
    assert (auto.notional_usd.low, auto.notional_usd.high, auto.leverage) == (800, 1200, 5)


def test_accounts_switch_off_and_all_back_on(path, monkeypatch, capsys):
    monkeypatch.setattr(menu, "_trees", trees)
    config = loaded(path)
    answers(monkeypatch, "2", "3", "0")
    menu._accounts_on_off(config, str(path))
    assert loaded(path).excluded_accounts == (S1, M2)
    screen = capsys.readouterr().out
    assert "S1.1" in screen and "M2" in screen

    answers(monkeypatch, "a", "0")
    menu._accounts_on_off(config, str(path))
    assert loaded(path).excluded_accounts == ()


def test_weekly_report_for_the_week_asked(path, monkeypatch, capsys):
    monkeypatch.setattr(menu, "_trees", trees)
    asked = []

    def build(base_url, accounts, week):
        asked.append((accounts, week))
        return weekly.WeekReport(
            week=week, start=weekly.week_window(week)[0], end=weekly.week_window(week)[1],
            balance_start=1500.0, balance_end=1492.75, external_net=0.0, fees=5.0, price=2.25,
            funding=0.0, volume=40_000.0, points={M1: {"mainnet_week3_trading": 400.0}},
        )

    monkeypatch.setattr(weekly, "build_report", build)
    answers(monkeypatch, "3")
    menu._weekly_report(loaded(path))
    assert asked == [([[M1, S1], [M2]], 3)]
    assert "Week 3" in capsys.readouterr().out


def test_a_week_not_ended_is_refused(path, monkeypatch, capsys):
    monkeypatch.setattr(weekly, "build_report", lambda *a: pytest.fail("built a report"))
    answers(monkeypatch, "9999")
    menu._weekly_report(loaded(path))
    assert "has not ended yet" in capsys.readouterr().out


def test_plain_titles_lose_only_the_emoji():
    assert menu._plain("🎯 Run targets") == "Run targets"
    assert menu._plain("Run targets") == "Run targets"
    assert all(menu._plain(title).isascii() for title, _ in tg_settings.SECTIONS.values())


def test_every_setting_says_how_to_type_it():
    for _, settings in tg_settings.SECTIONS.values():
        for setting in settings:
            assert tg_settings.example(setting)

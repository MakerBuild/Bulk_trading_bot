"""⚙️ Settings in the Telegram menu: what was edited in settings.yaml by hand.

Everything here runs against a real settings file in a temp folder, written
by the terminal menu's own writers and read back by the loader the bot starts
with. A value the bot would refuse is refused before the file changes.
"""

import pathlib
import types

import pytest

from bulkdn import config as config_mod
from bulkdn import menu
from bulkdn import telegram_control as tc
from bulkdn import tg_settings
from bulkdn.config import load_config

from test_telegram_control import Harness, button

DEFAULT = pathlib.Path(__file__).resolve().parents[1] / "settings.default.yaml"
LOADED = types.SimpleNamespace(private_keys=["KEY-ONE"], private_key="KEY-ONE")


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    monkeypatch.delenv(config_mod.PRIVATE_KEY_ENV, raising=False)
    path = tmp_path / "settings.yaml"
    path.write_bytes(DEFAULT.read_bytes())
    return path


class Live(Harness):
    """A controller wired to a real settings file."""

    def __init__(self, path, files=menu):
        super().__init__()
        reload = tc.config_reloader(str(path), LOADED)
        self.c.config = reload()
        self.c.reload = reload
        self.c.config_path = str(path)
        self.c.files = files
        self.path = path

    @property
    def text(self):
        return self.path.read_text(encoding="utf-8")


def all_labels(reply):
    return [label for row in reply.buttons or [] for label, _ in row]


# -- the screens ------------------------------------------------------------------


def test_the_keyboard_has_settings():
    assert tc.KEYBOARD["⚙️ Settings"] == "/settings"
    assert any("⚙️ Settings" in row for row in tc._KEYBOARD_ROWS)


async def test_settings_opens_on_every_section(settings_file):
    h = Live(settings_file)
    home = await h.say("⚙️ Settings")
    assert "Mode: multi" in home.text and "BTC-USD $100" in home.text
    labels = all_labels(home)
    assert "📈 Markets and sizes" in labels and "🎛 Mode" in labels
    for title, _ in tg_settings.SECTIONS.values():
        assert title in labels
    for row in home.buttons:
        for _, data in row:
            assert len(data.encode()) <= 64


async def test_a_section_lists_its_values(settings_file):
    h = Live(settings_file)
    targets = await h.c.press("cfg:targets")
    assert "Fee budget $: 3" in all_labels(targets)
    assert "Hold, minutes: 0.5-1.5" in all_labels(targets)


# -- changing a value --------------------------------------------------------------


async def test_a_target_is_asked_for_typed_and_saved(settings_file):
    h = Live(settings_file)
    ask = await h.c.press(button(await h.c.press("cfg:targets"), "Cycles"))
    assert "Send the new value" in ask.text and "Now: <code>0</code>" in ask.text

    done = await h.say("3")
    assert "✅ Cycles: 0 → 3" in done.text
    assert "  cycles: 3" in h.text
    assert h.c.config.target.cycles == 3
    assert h.c.awaiting is None


async def test_one_markets_size_changes_and_the_others_stay(settings_file):
    h = Live(settings_file)
    btc = await h.c.press("mkt:BTC-USD")
    await h.c.press(button(btc, "Size per cycle $"))
    done = await h.say("300 - 600")
    assert "100 → 300-600" in done.text
    legs = {leg.symbol: leg for leg in load_config(str(settings_file), require_credentials=False).markets}
    assert (legs["BTC-USD"].notional_span.low, legs["BTC-USD"].notional_span.high) == (300, 600)
    assert legs["ETH-USD"].notional_usd == 100


async def test_a_value_people_type_is_read(settings_file):
    h = Live(settings_file)
    await h.c.press("edit:volume")
    await h.say("$250k")
    assert h.c.config.target.volume_usd == 250_000


async def test_nonsense_is_refused_and_the_question_stays_open(settings_file):
    h = Live(settings_file)
    before = h.text
    await h.c.press("edit:cycles")
    assert "not a number" in (await h.say("lots")).text
    assert "whole number" in (await h.say("2.5")).text
    assert h.c.awaiting is not None and h.text == before


async def test_a_value_the_bot_would_refuse_leaves_the_file_alone(settings_file):
    h = Live(settings_file)
    before = h.text
    await h.c.press("edit:groups")
    reply = await h.say("0")
    assert "max_groups must be at least 1" in reply.text
    assert h.text == before and h.c.config.max_groups == 5


async def test_cancel_and_a_button_both_end_the_question(settings_file):
    h = Live(settings_file)
    await h.c.press("edit:cycles")
    assert "Kept" in (await h.say("/cancel")).text
    assert "Use the buttons" in (await h.say("3")).text

    await h.c.press("edit:cycles")
    assert "Idle" in (await h.say("📊 Status")).text
    assert "Use the buttons" in (await h.say("3")).text
    assert h.c.config.target.cycles == 0


async def test_an_old_question_expires(settings_file):
    h = Live(settings_file)
    await h.c.press("edit:cycles")
    h.now += tc.INPUT_TTL_S + 1
    assert "expired" in (await h.say("3")).text
    assert h.c.config.target.cycles == 0


async def test_a_missing_block_is_added(settings_file):
    text = settings_file.read_text(encoding="utf-8")
    start = text.index("pause:")
    end = text.index("\n\n", start)
    settings_file.write_text(text[:start] + text[end + 2:], encoding="utf-8")
    h = Live(settings_file)
    await h.c.press("edit:move")
    await h.say("40")
    assert h.c.config.pause.max_move_bps == 40


async def test_without_a_settings_file_nothing_is_written():
    h = Harness()
    assert "settings file" in (await h.c.press("edit:cycles")).text


# -- markets -------------------------------------------------------------------------


async def test_a_market_switches_off_and_on_but_never_the_last(settings_file):
    h = Live(settings_file)
    off = await h.c.press("mon:ETH-USD")
    assert "(off)" in off.text
    assert [leg.symbol for leg in h.c.config.active_legs] == ["BTC-USD"]

    last = await h.c.press("mon:BTC-USD")
    assert "only market switched on" in last.text
    assert [leg.symbol for leg in h.c.config.active_legs] == ["BTC-USD"]

    assert "(on)" in (await h.c.press("mon:ETH-USD")).text


async def test_a_market_is_added_from_the_exchanges_list(settings_file):
    listed = {
        "BTC-USD": {"symbol": "BTC-USD"},
        "SOL-USD": {"symbol": "SOL-USD", "minNotional": 10, "maxLeverage": 5},
    }
    files = types.SimpleNamespace(
        **{name: getattr(menu, name) for name in (
            "write_setting", "write_block_setting", "write_market_setting",
            "set_market_enabled", "add_market",
        )},
        exchange_markets=lambda config: listed,
    )
    h = Live(settings_file, files=files)
    offered = await h.c.press("madd:")
    assert all_labels(offered)[:1] == ["SOL-USD"]

    added = await h.c.press(button(offered, "SOL-USD"))
    assert "SOL-USD" in added.text and "added" in added.text
    sol = next(leg for leg in h.c.config.markets if leg.symbol == "SOL-USD")
    assert sol.leverage == 5, "leverage was not clamped to the market's maximum"


# -- every setting names a real key ------------------------------------------------------


def _changed(setting, value):
    """A different value of the same kind, inside what the loader accepts."""
    low = getattr(value, "low", value) or 0
    high = getattr(value, "high", value) or 0
    if setting.kind == "int":
        return str(int(high) + 1)
    if setting.kind == "span":
        if high == 0:
            return "1"
        if low == high:
            return tg_settings.render(high * 0.8)
        return f"{tg_settings.render(low * 0.8)}-{tg_settings.render(high * 0.8)}"
    return tg_settings.render(high * 0.8) if high else "1"


@pytest.mark.parametrize("setting", [s for _, group in tg_settings.SECTIONS.values() for s in group],
                         ids=lambda s: s.id)
def test_every_setting_writes_the_key_it_reads(settings_file, setting):
    config = load_config(str(settings_file), require_credentials=False)
    value = _changed(setting, setting.read(config))
    if setting.block == tg_settings.TOP:
        menu.write_setting(str(settings_file), setting.key, value)
    else:
        menu.write_block_setting(str(settings_file), setting.block, setting.key, value)
    after = load_config(str(settings_file), require_credentials=False)
    assert tg_settings.shown(setting.read(after)) == value


@pytest.mark.parametrize("setting", tg_settings.MARKET_SETTINGS, ids=lambda s: s.id)
def test_every_market_setting_writes_the_key_it_reads(settings_file, setting):
    def btc(config):
        return next(leg for leg in config.markets if leg.symbol == "BTC-USD")

    value = _changed(setting, setting.read(btc(load_config(str(settings_file), require_credentials=False))))
    menu.write_market_setting(str(settings_file), "BTC-USD", setting.key, value)
    after = load_config(str(settings_file), require_credentials=False)
    assert tg_settings.shown(setting.read(btc(after))) == value


# -- reading what is typed ---------------------------------------------------------------


def setting_of(kind):
    return tg_settings.Setting("x", "", "x", "x", kind, "", lambda c: None)


@pytest.mark.parametrize("typed, written", [
    ("250k", "250000"), ("$1,500", "1500"), ("0.25", "0.25"), ("1.5m", "1500000"),
])
def test_numbers(typed, written):
    assert tg_settings.parse(setting_of("number"), typed) == written


@pytest.mark.parametrize("typed, written", [
    ("100-200", "100-200"), ("100 - 200", "100-200"), ("0.5–1.5", "0.5-1.5"),
    ("200-100", "100-200"), ("1k-2k", "1000-2000"), ("300", "300"), ("5-5", "5"),
])
def test_ranges(typed, written):
    assert tg_settings.parse(setting_of("span"), typed) == written


@pytest.mark.parametrize("kind, typed", [("number", "-5"), ("int", "1.5"), ("span", "1-2-3"), ("number", "inf")])
def test_refused(kind, typed):
    with pytest.raises(ValueError):
        tg_settings.parse(setting_of(kind), typed)


def test_setting_ids_are_unique_and_short():
    ids = [s.id for _, group in tg_settings.SECTIONS.values() for s in group]
    ids += [s.id for s in tg_settings.MARKET_SETTINGS]
    assert len(ids) == len(set(ids)) == len(tg_settings.BY_ID)
    assert all(len(f"edit:{i}@SOME-LONGER-USD".encode()) <= 64 for i in ids)

"""🎛 Mode and 👥 Accounts in the Telegram menu.

Both set what the NEXT run trades, write it to settings.yaml with the terminal
menu's own writer so it outlives a restart, and never touch a run in
progress. A switched-off account is left out of the pool a run draws from,
but Close all still closes it, and a cycle it has open is finished first.
"""

import types

from bulkdn import telegram_control as tc
from bulkdn.cli import drop_excluded
from bulkdn.config import load_config
from bulkdn.menu import write_setting
from bulkdn.state import Phase, StateStore, StrategyState

from test_telegram_control import Harness, button

# Made-up pubkeys, base58-shaped. Two masters; the second owns no subs.
M1 = "Aaaa1111111111111111111111111111111111111111"
S1A = "Bbbb2222222222222222222222222222222222222222"
S1B = "Cccc3333333333333333333333333333333333333333"
M2 = "Dddd4444444444444444444444444444444444444444"


def trees(_config):
    return [
        types.SimpleNamespace(index=1, master=M1, subs=(S1A, S1B), accounts=(M1, S1A, S1B)),
        types.SimpleNamespace(index=2, master=M2, subs=(), accounts=(M2,)),
    ]


class Settings(Harness):
    """A harness whose controller lists accounts and writes settings."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.written = []
        self.listings = 0

        def listing(config):
            self.listings += 1
            return trees(config)

        self.c.trees_fn = listing
        self.c.config_path = "settings.yaml"
        self.c.write_setting = lambda path, key, value: self.written.append((key, value))


def labels(reply):
    return [label for row in reply.buttons or [] for label, _ in row]


# -- the keyboard --------------------------------------------------------------


def test_the_keyboard_has_mode_and_accounts():
    rows = tc._KEYBOARD_ROWS
    flat = [label for row in rows for label in row]
    assert "🎛 Mode" in flat and "👥 Accounts" in flat
    assert all(label in tc.KEYBOARD for label in flat)


async def test_an_old_keyboard_accounts_button_still_opens_margin():
    # A phone shows the keyboard it was last sent until it gets a new one.
    h = Settings()
    assert "Account management" in (await h.say("💰 Accounts")).text
    assert "Account management" in (await h.say("💰 Margin")).text


# -- 🎛 Mode -------------------------------------------------------------------


async def test_mode_menu_shows_every_mode_and_marks_the_current_one():
    h = Settings()
    reply = await h.say("🎛 Mode")
    assert "now multi" in reply.text
    assert labels(reply) == ["✅ multi", "single", "delta-neutral", "auto-spread"]
    assert "taker" in reply.text, "delta-neutral's fee cost is not mentioned"


async def test_choosing_delta_neutral_saves_it():
    h = Settings()
    reply = await h.c.press(button(await h.say("🎛 Mode"), "delta-neutral"))
    assert h.written == [("mode", "delta-neutral")]
    assert h.config.mode == "delta-neutral"
    assert "Saved" in reply.text and "✅ delta-neutral" in labels(reply)


async def test_single_asks_which_master_and_refuses_one_without_subs():
    h = Settings()
    masters = await h.c.press("setmode:single")
    assert h.written == [], "single was saved before a master was chosen"
    assert any(label.startswith("master 1") for label in labels(masters))

    refused = await h.c.press(button(masters, "master 2"))
    assert "no sub-accounts" in refused.text and h.written == []

    saved = await h.c.press(button(masters, "master 1"))
    assert h.written == [("single_master", "1"), ("mode", "single")]
    assert h.config.mode == "single" and "master 1" in saved.text


async def test_a_mode_chosen_mid_run_applies_from_the_next_run():
    h = Settings()
    await h.start_run()
    reply = await h.c.press("setmode:delta-neutral")
    assert "next Run" in reply.text
    assert h.runs[0][0].mode == "multi", "the running run's settings changed under it"
    h.release.set()
    await h.settle()


async def test_a_failed_write_is_reported_and_changes_nothing():
    h = Settings()

    def broken(path, key, value):
        raise OSError("disk full")

    h.c.write_setting = broken
    reply = await h.c.press("setmode:delta-neutral")
    assert "Could not save" in reply.text and h.config.mode == "multi"


def test_a_mode_from_telegram_outlives_the_command_line_override(tmp_path, monkeypatch):
    from bulkdn import config as config_mod

    monkeypatch.delenv(config_mod.PRIVATE_KEY_ENV, raising=False)
    path = tmp_path / "settings.yaml"
    path.write_text(
        "mode: delta-neutral\nmarkets:\n  - symbol: BTC-USD\n    notional_usd: 100\n",
        encoding="utf-8",
    )
    loaded = types.SimpleNamespace(private_keys=["KEY-ONE"], private_key="KEY-ONE")
    reload = tc.config_reloader(str(path), loaded, mode="multi")
    assert reload().mode == "multi"
    reload.forget_mode()
    assert reload().mode == "delta-neutral"


# -- 👥 Accounts ----------------------------------------------------------------


async def test_accounts_lists_every_account_switched_on():
    h = Settings()
    reply = await h.say("👥 Accounts")
    assert "3 of 4 on" not in reply.text and "4 of 4 on" in reply.text
    assert [label.split()[0] for label in labels(reply)] == ["✅"] * 4
    assert [label.split()[1] for label in labels(reply)] == ["M1", "S1.1", "S1.2", "M2"]
    for row in reply.buttons:
        for _, data in row:
            assert len(data.encode()) <= 64, "Telegram refuses callback data over 64 bytes"


async def test_a_press_switches_an_account_off_and_back_on():
    h = Settings()
    listing = await h.say("👥 Accounts")
    off = await h.c.press(button(listing, "✅ S1.2"))
    assert h.written[-1] == ("excluded_accounts", f'["{S1B}"]')
    assert h.config.excluded_accounts == (S1B,)
    assert "3 of 4 on" in off.text and "⬜ S1.2" in " ".join(labels(off))

    on = await h.c.press(button(off, "⬜ S1.2"))
    assert h.written[-1] == ("excluded_accounts", "[]")
    assert h.config.excluded_accounts == () and "4 of 4 on" in on.text


async def test_switch_all_on():
    h = Settings()
    h.config.excluded_accounts = (S1A, M2)
    reply = await h.c.press(button(await h.say("👥 Accounts"), "✅ Switch all on"))
    assert h.written[-1] == ("excluded_accounts", "[]")
    assert "4 of 4 on" in reply.text


async def test_an_unknown_account_button_relists_instead_of_guessing():
    h = Settings()
    reply = await h.c.press("acct:Zzzz999999999999")
    assert h.written == [] and "out of date" in reply.text


async def test_the_listing_is_reused_between_presses():
    h = Settings()
    listing = await h.say("👥 Accounts")
    await h.c.press(button(listing, "✅ M2"))
    assert h.listings == 1
    h.now += tc.TREES_TTL_S + 1
    await h.say("👥 Accounts")
    assert h.listings == 2


async def test_the_run_question_says_how_many_are_switched_off():
    h = Settings()
    h.config.excluded_accounts = (M2,)
    assert "1 account(s) switched off" in (await h.say("▶️ Run")).text


def test_the_written_list_reads_back(tmp_path, monkeypatch):
    """The real writer and the real loader agree on the format."""
    from bulkdn import config as config_mod

    monkeypatch.delenv(config_mod.PRIVATE_KEY_ENV, raising=False)
    path = tmp_path / "settings.yaml"
    path.write_text(
        "mode: multi  # a comment\nmarkets:\n  - symbol: BTC-USD\n    notional_usd: 100\n",
        encoding="utf-8",
    )
    write_setting(str(path), "excluded_accounts", f'["{M2}", "{S1A}"]')
    write_setting(str(path), "mode", "delta-neutral")
    config = load_config(str(path), require_credentials=False)
    assert config.excluded_accounts == (M2, S1A)
    assert config.mode == "delta-neutral"
    assert "# a comment" in path.read_text(encoding="utf-8")

    write_setting(str(path), "excluded_accounts", "[]")
    assert load_config(str(path), require_credentials=False).excluded_accounts == ()


# -- the run's pool -------------------------------------------------------------


def session(pubkey):
    return types.SimpleNamespace(pubkey=pubkey)


def test_switched_off_accounts_leave_the_pool(tmp_path):
    config = types.SimpleNamespace(
        excluded_accounts=(S1B, M2), state_file=str(tmp_path / "state.json"),
    )
    pool = [session(M1), session(S1A), session(S1B), session(M2)]
    assert [s.pubkey for s in drop_excluded(pool, config)] == [M1, S1A]


def test_an_account_with_a_cycle_open_is_kept_to_finish_it(tmp_path):
    path = str(tmp_path / "state.json")
    state = StrategyState()
    leg = state.leg("g1:BTC-USD", "BTC-USD")
    leg.phase, leg.maker, leg.takers = Phase.HOLD, M1, [S1B]
    done = state.leg("g2:BTC-USD", "BTC-USD")
    done.phase, done.maker, done.takers = Phase.COMPLETE, M2, [S1A]
    StateStore(path).save(state)

    config = types.SimpleNamespace(excluded_accounts=(S1B, M2), state_file=path)
    pool = [session(M1), session(S1A), session(S1B), session(M2)]
    assert [s.pubkey for s in drop_excluded(pool, config)] == [M1, S1A, S1B]

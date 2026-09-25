"""Running the bot from Telegram.

What matters is what a chat window must never do: answer a stranger, start or
close anything without a fresh confirmation, act on a button from an old
message, run two pieces of work on the same accounts, or replay commands typed
while the process was off.
"""

import asyncio
import types

import pytest

from bulkdn import telegram_control as tc
from bulkdn.telegram_control import Controller, PollConflict, Reply, TelegramPoller


def settings(volume=100_000.0):
    return types.SimpleNamespace(
        mode="multi",
        active_legs=[types.SimpleNamespace(symbol="BTC-USD")],
        target=types.SimpleNamespace(volume_usd=volume, burn_usd=0.0, cycles=0),
    )


class FakeStrategy:
    def __init__(self):
        self.stops = []

    def request_stop(self, reason):
        self.stops.append(reason)

    def status_lines(self):
        return ["  g1:BTC-USD OPEN cycle 1", "  volume ###  $5,000 / $100,000  5.0%"]


def button(reply: Reply, label_start: str) -> str:
    """The callback data of the button whose label starts with `label_start`."""
    for row in reply.buttons or []:
        for label, data in row:
            if label.startswith(label_start):
                return data
    raise AssertionError(f"no {label_start!r} button in {reply.buttons}")


class Harness:
    """A Controller with its runs, closes and clock under the test's control."""

    def __init__(self, dry_run=False, status_text="phase        : idle\n"):
        self.config = settings()
        self.sent = []
        self.runs = []
        self.closes = []
        self.now = 1000.0
        self.release = asyncio.Event()
        self.strategy = FakeStrategy()
        self.status_text = status_text
        self.codes = iter(["c1", "c2", "c3", "c4"])
        self.c = Controller(
            self.config, dry_run,
            run_fn=self.run, flatten_fn=self.flatten, status_fn=self.status,
            send=self.send, clock=lambda: self.now, make_code=lambda: next(self.codes),
        )

    async def run(self, config, dry_run, on_strategy=None):
        self.runs.append((config, dry_run))
        on_strategy(self.strategy)
        await self.release.wait()
        return 0

    async def flatten(self, config, dry_run):
        self.closes.append(dry_run)
        return 0

    async def status(self, config):
        return self.status_text

    async def send(self, text):
        self.sent.append(text)

    async def say(self, text) -> Reply:
        return await self.c.handle(text)

    async def start_run(self):
        ask = await self.say("▶️ Run")
        reply = await self.c.press(button(ask, "▶️ Start"))
        await self.settle()
        return reply

    async def settle(self):
        for _ in range(5):
            await asyncio.sleep(0)


# -- the buttons ---------------------------------------------------------------


async def test_the_keyboard_buttons_are_the_commands():
    h = Harness()
    assert "Idle" in (await h.say("📊 Status")).text
    assert "Nothing is running" in (await h.say("⏹ Stop")).text
    assert (await h.say("/help")).keyboard, "help does not bring the keyboard back"
    assert (await h.say("hello")).keyboard


async def test_run_starts_only_from_its_start_button():
    h = Harness()
    ask = await h.say("▶️ Run")
    assert "LIVE" in ask.text and "volume $100,000" in ask.text
    assert h.runs == [], "a run started before it was confirmed"

    reply = await h.c.press(button(ask, "▶️ Start"))
    await h.settle()
    assert len(h.runs) == 1 and "Starting" in reply.text
    h.release.set()
    await h.settle()


async def test_cancel_cancels():
    h = Harness()
    ask = await h.say("▶️ Run")
    assert "Cancelled" in (await h.c.press(button(ask, "❌"))).text
    assert "expired" in (await h.c.press(button(ask, "▶️ Start"))).text
    assert h.runs == []


async def test_a_button_on_an_old_message_does_nothing():
    """And does not cancel the question that is actually open."""
    h = Harness()
    old = await h.say("▶️ Run")
    new = await h.say("🛑 Close all")

    assert "expired" in (await h.c.press(button(old, "▶️ Start"))).text
    assert h.runs == []
    await h.c.press(button(new, "✅"))
    await h.settle()
    assert h.closes == [False], "the stale press cancelled the live question"


async def test_a_button_is_spent_by_its_first_press():
    h = Harness()
    ask = await h.say("🛑 Close all")
    await h.c.press(button(ask, "✅"))
    await h.settle()
    assert "expired" in (await h.c.press(button(ask, "✅"))).text
    assert h.closes == [False]


async def test_a_button_lapses():
    h = Harness()
    ask = await h.say("▶️ Run")
    h.now += tc.CONFIRM_TTL_S + 1
    assert "expired" in (await h.c.press(button(ask, "▶️ Start"))).text
    assert h.runs == []


async def test_a_forged_press_with_the_wrong_action_does_nothing():
    """A run's code on a close's action is not a close."""
    h = Harness()
    ask = await h.say("▶️ Run")
    code = button(ask, "▶️ Start").split(":")[1]
    assert "expired" in (await h.c.press(f"close:{code}")).text
    await h.settle()
    assert h.closes == [] and h.runs == []


async def test_typed_confirmation_still_works_and_one_wrong_guess_ends_it():
    h = Harness()
    await h.say("/run")
    assert "Wrong code" in (await h.say("/yes nope")).text
    assert "Nothing to confirm" in (await h.say("/yes c1")).text

    await h.say("/run")
    await h.say("/yes c2")
    await h.settle()
    assert len(h.runs) == 1
    h.release.set()
    await h.settle()


# -- one piece of work at a time -------------------------------------------------


async def test_a_second_run_is_refused_while_one_is_going():
    h = Harness()
    await h.start_run()
    assert "already in progress" in (await h.say("▶️ Run")).text
    h.release.set()
    await h.settle()


async def test_a_volume_target_applies_to_that_run_only():
    h = Harness()
    ask = await h.say("/run 250k")
    assert "$250,000" in ask.text
    await h.c.press(button(ask, "▶️ Start"))
    await h.settle()

    config, dry_run = h.runs[0]
    assert config.target.volume_usd == 250_000
    assert h.config.target.volume_usd == 100_000, "the next run would inherit it"
    assert dry_run is False
    h.release.set()
    await h.settle()


async def test_a_dry_control_loop_starts_dry_runs():
    h = Harness(dry_run=True)
    assert "DRY-RUN" in (await h.say("▶️ Run")).text
    h.c.pending = None
    await h.start_run()
    assert h.runs[0][1] is True
    h.release.set()
    await h.settle()


# -- stop and close against a run --------------------------------------------------


async def test_stop_is_the_s_key():
    h = Harness()
    await h.start_run()
    reply = await h.say("⏹ Stop")
    assert h.strategy.stops == ["telegram"]
    assert "positions stay" in reply.text
    h.release.set()
    await h.settle()


async def test_close_during_a_run_stops_it_first_then_closes():
    h = Harness()
    await h.start_run()

    ask = await h.say("🛑 Close all")
    assert "Stop the current run" in ask.text
    await h.c.press(button(ask, "✅"))
    await h.settle()
    assert h.strategy.stops, "it closed underneath a live run"
    assert h.closes == [], "it closed before the run had stopped"

    h.release.set()                  # the run notices the stop and ends
    await h.settle()
    assert h.closes == [False]


async def test_close_during_startup_does_not_wait_out_the_whole_run():
    """A run still starting has no strategy yet. Waiting on the task alone
    would have waited for the entire run to finish before closing."""
    h = Harness()
    started = asyncio.Event()

    async def slow_start(config, dry_run, on_strategy=None):
        await started.wait()
        on_strategy(h.strategy)
        await h.release.wait()
        return 0

    h.c.run_fn = slow_start
    await h.start_run()
    ask = await h.say("🛑 Close all")
    await h.c.press(button(ask, "✅"))
    await h.settle()

    started.set()                    # the strategy exists now
    await asyncio.sleep(0.6)
    assert h.strategy.stops == ["telegram close"]
    h.release.set()
    await h.settle()
    assert h.closes == [False]


# -- answers -------------------------------------------------------------------------


async def test_status_during_a_run_is_the_screen_block_with_a_refresh():
    h = Harness()
    await h.start_run()

    reply = await h.say("📊 Status")
    assert "g1:BTC-USD OPEN cycle 1" in reply.text and "LIVE run" in reply.text
    again = await h.c.press(button(reply, "🔄"))
    assert "g1:BTC-USD OPEN cycle 1" in again.text
    h.release.set()
    await h.settle()


async def test_idle_status_leaves_out_the_account_pubkeys():
    listing = (
        "accounts     : 2 on 1 socket(s)\n"
        "  m1     ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ\n"
        "positions:\n  BTC-USD    net=+0.00000000  all flat\n"
    )
    h = Harness(status_text=listing)
    reply = await h.say("📊 Status")
    assert "all flat" in reply.text
    assert "ZZZZZZZZ" not in reply.text


async def test_log_sends_the_tail(tmp_path):
    path = tmp_path / "logs.txt"
    path.write_text("".join(f"2026-09-25 18:00:{i:02d} INFO line {i}\n" for i in range(40)))
    h = Harness()
    h.c.log_path = str(path)

    reply = await h.say("/log 3")
    assert "line 39" in reply.text and "line 37" in reply.text and "line 36" not in reply.text
    assert "2026-09-25" not in reply.text
    assert "line 39" in (await h.say("📜 Log")).text


def test_buttons_render_as_telegram_markup():
    inline = Reply("x", [[("▶️ Start", "run:c1"), ("❌ Cancel", "cancel:c1")]]).markup()
    assert inline["inline_keyboard"][0][1] == {"text": "❌ Cancel", "callback_data": "cancel:c1"}
    keyboard = Reply("x", keyboard=True).markup()
    labels = [b["text"] for row in keyboard["keyboard"] for b in row]
    assert set(labels) == set(tc.KEYBOARD), "a keyboard button that no command answers"
    assert Reply("x").markup() is None


def test_amounts_are_read_the_way_people_type_them():
    assert tc._parse_amount("100000") == 100_000
    assert tc._parse_amount("100k") == 100_000
    assert tc._parse_amount("$1,500") == 1_500
    assert tc._parse_amount("1.5m") == 1_500_000
    assert tc._parse_amount("lots") is None


# -- who is answered ---------------------------------------------------------------------


def test_only_listed_senders_are_answered():
    strangers = set()
    ours = {"message": {"from": {"id": 7}, "text": "/status"}}
    theirs = {"message": {"from": {"id": 9}, "text": "/run"}}
    assert tc.authorised_text(ours, {7}, strangers) == "/status"
    assert tc.authorised_text(theirs, {7}, strangers) is None
    assert strangers == {9}


def test_a_listed_id_in_someone_elses_group_is_still_judged_by_sender():
    update = {"message": {"from": {"id": 9}, "chat": {"id": 7}, "text": "/close"}}
    assert tc.authorised_text(update, {7}, set()) is None


def test_a_button_is_judged_by_who_pressed_it():
    """A button in a group message can be pressed by anyone in the group."""
    ours = {"callback_query": {"id": "q", "from": {"id": 7}, "data": "close:c1"}}
    theirs = {"callback_query": {"id": "q", "from": {"id": 9}, "data": "close:c1"}}
    assert tc.authorised_press(ours, {7}, set()) is not None
    assert tc.authorised_press(theirs, {7}, set()) is None


# -- polling ---------------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.calls = []

    def post(self, url, json, timeout, proxy):
        self.calls.append((url.rsplit("/", 1)[-1], json))
        status, payload = self.answers.pop(0) if self.answers else (200, {"ok": True, "result": True})
        return FakeResponse(status, payload)


async def test_commands_sent_while_off_are_dropped_at_start():
    session = FakeSession([
        (200, {"ok": True, "result": [{"update_id": 41, "message": {"text": "/run"}}]}),
        (200, {"ok": True, "result": []}),
        (200, {"ok": True, "result": []}),
    ])
    poller = TelegramPoller("t")

    assert await poller.skip_backlog(session) == 1
    await poller.updates(session)
    assert session.calls[1][1]["offset"] == 42, "the backlog was not confirmed away"
    assert session.calls[2][1]["offset"] == 42
    assert "callback_query" in session.calls[2][1]["allowed_updates"], "buttons are never heard"


async def test_a_second_poller_on_the_token_is_reported():
    poller = TelegramPoller("t")
    with pytest.raises(PollConflict):
        await poller.updates(FakeSession([(409, {"ok": False})]))


# -- the loop as a whole -----------------------------------------------------------------------


async def test_a_press_spins_down_and_replaces_its_message():
    h = Harness()
    ask = await h.say("▶️ Run")
    session = FakeSession()
    api = tc.BotApi(session, "t")
    update = {"callback_query": {
        "id": "q1", "from": {"id": 7}, "data": button(ask, "❌"),
        "message": {"message_id": 55, "chat": {"id": 7}},
    }}
    await tc._handle_update(update, api, h.c, {7}, set())

    methods = [name for name, _ in session.calls]
    assert methods == ["answerCallbackQuery", "editMessageText"]
    edit = session.calls[1][1]
    assert edit["message_id"] == 55 and edit["text"] == "Cancelled."
    assert "reply_markup" not in edit, "the spent buttons were left on the message"


async def test_the_loop_answers_ours_and_ignores_strangers(monkeypatch):
    sent = []
    batches = [
        [{"update_id": 1, "message": {"from": {"id": 9}, "chat": {"id": 9}, "text": "/run"}},
         {"update_id": 2, "message": {"from": {"id": 7}, "chat": {"id": 7}, "text": "/help"}}],
    ]

    class Poller:
        def __init__(self, token):
            pass

        async def skip_backlog(self, session):
            return 3

        async def updates(self, session):
            if batches:
                return batches.pop(0)
            raise asyncio.CancelledError()      # ends the loop like Ctrl+C

    class Api:
        def __init__(self, session, token):
            pass

        async def send(self, chat_id, reply):
            sent.append((chat_id, reply))

    class Notifier:
        def __init__(self, config):
            pass

        async def send(self, text, **kw):
            sent.append((None, text))

        async def drain(self):
            return None

        def _redacted(self, text):
            return text

    monkeypatch.setattr(tc, "TelegramPoller", Poller)
    monkeypatch.setattr(tc, "BotApi", Api)
    monkeypatch.setattr(tc, "Notifier", Notifier)
    config = settings()
    config.telegram = types.SimpleNamespace(enabled=True, user_ids=[7], bot_token="t")

    with pytest.raises(asyncio.CancelledError):
        await tc.serve(config, dry_run=True)

    assert [chat for chat, _ in sent] == [7, 7], "the stranger's /run was answered"
    hello, help_reply = sent[0][1], sent[1][1]
    assert "DRY-RUN" in hello.text and hello.keyboard
    assert "Buttons below" in help_reply.text


async def test_nothing_starts_without_telegram_configured(capsys):
    config = settings()
    config.telegram = types.SimpleNamespace(enabled=False, user_ids=[], bot_token="")
    assert await tc.serve(config, dry_run=True) == 1
    assert "BotFather" in capsys.readouterr().out


# -- switching between live and dry ----------------------------------------------------


async def test_going_live_takes_a_confirmed_button():
    h = Harness(dry_run=True)
    ask = await h.say("🔁 Live / Dry run")
    assert "real funds" in ask.text
    assert h.c.dry_run is True, "it switched before it was confirmed"

    reply = await h.c.press(button(ask, "🔴"))
    assert h.c.dry_run is False and "LIVE" in reply.text

    h.c.pending = None
    await h.start_run()
    assert h.runs[0][1] is False, "the next run did not trade live"
    h.release.set()
    await h.settle()


async def test_back_to_dry_and_cancel():
    h = Harness(dry_run=False)
    ask = await h.say("/mode")
    assert "Cancelled" in (await h.c.press(button(ask, "❌"))).text
    assert h.c.dry_run is False

    ask = await h.say("/mode")
    await h.c.press(button(ask, "🧪"))
    assert h.c.dry_run is True


async def test_no_switching_while_something_runs():
    """A run and its own close must not trade different money."""
    h = Harness(dry_run=True)
    await h.start_run()
    reply = await h.say("🔁 Live / Dry run")
    assert "in progress" in reply.text and not reply.buttons
    assert h.c.dry_run is True
    h.release.set()
    await h.settle()


async def test_a_switch_confirmed_after_a_run_started_does_nothing():
    h = Harness(dry_run=True)
    ask = await h.say("/mode")
    h.c.pending, pending = None, h.c.pending   # a run slips in between
    await h.start_run()
    h.c.pending = pending
    reply = await h.c.press(button(ask, "🔴"))
    assert "mode unchanged" in reply.text and h.c.dry_run is True
    h.release.set()
    await h.settle()


# -- account management ------------------------------------------------------------------


class MarginDesk:
    """Fake planners and sender, standing in for menu.plan_* and submit_plan."""

    def __init__(self, moves):
        self.moves = moves
        self.planned = []
        self.sent = []

    def planner(self, kind):
        def plan(config):
            self.planned.append(kind)
            return types.SimpleNamespace(
                lines=["\n  master 1 6r...", "  sub 1    100.00"], moves=list(self.moves),
            )
        return plan

    def submit(self, config, moves):
        self.sent.append(moves)
        return [f"    {src} -> {dst} {amount:,.2f}: ok" for _t, src, dst, amount in moves]


def with_margin(h, moves):
    desk = MarginDesk(moves)
    h.c.margin = {"balance": desk.planner("balance"), "collect": desk.planner("collect")}
    h.c.submit_transfers = desk.submit
    return desk


MOVES = [(None, "SUB1PUBKEY11111", "MASTERPUBKEY111", 120.5),
         (None, "SUB2PUBKEY22222", "MASTERPUBKEY111", 80.0)]


async def test_accounts_offers_balance_and_collect():
    h = Harness()
    reply = await h.say("💰 Accounts")
    assert button(reply, "⚖️") == "margin:balance"
    assert button(reply, "⬆️") == "margin:collect"


async def test_collect_sends_exactly_the_plan_it_showed():
    h = Harness()
    desk = with_margin(h, MOVES)
    plan = await h.c.press("margin:collect")
    assert "2 transfer(s), 200.50 in all" in plan.text
    assert desk.sent == [], "it moved margin before it was confirmed"

    reply = await h.c.press(button(plan, "✅"))
    assert "Sending 2" in reply.text
    await h.settle()
    await asyncio.sleep(0.05)
    assert desk.sent == [MOVES]
    assert any("ok" in text for text in h.sent), "the outcome was never reported"


async def test_no_margin_moves_during_a_run():
    """Collecting mid-run takes the free margin an open position leans on."""
    h = Harness()
    desk = with_margin(h, MOVES)
    await h.start_run()
    reply = await h.c.press("margin:collect")
    assert "in progress" in reply.text and not reply.buttons
    assert desk.planned == []
    h.release.set()
    await h.settle()


async def test_dry_run_shows_the_plan_and_sends_nothing():
    h = Harness(dry_run=True)
    desk = with_margin(h, MOVES)
    plan = await h.c.press("margin:balance")
    assert "DRY-RUN" in plan.text and not plan.buttons
    assert desk.sent == []


async def test_a_stale_transfer_button_does_nothing():
    h = Harness()
    desk = with_margin(h, MOVES)
    plan = await h.c.press("margin:collect")
    h.now += tc.CONFIRM_TTL_S + 1
    assert "expired" in (await h.c.press(button(plan, "✅"))).text
    assert desk.sent == []


async def test_nothing_to_move_says_so():
    h = Harness()
    with_margin(h, [])
    reply = await h.c.press("margin:collect")
    assert "Nothing to collect" in reply.text and not reply.buttons


async def test_no_run_starts_while_transfers_are_going():
    h = Harness()
    desk = with_margin(h, MOVES)
    gate = __import__("threading").Event()
    real = desk.submit

    def slow(config, moves):
        gate.wait(2)
        return real(config, moves)

    h.c.submit_transfers = slow
    plan = await h.c.press("margin:collect")
    await h.c.press(button(plan, "✅"))
    assert "already in progress" in (await h.say("▶️ Run")).text
    assert "Sending margin transfers" in (await h.say("📊 Status")).text
    gate.set()
    await asyncio.sleep(0.2)
    assert desk.sent == [MOVES]

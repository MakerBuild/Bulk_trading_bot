"""Running the bot from Telegram.

What matters is what a chat window must never do: answer a stranger, start or
close anything without a fresh confirmation, run two pieces of work on the same
accounts, or replay commands typed while the process was off.
"""

import asyncio
import types

import pytest

from bulkdn import telegram_control as tc
from bulkdn.telegram_control import Controller, PollConflict, TelegramPoller


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
        self.codes = iter(["1111", "2222", "3333", "4444"])
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

    async def settle(self):
        for _ in range(5):
            await asyncio.sleep(0)


# -- nothing that spends goes without a fresh code ---------------------------


async def test_run_waits_for_its_code():
    h = Harness()
    reply = await h.c.handle("/run")
    assert "/yes 1111" in reply and "LIVE" in reply
    assert h.runs == [], "a run started before it was confirmed"

    await h.c.handle("/yes 1111")
    await h.settle()
    assert len(h.runs) == 1


async def test_a_wrong_code_cancels_rather_than_allowing_another_guess():
    h = Harness()
    await h.c.handle("/run")
    assert "wrong code" in await h.c.handle("/yes 9999")
    assert "nothing to confirm" in await h.c.handle("/yes 1111")
    assert h.runs == []


async def test_a_code_lapses():
    h = Harness()
    await h.c.handle("/run")
    h.now += tc.CONFIRM_TTL_S + 1
    assert "nothing to confirm" in await h.c.handle("/yes 1111")
    assert h.runs == []


async def test_close_waits_for_its_code():
    h = Harness()
    reply = await h.c.handle("/close")
    assert "every position at market" in reply and "/yes 1111" in reply
    assert h.closes == []
    await h.c.handle("/yes 1111")
    await h.settle()
    assert h.closes == [False]


# -- one piece of work at a time ---------------------------------------------


async def test_a_second_run_is_refused_while_one_is_going():
    h = Harness()
    await h.c.handle("/run")
    await h.c.handle("/yes 1111")
    await h.settle()

    assert "already in progress" in await h.c.handle("/run")
    h.release.set()
    await h.settle()


async def test_a_volume_target_applies_to_that_run_only():
    h = Harness()
    await h.c.handle("/run 250k")
    await h.c.handle("/yes 1111")
    await h.settle()

    config, dry_run = h.runs[0]
    assert config.target.volume_usd == 250_000
    assert h.config.target.volume_usd == 100_000, "the next /run would inherit it"
    assert dry_run is False
    h.release.set()
    await h.settle()


async def test_a_dry_control_loop_starts_dry_runs():
    h = Harness(dry_run=True)
    assert "DRY-RUN" in await h.c.handle("/run")
    await h.c.handle("/yes 1111")
    await h.settle()
    assert h.runs[0][1] is True
    h.release.set()
    await h.settle()


# -- stop and close against a run --------------------------------------------


async def test_stop_is_the_s_key():
    h = Harness()
    assert "nothing is running" in await h.c.handle("/stop")
    await h.c.handle("/run")
    await h.c.handle("/yes 1111")
    await h.settle()

    reply = await h.c.handle("/stop")
    assert h.strategy.stops == ["telegram"]
    assert "positions stay" in reply
    h.release.set()
    await h.settle()


async def test_close_during_a_run_stops_it_first_then_closes():
    h = Harness()
    await h.c.handle("/run")
    await h.c.handle("/yes 1111")
    await h.settle()

    await h.c.handle("/close")
    await h.c.handle("/yes 2222")
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
    await h.c.handle("/run")
    await h.c.handle("/yes 1111")
    await h.c.handle("/close")
    await h.c.handle("/yes 2222")
    await h.settle()

    started.set()                    # the strategy exists now
    await asyncio.sleep(0.6)
    assert h.strategy.stops == ["telegram /close"]
    h.release.set()
    await h.settle()
    assert h.closes == [False]


# -- answers -----------------------------------------------------------------


async def test_status_during_a_run_is_the_screen_block():
    h = Harness()
    await h.c.handle("/run")
    await h.c.handle("/yes 1111")
    await h.settle()

    reply = await h.c.handle("/status")
    assert "g1:BTC-USD OPEN cycle 1" in reply and "LIVE run" in reply
    h.release.set()
    await h.settle()


async def test_idle_status_leaves_out_the_account_pubkeys():
    listing = (
        "accounts     : 2 on 1 socket(s)\n"
        "  m1     ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ\n"
        "positions:\n  BTC-USD    net=+0.00000000  all flat\n"
    )
    h = Harness(status_text=listing)
    reply = await h.c.handle("/status")
    assert "all flat" in reply
    assert "ZZZZZZZZ" not in reply


async def test_log_sends_the_tail(tmp_path):
    path = tmp_path / "logs.txt"
    path.write_text("".join(f"2026-09-25 18:00:{i:02d} INFO line {i}\n" for i in range(40)))
    h = Harness()
    h.c.log_path = str(path)

    reply = await h.c.handle("/log 3")
    assert "line 39" in reply and "line 37" in reply and "line 36" not in reply
    assert "2026-09-25" not in reply


async def test_unknown_and_plain_text():
    h = Harness()
    assert "/help" in await h.c.handle("/frobnicate")
    assert await h.c.handle("hello") is None
    assert "/run" in await h.c.handle("/help@SomeBot")


def test_amounts_are_read_the_way_people_type_them():
    assert tc._parse_amount("100000") == 100_000
    assert tc._parse_amount("100k") == 100_000
    assert tc._parse_amount("$1,500") == 1_500
    assert tc._parse_amount("1.5m") == 1_500_000
    assert tc._parse_amount("lots") is None


# -- who is answered -----------------------------------------------------------


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


# -- polling -------------------------------------------------------------------


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
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def post(self, url, json, timeout, proxy):
        self.calls.append(json)
        status, payload = self.answers.pop(0)
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
    assert session.calls[1]["offset"] == 42, "the backlog was not confirmed away"
    assert session.calls[2]["offset"] == 42


async def test_a_second_poller_on_the_token_is_reported():
    poller = TelegramPoller("t")
    with pytest.raises(PollConflict):
        await poller.updates(FakeSession([(409, {"ok": False})]))


# -- the loop as a whole ---------------------------------------------------------


async def test_the_loop_answers_ours_and_ignores_strangers(monkeypatch):
    sent = []
    batches = [
        [{"update_id": 1, "message": {"from": {"id": 9}, "text": "/run"}},
         {"update_id": 2, "message": {"from": {"id": 7}, "text": "/help"}}],
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

    class Notifier:
        def __init__(self, config):
            pass

        async def send(self, text, **kw):
            sent.append(text)

        async def drain(self):
            return None

        def _redacted(self, text):
            return text

    monkeypatch.setattr(tc, "TelegramPoller", Poller)
    monkeypatch.setattr(tc, "Notifier", Notifier)
    config = settings()
    config.telegram = types.SimpleNamespace(enabled=True, user_ids=[7], bot_token="t")

    with pytest.raises(asyncio.CancelledError):
        await tc.serve(config, dry_run=True)

    assert sent[0].startswith("control is on") and "DRY-RUN" in sent[0]
    assert len(sent) == 2 and "/run" in sent[1] and "commands" in sent[1], (
        "the stranger's /run was answered, or ours was not"
    )


async def test_nothing_starts_without_telegram_configured(capsys):
    config = settings()
    config.telegram = types.SimpleNamespace(enabled=False, user_ids=[], bot_token="")
    assert await tc.serve(config, dry_run=True) == 1
    assert "BotFather" in capsys.readouterr().out

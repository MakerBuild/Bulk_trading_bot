"""Running the bot from Telegram.

A long-lived process that listens for commands from the configured
`telegram.user_ids` and starts, watches, stops and closes runs on their
behalf. Meant for a server nobody is logged in to: the operator starts it once
over RDP, types the key password once, and after that the phone is enough.

It is the same bot underneath. A run started here is `cmd_run` on a fresh copy
of the settings, a close is `cmd_flatten`, and the idle status is `cmd_status`
-- nothing here trades on its own account.

Driven by buttons: a keyboard under the chat for the everyday commands, and
Start / Cancel buttons on the message that asks for a confirmation. The slash
commands still work for anyone who prefers typing.

Three rules keep a chat window from being a way to lose money:

* Only the ids in `telegram.user_ids` are answered, judged by who sent the
  message or pressed the button. Anyone else is ignored, and logged once.
* Anything that spends -- starting a run, closing at market -- is confirmed
  on a message that says exactly what will happen, and the confirmation is
  one-time: its button carries a code that lapses after a minute, is spent by
  the first press, and is superseded by the next question. A button on an old
  message does nothing.
* Commands sent while this process was not running are dropped at start. A
  /run typed yesterday into a dead chat must not fire the moment it comes back.

Polling, not a webhook: nothing listens on a port, so a server behind NAT or a
firewall needs no changes. Telegram lets one process poll a token at a time,
and answers a second with 409 -- which here doubles as a warning that another
copy of the bot is running.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import html
import io
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiohttp

from .notify import AlertHandler, Notifier, telegram_proxy
from .retry import describe

log = logging.getLogger(__name__)
# Where every bulkdn module's lines end up, and so where alerts are read from.
_BOT_LOG = logging.getLogger("bulkdn")

# How long a confirmation stays good.
CONFIRM_TTL_S = 60.0
# Seconds Telegram holds a getUpdates open waiting for a message.
POLL_TIMEOUT_S = 25
# Telegram refuses a message past 4096 characters, and the notifier splits
# longer ones -- through a <pre> if need be, leaving each half with an unclosed
# tag, which Telegram refuses outright. So bodies are kept well inside one.
_MAX_TEXT = 1700
LOG_LINES_DEFAULT = 15
LOG_LINES_MAX = 30

# `cmd_status` lists every account's full pubkey. Useful at a desk, noise on a
# phone, and nothing a status message needs.
_ACCOUNT_LINE = re.compile(r"^\s{2}\S+\s+[1-9A-HJ-NP-Za-km-z]{32,44}\s*$")

# The keyboard under the chat, and the command each of its buttons sends.
KEYBOARD = {
    "📊 Status": "/status",
    "▶️ Run": "/run",
    "⏹ Stop": "/stop",
    "🛑 Close all": "/close",
    "📜 Log": "/log",
}
_KEYBOARD_ROWS = [["📊 Status", "▶️ Run"], ["⏹ Stop", "🛑 Close all"], ["📜 Log"]]

# Spelled out down to the lines to paste: install.bat creates settings.yaml
# once and never touches it again, so a file made before Telegram existed has
# no telegram: block to "fill in".
SETUP_HELP = """
  Telegram is not set up yet.

    1. In Telegram, message @BotFather, send /newbot and copy the token.
    2. Message @userinfobot to get your numeric user id.
    3. Open the chat with your new bot and press Start.
    4. In settings.yaml, fill in the telegram: block -- or, if the file has
       none, add these lines at the end (not indented):

telegram:
  bot_token: "123456789:AAE-your-token-here"
  user_ids: [123456789]

  Then start this again."""

HELP = (
    "<b>Buttons below</b>\n"
    "📊 Status -- the run, or positions and orders when idle\n"
    "▶️ Run -- start a run with the targets in settings.yaml\n"
    "⏹ Stop -- stop the run and cancel its orders; positions stay\n"
    "🛑 Close all -- close every position at market (stops a run first)\n"
    "📜 Log -- the last lines of logs.txt\n\n"
    "<b>Typed</b>\n"
    "/run 250k -- a run with this volume target\n"
    "/log 30 -- more log lines"
)


class PollConflict(RuntimeError):
    """Telegram says another process is polling this bot token."""


Buttons = list[list[tuple[str, str]]]


@dataclass
class Reply:
    """A message to send: text, and optionally buttons under it.

    `buttons` are inline, each a (label, callback data) pair. `keyboard` puts
    the command keyboard under the chat instead -- a message carries one or the
    other, never both.
    """

    text: str
    buttons: Buttons | None = None
    keyboard: bool = False

    def markup(self) -> dict | None:
        if self.buttons:
            return {"inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in row]
                for row in self.buttons
            ]}
        if self.keyboard:
            return {
                "keyboard": [[{"text": label} for label in row] for row in _KEYBOARD_ROWS],
                "resize_keyboard": True,
                "is_persistent": True,
            }
        return None


@dataclass
class Pending:
    action: str
    code: str
    expires_at: float
    volume: float | None = None


class BotApi:
    """The few Bot API calls this needs, on one session."""

    def __init__(self, session: aiohttp.ClientSession, token: str):
        self.session = session
        self.token = token

    async def call(self, method: str, timeout_s: float = 20.0, **params):
        async with self.session.post(
            f"https://api.telegram.org/bot{self.token}/{method}",
            json=params,
            timeout=aiohttp.ClientTimeout(total=timeout_s),
            proxy=telegram_proxy(),
        ) as response:
            if response.status == 409:
                raise PollConflict("another process is polling this bot token")
            payload = await response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"telegram {method}: {payload.get('description', payload)}")
        return payload.get("result")

    async def send(self, chat_id: int, reply: Reply) -> None:
        params = {
            "chat_id": chat_id,
            "text": reply.text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        markup = reply.markup()
        if markup:
            params["reply_markup"] = markup
        await self.call("sendMessage", **params)

    async def edit(self, chat_id: int, message_id: int, reply: Reply) -> None:
        """Replace a message's text and buttons -- used to spend a button."""
        params = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": reply.text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply.buttons:
            params["reply_markup"] = reply.markup()
        try:
            await self.call("editMessageText", **params)
        except RuntimeError as exc:
            # A refresh that found nothing new. Not a failure.
            if "not modified" not in str(exc):
                raise

    async def answer(self, callback_id: str) -> None:
        """Stop the button's spinner. Telegram expects it for every press."""
        await self.call("answerCallbackQuery", callback_query_id=callback_id)


class TelegramPoller:
    """getUpdates, one batch at a time, remembering where it got to."""

    def __init__(self, token: str):
        self.token = token
        self.offset: int | None = None

    async def _call(self, session: aiohttp.ClientSession, **params) -> list:
        api = BotApi(session, self.token)
        return await api.call("getUpdates", timeout_s=POLL_TIMEOUT_S + 15, **params) or []

    async def skip_backlog(self, session: aiohttp.ClientSession) -> int:
        """Drop everything sent before now. Returns how many were dropped.

        `offset=-1` answers with the newest update only; confirming past it
        discards the rest on Telegram's side as well.
        """
        latest = await self._call(session, offset=-1, timeout=0)
        if not latest:
            return 0
        self.offset = latest[-1]["update_id"] + 1
        await self._call(session, offset=self.offset, timeout=0)
        return len(latest)

    async def updates(self, session: aiohttp.ClientSession) -> list[dict]:
        params = {"timeout": POLL_TIMEOUT_S, "allowed_updates": ["message", "callback_query"]}
        if self.offset is not None:
            params["offset"] = self.offset
        batch = await self._call(session, **params)
        if batch:
            self.offset = batch[-1]["update_id"] + 1
        return batch


RunFn = Callable[..., Awaitable[int]]


class Controller:
    """Turns commands and button presses into runs, stops, closes and answers.

    Holds at most one piece of work at a time -- a run or a close -- because
    two of either on the same accounts is the double-hedging this bot has
    spent its history preventing.
    """

    def __init__(
        self,
        config,
        dry_run: bool,
        *,
        run_fn: RunFn,
        flatten_fn: RunFn,
        status_fn: Callable[[object], Awaitable[str]],
        send: Callable[[str], Awaitable[None]],
        log_path: str = "logs.txt",
        clock: Callable[[], float] = time.monotonic,
        make_code: Callable[[], str] | None = None,
        alerts: AlertHandler | None = None,
    ):
        self.config = config
        self.dry_run = dry_run
        self.run_fn = run_fn
        self.flatten_fn = flatten_fn
        self.status_fn = status_fn
        self.send = send
        self.log_path = log_path
        self.clock = clock
        self.make_code = make_code or (lambda: secrets.token_hex(4))
        self.alerts = alerts
        self.pending: Pending | None = None
        self.work: asyncio.Task | None = None
        self.work_kind = ""
        self.work_started = 0.0
        self.strategy = None

    # -- state -------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self.work is not None and not self.work.done()

    @property
    def mode_word(self) -> str:
        return "DRY-RUN" if self.dry_run else "LIVE"

    def _set_strategy(self, strategy) -> None:
        self.strategy = strategy

    # -- messages ------------------------------------------------------------

    async def handle(self, text: str | None) -> Reply | None:
        """Answer one message from an authorised user. None means no reply."""
        if not text:
            return None
        text = KEYBOARD.get(text.strip(), text)
        if not text.startswith("/"):
            return Reply("Use the buttons below.", keyboard=True)
        words = text.strip().split()
        # "/status@SomeBot" is what a command looks like in a group chat.
        command = words[0].lower().split("@")[0]
        args = words[1:]

        if command in ("/help", "/start"):
            return Reply(HELP, keyboard=True)
        if command == "/status":
            return await self._status()
        if command == "/log":
            return self._log(args)
        if command == "/stop":
            return self._stop()
        if command == "/run":
            return self._ask_run(args)
        if command == "/close":
            return self._ask_close()
        if command == "/yes":
            return self._confirm(args)
        return Reply(f"Unknown command {html.escape(command)}.", keyboard=True)

    async def press(self, data: str) -> Reply:
        """Answer a button press. The reply replaces the pressed message."""
        action, _, code = (data or "").partition(":")
        if action == "refresh":
            return await self._status() if code == "status" else self._log([])
        pending = self.pending
        if pending is None or code != pending.code or self.clock() > pending.expires_at:
            # Not cleared: a stale button on an old message must not cancel
            # the question that is actually open.
            return Reply("⌛ This button has expired. Ask again.")
        self.pending = None
        if action == "cancel":
            return Reply("Cancelled.")
        if action != pending.action:
            return Reply("⌛ This button has expired. Ask again.")
        return self._execute(pending)

    # -- answers -----------------------------------------------------------------

    async def _status(self) -> Reply:
        refresh = [[("🔄 Refresh", "refresh:status")]]
        if self.busy and self.work_kind == "close":
            return Reply("Closing positions...", refresh)
        if self.busy:
            minutes = (self.clock() - self.work_started) / 60
            if self.strategy is None:
                return Reply(f"<b>{self.mode_word} run starting</b> ({minutes:.0f} min)", refresh)
            try:
                lines = self.strategy.status_lines()
            except Exception as exc:  # noqa: BLE001 - a status must never fail the run
                return Reply(
                    f"<b>{self.mode_word} run</b>, {minutes:.0f} min -- status unavailable: "
                    f"{html.escape(describe(exc))}", refresh,
                )
            body = "\n".join(line.strip() for line in lines if line.strip())
            return Reply(
                f"<b>{self.mode_word} run</b>, {minutes:.0f} min\n<pre>{html.escape(body)}</pre>",
                refresh,
            )
        try:
            text = await self.status_fn(copy.deepcopy(self.config))
        except Exception as exc:  # noqa: BLE001 - report it, keep listening
            return Reply(f"Idle -- could not read positions: {html.escape(describe(exc))}", refresh)
        text = "\n".join(line for line in text.splitlines() if not _ACCOUNT_LINE.match(line))
        return Reply(
            f"<b>Idle</b> ({self.mode_word})\n<pre>{html.escape(text[-_MAX_TEXT:])}</pre>", refresh,
        )

    def _log(self, args: list[str]) -> Reply:
        try:
            wanted = int(args[0]) if args else LOG_LINES_DEFAULT
        except ValueError:
            return Reply("Usage: /log 30")
        wanted = max(1, min(wanted, LOG_LINES_MAX))
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()[-wanted:]
        except OSError as exc:
            return Reply(f"Could not read {html.escape(self.log_path)}: {html.escape(str(exc))}")
        # The date is the same on every line of a recent tail; the time is not.
        text = "".join(line[11:] if line[:4].isdigit() else line for line in lines)
        return Reply(f"<pre>{html.escape(text[-_MAX_TEXT:])}</pre>", [[("🔄 Refresh", "refresh:log")]])

    def _stop(self) -> Reply:
        if not self.busy:
            return Reply("Nothing is running.")
        if self.work_kind == "close":
            return Reply("A close is in progress -- it cannot be stopped halfway.")
        if self.strategy is None:
            return Reply("The run is still starting -- press Stop again in a few seconds.")
        self.strategy.request_stop("telegram")
        return Reply("⏹ Stopping -- orders are cancelled, open positions stay. 🛑 Close all closes them.")

    def _goal(self, volume: float | None) -> str:
        if volume is not None:
            return f"volume target ${volume:,.0f}"
        target = self.config.target
        goals = []
        if target.volume_usd > 0:
            goals.append(f"volume ${target.volume_usd:,.0f}")
        if target.burn_usd > 0:
            goals.append(f"fees ${target.burn_usd:,.2f}")
        if target.cycles > 0:
            goals.append(f"{target.cycles} cycle(s)")
        return ", ".join(goals) or "no target -- runs until Stop"

    def _ask_run(self, args: list[str]) -> Reply:
        if self.busy:
            return Reply(f"A {self.work_kind} is already in progress -- stop it first.")
        volume = None
        if args:
            volume = _parse_amount(args[0])
            if volume is None or volume <= 0:
                return Reply("Usage: /run or /run 250k (volume target in $)")
        code = self._new_pending("run", volume)
        markets = ", ".join(leg.symbol for leg in self.config.active_legs)
        other = "" if volume is not None else "\nAnother target: type /run 250k"
        return Reply(
            f"Start a <b>{self.mode_word}</b> run?\n"
            f"{html.escape(self.config.mode)} mode, {html.escape(markets)}\n"
            f"{html.escape(self._goal(volume))}{other}",
            [[("▶️ Start", f"run:{code}"), ("❌ Cancel", f"cancel:{code}")]],
        )

    def _ask_close(self) -> Reply:
        if self.busy and self.work_kind == "close":
            return Reply("A close is already in progress.")
        code = self._new_pending("close")
        first = "Stop the current run, then close" if self.busy else "Close"
        return Reply(
            f"{first} <b>every position at market</b> on every account ({self.mode_word})?",
            [[("✅ Yes, close all", f"close:{code}"), ("❌ Cancel", f"cancel:{code}")]],
        )

    def _new_pending(self, action: str, volume: float | None = None) -> str:
        code = self.make_code()
        self.pending = Pending(action, code, self.clock() + CONFIRM_TTL_S, volume)
        return code

    def _confirm(self, args: list[str]) -> Reply:
        """The typed confirmation, `/yes CODE`, for anyone not using buttons."""
        pending, self.pending = self.pending, None
        if pending is None or self.clock() > pending.expires_at:
            return Reply("Nothing to confirm -- it expired or was never asked.")
        if not args or args[0] != pending.code:
            # Cleared either way: one wrong guess ends it.
            return Reply("Wrong code -- cancelled. Ask again.")
        return self._execute(pending)

    def _execute(self, pending: Pending) -> Reply:
        if pending.action == "run":
            if self.busy:
                return Reply(f"A {self.work_kind} started meanwhile -- not starting another.")
            self._start("run", self._do_run(pending.volume))
            return Reply(
                f"▶️ Starting the <b>{self.mode_word}</b> run: "
                f"{html.escape(self._goal(pending.volume))}.",
                [[("📊 Status", "refresh:status")]],
            )
        if pending.action == "close":
            if self.busy and self.work_kind == "close":
                return Reply("A close is already in progress.")
            previous = self.work if self.busy else None
            self._start("close", self._do_close(previous))
            return Reply("🛑 Closing every position at market -- I will report when done.")
        return Reply("Nothing to confirm.")

    # -- work --------------------------------------------------------------

    def _start(self, kind: str, coro) -> None:
        self.work_kind = kind
        self.work_started = self.clock()
        self.work = asyncio.get_running_loop().create_task(coro)

    async def _do_run(self, volume: float | None) -> None:
        config = copy.deepcopy(self.config)
        if volume is not None:
            config.target.volume_usd = volume
        try:
            code = await self.run_fn(config, self.dry_run, on_strategy=self._set_strategy)
        except Exception as exc:  # noqa: BLE001 - reported, and the loop carries on
            log.exception("run started from telegram failed")
            await self.send(f"⚠️ The run ended with an error: {html.escape(describe(exc))}")
        else:
            await self.send(f"The run has ended (exit code {code}).")
        finally:
            self.strategy = None

    async def _do_close(self, running: asyncio.Task | None) -> None:
        if running is not None:
            # A run still starting has no strategy to stop yet. Waiting on the
            # task alone would wait out the whole run, so the stop is sent the
            # moment there is something to send it to.
            while not running.done() and self.strategy is None:
                await asyncio.sleep(0.5)
            if self.strategy is not None:
                self.strategy.request_stop("telegram close")
            # Waited for, not cancelled: a run cut off between sending an order
            # and hearing back leaves exactly the order nobody can account for.
            with contextlib.suppress(Exception):
                await running
        # A flatten that cannot finish says so in the log with MANUAL ACTION
        # REQUIRED; forwarded, so the phone hears it too.
        if self.alerts is not None:
            self.alerts.attach(_BOT_LOG)
        try:
            code = await self.flatten_fn(copy.deepcopy(self.config), self.dry_run)
        except Exception as exc:  # noqa: BLE001 - reported
            log.exception("close started from telegram failed")
            await self.send(f"⚠️ Close failed: {html.escape(describe(exc))} -- check Status now.")
            return
        finally:
            if self.alerts is not None:
                self.alerts.detach(_BOT_LOG)
        verdict = "done" if code == 0 else f"finished with exit code {code}"
        await self.send(f"🛑 Close {verdict}. Press Status to see what is left.")

    async def shutdown(self, timeout_s: float = 120.0) -> None:
        """Let work in progress finish before the process exits."""
        if not self.busy:
            return
        if self.work_kind == "run" and self.strategy is not None:
            self.strategy.request_stop("control loop exiting")
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(self.work), timeout_s)


def _parse_amount(text: str) -> float | None:
    """ "100000", "100k", "1.5m", "$100,000" -> dollars."""
    cleaned = text.lower().replace("$", "").replace(",", "").replace("_", "")
    scale = 1.0
    if cleaned.endswith("k"):
        scale, cleaned = 1_000.0, cleaned[:-1]
    elif cleaned.endswith("m"):
        scale, cleaned = 1_000_000.0, cleaned[:-1]
    try:
        return float(cleaned) * scale
    except ValueError:
        return None


def _sender_allowed(sender, allowed: set[int], strangers: set[int]) -> bool:
    if sender in allowed:
        return True
    if sender is not None and sender not in strangers:
        strangers.add(sender)
        log.warning("telegram control: ignoring messages from user id %s", sender)
    return False


def authorised_text(update: dict, allowed: set[int], strangers: set[int]) -> str | None:
    """The text of a message from an allowed user, or None.

    Judged by who SENT it, not by the chat it is in: a group the bot was added
    to has members who are not on the list.
    """
    message = update.get("message") or {}
    sender = (message.get("from") or {}).get("id")
    if not _sender_allowed(sender, allowed, strangers):
        return None
    text = message.get("text")
    if text:
        log.info("telegram command from %s: %s", sender, text[:60])
    return text


def authorised_press(update: dict, allowed: set[int], strangers: set[int]) -> dict | None:
    """A button press by an allowed user, or None. Judged by who pressed it."""
    press = update.get("callback_query") or {}
    sender = (press.get("from") or {}).get("id")
    if not _sender_allowed(sender, allowed, strangers):
        return None
    log.info("telegram button from %s: %s", sender, (press.get("data") or "").split(":")[0])
    return press


async def capture_status(config) -> str:
    """`cmd_status`'s printout, as text."""
    from .cli import cmd_status

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        await cmd_status(config)
    return buffer.getvalue()


async def _handle_update(update: dict, api: BotApi, controller: Controller,
                         allowed: set[int], strangers: set[int]) -> None:
    if "callback_query" in update:
        press = authorised_press(update, allowed, strangers)
        if press is None:
            return
        with contextlib.suppress(Exception):
            await api.answer(press["id"])
        reply = await controller.press(press.get("data") or "")
        message = press.get("message") or {}
        chat = (message.get("chat") or {}).get("id")
        if chat is not None and message.get("message_id") is not None:
            await api.edit(chat, message["message_id"], reply)
        return

    text = authorised_text(update, allowed, strangers)
    if text is None:
        return
    reply = await controller.handle(text)
    chat = ((update.get("message") or {}).get("chat") or {}).get("id")
    if reply is not None and chat is not None:
        await api.send(chat, reply)


async def serve(config, dry_run: bool, *, log_path: str = "logs.txt") -> int:
    """Listen for commands until interrupted."""
    from .cli import cmd_flatten, cmd_run

    telegram = config.telegram
    if not telegram.enabled:
        print(SETUP_HELP)
        return 1

    notifier = Notifier(telegram)
    allowed = set(telegram.user_ids)
    controller = Controller(
        config, dry_run,
        run_fn=cmd_run, flatten_fn=cmd_flatten, status_fn=capture_status,
        send=notifier.send, log_path=log_path, alerts=AlertHandler(notifier),
    )
    poller = TelegramPoller(telegram.bot_token)
    strangers: set[int] = set()
    backoff = 5.0

    try:
        async with aiohttp.ClientSession() as session:
            api = BotApi(session, telegram.bot_token)
            try:
                dropped = await poller.skip_backlog(session)
            except PollConflict:
                print("Another process is already polling this bot token -- is a second copy running?")
                return 1
            if dropped:
                log.info("telegram control: ignored %d command(s) sent while it was off", dropped)
            log.info("telegram control is on (%s) for user id(s) %s",
                     controller.mode_word, ", ".join(map(str, sorted(allowed))))
            print(f"\n  Telegram control is on ({controller.mode_word}). Use the buttons in the chat.")
            print("  Ctrl+C here stops listening; a run in progress is stopped first.\n")
            hello = Reply(f"✅ Control is on (<b>{controller.mode_word}</b>). Use the buttons below.",
                          keyboard=True)
            for user_id in sorted(allowed):
                try:
                    await api.send(user_id, hello)
                except Exception as exc:  # noqa: BLE001 - one unreachable user must not stop it
                    log.warning("telegram: could not message %s: %s", user_id,
                                notifier._redacted(describe(exc)))

            while True:
                try:
                    batch = await poller.updates(session)
                    backoff = 5.0
                except PollConflict:
                    log.error("another process is polling this bot token -- is a second copy running?")
                    await notifier.send("⚠️ Another process is polling this bot -- is a second copy running?")
                    await asyncio.sleep(30)
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep listening
                    log.warning("telegram poll failed: %s", notifier._redacted(describe(exc)))
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 120.0)
                    continue

                for update in batch:
                    try:
                        await _handle_update(update, api, controller, allowed, strangers)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - one bad update must not end the loop
                        log.warning("telegram update failed: %s", notifier._redacted(describe(exc)))
    finally:
        await controller.shutdown()
        await notifier.drain()

"""Running the bot from Telegram.

A long-lived process that listens for commands from the configured
`telegram.user_ids` and starts, watches, stops and closes runs on their
behalf. Meant for a server nobody is logged in to: the operator starts it once
over RDP, types the key password once, and after that the phone is enough.

It is the same bot underneath. A run started here is `cmd_run` on a fresh copy
of the settings, a close is `cmd_flatten`, and the idle status is `cmd_status`
-- nothing here trades on its own account.

Three rules keep a chat window from being a way to lose money:

* Only the ids in `telegram.user_ids` are answered. Anyone else who finds the
  bot is ignored, and logged once.
* Anything that spends -- starting a run, closing at market -- is confirmed
  with a one-time code that lapses after a minute. A wrong code cancels it, so
  a code cannot be guessed at leisure.
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

# How long a confirmation code stays good.
CONFIRM_TTL_S = 60.0
# Seconds Telegram holds a getUpdates open waiting for a message.
POLL_TIMEOUT_S = 25
# Kept inside one of the notifier's chunks (1900 characters, header
# included). A longer text is split, and a split through a <pre> leaves each
# half with an unclosed tag -- which Telegram refuses outright.
_MAX_TEXT = 1700
LOG_LINES_DEFAULT = 15
LOG_LINES_MAX = 30

# `cmd_status` lists every account's full pubkey. Useful at a desk, noise on a
# phone, and nothing a status message needs.
_ACCOUNT_LINE = re.compile(r"^\s{2}\S+\s+[1-9A-HJ-NP-Za-km-z]{32,44}\s*$")

HELP = (
    "<b>commands</b>\n"
    "/status -- the run, or positions and orders when idle\n"
    "/run -- start a run with the targets in settings.yaml\n"
    "/run 100000 -- start a run with this volume target ($)\n"
    "/stop -- stop the run and cancel its orders; positions stay\n"
    "/close -- close every position at market (stops a run first)\n"
    "/log [n] -- the last n lines of logs.txt\n"
    "/yes CODE -- confirm a /run or /close"
)


class PollConflict(RuntimeError):
    """Telegram says another process is polling this bot token."""


@dataclass
class Pending:
    action: str
    code: str
    expires_at: float
    volume: float | None = None


class TelegramPoller:
    """getUpdates, one batch at a time, remembering where it got to."""

    def __init__(self, token: str):
        self.token = token
        self.offset: int | None = None

    async def _call(self, session: aiohttp.ClientSession, method: str, **params) -> list:
        async with session.post(
            f"https://api.telegram.org/bot{self.token}/{method}",
            json=params,
            timeout=aiohttp.ClientTimeout(total=POLL_TIMEOUT_S + 15),
            proxy=telegram_proxy(),
        ) as response:
            if response.status == 409:
                raise PollConflict("another process is polling this bot token")
            payload = await response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"telegram {method}: {payload.get('description', payload)}")
        return payload.get("result") or []

    async def skip_backlog(self, session: aiohttp.ClientSession) -> int:
        """Drop everything sent before now. Returns how many were dropped.

        `offset=-1` answers with the newest update only; confirming past it
        discards the rest on Telegram's side as well.
        """
        latest = await self._call(session, "getUpdates", offset=-1, timeout=0)
        if not latest:
            return 0
        self.offset = latest[-1]["update_id"] + 1
        await self._call(session, "getUpdates", offset=self.offset, timeout=0)
        return len(latest)

    async def updates(self, session: aiohttp.ClientSession) -> list[dict]:
        params = {"timeout": POLL_TIMEOUT_S, "allowed_updates": ["message"]}
        if self.offset is not None:
            params["offset"] = self.offset
        batch = await self._call(session, "getUpdates", **params)
        if batch:
            self.offset = batch[-1]["update_id"] + 1
        return batch


RunFn = Callable[..., Awaitable[int]]


class Controller:
    """Turns commands into runs, stops, closes and answers.

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
        self.make_code = make_code or (lambda: f"{secrets.randbelow(10_000):04d}")
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

    # -- commands ----------------------------------------------------------

    async def handle(self, text: str | None) -> str | None:
        """Answer one message from an authorised user. None means no reply."""
        if not text or not text.startswith("/"):
            return None
        words = text.strip().split()
        # "/status@SomeBot" is what a command looks like in a group chat.
        command = words[0].lower().split("@")[0]
        args = words[1:]

        if command in ("/help", "/start"):
            return HELP
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
        return f"unknown command {html.escape(command)} -- /help"

    async def _status(self) -> str:
        if self.busy and self.work_kind == "close":
            return "closing positions -- /status again when it is done"
        if self.busy:
            minutes = (self.clock() - self.work_started) / 60
            if self.strategy is None:
                return f"<b>{self.mode_word} run starting</b> ({minutes:.0f} min)"
            try:
                lines = self.strategy.status_lines()
            except Exception as exc:  # noqa: BLE001 - a status must never fail the run
                return f"<b>{self.mode_word} run</b>, {minutes:.0f} min -- status unavailable: {html.escape(describe(exc))}"
            body = "\n".join(line.strip() for line in lines if line.strip())
            return f"<b>{self.mode_word} run</b>, {minutes:.0f} min\n<pre>{html.escape(body)}</pre>"
        try:
            text = await self.status_fn(copy.deepcopy(self.config))
        except Exception as exc:  # noqa: BLE001 - report it, keep listening
            return f"idle -- could not read positions: {html.escape(describe(exc))}"
        text = "\n".join(line for line in text.splitlines() if not _ACCOUNT_LINE.match(line))
        return f"<b>idle</b> ({self.mode_word})\n<pre>{html.escape(text[-_MAX_TEXT:])}</pre>"

    def _log(self, args: list[str]) -> str:
        try:
            wanted = int(args[0]) if args else LOG_LINES_DEFAULT
        except ValueError:
            return "usage: /log [number of lines]"
        wanted = max(1, min(wanted, LOG_LINES_MAX))
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()[-wanted:]
        except OSError as exc:
            return f"could not read {html.escape(self.log_path)}: {html.escape(str(exc))}"
        # The date is the same on every line of a recent tail; the time is not.
        text = "".join(line[11:] if line[:4].isdigit() else line for line in lines)
        return f"<pre>{html.escape(text[-_MAX_TEXT:])}</pre>"

    def _stop(self) -> str:
        if not self.busy:
            return "nothing is running"
        if self.work_kind == "close":
            return "a close is in progress -- it cannot be stopped halfway"
        if self.strategy is None:
            return "the run is still starting -- send /stop again in a few seconds"
        self.strategy.request_stop("telegram")
        return "stopping -- orders are cancelled, open positions stay. /close closes them."

    def _ask_run(self, args: list[str]) -> str:
        if self.busy:
            return f"a {self.work_kind} is already in progress -- /stop it first"
        volume = None
        if args:
            volume = _parse_amount(args[0])
            if volume is None or volume <= 0:
                return "usage: /run or /run 100000 (volume target in $)"
        target = self.config.target
        if volume is not None:
            goal = f"volume target ${volume:,.0f}"
        else:
            goals = []
            if target.volume_usd > 0:
                goals.append(f"volume ${target.volume_usd:,.0f}")
            if target.burn_usd > 0:
                goals.append(f"fees ${target.burn_usd:,.2f}")
            if target.cycles > 0:
                goals.append(f"{target.cycles} cycle(s)")
            goal = "targets from settings: " + (", ".join(goals) or "none -- runs until /stop")
        code = self._new_pending("run", volume)
        markets = ", ".join(leg.symbol for leg in self.config.active_legs)
        return (
            f"Start a <b>{self.mode_word}</b> run: {html.escape(self.config.mode)} mode, "
            f"{html.escape(markets)}, {html.escape(goal)}.\n"
            f"Confirm within {CONFIRM_TTL_S:.0f}s: /yes {code}"
        )

    def _ask_close(self) -> str:
        if self.busy and self.work_kind == "close":
            return "a close is already in progress"
        code = self._new_pending("close")
        first = "stop the current run, then " if self.busy else ""
        return (
            f"This will {first}close <b>every position at market</b> on every "
            f"account ({self.mode_word}).\n"
            f"Confirm within {CONFIRM_TTL_S:.0f}s: /yes {code}"
        )

    def _new_pending(self, action: str, volume: float | None = None) -> str:
        code = self.make_code()
        self.pending = Pending(action, code, self.clock() + CONFIRM_TTL_S, volume)
        return code

    def _confirm(self, args: list[str]) -> str:
        pending, self.pending = self.pending, None
        if pending is None or self.clock() > pending.expires_at:
            return "nothing to confirm -- it expired or was never asked"
        if not args or args[0] != pending.code:
            # Cleared either way: one wrong guess ends it.
            return "wrong code -- cancelled. Ask again."
        if pending.action == "run":
            if self.busy:
                return f"a {self.work_kind} started meanwhile -- not starting another"
            self._start("run", self._do_run(pending.volume))
            return f"starting the {self.mode_word} run. /status to watch, /stop to stop."
        if pending.action == "close":
            if self.busy and self.work_kind == "close":
                return "a close is already in progress"
            previous = self.work if self.busy else None
            self._start("close", self._do_close(previous))
            return "closing every position at market -- I will report when done."
        return "nothing to confirm"

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
            await self.send(f"⚠️ the run ended with an error: {html.escape(describe(exc))}\n/status to check.")
        else:
            await self.send(f"run ended (exit code {code}). /status to check, /run for another.")
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
                self.strategy.request_stop("telegram /close")
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
            await self.send(f"⚠️ close failed: {html.escape(describe(exc))} -- check /status now")
            return
        finally:
            if self.alerts is not None:
                self.alerts.detach(_BOT_LOG)
        verdict = "done" if code == 0 else f"finished with exit code {code}"
        await self.send(f"close {verdict}. /status to check what is left.")

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


def authorised_text(update: dict, allowed: set[int], strangers: set[int]) -> str | None:
    """The text of an update from an allowed user, or None.

    Judged by who SENT it, not by the chat it is in: a group the bot was added
    to has members who are not on the list.
    """
    message = update.get("message") or {}
    sender = (message.get("from") or {}).get("id")
    if sender not in allowed:
        if sender is not None and sender not in strangers:
            strangers.add(sender)
            log.warning("telegram control: ignoring messages from user id %s", sender)
        return None
    text = message.get("text")
    if text:
        log.info("telegram command from %s: %s", sender, text[:60])
    return text


async def capture_status(config) -> str:
    """`cmd_status`'s printout, as text."""
    from .cli import cmd_status

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        await cmd_status(config)
    return buffer.getvalue()


async def serve(config, dry_run: bool, *, log_path: str = "logs.txt") -> int:
    """Listen for commands until interrupted."""
    from .cli import cmd_flatten, cmd_run

    telegram = config.telegram
    if not telegram.enabled:
        print(
            "Telegram is not configured. Put the bot token from @BotFather and "
            "your numeric user id in the telegram: block of settings.yaml.",
        )
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
            try:
                dropped = await poller.skip_backlog(session)
            except PollConflict:
                print("Another process is already polling this bot token -- is a second copy running?")
                return 1
            if dropped:
                log.info("telegram control: ignored %d command(s) sent while it was off", dropped)
            log.info("telegram control is on (%s) for user id(s) %s",
                     controller.mode_word, ", ".join(map(str, sorted(allowed))))
            print(f"\n  Telegram control is on ({controller.mode_word}). Send /help to the bot.")
            print("  Ctrl+C here stops listening; a run in progress is stopped first.\n")
            await notifier.send(f"control is on (<b>{controller.mode_word}</b>). /help")

            while True:
                try:
                    batch = await poller.updates(session)
                    backoff = 5.0
                except PollConflict:
                    log.error("another process is polling this bot token -- is a second copy running?")
                    await notifier.send("⚠️ another process is polling this bot -- is a second copy running?")
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
                    text = authorised_text(update, allowed, strangers)
                    if text is None:
                        continue
                    try:
                        reply = await controller.handle(text)
                    except Exception as exc:  # noqa: BLE001 - one bad command must not end the loop
                        log.exception("telegram command failed")
                        reply = f"⚠️ {html.escape(describe(exc))}"
                    if reply:
                        await notifier.send(reply)
    finally:
        await controller.shutdown()
        await notifier.drain()

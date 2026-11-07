"""Running the bot from Telegram.

A long-lived process that listens for commands from the configured
`telegram.user_ids` and starts, watches, stops and closes runs on their
behalf. Meant for a server nobody is logged in to: the operator starts it once
over RDP, types the key password once, and after that the phone is enough.

It is the same bot underneath. A run started here is `cmd_run` on a fresh copy
of the settings, a close is `cmd_flatten`, and the idle status is `cmd_status`
-- nothing here trades on its own account. "Fresh" means re-read: given the
settings file's path, each /run, /close and /status reads it again, so an edit
made while this process runs is what the next run trades. The signing keys
are not re-read -- they were decrypted once, at start, with the password typed
then.

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
import datetime as dt
import html
import io
import json
import logging
import os
import re
import secrets
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiohttp

from . import ratelimit
from . import tg_settings
from . import weekly
from .config import AUTO_SPREAD_LABELS as KIND_LABELS
from .config import TAKING_MODES
from .notify import AlertHandler, Notifier, telegram_proxy
from .retry import describe

log = logging.getLogger(__name__)
# Where every bulkdn module's lines end up, and so where alerts are read from.
_BOT_LOG = logging.getLogger("bulkdn")

# How long a confirmation stays good.
CONFIRM_TTL_S = 60.0
# Seconds Telegram holds a getUpdates open waiting for a message.
POLL_TIMEOUT_S = 25
# A reply is one message -- and on a refresh, one EDITED message -- so it cannot
# be split the way the notifier splits what it sends. Its free text is cut to
# its tail instead, measured before escaping, well inside Telegram's 4096.
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
    "🎛 Mode": "/tradingmode",
    "👥 Accounts": "/pick",
    "📜 Log": "/log",
    "💰 Margin": "/accounts",
    "🔁 Live / Dry run": "/mode",
    "⚙️ Settings": "/settings",
    "📅 Weekly report": "/week",
    # What the margin button was called before Mode and Accounts existed. A
    # phone keeps the old keyboard until the bot sends a new one, and a press
    # on it must still do what its label said.
    "💰 Accounts": "/accounts",
}
_KEYBOARD_ROWS = [
    ["📊 Status", "▶️ Run"], ["⏹ Stop", "🛑 Close all"],
    ["🎛 Mode", "👥 Accounts"], ["📜 Log", "💰 Margin"], ["⚙️ Settings", "🔁 Live / Dry run"],
    ["📅 Weekly report"],
]
# The automatic weekly report: how often the clock is looked at, how long
# after the payout it waits before reading, and how long it waits for the
# indexer to show the week's AURA before sending without it.
WEEKLY_CHECK_S = 300.0
WEEKLY_DELAY = dt.timedelta(minutes=30)
WEEKLY_POINTS_WAIT = dt.timedelta(hours=6)
# How long a typed answer to "send the new value" is waited for.
INPUT_TTL_S = 300.0

# The trading modes, in the order the picker offers them, and what each puts
# in play. The same ones the terminal menu offers.
TRADING_MODES = {
    "multi": "every master and every sub, in one pool",
    "single": "one master and its own sub-accounts",
    "delta-neutral": "a master + a random own sub, taker orders only "
                     "(both legs pay the taker fee)",
    "auto-spread": "delta-neutral on whichever market has the narrowest spread "
                   "when each new cycle opens -- TradFi, Crypto or All",
}
# Auto-spread's choices of market, as its buttons name them.
AUTO_KINDS = {
    "tradfi": "stocks and indices only (tradfi fee)",
    "crypto": "coins only",
    "all": "every listed market",
}
# How long a listing of the account trees is reused between presses of the
# account picker. Each listing is an HTTP call per key.
TREES_TTL_S = 120.0

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
    "🎛 Mode -- multi, single, delta-neutral or auto-spread, for the next run\n"
    "👥 Accounts -- switch accounts on or off for the next run\n"
    "📜 Log -- the last lines of logs.txt\n"
    "💰 Margin -- balance margin across sub-accounts, or collect it to the master\n"
    "⚙️ Settings -- markets, sizes, targets and risk limits, saved to settings.yaml\n"
    "📅 Weekly report -- spend and AURA for an AURA week (Sat 13:00 UTC), and a switch "
    "to get it every Saturday\n"
    "🔁 Live / Dry run -- switch whether runs and closes trade real funds\n\n"
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
    # For a margin plan: the transfers it will send, as planned when asked.
    moves: list | None = None


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
        margin: dict[str, Callable] | None = None,
        submit_transfers: Callable | None = None,
        reload: Callable[[], object] | None = None,
        config_path: str | None = None,
        trees_fn: Callable[[object], list] | None = None,
        write_setting: Callable[[str, str, str], None] | None = None,
        files=None,
        report_fn: Callable | None = None,
        weekly_path: str | None = None,
        utcnow: Callable[[], dt.datetime] | None = None,
    ):
        self.config = config
        # Where 🎛 Mode and 👥 Accounts write their choice, so it outlives a
        # restart -- the same file and the same writer the terminal menu uses.
        # None keeps the choice in memory only.
        self.config_path = config_path
        self.write_setting = write_setting
        # config -> the account trees (menu.Tree), read from the exchange.
        self.trees_fn = trees_fn
        self._trees: list | None = None
        self._trees_at = 0.0
        # The settings file's writers (bulkdn.menu, or a stand-in in tests),
        # and the setting a typed value is awaited for: (setting, market or
        # None, deadline).
        self.files = files
        self.awaiting: tuple | None = None
        # (config, trees, week) -> weekly.WeekReport; blocking HTTP, so it
        # runs in a thread. `weekly_path` keeps the Saturday switch and the
        # last week sent across restarts.
        self.report_fn = report_fn
        self.weekly_path = weekly_path
        self.utcnow = utcnow or (lambda: dt.datetime.now(dt.timezone.utc))
        self._report: asyncio.Task | None = None
        self.weekly = self._load_weekly()
        # Reads the settings file again; None keeps the config given at start.
        # See `config_reloader`.
        self.reload = reload
        self.dry_run = dry_run
        self.run_fn = run_fn
        self.flatten_fn = flatten_fn
        self.status_fn = status_fn
        self.send = send
        self.log_path = log_path
        self.clock = clock
        self.make_code = make_code or (lambda: secrets.token_hex(4))
        self.alerts = alerts
        # "balance" and "collect", each config -> MarginPlan; and the sender.
        # The same functions the menu uses, so both make the same plan.
        self.margin = margin or {}
        self.submit_transfers = submit_transfers
        self.pending: Pending | None = None
        self.work: asyncio.Task | None = None
        self.work_kind = ""
        self.work_started = 0.0
        self.strategy = None
        # Set once a starting run has a strategy to stop. See `_do_close`.
        self._strategy_ready = asyncio.Event()

    # -- state -------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self.work is not None and not self.work.done()

    @property
    def mode_word(self) -> str:
        return "DRY-RUN" if self.dry_run else "LIVE"

    def _set_strategy(self, strategy) -> None:
        self.strategy = strategy
        self._strategy_ready.set()

    def _refresh_config(self) -> str | None:
        """Re-read the settings file. None if it read, else why not.

        On failure the last copy that read is kept: a typo saved into the file
        must not leave Close all unable to close.
        """
        if self.reload is None:
            return None
        try:
            self.config = self.reload()
        except Exception as exc:  # noqa: BLE001 - reported, the last good copy stays
            log.warning("telegram control: could not re-read the settings: %s", describe(exc))
            return describe(exc)
        return None

    # -- messages ------------------------------------------------------------

    async def handle(self, text: str | None) -> Reply | None:
        """Answer one message from an authorised user. None means no reply."""
        if not text:
            return None
        text = KEYBOARD.get(text.strip(), text)
        if not text.startswith("/"):
            if self.awaiting is not None:
                return self._apply_input(text)
            return Reply("Use the buttons below.", keyboard=True)
        words = text.strip().split()
        # "/status@SomeBot" is what a command looks like in a group chat.
        command = words[0].lower().split("@")[0]
        args = words[1:]
        # Any command ends a question about a setting: a button pressed
        # instead of an answer is a change of mind, not a value.
        asked, self.awaiting = self.awaiting, None
        if command == "/cancel":
            return Reply("Kept as it was." if asked else "Nothing to cancel.")
        if command == "/settings":
            self._refresh_config()
            return self._settings_home()
        if command == "/week":
            try:
                week = int(args[0]) if args else None
            except ValueError:
                return Reply("Usage: /week or /week 9")
            return self._ask_week(week)

        if command in ("/help", "/start"):
            return Reply(HELP, keyboard=True)
        if command in ("/run", "/close", "/status"):
            problem = self._refresh_config()
            if problem is not None:
                if command == "/run":
                    # Not on the last good copy: whoever just edited the file
                    # expects the run to trade what they wrote.
                    return Reply(
                        f"⚠️ settings.yaml could not be read: {html.escape(problem)}\n"
                        "Fix it and ask again -- no run was started."
                    )
                reply = await self._status() if command == "/status" else self._ask_close()
                reply.text = (
                    f"⚠️ settings.yaml could not be read ({html.escape(problem)}) -- "
                    f"using the last copy that did.\n{reply.text}"
                )
                return reply
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
        if command == "/mode":
            return self._ask_mode()
        if command == "/accounts":
            return self._accounts_menu()
        if command == "/tradingmode":
            self._refresh_config()
            return self._mode_menu()
        if command == "/pick":
            self._refresh_config()
            return await self._pick_menu()
        if command == "/yes":
            return self._confirm(args)
        return Reply(f"Unknown command {html.escape(command)}.", keyboard=True)

    async def press(self, data: str) -> Reply:
        """Answer a button press. The reply replaces the pressed message."""
        action, _, code = (data or "").partition(":")
        if action == "refresh":
            if code != "status":
                return self._log([])
            self._refresh_config()
            return await self._status()
        if action == "margin":
            # Only reads: the plan it shows is confirmed separately.
            return await self._plan_margin(code)
        # Settings, not trades: what the next run trades, which its own Start
        # button confirms. So no one-time code -- a press on an old message
        # sets what it says, and the reply shows the result.
        if action == "setmode":
            return await self._set_mode(code)
        if action == "setmaster":
            return await self._set_master(code)
        if action == "setauto":
            return self._set_auto(code)
        if action == "acct":
            return await self._toggle_account(code)
        if action == "wk":
            return self._ask_week(int(code) if code.isdigit() else None)
        if action == "wkauto":
            return self._switch_weekly(code == "on")
        if action in ("cfg", "mkt", "edit", "mon", "madd"):
            self.awaiting = None
            return await self._settings_press(action, code)
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
        if self.busy and self.work_kind == "transfer":
            return Reply("Sending margin transfers...", refresh)
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
        if self.work_kind in ("close", "transfer"):
            return Reply(f"A {self.work_kind} is in progress -- it cannot be stopped halfway.")
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
        markets = self._markets_line()
        other = "" if volume is not None else "\nAnother target: type /run 250k"
        off = len(self.config.excluded_accounts)
        accounts = f", {off} account(s) switched off" if off else ""
        return Reply(
            f"Start a <b>{self.mode_word}</b> run?\n"
            f"{html.escape(self._mode_line())} mode, {html.escape(markets)}{accounts}\n"
            f"{html.escape(self._goal(volume))}{other}",
            [[("▶️ Start", f"run:{code}"), ("❌ Cancel", f"cancel:{code}")]],
        )

    def _ask_close(self) -> Reply:
        if self.busy and self.work_kind == "close":
            return Reply("A close is already in progress.")
        if self.busy and self.work_kind == "transfer":
            return Reply("Margin transfers are being sent -- ask again when they are done.")
        code = self._new_pending("close")
        first = "Stop the current run, then close" if self.busy else "Close"
        return Reply(
            f"{first} <b>every position at market</b> on every account ({self.mode_word})?",
            [[("✅ Yes, close all", f"close:{code}"), ("❌ Cancel", f"cancel:{code}")]],
        )

    def _ask_mode(self) -> Reply:
        """Offer the other mode. Only while nothing is running.

        A run keeps the mode it started with, and a close takes the mode at
        the moment it is confirmed -- switching underneath either would leave
        a run and its own close trading different money.
        """
        if self.busy:
            return Reply(
                f"Now <b>{self.mode_word}</b>. A {self.work_kind} is in progress -- "
                "switch once it is over."
            )
        if self.dry_run:
            code = self._new_pending("live")
            return Reply(
                "Now <b>DRY-RUN</b>: nothing is sent to the exchange.\n"
                "Switch to <b>LIVE</b>? After that, Run and Close all trade "
                "<b>real funds</b>.",
                [[("🔴 Switch to LIVE", f"live:{code}"), ("❌ Cancel", f"cancel:{code}")]],
            )
        code = self._new_pending("dry")
        return Reply(
            "Now <b>LIVE</b>: Run and Close all trade real funds.\n"
            "Switch to <b>DRY-RUN</b>?",
            [[("🧪 Switch to DRY-RUN", f"dry:{code}"), ("❌ Cancel", f"cancel:{code}")]],
        )

    def _accounts_menu(self) -> Reply:
        return Reply(
            "<b>Account management</b>\n"
            "⚖️ Balance -- even out free margin across each master's accounts\n"
            "⬆️ Collect -- move every sub-account's free margin up to its master\n\n"
            "Only free (transferable) margin moves, and only inside one master's "
            "accounts: the exchange cannot move margin between masters. "
            "Each shows the plan first; nothing is sent until you confirm it.",
            [[("⚖️ Balance margin", "margin:balance"), ("⬆️ Collect to master", "margin:collect")]],
        )

    # -- mode and accounts ------------------------------------------------------

    def _mode_line(self) -> str:
        if self.config.mode == "single":
            return f"single (master {self.config.single_master})"
        if self.config.mode == "auto-spread":
            return f"auto-spread ({KIND_LABELS[self.config.auto_spread.markets]})"
        return self.config.mode

    def _markets_line(self) -> str:
        """Which markets the next run trades, as one line."""
        if self.config.mode == "auto-spread":
            kind = self.config.auto_spread.markets
            which = "market" if kind == "all" else f"{KIND_LABELS[kind]} market"
            return f"the narrowest-spread {which} at each new cycle"
        return ", ".join(leg.symbol for leg in self.config.active_legs)

    def _later(self) -> str:
        """A note for a change made while a run is going."""
        if self.busy and self.work_kind == "run":
            return "\nThe run in progress keeps what it started with; this applies from the next Run."
        return ""

    def _mode_menu(self, note: str = "") -> Reply:
        lines = "\n".join(
            f"{'▸' if mode == self.config.mode else '·'} <b>{mode}</b> -- {html.escape(about)}"
            for mode, about in TRADING_MODES.items()
        )
        modes = [
            (f"{'✅ ' if mode == self.config.mode else ''}{mode}", f"setmode:{mode}")
            for mode in TRADING_MODES
        ]
        buttons = [modes[i:i + 2] for i in range(0, len(modes), 2)]
        return Reply(
            f"{note}<b>Trading mode</b>: now {html.escape(self._mode_line())}\n{lines}{self._later()}",
            buttons,
        )

    def _save(self, key: str, value: str) -> str | None:
        """Write one setting and re-read the file. None if done, else why not."""
        if self.config_path is None or self.write_setting is None:
            return None
        try:
            self.write_setting(self.config_path, key, value)
        except Exception as exc:  # noqa: BLE001 - reported, nothing else changed
            log.warning("telegram control: could not write %s: %s", key, describe(exc))
            return describe(exc)
        return self._refresh_config()

    def _forget_cli_mode(self) -> None:
        # A --mode on the command line would otherwise win on every re-read.
        forget = getattr(self.reload, "forget_mode", None)
        if forget is not None:
            forget()

    async def _set_mode(self, mode: str) -> Reply:
        if mode not in TRADING_MODES:
            return self._mode_menu()
        if mode == "single":
            return await self._master_menu()
        if mode == "auto-spread":
            return self._auto_menu()
        self._forget_cli_mode()
        problem = self._save("mode", mode)
        if problem is not None:
            return Reply(f"⚠️ Could not save the mode: {html.escape(problem)}")
        self.config.mode = mode
        log.warning("telegram control: trading mode set to %s", mode)
        return self._mode_menu("✅ Saved.\n")

    def _auto_menu(self, note: str = "") -> Reply:
        current = self.config.auto_spread.markets if self.config.mode == "auto-spread" else None
        lines = "\n".join(
            f"{'▸' if kind == current else '·'} <b>{KIND_LABELS[kind]}</b> -- {html.escape(about)}"
            for kind, about in AUTO_KINDS.items()
        )
        buttons = [[
            (f"{'✅ ' if kind == current else ''}{KIND_LABELS[kind]}", f"setauto:{kind}")
            for kind in AUTO_KINDS
        ], [("⬅️ Modes", "cfg:mode")]]
        spread = tg_settings.render(self.config.taking.max_spread_bps)
        return Reply(
            f"{note}<b>Auto-spread</b>: which markets may it pick from?\n{lines}\n"
            f"Each new cycle goes to the one with the narrowest spread at that moment, "
            f"and none opens while every spread is wider than {spread} bps "
            "(⚙️ Settings → ⚡ Delta-neutral). Taker orders, a master + its own sub, as in "
            "delta-neutral." + self._later(),
            buttons,
        )

    def _set_auto(self, kind: str) -> Reply:
        if kind not in AUTO_KINDS:
            return self._auto_menu()
        if self.config_path is None or self.files is None:
            return Reply("There is no settings file to save this to.")
        try:
            self.files.write_block_setting(self.config_path, "auto_spread", "markets", kind)
        except Exception as exc:  # noqa: BLE001 - reported, nothing else changed
            return Reply(f"⚠️ Could not save: {html.escape(describe(exc))}")
        self._forget_cli_mode()
        problem = self._save("mode", "auto-spread")
        if problem is not None:
            return Reply(f"⚠️ Could not save the mode: {html.escape(problem)}")
        self.config.mode = "auto-spread"
        self.config.auto_spread.markets = kind
        log.warning("telegram control: trading mode set to auto-spread (%s)", kind)
        return self._mode_menu("✅ Saved.\n")

    async def _master_menu(self) -> Reply:
        trees, problem = await self._read_trees()
        if problem is not None:
            return Reply(f"Could not list the accounts: {html.escape(problem)}")
        rows = []
        for tree in trees:
            current = self.config.mode == "single" and tree.index == self.config.single_master
            rows.append([(
                f"{'✅ ' if current else ''}master {tree.index} {_short(tree.accounts[0])} "
                f"({len(tree.subs)} sub)",
                f"setmaster:{tree.index}",
            )])
        return Reply("<b>Single mode</b>: which master trades, with its own sub-accounts?", rows)

    async def _set_master(self, code: str) -> Reply:
        trees, problem = await self._read_trees()
        if problem is not None:
            return Reply(f"Could not list the accounts: {html.escape(problem)}")
        chosen = next((tree for tree in trees if str(tree.index) == code), None)
        if chosen is None:
            return await self._master_menu()
        if not chosen.subs:
            return Reply(
                f"master {chosen.index} {_short(chosen.accounts[0])} owns no sub-accounts, so there "
                "is nothing for it to pair with. Create one first."
            )
        self._forget_cli_mode()
        # The master first: single mode with a master the file does not hold
        # yet is a file that fails to load.
        problem = self._save("single_master", str(chosen.index)) or self._save("mode", "single")
        if problem is not None:
            return Reply(f"⚠️ Could not save the mode: {html.escape(problem)}")
        self.config.single_master = chosen.index
        self.config.mode = "single"
        log.warning("telegram control: trading mode set to single, master %d", chosen.index)
        return self._mode_menu("✅ Saved.\n")

    async def _read_trees(self) -> tuple[list, str | None]:
        """The account trees, reused for a short while. (trees, problem)."""
        if self.trees_fn is None:
            return [], "not available here"
        if self._trees is not None and self.clock() - self._trees_at < TREES_TTL_S:
            return self._trees, None
        try:
            # Blocking HTTP, one call per key: off the loop.
            trees = await asyncio.to_thread(self.trees_fn, copy.deepcopy(self.config))
        except Exception as exc:  # noqa: BLE001 - reported
            return [], describe(exc)
        self._trees, self._trees_at = trees, self.clock()
        return trees, None

    async def _pick_menu(self, note: str = "") -> Reply:
        """Every account under every key, each a button that switches it."""
        trees, problem = await self._read_trees()
        if problem is not None:
            return Reply(f"Could not list the accounts: {html.escape(problem)}")
        excluded = set(self.config.excluded_accounts)
        rows: Buttons = []
        for tree in trees:
            row: list[tuple[str, str]] = []
            for number, pubkey in enumerate(tree.accounts):
                name = f"M{tree.index}" if number == 0 else f"S{tree.index}.{number}"
                mark = "⬜" if pubkey in excluded else "✅"
                row.append((f"{mark} {name} {_short(pubkey)}", f"acct:{pubkey[:_PICK_PREFIX]}"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
        if excluded:
            rows.append([("✅ Switch all on", "acct:all")])
        known = {pubkey for tree in trees for pubkey in tree.accounts}
        on = len(known - excluded)
        if self.config.mode == "single":
            scope = f"Single mode trades master {self.config.single_master}'s accounts only."
        elif self.config.mode in TAKING_MODES:
            scope = (f"{self.config.mode.capitalize()} pairs a master with its own subs: "
                     "a master switched off takes its subs out too.")
        else:
            scope = "Multi mode pairs any accounts switched on."
        return Reply(
            f"{note}<b>Accounts</b>: {on} of {len(known)} on (M = master, S = its sub)\n"
            f"{html.escape(scope)}\n"
            "Tap one to switch it. Off = never drawn into a new cycle; "
            "🛑 Close all still closes it." + self._later(),
            rows,
        )

    async def _toggle_account(self, code: str) -> Reply:
        trees, problem = await self._read_trees()
        if problem is not None:
            return Reply(f"Could not list the accounts: {html.escape(problem)}")
        known = [pubkey for tree in trees for pubkey in tree.accounts]
        excluded = list(self.config.excluded_accounts)
        if code == "all":
            excluded = []
        else:
            matches = [pubkey for pubkey in known if pubkey[:_PICK_PREFIX] == code]
            if len(matches) != 1:
                return await self._pick_menu("⌛ That list was out of date -- here it is again.\n")
            pubkey = matches[0]
            if pubkey in excluded:
                excluded.remove(pubkey)
            else:
                excluded.append(pubkey)
        value = "[" + ", ".join(f'"{pubkey}"' for pubkey in excluded) + "]"
        problem = self._save("excluded_accounts", value)
        if problem is not None:
            return Reply(f"⚠️ Could not save: {html.escape(problem)}")
        self.config.excluded_accounts = tuple(excluded)
        log.warning("telegram control: %d account(s) switched off", len(excluded))
        return await self._pick_menu()

    # -- ⚙️ settings -----------------------------------------------------------

    def _editable(self) -> str | None:
        """None when settings can be written from here, else why not."""
        if self.files is None or self.config_path is None:
            return "Settings can be changed here only when control was started with its settings file."
        return None

    def _settings_home(self, note: str = "") -> Reply:
        on = [leg for leg in self.config.markets if leg.enabled]
        markets = ", ".join(
            f"{leg.symbol} ${tg_settings.shown(tg_settings.MARKET_SETTINGS[0].read(leg))}"
            for leg in on
        ) or "none switched on"
        if self.config.mode == "auto-spread":
            markets = (f"{self._markets_line()} -- a market listed here trades its own "
                       "size; any other the 🔀 Auto-spread size")
        rows: Buttons = [
            [("📈 Markets and sizes", "cfg:markets")],
            [("🎛 Mode", "cfg:mode"), ("👥 Accounts", "cfg:accounts")],
        ]
        names = list(tg_settings.SECTIONS)
        for start in range(0, len(names), 2):
            rows.append([
                (tg_settings.SECTIONS[name][0], f"cfg:{name}") for name in names[start:start + 2]
            ])
        return Reply(
            f"{note}<b>⚙️ Settings</b> -- saved to settings.yaml, used from the next Run\n"
            f"Mode: {html.escape(self._mode_line())}\n"
            f"Markets: {html.escape(markets)}" + self._later(),
            rows,
        )

    def _section(self, name: str, note: str = "") -> Reply:
        title, settings = tg_settings.SECTIONS[name]
        lines, buttons = [], []
        for setting in settings:
            value = tg_settings.shown(setting.read(self.config))
            lines.append(f"<b>{html.escape(setting.label)}</b>: {html.escape(value)} -- "
                         f"{html.escape(setting.help)}")
            buttons.append((f"{setting.label}: {value}", f"edit:{setting.id}"))
        rows: Buttons = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        rows.append([("⬅️ Settings", "cfg:")])
        return Reply(
            f"{note}<b>{html.escape(title)}</b>\n" + "\n".join(lines) +
            "\nTap one to change it." + self._later(),
            rows,
        )

    def _leg(self, symbol: str):
        return next((leg for leg in self.config.markets if leg.symbol == symbol), None)

    def _markets_screen(self, note: str = "") -> Reply:
        rows: Buttons = []
        for leg in self.config.markets:
            size = tg_settings.shown(tg_settings.MARKET_SETTINGS[0].read(leg))
            rows.append([(f"{'✅' if leg.enabled else '⬜'} {leg.symbol} ${size}", f"mkt:{leg.symbol}")])
        rows.append([("➕ Add market", "madd:"), ("⬅️ Settings", "cfg:")])
        on = sum(1 for leg in self.config.markets if leg.enabled)
        return Reply(
            f"{note}<b>📈 Markets</b>: {on} of {len(self.config.markets)} switched on. "
            "Every market switched on trades at the same time.\n"
            "Tap one for its size, leverage and order settings." + self._later(),
            rows,
        )

    def _market_screen(self, symbol: str, note: str = "") -> Reply:
        leg = self._leg(symbol)
        if leg is None:
            return self._markets_screen(f"{html.escape(symbol)} is no longer in the file.\n")
        lines, buttons = [], []
        for setting in tg_settings.MARKET_SETTINGS:
            value = tg_settings.shown(setting.read(leg))
            lines.append(f"<b>{html.escape(setting.label)}</b>: {html.escape(value)} -- "
                         f"{html.escape(setting.help)}")
            buttons.append((f"{setting.label}: {value}", f"edit:{setting.id}@{symbol}"))
        if leg.size and not leg.notional_usd and leg.notional_span is None:
            lines.insert(0, f"Sized in coins: size {tg_settings.render(leg.size)}. Delete `size:` "
                            "in the file to size it in dollars here.")
        rows: Buttons = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        rows.append([
            ("⬜ Switch off" if leg.enabled else "✅ Switch on", f"mon:{symbol}"),
            ("⬅️ Markets", "cfg:markets"),
        ])
        state = "on" if leg.enabled else "off"
        return Reply(
            f"{note}<b>{html.escape(symbol)}</b> ({state})\n" + "\n".join(lines) + self._later(),
            rows,
        )

    async def _settings_press(self, action: str, code: str) -> Reply:
        if action == "cfg":
            self._refresh_config()
            if code == "":
                return self._settings_home()
            if code == "markets":
                return self._markets_screen()
            if code == "mode":
                return self._mode_menu()
            if code == "accounts":
                return await self._pick_menu()
            if code in tg_settings.SECTIONS:
                return self._section(code)
            return self._settings_home()
        if action == "mkt":
            self._refresh_config()
            return self._market_screen(code)

        problem = self._editable()
        if problem is not None:
            return Reply(problem)
        if action == "edit":
            return self._ask_value(code)
        if action == "mon":
            return self._switch_market(code)
        return await self._add_market(code)

    def _ask_value(self, code: str) -> Reply:
        setting_id, _, symbol = code.partition("@")
        setting = tg_settings.BY_ID.get(setting_id)
        if setting is None:
            return self._settings_home()
        if setting.block == tg_settings.MARKET:
            leg = self._leg(symbol)
            if leg is None:
                return self._markets_screen()
            where, now = symbol, setting.read(leg)
        else:
            where, now = None, setting.read(self.config)
        self.awaiting = (setting, where, self.clock() + INPUT_TTL_S)
        title = f"{symbol} -- {setting.label}" if where else setting.label
        return Reply(
            f"<b>{html.escape(title)}</b>\n{html.escape(setting.help)}\n"
            f"Now: <code>{html.escape(tg_settings.shown(now))}</code>\n\n"
            f"Send the new value -- {tg_settings.example(setting)}. /cancel keeps it."
        )

    def _apply_input(self, text: str) -> Reply:
        setting, symbol, deadline = self.awaiting
        if self.clock() > deadline:
            self.awaiting = None
            return Reply("⌛ That question expired. Open ⚙️ Settings and pick the setting again.")
        try:
            value = tg_settings.parse(setting, text)
        except ValueError as exc:
            return Reply(f"⚠️ {html.escape(str(exc))}. Send {tg_settings.example(setting)}, or /cancel.")
        leg = self._leg(symbol) if symbol else None
        if symbol and leg is None:
            self.awaiting = None
            return self._markets_screen(f"{html.escape(symbol)} is no longer in the file.\n")
        before = tg_settings.shown(setting.read(leg if symbol else self.config))
        try:
            if setting.block == tg_settings.MARKET:
                self.files.write_market_setting(self.config_path, symbol, setting.key, value)
            elif setting.block == tg_settings.TOP:
                self.files.write_setting(self.config_path, setting.key, value)
            else:
                self.files.write_block_setting(self.config_path, setting.block, setting.key, value)
        except Exception as exc:  # noqa: BLE001 - reported; the file is as it was
            return Reply(f"⚠️ {html.escape(describe(exc))}\nSend another value, or /cancel.")
        self.awaiting = None
        problem = self._refresh_config()
        where = f"{symbol} " if symbol else ""
        log.warning("telegram control: %s%s set to %s", where, setting.key, value)
        note = f"✅ {html.escape(where + setting.label)}: {html.escape(before)} → {html.escape(value)}\n"
        if problem is not None:
            note += f"⚠️ Saved, but could not be read back: {html.escape(problem)}\n"
        if symbol:
            return self._market_screen(symbol, note)
        section = next(
            name for name, (_, settings) in tg_settings.SECTIONS.items() if setting in settings
        )
        return self._section(section, note)

    def _switch_market(self, symbol: str) -> Reply:
        self._refresh_config()
        leg = self._leg(symbol)
        if leg is None:
            return self._markets_screen()
        if leg.enabled and sum(1 for other in self.config.markets if other.enabled) == 1:
            return self._market_screen(
                symbol, "That is the only market switched on -- turn another on first.\n",
            )
        try:
            self.files.set_market_enabled(self.config_path, symbol, not leg.enabled)
        except Exception as exc:  # noqa: BLE001 - reported
            return Reply(f"⚠️ {html.escape(describe(exc))}")
        self._refresh_config()
        log.warning("telegram control: %s switched %s", symbol, "off" if leg.enabled else "on")
        return self._market_screen(symbol, "✅ Saved.\n")

    async def _add_market(self, symbol: str) -> Reply:
        try:
            listed = await asyncio.to_thread(self.files.exchange_markets, copy.deepcopy(self.config))
        except Exception as exc:  # noqa: BLE001 - reported
            return Reply(f"Could not read the exchange's markets: {html.escape(describe(exc))}")
        have = {leg.symbol for leg in self.config.markets}
        if not symbol:
            available = sorted(name for name in listed if name not in have)
            if not available:
                return self._markets_screen("Every market the exchange lists is already in the file.\n")
            buttons = [(name, f"madd:{name}") for name in available]
            rows: Buttons = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
            rows.append([("⬅️ Markets", "cfg:markets")])
            return Reply(
                "<b>➕ Add market</b>\nIts sizes are copied from a market already set up; "
                "change them after.",
                rows,
            )
        if symbol in have or symbol not in listed:
            return self._markets_screen()
        try:
            notes = self.files.add_market(
                copy.deepcopy(self.config), self.config_path, symbol, listed[symbol],
            )
        except Exception as exc:  # noqa: BLE001 - reported; the file is as it was
            return Reply(f"⚠️ {html.escape(describe(exc))}")
        self._refresh_config()
        log.warning("telegram control: %s added", symbol)
        said = "\n".join(line.strip() for line in notes if line.strip())
        return self._market_screen(symbol, f"<pre>{html.escape(said)}</pre>\n")

    # -- 📅 weekly report -----------------------------------------------------

    def _load_weekly(self) -> dict:
        state = {"enabled": False, "last_sent": 0}
        if not self.weekly_path:
            return state
        try:
            with open(self.weekly_path, encoding="utf-8") as handle:
                state.update(json.load(handle))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.warning("telegram control: %s unreadable (%s) -- weekly report off", self.weekly_path, describe(exc))
        return state

    def _save_weekly(self) -> None:
        if not self.weekly_path:
            return
        temp = f"{self.weekly_path}.tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(self.weekly, handle)
        os.replace(temp, self.weekly_path)

    def _week_buttons(self, week: int) -> Buttons:
        latest = weekly.last_finished_week(self.utcnow())
        steps = []
        if week > 1:
            steps.append((f"◀️ Week {week - 1}", f"wk:{week - 1}"))
        if week < latest:
            steps.append((f"Week {week + 1} ▶️", f"wk:{week + 1}"))
        auto = (("🔔 Every Saturday: on -- turn off", "wkauto:off") if self.weekly["enabled"]
                else ("🔕 Every Saturday: off -- turn on", "wkauto:on"))
        return [row for row in (steps, [auto]) if row]

    def _ask_week(self, week: int | None) -> Reply:
        if self.report_fn is None or self.trees_fn is None:
            return Reply("The weekly report is not available here.")
        latest = weekly.last_finished_week(self.utcnow())
        week = latest if week is None else week
        if not 1 <= week <= latest:
            return Reply(f"Week {week} has not ended yet -- the last finished one is {latest}.",
                         self._week_buttons(latest))
        if self._report is not None and not self._report.done():
            return Reply("A report is already being put together -- it will arrive shortly.")
        start, end = weekly.week_window(week)
        self._report = asyncio.get_running_loop().create_task(self._send_week(week))
        return Reply(
            f"⏳ Week {week}: {start:%b %d %H:%M} → {end:%b %d %H:%M} UTC. Reading every "
            "account's history -- the report follows in a minute or less.",
            self._week_buttons(week),
        )

    def _switch_weekly(self, on: bool) -> Reply:
        now = self.utcnow()
        latest = weekly.last_finished_week(now)
        self.weekly["enabled"] = on
        if on:
            # Switched on within a day of a payout: that week is still sent.
            _, end = weekly.week_window(latest)
            recent = now - end < dt.timedelta(days=1)
            self.weekly["last_sent"] = max(self.weekly.get("last_sent", 0), latest - 1 if recent else latest)
        try:
            self._save_weekly()
        except OSError as exc:
            return Reply(f"⚠️ Could not save the switch: {html.escape(str(exc))}")
        log.warning("telegram control: weekly report %s", "on" if on else "off")
        text = ("🔔 The report for each AURA week will arrive every Saturday, about 30 minutes "
                "after the 13:00 UTC payout." if on else "🔕 No more Saturday reports.")
        return Reply(text, self._week_buttons(latest))

    async def _week_report(self, week: int):
        trees, problem = await self._read_trees()
        if problem is not None:
            raise RuntimeError(f"could not list the accounts: {problem}")
        accounts = [list(tree.accounts) for tree in trees]
        return await asyncio.to_thread(self.report_fn, copy.deepcopy(self.config), accounts, week), trees

    async def _send_week(self, week: int) -> None:
        try:
            report, trees = await self._week_report(week)
        except Exception as exc:  # noqa: BLE001 - reported
            log.warning("weekly report for week %d failed: %s", week, describe(exc))
            await self.send(f"⚠️ Weekly report for week {week} failed: {html.escape(describe(exc))}")
            return
        await self._deliver(report, trees)

    async def _deliver(self, report, trees) -> None:
        names = {tree.accounts[0]: f"M{tree.index}" for tree in trees}
        lines = weekly.render(report, names)
        await self.send(f"📅 <b>Week {report.week}</b>", plain="\n".join(lines), pre=True)

    async def weekly_loop(self) -> None:
        """Send each finished week's report once, when the switch is on."""
        while True:
            await asyncio.sleep(WEEKLY_CHECK_S)
            try:
                await self._weekly_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - try again next tick
                log.warning("weekly report: %s", describe(exc))

    async def _weekly_tick(self) -> None:
        if not self.weekly.get("enabled") or self.report_fn is None or self.trees_fn is None:
            return
        now = self.utcnow()
        week = weekly.last_finished_week(now)
        if week <= self.weekly.get("last_sent", 0):
            return
        _, end = weekly.week_window(week)
        if now < end + WEEKLY_DELAY:
            return
        self._refresh_config()
        report, trees = await self._week_report(week)
        if report.total_points == 0 and now < end + WEEKLY_POINTS_WAIT:
            # The indexer has not filed the week yet. Asked again next tick.
            return
        await self._deliver(report, trees)
        self.weekly["last_sent"] = week
        self._save_weekly()

    async def _plan_margin(self, kind: str) -> Reply:
        """Read the balances and show what would move. Sends nothing.

        Refused while a run or a close is going: collecting to the master
        mid-run takes the free margin a sub-account with an open position is
        leaning on, which is how a hedged pair becomes a liquidated one.
        """
        planner = self.margin.get(kind)
        if planner is None:
            return Reply("Account management is not available here.")
        if self.busy:
            return Reply(
                f"A {self.work_kind} is in progress. Move margin between runs, "
                "not during one -- an open position may be using it."
            )
        try:
            # Blocking HTTP, paced by ratelimit -- off the loop, which is
            # still answering Telegram and must not freeze for it.
            plan = await asyncio.to_thread(planner, copy.deepcopy(self.config))
        except Exception as exc:  # noqa: BLE001 - reported, nothing sent
            return Reply(f"Could not read the balances: {html.escape(describe(exc))}")

        report = "\n".join(line.lstrip("\n") for line in plan.lines)
        title = "⚖️ Balance margin" if kind == "balance" else "⬆️ Collect to master"
        if not plan.moves:
            nothing = "Already balanced." if kind == "balance" else "Nothing to collect."
            return Reply(f"<b>{title}</b>\n<pre>{html.escape(report[-_MAX_TEXT:])}</pre>\n{nothing}")

        moves = "\n".join(
            f"{_short(src)} -> {_short(dst)}  {amount:,.2f}" for _tree, src, dst, amount in plan.moves
        )
        total = sum(amount for *_, amount in plan.moves)
        body = (
            f"<b>{title}</b> ({self.mode_word})\n"
            f"<pre>{html.escape(report[-(_MAX_TEXT // 2):])}</pre>\n"
            f"<b>{len(plan.moves)} transfer(s), {total:,.2f} in all:</b>\n"
            f"<pre>{html.escape(moves[-(_MAX_TEXT // 2):])}</pre>"
        )
        if self.dry_run:
            return Reply(body + "\nDRY-RUN: nothing will be sent. Switch to LIVE to move margin.")
        code = self._new_pending("transfer")
        self.pending.moves = list(plan.moves)
        return Reply(
            body,
            [[(f"✅ Send {len(plan.moves)} transfer(s)", f"transfer:{code}"),
              ("❌ Cancel", f"cancel:{code}")]],
        )

    async def _do_transfers(self, moves: list) -> None:
        try:
            lines = await asyncio.to_thread(self.submit_transfers, copy.deepcopy(self.config), moves)
        except Exception as exc:  # noqa: BLE001 - reported
            log.exception("transfers started from telegram failed")
            await self.send(f"⚠️ Transfers failed: {html.escape(describe(exc))} -- check 💰 Accounts.")
            return
        # Whole, not cut to its tail: the notifier splits a long report into
        # messages that each parse, and every transfer's outcome matters.
        report = "\n".join(line.lstrip("\n") for line in lines)
        await self.send("💸 Transfers", plain=report, pre=True)

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
        if pending.action == "transfer":
            if self.busy:
                return Reply(f"A {self.work_kind} started meanwhile -- nothing was sent.")
            if self.dry_run:
                return Reply("DRY-RUN: nothing was sent. Switch to LIVE to move margin.")
            self._start("transfer", self._do_transfers(pending.moves or []))
            return Reply(f"💸 Sending {len(pending.moves or [])} transfer(s)... I will report each one.")
        if pending.action in ("live", "dry"):
            if self.busy:
                return Reply(f"A {self.work_kind} started meanwhile -- mode unchanged.")
            self.dry_run = pending.action == "dry"
            log.warning("telegram control switched to %s", self.mode_word)
            if self.dry_run:
                return Reply("🧪 Now <b>DRY-RUN</b>: nothing is sent to the exchange.")
            return Reply("🔴 Now <b>LIVE</b>: Run and Close all trade real funds.")
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
        self._strategy_ready.clear()
        try:
            code = await self.run_fn(config, self.dry_run, on_strategy=self._set_strategy)
        except Exception as exc:  # noqa: BLE001 - reported, and the loop carries on
            log.exception("run started from telegram failed")
            await self.send(f"⚠️ The run ended with an error: {html.escape(describe(exc))}")
        else:
            await self.send(f"The run has ended (exit code {code}).")
        finally:
            self.strategy = None
            self._strategy_ready.clear()

    async def _do_close(self, running: asyncio.Task | None) -> None:
        if running is not None:
            # A run still starting has no strategy to stop yet. Waiting on the
            # task alone would wait out the whole run, so the stop is sent the
            # moment there is something to send it to -- woken by the strategy
            # arriving or the run ending, whichever is first, rather than by
            # looking every half second.
            if self.strategy is None and not running.done():
                ready = asyncio.ensure_future(self._strategy_ready.wait())
                try:
                    await asyncio.wait({running, ready}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    ready.cancel()
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


# How much of a pubkey the account picker's buttons carry. Telegram allows 64
# bytes of callback data, and a pubkey is up to 44; 16 characters of base58 do
# not collide between the accounts of one operator.
_PICK_PREFIX = 16


def _short(pubkey: str) -> str:
    return f"{pubkey[:6]}..{pubkey[-4:]}" if len(pubkey) > 12 else pubkey


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
    """`cmd_status`'s printout, as text, read in a worker thread.

    `cmd_status` is a coroutine in name only: it discovers every account and
    reads every position and open order with blocking HTTP, paced by
    ratelimit. Awaited here it ran on this loop, which then answered no button
    and polled no update until the last read came back. In a thread, on a loop
    of its own that serves nothing else, the wait is that thread's alone.

    `redirect_stdout` swaps `sys.stdout` for the whole process while it runs,
    so anything this loop printed meanwhile would land in the status too.
    Nothing here prints after startup; the log handlers hold their own
    streams.
    """
    return await asyncio.to_thread(_status_text, config)


def _status_text(config) -> str:
    from .cli import cmd_status

    buffer = io.StringIO()
    with ratelimit.private_loop(), contextlib.redirect_stdout(buffer):
        asyncio.run(cmd_status(config))
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


def config_reloader(path: str, loaded, *, mode: str | None = None) -> Callable[[], object]:
    """A function that reads the settings at `path` again, keeping `loaded`'s keys.

    The keys decrypted at start are handed in rather than read again -- the
    key file is encrypted, and asking for its password on every /run from a
    phone is not an option -- and the result is validated as fully as a fresh
    start would be. `mode` is the command line's override, if there was one,
    which a re-read must not undo.
    """
    override = {"mode": mode}

    def reload():
        from .config import load_config

        return load_config(path, mode=override["mode"], private_keys=list(loaded.private_keys))

    def forget_mode() -> None:
        """Drop the command line's mode: one chosen from 🎛 Mode is newer."""
        override["mode"] = None

    reload.forget_mode = forget_mode
    return reload


def _weekly_path(config) -> str | None:
    """Where the Saturday switch is kept: beside the state file."""
    state_file = getattr(config, "state_file", None)
    if not state_file:
        return None
    return os.path.join(os.path.dirname(state_file) or ".", "weekly_report.json")


async def serve(
    config,
    dry_run: bool,
    *,
    log_path: str = "logs.txt",
    config_path: str | None = None,
    mode: str | None = None,
) -> int:
    """Listen for commands until interrupted.

    With `config_path`, every /run, /close and /status re-reads the settings
    from it (see `config_reloader`); without, they use `config` throughout.
    """
    from .cli import cmd_flatten, cmd_run

    telegram = config.telegram
    if not telegram.enabled:
        print(SETUP_HELP)
        return 1

    notifier = Notifier(telegram)
    allowed = set(telegram.user_ids)
    from . import menu
    from .menu import account_trees, plan_balance, plan_collect, submit_plan, write_setting

    controller = Controller(
        config, dry_run,
        run_fn=cmd_run, flatten_fn=cmd_flatten, status_fn=capture_status,
        send=notifier.send, log_path=log_path, alerts=AlertHandler(notifier),
        margin={"balance": plan_balance, "collect": plan_collect},
        submit_transfers=submit_plan,
        reload=config_reloader(config_path, config, mode=mode) if config_path else None,
        config_path=config_path,
        trees_fn=account_trees,
        write_setting=write_setting,
        files=menu,
        report_fn=lambda cfg, trees, week: weekly.build_report(cfg.http_url, trees, week),
        weekly_path=_weekly_path(config),
    )
    poller = TelegramPoller(telegram.bot_token)
    strangers: set[int] = set()
    backoff = 5.0
    saturdays = asyncio.get_running_loop().create_task(controller.weekly_loop())

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
            if sys.stdin is not None and sys.stdin.isatty():
                # Not under systemd, where there is no Ctrl+C to press.
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
        saturdays.cancel()
        await controller.shutdown()
        await notifier.drain()

"""Telegram reporting for an unattended run.

The bot is meant to be left alone for hours: it opens, holds, exits, and can
halt itself on a risk breach. Without a notification channel the only place a
halt appears is a console nobody is watching, which makes the kill switch much
less useful than it looks.

Notification never affects trading. Every send is wrapped so a Telegram outage,
a bad token, or a network fault cannot propagate into the strategy -- a bot that
stops trading because it could not send a message would be worse than one that
sends nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
import urllib.parse
from dataclasses import dataclass, field

import aiohttp
from .retry import describe

log = logging.getLogger(__name__)

# Telegram rejects messages past ~4096 characters. Splitting well below that
# leaves room for the header without needing to measure it.
_CHUNK = 1900
_TIMEOUT_S = 15

# Where `proxy.apply` puts the configured proxy. Read back from here rather
# than passed in, because the environment IS the proxy module's output: it sets
# these when proxy.local names one and clears them when it does not, so they
# say exactly what the rest of the bot is using -- and the Notifier is built
# deep inside Runtime, far from where the proxy file was read.
_PROXY_ENV = ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")

# Alert lines are gathered for this long and sent as one message, so an
# emergency stop that logs a dozen critical lines in a second arrives as one
# message rather than a dozen -- and a loop logging the same failure does not
# flood the chat. At most _ALERT_MAX_LINES per message; the rest are counted.
_ALERT_BATCH_S = 2.0
_ALERT_MAX_LINES = 15

# Said once per process, not once per message: a run sends one per cycle, and
# the same warning every few minutes buries everything else in the log.
_socks_warned = False


def _telegram_proxy() -> str | None:
    """The proxy Telegram traffic should use, or None to go direct.

    aiohttp speaks HTTP proxies natively but needs `aiohttp_socks` for SOCKS,
    which install.bat does not install. So an http(s) proxy is used as it is,
    and a SOCKS one is skipped with a warning. Silently going direct would be
    the worst option: where BULK is blocked, api.telegram.org often is too,
    and "Telegram is quiet" would read as "nothing has happened".
    """
    global _socks_warned
    url = next((os.environ[name] for name in _PROXY_ENV if os.environ.get(name)), None)
    if not url:
        return None
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme in ("http", "https"):
        return url
    if not _socks_warned:
        _socks_warned = True
        log.warning(
            "the %s proxy in proxy.local is not applied to Telegram messages "
            "(that needs the aiohttp_socks package), so they are sent directly. "
            "Trading traffic still goes through the proxy.",
            scheme,
        )
    return None


@dataclass
class TelegramConfig:
    # repr=False: the token is the bot's whole identity -- anyone holding it
    # can send as the bot -- and a Config repr prints every field of every
    # nested dataclass.
    bot_token: str = field(default="", repr=False)
    user_ids: list[int] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.user_ids)


class Notifier:
    """Sends run events to Telegram, or does nothing when unconfigured."""

    def __init__(self, config: TelegramConfig | None = None, *, label: str = "bulkdn"):
        self.config = config or TelegramConfig()
        self.label = label
        # Strong references to the sends running in the background. The event
        # loop holds only a weak one to a task, so a fire-and-forget task that
        # nothing else holds can be garbage-collected mid-flight -- the message
        # then never arrives, and nothing anywhere says so.
        self._pending: set[asyncio.Task] = set()
        # See `AlertHandler`: lines waiting for the next alert message, and the
        # loop they are handed to from whatever thread logged them.
        self._alerts: list[str] = []
        self._alerts_dropped = 0
        self._alert_loop: asyncio.AbstractEventLoop | None = None
        self._alert_timer: asyncio.TimerHandle | None = None

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def alert(self, line: str) -> None:
        """Queue one line for the next alert message. Safe from any thread.

        Position reads run in worker threads and log from there, so this hands
        the line to the loop rather than touching anything itself.
        """
        loop = self._alert_loop
        if not self.enabled or loop is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):  # the loop closed in between
            loop.call_soon_threadsafe(self._queue_alert, line)

    def _queue_alert(self, line: str) -> None:
        if len(self._alerts) < _ALERT_MAX_LINES:
            self._alerts.append(line)
        else:
            self._alerts_dropped += 1
        if self._alert_timer is None:
            self._alert_timer = asyncio.get_running_loop().call_later(
                _ALERT_BATCH_S, self._flush_alerts
            )

    def _flush_alerts(self) -> None:
        if self._alert_timer is not None:
            self._alert_timer.cancel()
            self._alert_timer = None
        if not self._alerts:
            return
        text = "\n".join(self._alerts)
        if self._alerts_dropped:
            text += f"\n... and {self._alerts_dropped} more -- see logs.txt"
        self._alerts, self._alerts_dropped = [], 0
        self.send_soon(self.error(text))

    async def send(self, text: str, *, prefix: str = "") -> None:
        """Deliver one message. Silent no-op when Telegram is not configured."""
        if not self.enabled:
            return

        body = f"<b>{html.escape(self.label)}</b>"
        if prefix:
            body += f" {html.escape(prefix)}"
        body += f"\n\n{text}"

        try:
            async with aiohttp.ClientSession() as session:
                proxy = _telegram_proxy()
                for chunk in _split(body):
                    for user_id in self.config.user_ids:
                        await self._send_one(session, user_id, chunk, proxy=proxy)
        except Exception as exc:  # noqa: BLE001 - reporting must never break trading
            log.warning("telegram notification failed: %s", self._redacted(describe(exc)))

    def _redacted(self, text: str) -> str:
        """`text` with the bot token masked.

        The token sits in the request URL, and aiohttp puts that URL into its
        errors -- a proxy or Cloudflare answering with an HTML page raises
        ContentTypeError naming it in full. Logged as it was, the token went
        into logs.txt, the file operators send when asking for help.
        """
        token = self.config.bot_token
        return text.replace(token, "<bot-token>") if token else text

    async def _send_one(
        self,
        session: aiohttp.ClientSession,
        user_id: int,
        text: str,
        *,
        proxy: str | None = None,
    ) -> None:
        try:
            async with session.post(
                f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage",
                json={
                    "chat_id": user_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT_S),
                proxy=proxy,
            ) as response:
                payload = await response.json()
                if not payload.get("ok"):
                    log.warning(
                        "telegram rejected message to %s: %s",
                        user_id, self._redacted(str(payload)),
                    )
        except Exception as exc:  # noqa: BLE001 - one bad recipient must not stop the rest
            log.warning(
                "telegram send to %s failed: %s", user_id, self._redacted(describe(exc))
            )

    # -- event helpers -----------------------------------------------------
    #
    # Named for what happened rather than taking free text, so the wording of a
    # given event stays consistent across the places that raise it.

    async def run_started(self, *, endpoint: str, master: str, sub1: str, dry_run: bool) -> None:
        mode = "DRY-RUN" if dry_run else "LIVE"
        await self.send(
            f"mode: <b>{mode}</b>\n"
            f"endpoint: {html.escape(endpoint)}\n"
            f"master: <code>{html.escape(master)}</code>\n"
            f"sub1: <code>{html.escape(sub1)}</code>",
            prefix="▶️ started",
        )

    async def cycle_complete(self, *, cycle: int, of: int | None, detail: str = "") -> None:
        """Report a finished cycle WITHOUT waiting for Telegram.

        The strategy awaits this between cycles, and a send can take up to
        `_TIMEOUT_S` per chunk per recipient -- on a slow or blocked network,
        tens of seconds in which that leg could not start its next cycle. A
        progress report is not worth a second of trading, so it goes out in
        the background, the way the halt alert does. Still `async`, so every
        existing `await` of it keeps working and simply returns at once.
        """
        target = f"/{of}" if of else ""
        text = f"cycle <b>{cycle}{target}</b> complete"
        if detail:
            text += f"\n{detail}"
        self.send_soon(self.send(text, prefix="✅"))

    async def halted(self, reason: str) -> None:
        await self.send(
            f"<b>trading stopped</b>\n{html.escape(reason)}\n\n"
            "Orders cancelled and positions flattened.",
            prefix="🛑 HALT",
        )

    async def run_finished(self, *, cycles: int, detail: str = "") -> None:
        text = f"run finished after <b>{cycles}</b> cycle(s)"
        if detail:
            text += f"\n{detail}"
        await self.send(text, prefix="🏁")

    async def error(self, message: str) -> None:
        await self.send(f"<pre>{html.escape(message)}</pre>", prefix="⚠️ error")

    def send_soon(self, coro) -> None:
        """Fire a notification without making the caller wait for it.

        Used on paths where the bot is shutting down or reacting to a fill and
        should not block on a network round-trip to Telegram.
        """
        # The caller has already built the coroutine, so every path out of here
        # has to either schedule it or close it -- dropping it leaks it.
        if not self.enabled:
            coro.close()
            return
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            # No loop running: the caller is synchronous and the message is not
            # worth spinning one up for.
            coro.close()
            return
        # Held until done -- see `_pending`.
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def drain(self, timeout_s: float = 5.0) -> None:
        """Give background sends a moment to finish before the loop closes.

        `asyncio.run` cancels whatever is still pending when the run returns,
        so without this the last cycle's report -- or a halt alert fired a
        second before shutdown -- is cancelled mid-send. Bounded, because a
        Telegram outage must not hold up the process exiting.
        """
        # Alerts still gathering go now: the process is about to exit, and
        # the line that says why is usually among them.
        self._flush_alerts()
        pending = [task for task in self._pending if not task.done()]
        if not pending:
            return
        _done, still = await asyncio.wait(pending, timeout=timeout_s)
        if still:
            log.warning(
                "%d telegram notification(s) still unsent after %.0fs -- "
                "dropped at shutdown",
                len(still),
                timeout_s,
            )
            for task in still:
                task.cancel()


class AlertHandler(logging.Handler):
    """Forwards the log lines that need a person to Telegram.

    The bot is left alone for hours, and until now only halts, cycles and the
    start and end of a run reached Telegram. A crash, "COULD NOT CLOSE ... by
    hand" and "MANUAL ACTION REQUIRED" went to the log alone -- the lines that
    most need someone, in a file nobody reads until later.

    Forwarded: every CRITICAL line except the halt's own (it has a message of
    its own already), a crash, and a flatten that could not finish.
    """

    def __init__(self, notifier: Notifier):
        super().__init__(level=logging.ERROR)
        self.notifier = notifier

    def emit(self, record: logging.LogRecord) -> None:
        # A failed send logs a warning from here, and must not come back.
        if record.name == __name__:
            return
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string is not our problem here
            return
        if record.levelno >= logging.CRITICAL:
            if message.startswith(("HALT:", "halted:")):
                return
        elif "MANUAL ACTION REQUIRED" in message:
            pass
        elif message == "run failed" and record.exc_info and record.exc_info[1]:
            message = f"run failed: {describe(record.exc_info[1])}"
        else:
            return
        self.notifier.alert(message)

    def attach(self, logger: logging.Logger) -> None:
        """Start forwarding. Called from inside the running loop."""
        self.notifier._alert_loop = asyncio.get_running_loop()
        logger.addHandler(self)

    def detach(self, logger: logging.Logger) -> None:
        logger.removeHandler(self)


def _split(text: str) -> list[str]:
    return [text[i : i + _CHUNK] for i in range(0, len(text), _CHUNK)] or [text]

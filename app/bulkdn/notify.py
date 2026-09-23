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

    @property
    def enabled(self) -> bool:
        return self.config.enabled

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
            log.warning("telegram notification failed: %s", describe(exc))

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
                    log.warning("telegram rejected message to %s: %s", user_id, payload)
        except Exception as exc:  # noqa: BLE001 - one bad recipient must not stop the rest
            log.warning("telegram send to %s failed: %s", user_id, describe(exc))

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


def _split(text: str) -> list[str]:
    return [text[i : i + _CHUNK] for i in range(0, len(text), _CHUNK)] or [text]

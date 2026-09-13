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
from dataclasses import dataclass, field

import aiohttp
from .retry import describe

log = logging.getLogger(__name__)

# Telegram rejects messages past ~4096 characters. Splitting well below that
# leaves room for the header without needing to measure it.
_CHUNK = 1900
_TIMEOUT_S = 15


@dataclass
class TelegramConfig:
    bot_token: str = ""
    user_ids: list[int] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.user_ids)


class Notifier:
    """Sends run events to Telegram, or does nothing when unconfigured."""

    def __init__(self, config: TelegramConfig | None = None, *, label: str = "bulkdn"):
        self.config = config or TelegramConfig()
        self.label = label

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
                for chunk in _split(body):
                    for user_id in self.config.user_ids:
                        await self._send_one(session, user_id, chunk)
        except Exception as exc:  # noqa: BLE001 - reporting must never break trading
            log.warning("telegram notification failed: %s", describe(exc))

    async def _send_one(self, session: aiohttp.ClientSession, user_id: int, text: str) -> None:
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
        target = f"/{of}" if of else ""
        text = f"cycle <b>{cycle}{target}</b> complete"
        if detail:
            text += f"\n{detail}"
        await self.send(text, prefix="✅")

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
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            # No loop running: the caller is synchronous and the message is not
            # worth spinning one up for.
            coro.close()


def _split(text: str) -> list[str]:
    return [text[i : i + _CHUNK] for i in range(0, len(text), _CHUNK)] or [text]

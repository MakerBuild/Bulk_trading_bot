"""Holding back new groups while the market is moving fast.

Most of the non-fee cost is not the book's spread, which on BTC is a fraction
of a tick. It is the price moving between a maker fill and its hedge: a resting
order is filled when someone trades through its level, and the hedge then
meets the next one. That happens far more in a fast market. Two live runs on
the same settings, a day apart:

    quiet weekday, BTC moving ~25bps an hour       $4.65 per $100k
    Sunday night CME open, ~56bps an hour          $8.50 per $100k

with the hedge landing at the maker's own price 35% of the time in the first
and 18% in the second.

So a pause stops new groups from being drawn -- two ways, either or both:

  * by movement: BTC moved more than `max_move_bps` over the last
    `window_minutes`; it lifts after `calm_minutes` without such a move.
  * by the clock: inside one of the `schedule` windows, UTC.

Groups already trading are left alone and finish their cycle with the usual
instant hedge. Closing them at market to dodge the move would pay a taker fee
on every leg -- more than the move costs.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass(frozen=True)
class Window:
    """One schedule entry: on these days, from `start` to `end` minutes, UTC.

    An end at or before the start runs past midnight, and the days name the
    day it STARTS on: "sun 22:00-02:00" is Sunday 22:00 to Monday 02:00.
    """

    text: str
    days: frozenset[int]
    start: int
    end: int

    @classmethod
    def parse(cls, text: str) -> Window:
        parts = str(text).split()
        if len(parts) == 1:
            days_text, hours = "daily", parts[0]
        elif len(parts) == 2:
            days_text, hours = parts
        else:
            raise ValueError(f"{text!r}: expected like \"sun 22:00-02:00\"")
        return cls(text=str(text), days=_days(days_text, text), start=_minute(hours, 0, text),
                   end=_minute(hours, 1, text))

    def covers(self, moment: datetime) -> bool:
        day = moment.weekday()
        minute = moment.hour * 60 + moment.minute
        if self.start < self.end:
            return day in self.days and self.start <= minute < self.end
        # Past midnight: the evening of a listed day, or the small hours of the
        # day after one.
        return (day in self.days and minute >= self.start) or (
            (day - 1) % 7 in self.days and minute < self.end
        )


def _days(text: str, whole: str) -> frozenset[int]:
    text = text.lower()
    if text in ("daily", "*", "every"):
        return frozenset(range(7))
    days: set[int] = set()
    for chunk in text.split(","):
        if "-" in chunk:
            first, last = chunk.split("-", 1)
            a, b = _day(first, whole), _day(last, whole)
            day = a
            while True:
                days.add(day)
                if day == b:
                    break
                day = (day + 1) % 7
        else:
            days.add(_day(chunk, whole))
    return frozenset(days)


def _day(text: str, whole: str) -> int:
    key = text.strip().lower()[:3]
    if key not in DAYS:
        raise ValueError(f"{whole!r}: {text!r} is not a day (mon..sun, daily)")
    return DAYS.index(key)


def _minute(hours: str, which: int, whole: str) -> int:
    try:
        clock = hours.split("-")[which]
        h, m = clock.split(":")
        h, m = int(h), int(m)
    except (IndexError, ValueError) as exc:
        raise ValueError(f"{whole!r}: expected hours like 22:00-02:00") from exc
    if not (0 <= h <= 24 and 0 <= m < 60) or h * 60 + m > 24 * 60:
        raise ValueError(f"{whole!r}: {clock} is not a time of day")
    return h * 60 + m


@dataclass
class PauseConfig:
    # 0 switches the movement pause off.
    max_move_bps: float = 0.0
    window_minutes: float = 15.0
    calm_minutes: float = 10.0
    schedule: list[Window] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.max_move_bps > 0 or bool(self.schedule)

    def validate(self) -> None:
        if self.max_move_bps < 0:
            raise ValueError("pause.max_move_bps must be >= 0 (0 switches it off)")
        if self.max_move_bps > 0:
            if self.window_minutes <= 0:
                raise ValueError("pause.window_minutes must be > 0")
            if self.calm_minutes < 0:
                raise ValueError("pause.calm_minutes must be >= 0")


class MoveTracker:
    """High-low range of one price over a trailing window, in bps."""

    def __init__(self, window_s: float):
        self.window_s = window_s
        self._points: deque[tuple[float, float]] = deque()

    def add(self, price: float, now: float) -> None:
        if price and price > 0:
            self._points.append((now, price))
        while self._points and now - self._points[0][0] > self.window_s:
            self._points.popleft()

    def move_bps(self) -> float:
        if len(self._points) < 2:
            return 0.0
        prices = [p for _, p in self._points]
        low = min(prices)
        return (max(prices) - low) / low * 1e4


@dataclass
class Change:
    """A pause starting or lifting, for the log and Telegram."""

    paused: bool
    reason: str


class PauseGate:
    """Whether new groups may be drawn on one market right now."""

    def __init__(self, config: PauseConfig, label: str = "BTC", clock=time.time):
        self.config = config
        self.label = label
        self.clock = clock
        self.tracker = MoveTracker(config.window_minutes * 60)
        self._moved_at: float | None = None
        self.reason: str | None = None

    @property
    def paused(self) -> bool:
        return self.reason is not None

    def update(self, price: float | None) -> Change | None:
        """Feed the latest price; returns a Change when the state flips."""
        now = self.clock()
        reason = None

        if self.config.max_move_bps > 0:
            self.tracker.add(price or 0.0, now)
            move = self.tracker.move_bps()
            if move > self.config.max_move_bps:
                self._moved_at = now
                reason = (
                    f"{self.label} moved {move:.0f}bps in {self.config.window_minutes:g} min "
                    f"(limit {self.config.max_move_bps:g})"
                )
            elif self._moved_at is not None:
                calm_for = now - self._moved_at
                if calm_for < self.config.calm_minutes * 60:
                    reason = (
                        f"waiting for {self.config.calm_minutes:g} calm minutes "
                        f"after a fast move ({calm_for / 60:.0f} so far)"
                    )
                else:
                    self._moved_at = None

        if reason is None:
            moment = datetime.fromtimestamp(now, timezone.utc)
            for window in self.config.schedule:
                if window.covers(moment):
                    reason = f"scheduled pause {window.text} UTC"
                    break

        was = self.reason
        self.reason = reason
        if (was is None) != (reason is None):
            return Change(paused=reason is not None, reason=reason or (was or ""))
        if reason is not None and was is not None and _kind(was) != _kind(reason):
            # Still paused, for a different reason: say so once, not every tick.
            return Change(paused=True, reason=reason)
        return None


def _kind(reason: str) -> str:
    return "schedule" if reason.startswith("scheduled") else "move"

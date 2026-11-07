"""What the Telegram menu's ⚙️ Settings can change, and how a typed value is read.

Each setting names where it lives in settings.yaml -- top level, a block, or
one market -- how to read what is set now off the loaded config, and what
kind of value it takes. Writing goes through the terminal menu's own writers,
which load the edited file before it replaces the real one: a value the bot
would refuse to start with is refused here, with the reason, and the file is
left as it was.

Left out on purpose: the telegram block (whoever holds the chat must not be
able to hand it to someone else), the key file, the access gate, and the
plumbing -- urls, intervals, the state file.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

TOP = ""
MARKET = "market"


@dataclass(frozen=True)
class Setting:
    id: str          # short: it rides in Telegram's 64-byte callback data
    block: str       # TOP, a block name, or MARKET
    key: str         # the key as the file spells it
    label: str
    kind: str        # "number", "int" or "span"
    help: str
    read: Callable[[Any], Any]


def _span(span, single):
    return span if span is not None else single


SECTIONS: dict[str, tuple[str, list[Setting]]] = {
    "targets": ("🎯 Run targets", [
        Setting("cycles", "execution_target", "cycles", "Cycles", "int",
                "stop after this many open/hold/close cycles; 0 = no limit",
                lambda c: c.target.cycles),
        Setting("volume", "execution_target", "volume_usd", "Volume target $", "number",
                "stop once this much volume is done; 0 = no limit",
                lambda c: c.target.volume_usd),
        Setting("burn", "execution_target", "burn_usd", "Fee budget $", "number",
                "stop once this much has been spent on fees; 0 = no limit",
                lambda c: c.target.burn_usd),
        Setting("hold", TOP, "hold_minutes", "Hold, minutes", "span",
                "how long each cycle holds before closing; a range like 0.5-1.5",
                lambda c: c.hold_minutes),
        Setting("phase", TOP, "max_phase_minutes", "Max open/close, min", "number",
                "an open or close taking longer than this is called stuck",
                lambda c: c.max_phase_minutes),
    ]),
    "pool": ("👥 Groups", [
        Setting("groups", "pool", "max_groups", "Groups at once", "int",
                "how many pairs trade at the same time",
                lambda c: c.max_groups),
        Setting("hedgers", "pool", "max_takers", "Accounts per hedge", "int",
                "how many accounts may share one hedge (delta-neutral always uses 1)",
                lambda c: c.max_takers),
    ]),
    "risk": ("🛡 Risk", [
        Setting("exposure", "risk", "max_net_exposure_usd", "Max one-sided $", "number",
                "one-sided exposure allowed before the kill switch; keep it at least "
                "twice the largest order",
                lambda c: c.risk.max_net_exposure_usd),
        Setting("position", "risk", "max_position_usd", "Max position $", "number",
                "per account, per market",
                lambda c: c.risk.max_position_usd),
        Setting("rejects", "risk", "max_reject_streak", "Rejects in a row", "int",
                "rejected orders in a row before the bot stops",
                lambda c: c.risk.max_reject_streak),
        Setting("impact", "risk", "max_hedge_impact_bps", "Max hedge slippage bps", "number",
                "refuse a hedge that would slip more than this; 0 = no limit",
                lambda c: c.risk.max_hedge_impact_bps),
        Setting("margin", TOP, "max_margin_fraction", "Margin used, fraction", "number",
                "share of each account's margin the sizes may use, e.g. 0.25",
                lambda c: c.max_margin_fraction),
    ]),
    "taking": ("⚡ Delta-neutral", [
        Setting("slip", "taking", "max_slippage_bps", "Max slippage bps", "number",
                "depth is counted only this far from the best price",
                lambda c: c.taking.max_slippage_bps),
        Setting("spread", "taking", "max_spread_bps", "Max spread bps", "number",
                "wait while the spread is wider than this",
                lambda c: c.taking.max_spread_bps),
        Setting("depth", "taking", "depth_fraction", "Share of depth", "number",
                "a slice takes at most this share of the thinner side, e.g. 0.5",
                lambda c: c.taking.depth_fraction),
        Setting("pace", "taking", "slice_interval_s", "Pause between slices, s", "number",
                "seconds between one hedged slice and the next",
                lambda c: c.taking.slice_interval_s),
    ]),
    "auto": ("🔀 Auto-spread", [
        Setting("asize", "auto_spread", "notional_usd", "Size per cycle $", "span",
                "dollars per cycle in a market auto-spread picks that is not in "
                "📈 Markets; a range is drawn fresh each cycle",
                lambda c: c.auto_spread.notional_usd),
        Setting("alev", "auto_spread", "leverage", "Leverage", "number",
                "1-50 for those markets, capped by each market's own maximum",
                lambda c: c.auto_spread.leverage),
    ]),
    "pause": ("⏸ Pause on big moves", [
        Setting("move", "pause", "max_move_bps", "Pause on move, bps", "number",
                "pause opening when the price moved this much; 0 = off",
                lambda c: c.pause.max_move_bps),
        Setting("window", "pause", "window_minutes", "Move window, min", "number",
                "the move is measured over this many minutes",
                lambda c: c.pause.window_minutes),
        Setting("calm", "pause", "calm_minutes", "Calm before resuming, min", "number",
                "resume once it has been calm this long",
                lambda c: c.pause.calm_minutes),
    ]),
}

MARKET_SETTINGS: list[Setting] = [
    Setting("notional", MARKET, "notional_usd", "Size per cycle $", "span",
            "dollars per cycle; a range like 2000-3500 is drawn fresh each cycle",
            lambda leg: _span(leg.notional_span, leg.notional_usd)),
    Setting("cap", MARKET, "max_order_notional_usd", "Max order $", "span",
            "the largest single order; 0 = the whole size in one order",
            lambda leg: _span(leg.max_order_span, leg.max_order_notional_usd)),
    Setting("lev", MARKET, "leverage", "Leverage", "number",
            "1-50, capped by the market's own maximum",
            lambda leg: leg.leverage),
    Setting("offset", MARKET, "offset_bps", "Maker offset bps", "span",
            "how far from the touch the maker order rests (not used in delta-neutral)",
            lambda leg: _span(leg.offset_span, leg.offset_bps)),
    Setting("dist", MARKET, "max_distance_bps", "Re-price at bps", "number",
            "re-price the resting order once it drifts this far (not delta-neutral)",
            lambda leg: leg.max_distance_bps),
    Setting("patience", MARKET, "chase_patience_s", "Chase after, s", "number",
            "unfilled this long: go and get it; 0 = wait forever (not delta-neutral)",
            lambda leg: leg.chase_patience_s),
    Setting("ticks", MARKET, "improve_ticks", "Improve ticks", "int",
            "post this many ticks better than the best price (not delta-neutral)",
            lambda leg: leg.improve_ticks),
]

BY_ID: dict[str, Setting] = {
    setting.id: setting
    for _, settings in SECTIONS.values()
    for setting in settings
} | {setting.id: setting for setting in MARKET_SETTINGS}


# -- values ---------------------------------------------------------------------

_SUFFIX = {"k": 1e3, "m": 1e6}


def _number(text: str) -> float:
    """A number as people type it: 250k, $1,500, 0.5. Never negative."""
    raw = text.strip().lower().replace("$", "").replace(",", "").replace(" ", "")
    scale = 1.0
    if raw and raw[-1] in _SUFFIX:
        scale, raw = _SUFFIX[raw[-1]], raw[:-1]
    try:
        value = float(raw) * scale
    except ValueError:
        raise ValueError(f"{text.strip()!r} is not a number") from None
    if not math.isfinite(value) or value < 0:
        raise ValueError("it must be zero or more")
    return value


def render(value: float) -> str:
    """A number as the file spells it: 100, not 100.0; 0.25, not 2.5e-01."""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.10f}".rstrip("0").rstrip(".")


def parse(setting: Setting, text: str) -> str:
    """What to write for `text`, or ValueError saying what is wrong with it."""
    if setting.kind == "int":
        value = _number(text)
        if not value.is_integer():
            raise ValueError("it must be a whole number")
        return str(int(value))
    if setting.kind == "span":
        parts = [part for part in re.split(r"\s*(?:-|–|—|\.\.|to)\s*", text.strip()) if part]
        if len(parts) == 1:
            return render(_number(parts[0]))
        if len(parts) == 2:
            low, high = _number(parts[0]), _number(parts[1])
            if low > high:
                low, high = high, low
            return render(low) if low == high else f"{render(low)}-{render(high)}"
        raise ValueError("send one number, or a range like 100-200")
    return render(_number(text))


def shown(value: Any) -> str:
    """The current value, the way the file would spell it."""
    if value is None:
        return "-"
    low, high = getattr(value, "low", None), getattr(value, "high", None)
    if low is not None and high is not None:
        return render(low) if low == high else f"{render(low)}-{render(high)}"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (int, float)):
        return render(value)
    return str(value)


def example(setting: Setting) -> str:
    if setting.kind == "span":
        return "a number, or a range like 100-200"
    if setting.kind == "int":
        return "a whole number"
    return "a number"

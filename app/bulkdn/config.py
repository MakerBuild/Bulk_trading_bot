"""Configuration loading and validation.

The master private key is deliberately never sourced from the config file --
only from the BULK_PRIVATE_KEY environment variable, or from a key file the
keystore reads -- so a config can be committed or shared without leaking
signing authority.
"""

from __future__ import annotations

import difflib
import logging
import math
import os
import random
from dataclasses import dataclass, field
from typing import Any

import yaml

from . import keystore
from .notify import TelegramConfig
from .referral import AccessConfig

log = logging.getLogger(__name__)

PRIVATE_KEY_ENV = "BULK_PRIVATE_KEY"
# The shipped settings, which install.bat copies to settings.yaml on a first
# run. Under app/ because the operator never edits this one -- an update
# overwrites it, and the bot never reads it. Erasing local data resets their
# copy from here, so "clean" means the same file a fresh install produces.
SETTINGS_TEMPLATE = "app/settings.default.yaml"

# Fallback for when exporting an environment variable is inconvenient. Read
# relative to the current working directory, same as `settings.yaml` itself.
# The file is git-ignored; see .gitignore. It holds either an encrypted
# envelope or a bare base58 line -- `bulkdn.keystore` reads both and prompts
# for a password when the file is encrypted.
PRIVATE_KEY_FILE = "private_key.local"

# What the file looks like with no key in it. Erasing the key rewrites the file
# to this rather than deleting it: the operator still needs somewhere obvious to
# paste the next one, and a missing file means re-running install.bat to get it
# back. install.bat writes the same text on a first install -- test_erase checks
# the two have not drifted.
PRIVATE_KEY_TEMPLATE = """# Paste your BULK master account's base58 private key on the line
# below -- one line, no quotes, nothing else.
#
# This file never leaves your machine. Once the key is in, encrypt it
# from the menu: Accounts Management -> Encrypt Private Key.
#
# A sub-account has no key of its own -- it is created by, and signed
# for by, its master. To trade several masters, put each key on a
# line of its own; add them all before encrypting.

"""

# Mainnet endpoints, as published in the BULK OpenAPI spec (v3.0.10).
#
# This bot is mainnet-only. There is no network selector: the signature domain
# byte is part of the signed payload, and a mismatch between it and the host
# being posted to is rejected as `bad signature`, so pinning both together in
# one place removes the whole class of mistake.
#
# TLS on both hosts verifies against certifi's bundle (see ws_compat). An
# earlier note here said the WebSocket host served an expired certificate; it
# never did. What had expired was a root in the WINDOWS certificate store, which
# `requests` never consulted because it ships certifi -- so HTTP worked while the
# socket, trusting the system store, failed. With the socket on certifi too,
# `ws_ssl_auto_bypass` is no longer needed and is off by default.
MAINNET_HTTP_URL = "https://mainnet-api1.bulk.trade/api/v1"
MAINNET_WS_URL = "wss://mainnet-ws1.bulk.trade"

# Domain byte 1. Trusted client configuration, never a JSON field or header.
SIGNATURE_DOMAIN_NAME = "MAINNET"


class ConfigError(Exception):
    """Raised when the configuration is missing or internally inconsistent."""


@dataclass
class LegConfig:
    """One symbol's worth of strategy parameters.

    How much to trade is written **either** in dollars or in the base coin:

        notional_usd: 100      $100 of whatever `symbol` names
        size: 0.001            0.001 of the base coin

    `notional_usd` is the one to prefer, because it does not change meaning
    when `symbol` does. `size: 2.0` is $200 of SOL and $5,200 of ETH, so
    switching the symbol and leaving the number silently resizes the cycle by
    a factor of twenty-six. A dollar amount says the same thing in any market.

    A dollar amount has no size until there is a price, so `size` stays 0 until
    `sizing.resolve_notionals` fills it in at startup. Everything downstream
    reads `size` and never needs to know which way the leg was written.

    `offset_bps` places the resting order inside the touch; `max_distance_bps`
    is the drift that triggers a cancel+replace.
    """

    symbol: str
    # Exactly one of these two carries the leg's size. See the class docstring.
    size: float = 0.0
    # The value in force right now. A leg written as a range redraws this
    # at the start of every cycle; a fixed one keeps the same number.
    notional_usd: float = 0.0
    # How it was written, kept so the redraw has something to draw from.
    # None means the setting was a plain number and never varies.
    notional_span: Span | None = None
    offset_bps: float = 0.0
    # `offset_bps` written as a range, drawn per cycle. The price a
    # resting order takes is a pure function of the book and this
    # number, so two groups trading one market off one offset compute
    # the same tick and queue behind each other -- which a pool of
    # accounts is there to avoid. None means the number was fixed.
    offset_span: Span | None = None
    max_distance_bps: float = 5.0
    # How long a resting order may go unfilled before it stops sitting
    # `offset_bps` inside the touch and moves onto it. Still passive -- the
    # order never crosses, so the fill stays on the maker side. 0 keeps the
    # offset for as long as the order rests.
    chase_patience_s: float = 3.0
    # How many ticks PAST the touch to post, once the offset has been given up.
    #
    # 1 makes the order the best bid or the best ask outright, which fills ahead
    # of everyone queued at the old touch; 0 joins that queue and waits behind
    # all of it. Still passive either way -- the order is posted inside the
    # spread, never across it -- so this buys priority with a tick of price
    # rather than with a taker fee. Clamped to stay one tick short of the other
    # side, so a one-tick spread leaves the order on the touch.
    improve_ticks: int = 1
    # Once on the touch, rest only at a price where at least this many dollars
    # of OTHER people's orders already rest, instead of stepping ahead of the
    # book. 0 (the default) keeps `improve_ticks`.
    #
    # From live fills: when our order was the only thing at its price, the
    # hedge met a touch ~1.6bps worse, because taking us emptied the level;
    # with more than 1 BTC resting beside us it met our own price. Queueing
    # behind others is slower to fill, which is the price of it.
    join_depth_usd: float = 0.0
    # The per-order cap, in whichever unit suits; it defaults to the whole leg.
    max_order_size: float = 0.0
    max_order_notional_usd: float = 0.0
    max_order_span: Span | None = None
    # None leaves whatever the account already has. The exchange's own ceiling
    # for the market is checked at startup, since it differs per symbol.
    leverage: float | None = None
    # Whether this market trades at all. Off rather than deleted, so the
    # menu can turn a market off without throwing away the size, the offset
    # and the cap someone tuned for it -- and turn it back on next week with
    # those numbers intact.
    enabled: bool = True

    @property
    def priced_in_usd(self) -> bool:
        """Whether this leg still needs a price before it has a size."""
        return self.notional_usd > 0 or self.max_order_notional_usd > 0

    def validate(self, name: str) -> None:
        if not self.symbol:
            raise ConfigError(f"legs.{name}.symbol is required")

        if self.size < 0 or self.notional_usd < 0:
            raise ConfigError(f"legs.{name}: size and notional_usd must be >= 0")
        if self.size > 0 and self.notional_usd > 0:
            raise ConfigError(
                f"legs.{name} sets both `size` and `notional_usd` -- use one. "
                f"`notional_usd` is in dollars and keeps its meaning when you "
                f"change `symbol`; `size` is in the base coin and does not."
            )
        if self.size <= 0 and self.notional_usd <= 0:
            raise ConfigError(
                f"legs.{name} has no size: set `notional_usd: 100` for $100 of "
                f"{self.symbol}, or `size` for an amount of the base coin."
            )

        if self.max_order_size < 0 or self.max_order_notional_usd < 0:
            raise ConfigError(f"legs.{name}: the max_order_* caps must be >= 0")
        if self.max_order_size > 0 and self.max_order_notional_usd > 0:
            raise ConfigError(
                f"legs.{name} sets both `max_order_size` and "
                f"`max_order_notional_usd` -- use one."
            )
        # Both may be zero: the cap then defaults to the whole leg, which is
        # applied once the leg has a size.
        if self.notional_usd <= 0 and self.max_order_notional_usd <= 0 and self.max_order_size <= 0:
            raise ConfigError(f"legs.{name}.max_order_size must be > 0")

        if self.offset_bps < 0:
            raise ConfigError(f"legs.{name}.offset_bps must be >= 0")
        if self.max_distance_bps <= 0:
            raise ConfigError(f"legs.{name}.max_distance_bps must be > 0")
        if self.chase_patience_s < 0:
            raise ConfigError(f"legs.{name}.chase_patience_s must be >= 0 (0 disables it)")
        if self.improve_ticks < 0:
            raise ConfigError(
                f"legs.{name}.improve_ticks must be >= 0 (0 joins the touch "
                "instead of beating it)"
            )
        if self.join_depth_usd < 0:
            raise ConfigError(
                f"legs.{name}.join_depth_usd must be >= 0 (0 switches it off)"
            )
        if self.leverage is not None and not 1.0 <= self.leverage <= 50.0:
            raise ConfigError(f"legs.{name}.leverage must be between 1 and 50")


@dataclass
class RiskConfig:
    max_net_exposure_usd: float = 500.0
    max_position_usd: float = 5000.0
    max_reject_streak: int = 5
    ws_stale_timeout_s: float = 30.0
    price_stale_timeout_s: float = 15.0
    # Expected slippage ceiling for a hedge, read off the published impact
    # curve. 0 disables the check, which is also what happens for a market
    # with no curve published.
    max_hedge_impact_bps: float = 0.0

    def validate(self) -> None:
        if self.max_net_exposure_usd <= 0:
            raise ConfigError("risk.max_net_exposure_usd must be > 0")
        if self.max_position_usd <= 0:
            raise ConfigError("risk.max_position_usd must be > 0")
        if self.max_reject_streak < 1:
            raise ConfigError("risk.max_reject_streak must be >= 1")
        if self.max_hedge_impact_bps < 0:
            raise ConfigError("risk.max_hedge_impact_bps must be >= 0")
        # Zero is refused as well as a negative: both make every quote and
        # every socket read as stale from the first tick, so the bot would
        # halt -- or refuse to re-price -- on data that is milliseconds old.
        # Neither has an "off" meaning to preserve.
        if self.ws_stale_timeout_s <= 0:
            raise ConfigError("risk.ws_stale_timeout_s must be > 0 seconds")
        if self.price_stale_timeout_s <= 0:
            raise ConfigError("risk.price_stale_timeout_s must be > 0 seconds")


@dataclass
class ExecutionTarget:
    """When to stop starting new cycles.

    Whichever limit is reached first ends the run; `0` disables one. `cycles`
    is checked against the counter the bot keeps, while the other two are
    measured from the exchange's own fill records, so they survive a restart
    and cannot drift from what was actually charged.

    `volume_usd` counts qualifying volume only. Fills that crossed between the
    master and its own sub-account are real spend but earn no tier credit, so
    counting them would overstate progress toward a volume goal.
    """

    cycles: int = 1
    # How much fee activity to accumulate before stopping, as a plain amount.
    # A maker-heavy pair earns more than it pays and so runs a negative total;
    # the sign is not something to configure, so this counts the distance from
    # zero either way. 0 disables it.
    burn_usd: float = 0.0
    volume_usd: float = 0.0

    def validate(self) -> None:
        if self.cycles < 0:
            raise ConfigError("execution_target.cycles must be >= 0 (0 = unlimited)")
        if self.burn_usd < 0:
            raise ConfigError(
                "execution_target.burn_usd must be >= 0 -- it is an amount of "
                "fees to spend, written as a plain positive amount"
            )
        if self.volume_usd < 0:
            raise ConfigError("execution_target.volume_usd must be >= 0")

    @property
    def measures_fills(self) -> bool:
        """Whether anything here needs the fill history read."""
        return self.burn_usd > 0 or self.volume_usd > 0


@dataclass(frozen=True)
class Span:
    """A number that may be written as a range, and drawn from each time.

    A fixed value repeats exactly, and an exact repeat is a pattern in the
    fill history whether it is a hold length or an order size. A range
    removes it at no cost, so anything that can be written as one accepts
    all three spellings:

        4000            exactly that, every time
        2000-6000       drawn uniformly between the two
        [2000, 6000]    the same thing, spelled as a list

    `name` is only ever used to say which setting was wrong; a message
    naming `hold_minutes` when the operator mistyped `notional_usd` sends
    them to the wrong line of the file.
    """

    low: float
    high: float

    @classmethod
    def parse(cls, value: Any, name: str = "value") -> Span:
        """Read a number, a `low-high` string, or a two-item list."""
        if isinstance(value, Span):
            return value

        if isinstance(value, bool):
            # bool is an int subclass, and `yes` is a mistake rather than a
            # value of one.
            raise ConfigError(f"{name} must be a number or a range, got {value!r}")

        if isinstance(value, (int, float)):
            return cls._checked(value, value, value, name)

        if isinstance(value, (list, tuple)):
            if len(value) != 2:
                raise ConfigError(
                    f"{name} as a list must hold exactly two values, got {list(value)!r}"
                )
            return cls._checked(value[0], value[1], value, name)

        if isinstance(value, str):
            text = value.strip()
            # YAML reads `0.5-1` as a string, which is the spelling the
            # settings file documents, so it is the one that must work.
            low, sep, high = text.partition("-")
            if not sep:
                return cls._checked(text, text, value, name)
            return cls._checked(low, high, value, name)

        raise ConfigError(f"{name} must be a number or a range, got {value!r}")

    @classmethod
    def _checked(cls, low: Any, high: Any, original: Any, name: str) -> Span:
        try:
            lo, hi = float(str(low).strip()), float(str(high).strip())
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"{name} must be a number or a range like 2000-6000, got {original!r}"
            ) from exc
        if lo < 0:
            raise ConfigError(f"{name} must be >= 0, got {original!r}")
        if hi < lo:
            raise ConfigError(
                f"{name} range runs backwards -- write the smaller number "
                f"first, got {original!r}"
            )
        return cls(lo, hi)

    def pick(self) -> float:
        """One draw. A fixed value returns itself."""
        if self.high == self.low:
            return self.low
        return random.uniform(self.low, self.high)

    @property
    def is_range(self) -> bool:
        return self.high != self.low

    def __str__(self) -> str:
        if self.is_range:
            return f"{self.low:g}-{self.high:g}"
        return f"{self.low:g}"


@dataclass(frozen=True)
class HoldTime(Span):
    """How long to stay fully open, drawn fresh at the start of every hold.

        hold_minutes: 0.5          exactly 30 seconds, every cycle
        hold_minutes: 0.5-1        somewhere between 30 and 60 seconds
        hold_minutes: [0.5, 1]     the same thing, spelled as a list

    The draw happens once per cycle and is then stored as an absolute
    deadline in the state file, so a restart mid-hold resumes the hold it
    was serving rather than rolling a new one.
    """

    @classmethod
    def parse(cls, value: Any, name: str = "hold_minutes") -> HoldTime:
        return super().parse(value, name)


@dataclass
class Config:
    # Every market this config knows about, in file order. Which of them
    # actually trade is each one's `enabled`, so a market can be turned off
    # from the menu without losing the numbers tuned for it.
    #
    # A list rather than two named blocks: the two names said which account
    # opened which leg, and accounts are no longer chosen that way -- they are
    # drawn from the pool per cycle. What is left is a set of markets, and
    # there is no reason for that set to be exactly two.
    markets: list[LegConfig]
    # WHICH ACCOUNTS trade together. Not how many markets -- that is the list
    # above, and every mode trades all of the enabled ones.
    #
    #   single   one master and its own sub-accounts. Every trade is between
    #            accounts the exchange can see belong to each other.
    #   multi    every master and every sub-account, in one pool. A group is
    #            drawn from all of them, so most cycles put two masters on
    #            opposite sides of a trade -- and nothing on the exchange
    #            links those two to each other.
    #
    # That difference is the whole reason for the setting. Self-trades inside
    # one tree earn referral volume but no fee-tier volume, because the
    # documentation excludes them; a trade between two masters is not a
    # self-trade at all, to anyone looking.
    mode: str = "multi"
    # Which master trades in single mode: its line number in the key file,
    # counting from 1. Ignored in multi, where every key is in play.
    single_master: int = 1
    # How many groups may be open at once, and how many accounts may share
    # one hedge.
    #
    # The cap is a setting rather than a discovery: a hundred accounts
    # allow fifty groups, which is fifty resting orders being chased and
    # fifty hedges chasing them, against an exchange that answered 429 to
    # two accounts polling every five seconds. Reaching that limit through
    # rejections means finding out during a cycle rather than before one.
    max_groups: int = 5
    max_takers: int = 1
    hold_minutes: HoldTime = field(default_factory=lambda: HoldTime(5.0, 5.0))
    # How long OPEN or EXIT may run before the cycle is called stuck.
    # HOLD is exempt: it ends on a clock it sets itself.
    #
    # Not a performance knob -- a normal phase takes under a minute, so
    # this only ever fires when a resting order is not going to fill. It
    # matters most in EXIT, where the sweep that closes leftovers runs
    # only after the phase ends. 0 disables it.
    max_phase_minutes: float = 30.0
    chase_interval_s: float = 1.0
    # How often the hedge rule is re-run against the position book.
    # Local arithmetic, so this costs nothing and stays brisk.
    reconcile_interval_s: float = 5.0
    # How often positions are re-read from the exchange REGARDLESS of
    # whether anything looks wrong. The read exists to catch a socket
    # that has gone quiet without disconnecting -- and that condition is
    # already measured, so it triggers the read directly. This is only
    # the backstop underneath it, for a socket that chats but is wrong.
    #
    # It used to happen every reconcile_interval_s, which is one HTTP
    # request per account every five seconds. At two accounts that was
    # already enough to draw a 429 from the exchange; the pool this is
    # being prepared for has a hundred.
    position_sync_interval_s: float = 60.0
    cycles: int = 1
    hedge_tolerance_lots: float = 1.0
    overlay_ttl_ms: int = 2000
    # Fallback when the configured sizes need more margin than the accounts
    # hold: every leg is scaled so the cycle fits inside this share of the
    # smaller account's available margin. Sizes that already fit are used
    # as written.
    max_margin_fraction: float = 0.25
    # How each market is spelled in the settings file, same order as
    # `markets`. An old file says `legs.master_account`, a new one says
    # `markets[0]`, and an error has to name the one the operator will find
    # when they open the file.
    market_names: list[str] = field(default_factory=list)
    risk: RiskConfig = field(default_factory=RiskConfig)
    target: ExecutionTarget = field(default_factory=ExecutionTarget)
    state_file: str = "./app/state/strategy_state.json"
    log_level: str = "INFO"

    # Telegram reporting. Off unless both a token and at least one recipient
    # are set -- the bot runs unattended, so a halt that only reaches the
    # console reaches nobody.
    telegram: TelegramConfig = field(default_factory=TelegramConfig)

    @property
    def active_legs(self) -> list[LegConfig]:
        """The markets this run actually trades, in file order.

        Everything that used to name both legs reads this instead, so turning
        a market off is one list being shorter rather than a branch in each of
        nine places -- which is how one of them gets missed and a market keeps
        being sized, chased or capped after it stopped trading.
        """
        return [market for market in self.markets if market.enabled]

    @property
    def master_account(self) -> LegConfig:
        """The first market. Kept for the callers that only need any market.

        There is nothing master-ish about it any more -- the name survives
        because a handful of places want one market's chase settings and do
        not care which.
        """
        return self.markets[0]

    # Referral gating, checked once at startup. See bulkdn/referral.py for what
    # a client-side gate does and does not actually prevent.
    access: AccessConfig = field(default_factory=AccessConfig)

    # Escape hatches for TLS, both OFF by default.
    #
    # The WebSocket was once thought to serve an expired certificate, and the
    # bypass was switched on by default to keep the account stream up. The
    # certificate was fine: an expired root in the Windows store was rejecting
    # it, and verifying against certifi (ws_compat) fixed the actual cause. A
    # default that quietly drops verification would now only ever fire on a
    # certificate that really is wrong -- which is the one case where the fills
    # and positions every hedge is computed from must not be trusted. Kept, off,
    # for an operator whose network genuinely cannot connect any other way.
    ws_insecure_ssl: bool = False
    ws_ssl_auto_bypass: bool = False

    # Optional endpoint overrides, for when the derived host is wrong.
    http_url_override: str = ""
    ws_url_override: str = ""

    # Populated at load time, not from the YAML file.
    #
    # Several master keys may be given, one per line. Every account under every
    # one of them joins the pool the strategy draws pairs from; `private_key`
    # remains the first of them, because the single-account commands -- status,
    # transfer, create-subaccount -- act on one account and that one is it.
    #
    # Both are `repr=False`. A dataclass repr prints every field, and a Config
    # ends up in a repr more easily than it looks: a failing test's assertion
    # message, a debugger, a `log.debug("%r", config)` someone adds while
    # chasing something else. Any of those would put the signing keys into
    # logs.txt -- the file operators are told to send when asking for help.
    private_keys: list[str] = field(default_factory=list, repr=False)
    # The one a single-account command acts on. Kept as a field of its own
    # rather than derived, because most of the bot and most of its tests name
    # exactly one key and should not have to know a pool exists. The two are
    # reconciled below so they cannot disagree.
    private_key: str = field(default="", repr=False)

    @property
    def http_url(self) -> str:
        return self.http_url_override or MAINNET_HTTP_URL

    @property
    def ws_url(self) -> str:
        return self.ws_url_override or MAINNET_WS_URL

    @property
    def signature_domain_name(self) -> str:
        return SIGNATURE_DOMAIN_NAME

    @property
    def legs(self) -> dict[str, LegConfig]:
        """Markets keyed by symbol, which is how the rest of the bot finds them."""
        return {market.symbol: market for market in self.markets}

    def _market_name(self, index: int) -> str:
        """How a market is addressed in an error message.

        An old file spells its markets `legs.master_account`; a new one
        spells them `markets[0]`. The error has to name the one the operator
        will actually find when they open the file.
        """
        return self.market_names[index] if index < len(self.market_names) else str(index)

    def __post_init__(self) -> None:
        # Callers that build a Config directly pass a plain number; the
        # YAML path passes a HoldTime. Normalise so the rest of the code
        # only ever sees the range.
        self.hold_minutes = HoldTime.parse(self.hold_minutes)

        # A caller naming one key and a caller naming a pool must both end up
        # with the two agreeing. Most of the bot, and nearly all of its tests,
        # name exactly one and should not have to know a pool exists.
        if self.private_keys and not self.private_key:
            self.private_key = self.private_keys[0]
        elif self.private_key and not self.private_keys:
            self.private_keys = [self.private_key]

    def validate(self, require_credentials: bool = True) -> None:
        """Validate the configuration."""
        if not self.http_url or not self.ws_url:
            raise ConfigError("http_url and ws_url must not be empty")
        if require_credentials and not self.private_key:
            raise ConfigError(
                f"no private key found -- either export {PRIVATE_KEY_ENV}, or "
                f"put the master account's base58 key in {PRIVATE_KEY_FILE} "
                "(git-ignored, one line, no quotes) and encrypt it with "
                "`bulkdn encrypt-key`"
            )
        self.target.validate()
        if self.mode not in ("single", "multi"):
            raise ConfigError(
                f"mode must be 'single' or 'multi', got {self.mode!r}"
            )
        if self.max_groups < 1:
            raise ConfigError("max_groups must be at least 1")
        if self.max_takers < 1:
            raise ConfigError("max_takers must be at least 1")
        if not self.private_keys and require_credentials:
            raise ConfigError(
                "the bot trades the accounts under the keys in "
                f"{PRIVATE_KEY_FILE}, and that file named none"
            )
        if self.mode == "single" and self.single_master < 1:
            raise ConfigError(
                f"single_master is the key's line number counting from 1, got "
                f"{self.single_master}"
            )
        if (
            self.mode == "single"
            and self.private_keys
            and self.single_master > len(self.private_keys)
        ):
            raise ConfigError(
                f"single_master is {self.single_master} but {PRIVATE_KEY_FILE} "
                f"holds {len(self.private_keys)} key(s). Pick one of them from "
                "Configuration -> Markets & Accounts."
            )
        if not self.markets:
            raise ConfigError("config must define at least one market")
        # Validated even when switched off, so a typo in a market someone
        # turned off is found now rather than the day they turn it back on.
        for index, market in enumerate(self.markets):
            market.validate(self._market_name(index))
        if not self.active_legs:
            raise ConfigError(
                "every market is switched off, so there is nothing to trade. "
                "Turn one on from Configuration -> Markets & Accounts."
            )
        traded = [market.symbol for market in self.active_legs]
        if len(set(traded)) != len(traded):
            raise ConfigError(
                "two markets name the same symbol. A leg is held per symbol, "
                "so the second would overwrite the first and one of them would "
                "silently stop trading."
            )
        if not 0.0 < self.max_margin_fraction <= 1.0:
            raise ConfigError(
                "max_margin_fraction must be between 0 and 1 "
                f"(0.25 = a quarter of available margin), got {self.max_margin_fraction}"
            )
        if self.max_phase_minutes < 0:
            raise ConfigError("max_phase_minutes must be >= 0 (0 disables it)")
        if self.chase_interval_s <= 0:
            raise ConfigError("chase_interval_s must be > 0")
        if self.reconcile_interval_s <= 0:
            raise ConfigError("reconcile_interval_s must be > 0")
        if self.position_sync_interval_s <= 0:
            raise ConfigError("position_sync_interval_s must be > 0")
        if self.cycles < 0:
            raise ConfigError("cycles must be >= 0 (0 means run forever)")
        # Below one lot the exchange cannot express the correction, so a
        # sub-lot tolerance would make the hedger spin on an uncorrectable
        # residual.
        if self.hedge_tolerance_lots < 1.0:
            raise ConfigError("hedge_tolerance_lots must be >= 1.0")
        self.risk.validate()
        if self.overlay_ttl_ms < 0:
            raise ConfigError("overlay_ttl_ms must be >= 0")
        self._warn_exposure_below_order_cap()

    def _warn_exposure_below_order_cap(self) -> None:
        """Say so when one clean fill would trip the exposure kill switch.

        A resting order that fills all at once leaves the pair one-sided by
        its size until the hedge lands, so `risk.max_net_exposure_usd` has to
        sit above the per-order cap with room to spare. Below it, the first
        full fill halts the run: every order cancelled, every account closed
        at market. A warning rather than an error, because a coin-sized cap
        cannot be priced here and a deliberately tight limit is the operator's
        call -- but it must not be a surprise found in a halt message.
        """
        limit = self.risk.max_net_exposure_usd
        for index, market in enumerate(self.markets):
            if not market.enabled:
                continue
            cap = market.max_order_notional_usd
            if market.max_order_span is not None:
                cap = max(cap, market.max_order_span.high)
            if cap > 0 and limit <= cap:
                log.warning(
                    "risk.max_net_exposure_usd ($%s) is not above %s's per-order "
                    "cap ($%s): one resting order filling in full would trip "
                    "the exposure halt before the hedge lands. Twice the cap is "
                    "a sane floor.",
                    f"{limit:g}", self._market_name(index), f"{cap:g}",
                )


# -- reading values ----------------------------------------------------------
#
# Every scalar in the file goes through one of these rather than a bare
# `float()` / `int()` / `bool()`. The bare calls fail in two different bad ways:
#
#   * `float("5bps")` raises a ValueError whose traceback names no setting, so
#     the operator sees a stack dump and has to guess which of forty lines did
#     it -- and the menu, which catches ConfigError, catches nothing.
#   * `bool("false")` is True. A quoted `enabled: "false"` -- which is what some
#     editors and every copy-paste from a chat produce -- switched a market ON.
#
# Each helper names the setting in its error, which is the whole point.

_TRUE_WORDS = frozenset({"true", "yes", "on", "1"})
_FALSE_WORDS = frozenset({"false", "no", "off", "0"})


def _as_bool(value: Any, name: str) -> bool:
    """A yes/no setting, strictly.

    YAML already turns unquoted `true`/`no`/`off` into booleans; this is for
    the quoted spellings, which arrive as strings and which `bool()` reads as
    True whatever they say. Anything that is not clearly one or the other is
    refused rather than guessed: a switch that decides whether a market trades
    or whether TLS is checked should not be resolved by a coin toss.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    raise ConfigError(f"{name} must be true or false, got {value!r}")


def _as_float(value: Any, name: str) -> float:
    """A number, or a ConfigError naming the setting.

    `true` is refused even though Python calls it 1: a boolean where a number
    belongs is a mistake in the file, not a value. So are `.inf` and `.nan`,
    which YAML happily reads and which then pass every `> 0` check in validate
    -- or, for nan, fail every comparison silently.
    """
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be a number, got {value!r}")
    try:
        number = float(value.strip()) if isinstance(value, str) else float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc
    if not math.isfinite(number):
        raise ConfigError(f"{name} must be a finite number, got {value!r}")
    return number


def _as_int(value: Any, name: str) -> int:
    """A whole number. `2.5` for a count is refused, not truncated to 2.

    Truncating would quietly run a different setting from the one written --
    `max_groups: 2.5` is either a typo or a misunderstanding, and in both
    cases the operator should hear about it. `3.0` is accepted, since it is 3.
    """
    number = _as_float(value, name)
    if not number.is_integer():
        raise ConfigError(f"{name} must be a whole number, got {value!r}")
    return int(number)


def _as_str(value: Any, name: str) -> str:
    """A text setting. A number or a list here is a misplaced line, not text."""
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be text, got {value!r}")
    return value


def _mapping(value: Any, name: str) -> dict[str, Any]:
    """A block that must be a mapping. Absent or empty reads as `{}`."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping")
    return value


# -- spotting typos ----------------------------------------------------------
#
# Unknown keys were ignored in silence, so `notional_ust: 5000` loaded, traded
# the default, and the operator concluded the bot ignores its own settings.
# They are warned about rather than refused: a newer settings file opened by an
# older build must still start, and a key that did nothing yesterday is not a
# reason to refuse to close a position today.

_LEG_KEYS = frozenset({
    "symbol", "size", "notional_usd", "offset_bps", "max_distance_bps",
    "chase_patience_s", "improve_ticks", "join_depth_usd", "max_order_size",
    "max_order_notional_usd", "leverage", "enabled",
})
_TOP_KEYS = frozenset({
    "markets", "legs", "pool", "mode", "single_master", "hold_minutes",
    "max_phase_minutes", "chase_interval_s", "reconcile_interval_s",
    "position_sync_interval_s", "cycles", "execution_target",
    "hedge_tolerance_lots", "overlay_ttl_ms", "max_margin_fraction", "risk",
    "state_file", "log_level", "http_url", "ws_url", "ws_insecure_ssl",
    "ws_ssl_auto_bypass", "telegram", "access",
})
_POOL_KEYS = frozenset({"max_groups", "max_takers", "single_master"})
_RISK_KEYS = frozenset({
    "max_net_exposure_usd", "max_position_usd", "max_reject_streak",
    "ws_stale_timeout_s", "price_stale_timeout_s", "max_hedge_impact_bps",
})
_TARGET_KEYS = frozenset({"cycles", "burn_usd", "volume_usd"})
_TELEGRAM_KEYS = frozenset({"bot_token", "user_ids", "user_id"})
_ACCESS_KEYS = frozenset({
    "require_referral", "codes", "code", "wallets", "wallet", "allow_on_error",
})

# Settings that used to mean something. Named with what happened to them, so
# the warning says "delete this" rather than "did you mean ...", which for a
# retired key would point at an unrelated setting that happens to look alike.
_RETIRED = {
    "sub1_pubkey": (
        "no longer read and can be deleted. Sub-accounts are found from each "
        "master at startup, so there is nothing to pin."
    ),
    "tight_distance_bps": (
        "no longer used and can be deleted. A tightened order now follows the "
        "touch tick by tick, which is what that setting was trying to "
        "approximate in the wrong unit."
    ),
}


def _warn_unknown(raw: dict[str, Any], known: frozenset[str], where: str) -> None:
    """Log each key `known` does not list, with the nearest one that exists."""
    for key in raw:
        path = f"{where}.{key}" if where else str(key)
        if not isinstance(key, str):
            log.warning("%s is not a setting this bot reads, so it has no effect", path)
            continue
        if key in known:
            continue
        if key in _RETIRED:
            log.warning("%s is %s", path, _RETIRED[key])
            continue
        near = difflib.get_close_matches(key, sorted(known), n=1, cutoff=0.6)
        hint = f" -- did you mean `{near[0]}`?" if near else ""
        log.warning(
            "%s is not a setting this bot reads, so it has no effect%s", path, hint
        )


def _span_from_raw(raw: dict[str, Any], key: str, name: str) -> Span | None:
    """A leg setting that may be written as a range. None when it is absent.

    For the two dollar settings, the margin plan at startup is built from the
    HIGH end rather than from the first draw: every later draw then fits inside
    a budget that was already checked, so a cycle that happens to roll a big
    number cannot be the one that discovers there was not enough margin for it.
    `offset_bps` carries no such constraint and resolves to its low end.
    """
    if key not in raw or raw[key] is None:
        return None
    return Span.parse(raw[key], f"legs.{name}.{key}")


def _leg_from_dict(raw: dict[str, Any], name: str) -> LegConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"legs.{name} must be a mapping")
    # Retired and misspelled settings alike. Unknown keys used to be ignored
    # in silence, which let someone tune a number that was never read and
    # conclude the bot ignores its own config.
    _warn_unknown(raw, _LEG_KEYS, name)

    def number(key: str, default: float) -> float:
        value = raw.get(key)
        return default if value is None else _as_float(value, f"{name}.{key}")

    size = number("size", 0.0)
    cap_size = number("max_order_size", 0.0)

    # Both dollar settings may be written as a range. The value carried
    # forward is the HIGH end, because the margin plan at startup is built
    # from it: every later draw then fits inside a budget already checked,
    # so the cycle that rolls a big number is not the one that discovers
    # there was never margin for it.
    notional_span = _span_from_raw(raw, "notional_usd", name)
    cap_span = _span_from_raw(raw, "max_order_notional_usd", name)
    # The scalar is the LOW end here, not the high: it is only read by a
    # leg that was not drawn from a group, and the least passive end of
    # the range is the safer fallback -- an order resting further inside
    # the touch waits longer, and a phase that never fills is what
    # `max_phase_minutes` ends the run over.
    offset_span = _span_from_raw(raw, "offset_bps", name)
    notional_usd = notional_span.high if notional_span else 0.0
    cap_usd = cap_span.high if cap_span else 0.0

    # An unset cap means "the whole leg", expressed in the unit the leg used.
    # Resolving it here keeps `size` and its cap in step when a dollar leg is
    # later converted.
    if cap_size <= 0 and cap_usd <= 0:
        cap_size, cap_usd = size, notional_usd
        # A leg capped by its own size follows that size when it is drawn,
        # rather than being pinned to the high end for the rest of the run.
        cap_span = notional_span

    if "symbol" in raw and not isinstance(raw["symbol"], str):
        raise ConfigError(f"{name}.symbol must be a market name like BTC-USD")

    try:
        return LegConfig(
            symbol=raw["symbol"],
            size=size,
            notional_usd=notional_usd,
            notional_span=notional_span,
            max_order_span=cap_span,
            offset_bps=offset_span.low if offset_span else 0.0,
            offset_span=offset_span,
            max_distance_bps=number("max_distance_bps", 5.0),
            chase_patience_s=number("chase_patience_s", 3.0),
            improve_ticks=(
                _as_int(raw["improve_ticks"], f"{name}.improve_ticks")
                if raw.get("improve_ticks") is not None
                else 1
            ),
            join_depth_usd=number("join_depth_usd", 0.0),
            max_order_size=cap_size,
            max_order_notional_usd=cap_usd,
            leverage=(
                _as_float(raw["leverage"], f"{name}.leverage")
                if raw.get("leverage") is not None
                else None
            ),
            enabled=_as_bool(raw.get("enabled", True), f"{name}.enabled"),
        )
    except KeyError as exc:
        raise ConfigError(f"legs.{name} is missing required key {exc}") from exc


def _mode_from_raw(value: Any) -> str:
    """The mode, accepting what older settings files called it.

    `pool` was this mode's name while it was the third of three. It is now
    the only way accounts are chosen, so it is simply `multi`, and a file
    still saying `pool` keeps working rather than refusing to start over a
    word.
    """
    mode = str(value).strip().lower()
    if mode == "pool":
        return "multi"
    return mode


def _markets_from_raw(
    raw: dict[str, Any], legs: dict[str, Any]
) -> tuple[list[LegConfig], list[str]]:
    """The markets to trade, from either spelling of the settings file.

    `markets:` is a list, which is the spelling the menu writes. `legs:` is
    the older mapping of two named blocks, and it is still read as written --
    a subscriber's file must not stop working because the shape it uses was
    superseded. The two names it uses are kept in the error messages, so an
    operator reading a complaint about `legs.sub_account` can find that line.
    """
    raw_markets = raw.get("markets")
    if raw_markets is not None:
        if not isinstance(raw_markets, list) or not raw_markets:
            raise ConfigError("markets must be a non-empty list")
        markets = []
        names = []
        for index, entry in enumerate(raw_markets):
            name = f"markets[{index}]"
            names.append(name)
            markets.append(_leg_from_dict(entry, name))
        return markets, names

    if not legs:
        raise ConfigError("config must define `markets`, or a `legs` block")
    # Every block, whatever it is called, in file order. The names used to
    # mean something -- they said which account opened which leg -- and they
    # have not for a while: accounts are drawn per cycle now. So a block is
    # named whatever its author found clearest, and two files naming their
    # markets differently are the same file.
    #
    # This is also why `legs.btc` is no longer refused. It was, on the
    # grounds that a file using the old names would silently start with
    # default legs; nothing defaults now, so the old names load as what they
    # plainly say. Refusing them had become a trap of its own -- a market
    # added from the menu was named after its coin, and a `sol:` block was
    # turned away by a message about a rename from two versions ago.
    names = [f"legs.{name}" for name in legs]
    return [_leg_from_dict(legs[name], f"legs.{name}") for name in legs], names


def _load_private_keys(required: bool) -> list[str]:
    """Every signing key, from the environment or the key file, in order.

    The environment wins so an unattended run needs no password prompt, and it
    accepts several the same way the file does -- one per line, or separated by
    commas for the shells where a newline in a variable is a fight.

    When credentials are not required the file is skipped entirely, which keeps
    read-only commands from asking for a password they will not use.

    Order is the operator's: the first key is the account that single-account
    commands act on, so it must not be sorted or deduplicated into a different
    first place.
    """
    from_env = os.environ.get(PRIVATE_KEY_ENV, "")
    if from_env:
        return keystore.read_plaintext_all(from_env.replace(",", "\n"))
    if not required:
        return []
    try:
        return keystore.load_all(PRIVATE_KEY_FILE)
    except keystore.KeystoreError as exc:
        raise ConfigError(str(exc)) from exc


def _telegram_from_dict(raw: Any) -> TelegramConfig:
    """Read Telegram settings, tolerating a single id given unwrapped."""
    if not isinstance(raw, dict):
        raise ConfigError("telegram must be a mapping")
    _warn_unknown(raw, _TELEGRAM_KEYS, "telegram")

    user_ids = raw.get("user_ids", raw.get("user_id", []))
    if isinstance(user_ids, (int, str)):
        user_ids = [user_ids]
    try:
        parsed_ids = [int(uid) for uid in user_ids]
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"telegram.user_ids must be integers: {exc}") from exc

    token = str(raw.get("bot_token", "") or "")
    if token and not parsed_ids:
        raise ConfigError(
            "telegram.bot_token is set but telegram.user_ids is empty -- "
            "notifications would go nowhere"
        )
    return TelegramConfig(bot_token=token, user_ids=parsed_ids)


def _access_from_dict(raw: Any) -> AccessConfig:
    """Read referral-gating settings, tolerating a single code given unwrapped."""
    if not isinstance(raw, dict):
        raise ConfigError("access must be a mapping")
    _warn_unknown(raw, _ACCESS_KEYS, "access")

    def as_list(value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [str(item) for item in value]
        raise ConfigError("access.codes and access.wallets must be strings or lists")

    access = AccessConfig(
        require_referral=_as_bool(
            raw.get("require_referral", False), "access.require_referral"
        ),
        codes=as_list(raw.get("codes", raw.get("code"))),
        wallets=as_list(raw.get("wallets", raw.get("wallet"))),
        allow_on_error=_as_bool(
            raw.get("allow_on_error", False), "access.allow_on_error"
        ),
    )
    try:
        access.validate()
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    return access


def _read_yaml(path: str) -> dict[str, Any]:
    """The settings file as a mapping, or a ConfigError that says what to do.

    Every way this can fail is something an operator does to the file in an
    editor, so every one of them gets a sentence rather than a traceback:

    * Notepad on a Russian-locale Windows can save as cp1251. The first
      Cyrillic comment then makes the file undecodable as UTF-8, and the raw
      UnicodeDecodeError names a byte offset and nothing else.
    * A tab, or an unclosed quote, is a YAML error with a line number -- worth
      passing on, since it points straight at the line.
    * A file that is a list or a bare word at the top parses fine and then
      failed with AttributeError on the first `.get`.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"{path} is not saved as UTF-8 (byte {exc.start} is not valid "
            "UTF-8). An editor most likely saved it in the Windows code page. "
            "Open it in Notepad, choose File -> Save As, set Encoding to UTF-8, "
            "and save it again."
        ) from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path} must be a set of `name: value` settings at the top level, "
            f"but it reads as a {type(raw).__name__}"
        )
    return raw


def _log_level(value: Any) -> str:
    """The log level name. Text only; an unknown name is logged at INFO.

    `log_level: 10` or a list used to reach `configure_logging` and fail there
    with AttributeError on `.upper()`, before logging existed to report it.
    """
    level = _as_str(value, "log_level")
    if not isinstance(logging.getLevelName(level.upper()), int):
        log.warning(
            "log_level %r is not a level this understands -- logging at INFO. "
            "Use INFO, or DEBUG when diagnosing something.",
            level,
        )
    return level


def load_config(
    path: str,
    require_credentials: bool = True,
    mode: str | None = None,
) -> Config:
    """Load, merge, and validate configuration.

    `mode` overrides the file, for the command line flag of the same name. It
    is applied before validation rather than assigned afterwards, so a command
    line that asks for something contradictory is refused by the same rules as
    a settings file that does -- rather than running with a config nothing ever
    checked.
    """
    raw = _read_yaml(path)
    _warn_unknown(raw, _TOP_KEYS, "")

    def number(block: dict[str, Any], key: str, default: float, where: str = "") -> float:
        value = block.get(key)
        name = f"{where}.{key}" if where else key
        return default if value is None else _as_float(value, name)

    def whole(block: dict[str, Any], key: str, default: int, where: str = "") -> int:
        value = block.get(key)
        name = f"{where}.{key}" if where else key
        return default if value is None else _as_int(value, name)

    pool_raw = _mapping(raw.get("pool"), "pool")
    _warn_unknown(pool_raw, _POOL_KEYS, "pool")

    # A file written by the menu carries `markets:` and no `legs:` at all,
    # so an absent one is only an error when nothing else names a market.
    legs = raw.get("legs") or {}
    if not isinstance(legs, dict):
        raise ConfigError("legs must be a mapping")
    markets, market_names = _markets_from_raw(raw, legs)

    risk_raw = _mapping(raw.get("risk"), "risk")
    _warn_unknown(risk_raw, _RISK_KEYS, "risk")

    target_raw = _mapping(raw.get("execution_target"), "execution_target")
    _warn_unknown(target_raw, _TARGET_KEYS, "execution_target")
    # `cycles` was a top-level key before execution targets existed; the
    # top-level spelling still works and the nested one wins.
    #
    # Absent means unlimited, as the shipped file says. It meant 1 in the code,
    # which a pool run never honoured -- so settings written before cycles were
    # counted per group would suddenly have run one group and stopped.
    if target_raw.get("cycles") is not None:
        cycles = whole(target_raw, "cycles", 0, "execution_target")
    else:
        cycles = whole(raw, "cycles", 0)
    target = ExecutionTarget(
        cycles=cycles,
        burn_usd=number(target_raw, "burn_usd", 0.0, "execution_target"),
        volume_usd=number(target_raw, "volume_usd", 0.0, "execution_target"),
    )

    if raw.get("single_master") is not None:
        single_master = whole(raw, "single_master", 1)
    else:
        single_master = whole(pool_raw, "single_master", 1, "pool")

    config = Config(
        markets=markets,
        market_names=market_names,
        mode=_mode_from_raw(mode if mode is not None else raw.get("mode", "multi")),
        single_master=single_master,
        max_groups=whole(pool_raw, "max_groups", 5, "pool"),
        max_takers=whole(pool_raw, "max_takers", 1, "pool"),
        hold_minutes=HoldTime.parse(raw.get("hold_minutes", 5.0)),
        max_phase_minutes=number(raw, "max_phase_minutes", 30.0),
        chase_interval_s=number(raw, "chase_interval_s", 1.0),
        reconcile_interval_s=number(raw, "reconcile_interval_s", 5.0),
        position_sync_interval_s=number(raw, "position_sync_interval_s", 60.0),
        cycles=target.cycles,
        target=target,
        hedge_tolerance_lots=number(raw, "hedge_tolerance_lots", 1.0),
        overlay_ttl_ms=whole(raw, "overlay_ttl_ms", 2000),
        max_margin_fraction=number(raw, "max_margin_fraction", 0.25),
        risk=RiskConfig(
            max_net_exposure_usd=number(risk_raw, "max_net_exposure_usd", 500.0, "risk"),
            max_position_usd=number(risk_raw, "max_position_usd", 5000.0, "risk"),
            max_reject_streak=whole(risk_raw, "max_reject_streak", 5, "risk"),
            max_hedge_impact_bps=number(risk_raw, "max_hedge_impact_bps", 0.0, "risk"),
            ws_stale_timeout_s=number(risk_raw, "ws_stale_timeout_s", 30.0, "risk"),
            price_stale_timeout_s=number(risk_raw, "price_stale_timeout_s", 15.0, "risk"),
        ),
        state_file=_as_str(
            raw.get("state_file", "./app/state/strategy_state.json"), "state_file"
        ),
        log_level=_log_level(raw.get("log_level", "INFO")),
        http_url_override=_as_str(raw.get("http_url") or "", "http_url"),
        ws_url_override=_as_str(raw.get("ws_url") or "", "ws_url"),
        ws_insecure_ssl=_as_bool(raw.get("ws_insecure_ssl", False), "ws_insecure_ssl"),
        # Off by default -- see the field on Config for why.
        ws_ssl_auto_bypass=_as_bool(
            raw.get("ws_ssl_auto_bypass", False), "ws_ssl_auto_bypass"
        ),
        telegram=_telegram_from_dict(raw.get("telegram") or {}),
        access=_access_from_dict(raw.get("access") or {}),
        private_keys=_load_private_keys(require_credentials),
    )
    config.validate(require_credentials=require_credentials)
    return config

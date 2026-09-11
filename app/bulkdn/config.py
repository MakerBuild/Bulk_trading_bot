"""Configuration loading and validation.

The master private key is deliberately never sourced from the config file --
only from the BULK_PRIVATE_KEY environment variable, or from a key file the
keystore reads -- so a config can be committed or shared without leaking
signing authority.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from typing import Any

import yaml

from . import keystore
from .notify import TelegramConfig
from .referral import AccessConfig

PRIVATE_KEY_ENV = "BULK_PRIVATE_KEY"

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
# for by, the master -- so this one key is all the bot needs.

"""

# Mainnet endpoints, as published in the BULK OpenAPI spec (v3.0.10).
#
# This bot is mainnet-only. There is no network selector: the signature domain
# byte is part of the signed payload, and a mismatch between it and the host
# being posted to is rejected as `bad signature`, so pinning both together in
# one place removes the whole class of mistake.
#
# Note the WebSocket host currently serves an EXPIRED certificate while the
# HTTP host verifies cleanly, so `ws_ssl_auto_bypass` (on by default) is what
# keeps the account stream connectable.
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
    notional_usd: float = 0.0
    offset_bps: float = 0.0
    max_distance_bps: float = 5.0
    # The per-order cap, in whichever unit suits; it defaults to the whole leg.
    max_order_size: float = 0.0
    max_order_notional_usd: float = 0.0
    # None leaves whatever the account already has. The exchange's own ceiling
    # for the market is checked at startup, since it differs per symbol.
    leverage: float | None = None

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
                "fees, counted whichever way they went"
            )
        if self.volume_usd < 0:
            raise ConfigError("execution_target.volume_usd must be >= 0")

    @property
    def measures_fills(self) -> bool:
        """Whether anything here needs the fill history read."""
        return self.burn_usd > 0 or self.volume_usd > 0


@dataclass(frozen=True)
class HoldTime:
    """How long to stay fully open, drawn fresh at the start of every hold.

    A fixed hold gives every cycle the same length, which is a shape anyone
    reading the fill history can see. A range removes it at no cost, so the
    config accepts both and a plain number is simply a range of zero width:

        hold_minutes: 0.5          exactly 30 seconds, every cycle
        hold_minutes: 0.5-1        somewhere between 30 and 60 seconds
        hold_minutes: [0.5, 1]     the same thing, spelled as a list

    The draw happens once per cycle and is then stored as an absolute deadline
    in the state file, so a restart mid-hold resumes the hold it was serving
    rather than rolling a new one.
    """

    low: float
    high: float

    @classmethod
    def parse(cls, value: Any) -> HoldTime:
        """Read a number, a `low-high` string, or a two-item list."""
        if isinstance(value, HoldTime):
            return value

        if isinstance(value, bool):
            # bool is an int subclass, and `hold_minutes: yes` is a mistake
            # rather than a one-minute hold.
            raise ConfigError(f"hold_minutes must be a number or a range, got {value!r}")

        if isinstance(value, (int, float)):
            return cls._checked(value, value, value)

        if isinstance(value, (list, tuple)):
            if len(value) != 2:
                raise ConfigError(
                    f"hold_minutes as a list must hold exactly two values, got {list(value)!r}"
                )
            return cls._checked(value[0], value[1], value)

        if isinstance(value, str):
            text = value.strip()
            # YAML reads `0.5-1` as a string, which is the spelling the
            # settings file documents, so it is the one that must work.
            low, sep, high = text.partition("-")
            if not sep:
                return cls._checked(text, text, value)
            return cls._checked(low, high, value)

        raise ConfigError(f"hold_minutes must be a number or a range, got {value!r}")

    @classmethod
    def _checked(cls, low: Any, high: Any, original: Any) -> HoldTime:
        try:
            lo, hi = float(str(low).strip()), float(str(high).strip())
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"hold_minutes must be a number or a range like 0.5-1, got {original!r}"
            ) from exc
        if lo < 0:
            raise ConfigError(f"hold_minutes must be >= 0, got {original!r}")
        if hi < lo:
            raise ConfigError(
                f"hold_minutes range runs backwards -- write the smaller number "
                f"first, got {original!r}"
            )
        return cls(lo, hi)

    def pick(self) -> float:
        """Minutes to hold for one cycle."""
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


@dataclass
class Config:
    # Named for the account that OPENS each leg, not for a coin: the symbols
    # are configurable, so `btc`/`sol` would be a lie the moment someone
    # trades something else.
    master_account: LegConfig
    sub_account: LegConfig
    hold_minutes: HoldTime = field(default_factory=lambda: HoldTime(5.0, 5.0))
    chase_interval_s: float = 1.0
    reconcile_interval_s: float = 5.0
    cycles: int = 1
    hedge_tolerance_lots: float = 1.0
    overlay_ttl_ms: int = 2000
    # Fallback when the configured sizes need more margin than the accounts
    # hold: every leg is scaled so the cycle fits inside this share of the
    # smaller account's available margin. Sizes that already fit are used
    # as written.
    max_margin_fraction: float = 0.25
    # Normally discovered from the master at startup; set only to pin one
    # specific sub-account when the master has several.
    sub1_pubkey: str = ""
    risk: RiskConfig = field(default_factory=RiskConfig)
    target: ExecutionTarget = field(default_factory=ExecutionTarget)
    state_file: str = "./app/state/strategy_state.json"
    log_level: str = "INFO"

    # Telegram reporting. Off unless both a token and at least one recipient
    # are set -- the bot runs unattended, so a halt that only reaches the
    # console reaches nobody.
    telegram: TelegramConfig = field(default_factory=TelegramConfig)

    # Referral gating, checked once at startup. See bulkdn/referral.py for what
    # a client-side gate does and does not actually prevent.
    access: AccessConfig = field(default_factory=AccessConfig)

    # The live WebSocket endpoints have been observed serving certificates that
    # fail verification. Retrying once without checks keeps the account stream
    # available; set `ws_ssl_auto_bypass: false` to fail hard instead.
    ws_insecure_ssl: bool = False
    ws_ssl_auto_bypass: bool = True

    # Optional endpoint overrides, for when the derived host is wrong.
    http_url_override: str = ""
    ws_url_override: str = ""

    # Populated at load time, not from the YAML file.
    private_key: str = ""

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
        """Legs keyed by symbol, which is how the rest of the bot looks them up."""
        return {
            self.master_account.symbol: self.master_account,
            self.sub_account.symbol: self.sub_account,
        }

    def __post_init__(self) -> None:
        # Callers that build a Config directly pass a plain number; the
        # YAML path passes a HoldTime. Normalise so the rest of the code
        # only ever sees the range.
        self.hold_minutes = HoldTime.parse(self.hold_minutes)

    def validate(self, require_credentials: bool = True, require_sub1: bool = True) -> None:
        """Validate the configuration.

        `require_sub1` is relaxed for `create-subaccount`, which exists to
        produce that pubkey rather than assume it already exists.
        """
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
        self.master_account.validate("master_account")
        self.sub_account.validate("sub_account")
        if self.master_account.symbol == self.sub_account.symbol:
            raise ConfigError("the two legs must use different symbols")
        if not 0.0 < self.max_margin_fraction <= 1.0:
            raise ConfigError(
                "max_margin_fraction must be between 0 and 1 "
                f"(0.25 = a quarter of available margin), got {self.max_margin_fraction}"
            )
        if self.chase_interval_s <= 0:
            raise ConfigError("chase_interval_s must be > 0")
        if self.reconcile_interval_s <= 0:
            raise ConfigError("reconcile_interval_s must be > 0")
        if self.cycles < 0:
            raise ConfigError("cycles must be >= 0 (0 means run forever)")
        # Below one lot the exchange cannot express the correction, so a
        # sub-lot tolerance would make the hedger spin on an uncorrectable
        # residual.
        if self.hedge_tolerance_lots < 1.0:
            raise ConfigError("hedge_tolerance_lots must be >= 1.0")
        self.risk.validate()


def _leg_from_dict(raw: dict[str, Any], name: str) -> LegConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"legs.{name} must be a mapping")
    size = float(raw.get("size") or 0.0)
    notional_usd = float(raw.get("notional_usd") or 0.0)
    cap_size = float(raw.get("max_order_size") or 0.0)
    cap_usd = float(raw.get("max_order_notional_usd") or 0.0)

    # An unset cap means "the whole leg", expressed in the unit the leg used.
    # Resolving it here keeps `size` and its cap in step when a dollar leg is
    # later converted.
    if cap_size <= 0 and cap_usd <= 0:
        cap_size, cap_usd = size, notional_usd

    try:
        return LegConfig(
            symbol=raw["symbol"],
            size=size,
            notional_usd=notional_usd,
            offset_bps=float(raw.get("offset_bps", 0.0)),
            max_distance_bps=float(raw.get("max_distance_bps", 5.0)),
            max_order_size=cap_size,
            max_order_notional_usd=cap_usd,
            leverage=(
                float(raw["leverage"]) if raw.get("leverage") is not None else None
            ),
        )
    except KeyError as exc:
        raise ConfigError(f"legs.{name} is missing required key {exc}") from exc


def _load_private_key(required: bool) -> str:
    """The signing key, from the environment or the key file.

    The environment wins so an unattended run needs no password prompt. When
    credentials are not required the key file is skipped entirely, which keeps
    read-only commands from asking for a password they will not use.
    """
    from_env = os.environ.get(PRIVATE_KEY_ENV, "")
    if from_env or not required:
        return from_env
    try:
        return keystore.load(PRIVATE_KEY_FILE)
    except keystore.KeystoreError as exc:
        raise ConfigError(str(exc)) from exc


def _telegram_from_dict(raw: Any) -> TelegramConfig:
    """Read Telegram settings, tolerating a single id given unwrapped."""
    if not isinstance(raw, dict):
        raise ConfigError("telegram must be a mapping")

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

    def as_list(value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [str(item) for item in value]
        raise ConfigError("access.codes and access.wallets must be strings or lists")

    access = AccessConfig(
        require_referral=bool(raw.get("require_referral", False)),
        codes=as_list(raw.get("codes", raw.get("code"))),
        wallets=as_list(raw.get("wallets", raw.get("wallet"))),
        owner_wallets=as_list(raw.get("owner_wallets", raw.get("owner_wallet"))),
        invite_codes=as_list(raw.get("invite_codes", raw.get("invite_code"))),
        allow_on_error=bool(raw.get("allow_on_error", False)),
    )
    try:
        access.validate()
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    return access


def load_config(
    path: str,
    require_credentials: bool = True,
    require_sub1: bool = True,
) -> Config:
    """Load, merge, and validate configuration."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc

    legs = raw.get("legs")
    if not isinstance(legs, dict):
        raise ConfigError("legs must be a mapping")
    if "btc" in legs or "sol" in legs:
        # Renamed rather than silently ignored: a config still using the old
        # keys would otherwise start with default legs and trade the wrong
        # sizes.
        raise ConfigError(
            "legs.btc and legs.sol were renamed to legs.master_account and "
            "legs.sub_account -- they name the account that opens each leg, "
            "not a coin. Rename the two keys; the fields inside are unchanged."
        )
    missing = [k for k in ("master_account", "sub_account") if k not in legs]
    if missing:
        raise ConfigError(f"config must define legs.{' and legs.'.join(missing)}")

    risk_raw = raw.get("risk") or {}
    if not isinstance(risk_raw, dict):
        raise ConfigError("risk must be a mapping")

    target_raw = raw.get("execution_target") or {}
    if not isinstance(target_raw, dict):
        raise ConfigError("execution_target must be a mapping")
    # `cycles` was a top-level key before execution targets existed; the
    # top-level spelling still works and the nested one wins.
    target = ExecutionTarget(
        cycles=int(target_raw.get("cycles", raw.get("cycles", 1))),
        burn_usd=float(target_raw.get("burn_usd", 0.0)),
        volume_usd=float(target_raw.get("volume_usd", 0.0)),
    )

    config = Config(
        sub1_pubkey=raw.get("sub1_pubkey", ""),
        master_account=_leg_from_dict(legs["master_account"], "master_account"),
        sub_account=_leg_from_dict(legs["sub_account"], "sub_account"),
        hold_minutes=HoldTime.parse(raw.get("hold_minutes", 5.0)),
        chase_interval_s=float(raw.get("chase_interval_s", 1.0)),
        reconcile_interval_s=float(raw.get("reconcile_interval_s", 5.0)),
        cycles=target.cycles,
        target=target,
        hedge_tolerance_lots=float(raw.get("hedge_tolerance_lots", 1.0)),
        overlay_ttl_ms=int(raw.get("overlay_ttl_ms", 2000)),
        max_margin_fraction=float(raw.get("max_margin_fraction", 0.25)),
        risk=RiskConfig(
            max_net_exposure_usd=float(risk_raw.get("max_net_exposure_usd", 500.0)),
            max_position_usd=float(risk_raw.get("max_position_usd", 5000.0)),
            max_reject_streak=int(risk_raw.get("max_reject_streak", 5)),
            max_hedge_impact_bps=float(risk_raw.get("max_hedge_impact_bps", 0.0)),
            ws_stale_timeout_s=float(risk_raw.get("ws_stale_timeout_s", 30.0)),
            price_stale_timeout_s=float(risk_raw.get("price_stale_timeout_s", 15.0)),
        ),
        state_file=raw.get("state_file", "./app/state/strategy_state.json"),
        log_level=raw.get("log_level", "INFO"),
        http_url_override=raw.get("http_url", ""),
        ws_url_override=raw.get("ws_url", ""),
        ws_insecure_ssl=bool(raw.get("ws_insecure_ssl", False)),
        ws_ssl_auto_bypass=bool(raw.get("ws_ssl_auto_bypass", True)),
        telegram=_telegram_from_dict(raw.get("telegram") or {}),
        access=_access_from_dict(raw.get("access") or {}),
        private_key=_load_private_key(require_credentials),
    )
    config.validate(require_credentials=require_credentials, require_sub1=require_sub1)
    return config

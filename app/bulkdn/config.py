"""Configuration loading and validation.

The master private key is deliberately never sourced from the config file --
only from the BULK_PRIVATE_KEY environment variable, or from a key file the
keystore reads -- so a config can be committed or shared without leaking
signing authority.
"""

from __future__ import annotations

import os
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

    `size` is the total base quantity the cycle accumulates. `offset_bps`
    places the resting order inside the touch; `max_distance_bps` is the drift
    that triggers a cancel+replace.
    """

    symbol: str
    size: float
    offset_bps: float
    max_distance_bps: float
    max_order_size: float
    # None leaves whatever the account already has. The exchange's own ceiling
    # for the market is checked at startup, since it differs per symbol.
    leverage: float | None = None

    def validate(self, name: str) -> None:
        if not self.symbol:
            raise ConfigError(f"legs.{name}.symbol is required")
        if self.size <= 0:
            raise ConfigError(f"legs.{name}.size must be > 0")
        if self.max_order_size <= 0:
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
    burn_usd: float = 0.0
    volume_usd: float = 0.0

    def validate(self) -> None:
        if self.cycles < 0:
            raise ConfigError("execution_target.cycles must be >= 0 (0 = unlimited)")
        if self.burn_usd < 0:
            raise ConfigError("execution_target.burn_usd must be >= 0")
        if self.volume_usd < 0:
            raise ConfigError("execution_target.volume_usd must be >= 0")

    @property
    def measures_fills(self) -> bool:
        """Whether anything here needs the fill history read."""
        return self.burn_usd > 0 or self.volume_usd > 0


@dataclass
class Config:
    # Named for the account that OPENS each leg, not for a coin: the symbols
    # are configurable, so `btc`/`sol` would be a lie the moment someone
    # trades something else.
    master_account: LegConfig
    sub_account: LegConfig
    hold_minutes: float = 5.0
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
        if self.hold_minutes < 0:
            raise ConfigError("hold_minutes must be >= 0")
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
    try:
        return LegConfig(
            symbol=raw["symbol"],
            size=float(raw["size"]),
            offset_bps=float(raw.get("offset_bps", 0.0)),
            max_distance_bps=float(raw.get("max_distance_bps", 5.0)),
            max_order_size=float(raw.get("max_order_size", raw["size"])),
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
        hold_minutes=float(raw.get("hold_minutes", 5.0)),
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

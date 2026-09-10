"""Configuration loading and validation.

The master private key is deliberately never sourced from the config file --
only from the BULK_PRIVATE_KEY environment variable -- so a config can be
committed or shared without leaking signing authority.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

PRIVATE_KEY_ENV = "BULK_PRIVATE_KEY"

# Fallback for when exporting an environment variable is inconvenient. Read
# relative to the current working directory, same as `config.yaml` itself.
# The file is git-ignored; see .gitignore. Lines starting with `#` and blank
# lines are skipped, so a file that only has its explanatory header comment is
# correctly treated as "no key set" rather than as a key containing a `#`.
PRIVATE_KEY_FILE = "private_key.local"


def _read_private_key_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    return stripped
    except FileNotFoundError:
        pass
    return ""

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


@dataclass
class RiskConfig:
    max_net_exposure_usd: float = 500.0
    max_position_usd: float = 5000.0
    max_reject_streak: int = 5
    ws_stale_timeout_s: float = 30.0
    price_stale_timeout_s: float = 15.0

    def validate(self) -> None:
        if self.max_net_exposure_usd <= 0:
            raise ConfigError("risk.max_net_exposure_usd must be > 0")
        if self.max_position_usd <= 0:
            raise ConfigError("risk.max_position_usd must be > 0")
        if self.max_reject_streak < 1:
            raise ConfigError("risk.max_reject_streak must be >= 1")


@dataclass
class Config:
    sub1_pubkey: str
    btc: LegConfig
    sol: LegConfig
    hold_minutes: float = 5.0
    chase_interval_s: float = 1.0
    reconcile_interval_s: float = 5.0
    cycles: int = 1
    hedge_tolerance_lots: float = 1.0
    overlay_ttl_ms: int = 2000
    risk: RiskConfig = field(default_factory=RiskConfig)
    state_file: str = "./state/strategy_state.json"
    log_level: str = "INFO"

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
        return {self.btc.symbol: self.btc, self.sol.symbol: self.sol}

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
                f"paste the master account's base58 key into {PRIVATE_KEY_FILE} "
                "(git-ignored, one line, no quotes)"
            )
        if require_sub1 and (
            not self.sub1_pubkey or self.sub1_pubkey.startswith("REPLACE")
        ):
            raise ConfigError("sub1_pubkey must be set to a real sub-account pubkey")
        self.btc.validate("btc")
        self.sol.validate("sol")
        if self.btc.symbol == self.sol.symbol:
            raise ConfigError("the two legs must use different symbols")
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
        )
    except KeyError as exc:
        raise ConfigError(f"legs.{name} is missing required key {exc}") from exc


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
    if not isinstance(legs, dict) or "btc" not in legs or "sol" not in legs:
        raise ConfigError("config must define legs.btc and legs.sol")

    risk_raw = raw.get("risk") or {}
    if not isinstance(risk_raw, dict):
        raise ConfigError("risk must be a mapping")

    config = Config(
        sub1_pubkey=raw.get("sub1_pubkey", ""),
        btc=_leg_from_dict(legs["btc"], "btc"),
        sol=_leg_from_dict(legs["sol"], "sol"),
        hold_minutes=float(raw.get("hold_minutes", 5.0)),
        chase_interval_s=float(raw.get("chase_interval_s", 1.0)),
        reconcile_interval_s=float(raw.get("reconcile_interval_s", 5.0)),
        cycles=int(raw.get("cycles", 1)),
        hedge_tolerance_lots=float(raw.get("hedge_tolerance_lots", 1.0)),
        overlay_ttl_ms=int(raw.get("overlay_ttl_ms", 2000)),
        risk=RiskConfig(
            max_net_exposure_usd=float(risk_raw.get("max_net_exposure_usd", 500.0)),
            max_position_usd=float(risk_raw.get("max_position_usd", 5000.0)),
            max_reject_streak=int(risk_raw.get("max_reject_streak", 5)),
            ws_stale_timeout_s=float(risk_raw.get("ws_stale_timeout_s", 30.0)),
            price_stale_timeout_s=float(risk_raw.get("price_stale_timeout_s", 15.0)),
        ),
        state_file=raw.get("state_file", "./state/strategy_state.json"),
        log_level=raw.get("log_level", "INFO"),
        http_url_override=raw.get("http_url", ""),
        ws_url_override=raw.get("ws_url", ""),
        ws_insecure_ssl=bool(raw.get("ws_insecure_ssl", False)),
        ws_ssl_auto_bypass=bool(raw.get("ws_ssl_auto_bypass", True)),
        private_key=(
            os.environ.get(PRIVATE_KEY_ENV, "")
            or _read_private_key_file(PRIVATE_KEY_FILE)
        ),
    )
    config.validate(require_credentials=require_credentials, require_sub1=require_sub1)
    return config

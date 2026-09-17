"""Referral gating: run only for accounts that came in through a given owner.

BULK's indexer reports, for any wallet, how it arrived -- by two separate
routes that must both be checked:

    GET https://indexer.bulk.trade/v1/aura/wallet/<pubkey>
    -> {
         "referred_by_code":   "EXAMPLE",      # shareable referral code
         "referred_by_wallet": "3JcDtH4L...",
         "access": {
           "invited_by_code_id": "...",        # single-use invite code
           "invited_by_wallet":  "3JcDtH4L...",
         },
       }

Accounts arrive by either, and in practice most arrive by invite -- on the
wallet this was built for, roughly two thirds did. A gate that only read
`referred_by_*` would therefore refuse most of the people it was meant to
admit.

Matching is on the *wallet* rather than the code wherever possible. Invite
codes are single-use and reissued from a weekly allowance, so any list of them
is stale within the week; the wallet that issued them is stable.

Observed responses, which the checks below are built around:

    valid wallet, referred        200, referred_by_* populated
    valid wallet, invited         200, access.invited_by_* populated
    valid wallet, neither         200, both are null
    malformed pubkey              400

**What this does and does not buy.** The check runs inside the bot, on the
user's machine, in source they can read. Anyone willing to delete four lines is
past it. It stops the bot being passed around casually; it is not enforcement,
and it should not be relied on as if it were. Real enforcement needs the
authority to be somewhere the user does not control -- a server that issues the
orders, or a key they never hold.

**It gates startup only, never a running cycle.** A gate that tripped mid-run
would abandon open positions on both accounts and leave the pair directional,
which is a far worse outcome than an unlicensed run. So the decision is made
once, before anything is placed.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

import requests

from .retry import TRANSIENT_EXCEPTIONS, describe, retry

log = logging.getLogger(__name__)

INDEXER_URL = "https://indexer.bulk.trade/v1/aura/wallet"


@dataclass
class AccessConfig:
    """Who is allowed to run the bot.

    Matching on `wallets` is the stronger of the two: a code is a string the
    referrer chose and could be reassigned, while the referrer wallet is an
    identity. Codes are supported because they are what people actually know.
    """

    require_referral: bool = False
    codes: list[str] = field(default_factory=list)
    # Matched against both `referred_by_wallet` and `access.invited_by_wallet`:
    # one address covers everyone who arrived by either route, and keeps
    # covering them as invite codes are consumed and reissued.
    wallets: list[str] = field(default_factory=list)
    # The gate only ever blocks startup, so refusing on an indexer outage
    # cannot strand open positions -- which is why it defaults to closed.
    allow_on_error: bool = False

    @property
    def enabled(self) -> bool:
        """Whether THIS CONFIG asks for gating. Not whether gating happens.

        A sealed build gates regardless of what the config says, so nothing
        outside `check_access` should branch on this -- guarding the call with
        it is the same bypass as emptying the allow-list.
        """
        return self.require_referral

    def validate(self) -> None:
        # Skipped when the build is sealed: the owner is compiled in, so an
        # empty config block is the normal case rather than a mistake that
        # would refuse everyone.
        if is_sealed():
            return
        if self.require_referral and not (self.codes or self.wallets):
            raise ValueError(
                "access.require_referral is on but no codes or wallets are "
                "listed -- that would refuse every account, including yours"
            )


# -- the sealed part of the gate --------------------------------------------
#
# The whole of it: who the owner is. Every wallet is judged against these two
# values by asking the indexer, at startup, every time. There is no list of
# admitted accounts and no account that skips the question -- a wallet the
# indexer says came through the owner runs, and one it does not say that about
# does not, including the owner's own referral wallet, which nobody referred.
#
# That is a deliberate trade. A named list would keep working while the indexer
# is down; this refuses everyone until it answers again. What it buys is that
# nothing has to be released when someone new signs up, and nothing in the build
# can admit an account the exchange does not agree came through the owner.
#
# Two changes from reading these out of settings.yaml.
#
# They are sha256 digests, not addresses, so the source does not hand a reader
# the value to substitute. And they live in code, so deleting a line in the
# config cannot widen who may run.
#
# When SEALED_WALLETS is non-empty the build is sealed and the whole `access`
# block in settings.yaml is ignored -- every field of it. Each one is a bypass
# otherwise: `wallets` and `codes` add allowed referrers,
# `require_referral: false` switches the gate off, and `allow_on_error: true`
# turns a pulled network cable into a pass. A gate whose own config file can
# disable it is a gate with a documented bypass.
#
# WHAT THIS IS NOT: protection against someone editing the source. It is
# Python; the check is one `return True` away from gone, whatever it is hashed
# with, and freezing it into an executable moves that edit without preventing
# it. This raises the cost from "change a line in a config file" to "read and
# patch the program", which is the whole of what a client-side gate can do.
# See docs/DESIGN.md.
SEALED_WALLETS: tuple[str, ...] = (
    "e87f0b26a48973d7ec3318929d8516debfa7d92dc2ca6d4557d7368f37c7041c",
)
SEALED_CODES: tuple[str, ...] = (
    "8682907b1f0aef20dbac2739e6a985d3152241b15268965d72cda4554864dacb",
)

# Wallets the owner admits by name, alongside the referral question rather than
# through it. For the account that signed up under someone else's link, or the
# owner's own wallet, which nobody referred and which the rule above therefore
# refuses.
#
# The alternative was adding that account's referrer to SEALED_WALLETS, and
# that list is of REFERRERS: adding one admits everyone they have referred and
# everyone they refer later, none of whom the owner ever sees. This list is of
# operators, and admits exactly who is on it.
#
# It lives here and not in settings.yaml for the reason the block above gives:
# the config ships with the archive and is the operator's to edit, so a name
# they can add to is not a gate. Adding one costs a release, which is the
# point -- it is the same bar as changing who the owner is.
#
# Checked before the indexer, so a listed wallet runs while the indexer is
# down. That is deliberate and is the one privilege being on this list carries:
# the owner has already answered, in the build, the question the indexer would
# be asked.
SEALED_OPERATORS: tuple[str, ...] = (
    # Added 2026-09-17: signed up under another referrer, admitted by name.
    # The address is not written here, only its digest -- for the reason the
    # block above gives, and because a test enforces it.
    "9803cdf7e77c45ac14b26c5c97b5d4f209b48b4e98ca79b9912cbb0c6c8076db",
)


def wallet_digest(value: str) -> str:
    """How a wallet is compared. Addresses are case-sensitive base58."""
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def code_digest(value: str) -> str:
    """How a code is compared. People type these by hand, so case is folded."""
    return hashlib.sha256(value.strip().upper().encode("utf-8")).hexdigest()


def is_sealed() -> bool:
    """Whether this build carries its own owner identity."""
    return bool(SEALED_WALLETS or SEALED_CODES or SEALED_OPERATORS)


@dataclass(frozen=True)
class _Allowed:
    """Who the gate accepts as a referrer, as digests."""

    wallets: frozenset[str]
    codes: frozenset[str]
    # Admitted by name rather than by who referred them.
    operators: frozenset[str]
    allow_on_error: bool
    enabled: bool


def _allowed(config: AccessConfig) -> _Allowed:
    """The sealed values, or the config's when the build is not sealed.

    A sealed build ignores the config rather than merging with it. Merging
    would let the config add an allowed referrer, and there is no useful
    difference between adding one and replacing the list.
    """
    if is_sealed():
        return _Allowed(
            wallets=frozenset(SEALED_WALLETS),
            codes=frozenset(SEALED_CODES),
            operators=frozenset(SEALED_OPERATORS),
            allow_on_error=False,
            enabled=True,
        )
    # `operators` is not read from the config in either branch. Every other
    # field here has a config source because an unsealed build is configured
    # entirely; this one would be a line anybody could add to admit themselves.
    return _Allowed(
        wallets=frozenset(wallet_digest(w) for w in config.wallets if w.strip()),
        codes=frozenset(code_digest(c) for c in config.codes if c.strip()),
        operators=frozenset(SEALED_OPERATORS),
        allow_on_error=config.allow_on_error,
        enabled=config.require_referral,
    )


@dataclass(frozen=True)
class ReferralStatus:
    """How a wallet came to be on BULK.

    Two independent routes, which the API keeps separate and so does this:

    **Referral code** -- `referred_by_*`, the shareable code someone
    typed when signing up.

    **Invite code** -- `access.invited_by_*`, a single-use code from a limited
    weekly allowance.

    A wallet can arrive by either, so both are read. Match on the inviter's
    *wallet*, not the code. Two reasons, and the second is the one that bites:
    codes are consumed and reissued weekly, and `invited_by_code_id` is NOT the
    shareable code the owner holds. The owner sees `BULK-EDA-QQF`; the API
    returns an internal id -- `INV-3-031806` on the live records checked. A
    list of the codes an owner actually has can therefore never match.
    """

    wallet: str
    referred_by_code: str | None
    referred_by_wallet: str | None
    invited_by_code: str | None
    invited_by_wallet: str | None
    own_code: str | None
    qualified: bool | None

    @classmethod
    def from_api(cls, wallet: str, data: dict) -> ReferralStatus:
        access = data.get("access")
        if not isinstance(access, dict):
            access = {}
        return cls(
            wallet=wallet,
            referred_by_code=data.get("referred_by_code"),
            referred_by_wallet=data.get("referred_by_wallet"),
            # Named `code_id` upstream and of unspecified form, so it is
            # compared as an opaque string rather than parsed.
            invited_by_code=_as_str(access.get("invited_by_code_id")),
            invited_by_wallet=access.get("invited_by_wallet"),
            own_code=data.get("referral_code"),
            qualified=data.get("referred_qualified"),
        )

    @property
    def has_referrer(self) -> bool:
        return bool(self.referred_by_code or self.referred_by_wallet)

    @property
    def has_inviter(self) -> bool:
        return bool(self.invited_by_code or self.invited_by_wallet)

    @property
    def has_origin(self) -> bool:
        """Whether the wallet arrived by either route."""
        return self.has_referrer or self.has_inviter

    def describe_origin(self) -> str:
        parts = []
        if self.referred_by_code:
            parts.append(f"referral code {self.referred_by_code}")
        if self.referred_by_wallet:
            parts.append(f"referrer {self.referred_by_wallet}")
        if self.invited_by_code:
            parts.append(f"invite code {self.invited_by_code}")
        if self.invited_by_wallet:
            parts.append(f"inviter {self.invited_by_wallet}")
        return ", ".join(parts) or "no referral or invite"


def _as_str(value) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str
    status: ReferralStatus | None = None


class IndexerUnavailable(Exception):
    """The indexer could not be reached or returned something unusable."""


class AccessDenied(Exception):
    """The account is not allowed to run this bot."""


@retry("referral indexer", attempts=3, delay=2.0, exceptions=TRANSIENT_EXCEPTIONS)
def fetch_referral(wallet: str, *, timeout: int = 20, base_url: str = INDEXER_URL) -> ReferralStatus:
    """Read a wallet's referral record. Raises `IndexerUnavailable` on failure.

    Transient faults are retried; a 4xx is not, because a malformed or unknown
    pubkey will be malformed next time too.
    """
    response = requests.get(f"{base_url}/{wallet}", timeout=timeout)

    if response.status_code == 400:
        raise IndexerUnavailable(f"indexer rejected {wallet} as a wallet address")
    if response.status_code != 200:
        raise IndexerUnavailable(f"indexer returned HTTP {response.status_code}")

    try:
        data = response.json()
    except ValueError as exc:
        raise IndexerUnavailable(f"indexer returned non-JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise IndexerUnavailable("indexer returned an unexpected payload shape")

    return ReferralStatus.from_api(wallet, data)


def check_access(
    wallet: str,
    config: AccessConfig,
    *,
    base_url: str = INDEXER_URL,
) -> AccessDecision:
    """Decide whether `wallet` may run the bot.

    Comparison is case-insensitive for codes (people type them by hand) and
    exact for wallets (base58 is case-sensitive and a near-match is a different
    key, not a typo to be forgiven).
    """
    allowed = _allowed(config)
    if not allowed.enabled:
        return AccessDecision(True, "referral gating is off")

    # Named in the build by the owner. The only route that does not ask the
    # indexer, because the answer it would give has already been overruled on
    # purpose -- these are the accounts admitted despite what it says.
    if wallet_digest(wallet) in allowed.operators:
        log.info("%s is named directly in this build's operator list", wallet)
        return AccessDecision(True, f"{wallet} is admitted by this build directly")

    # No other wallet is admitted before this point. Every account, the owner's
    # included, is judged on what the indexer says about it right now.
    try:
        status = fetch_referral(wallet, base_url=base_url)
    except Exception as exc:  # noqa: BLE001 - every failure mode lands here
        if allowed.allow_on_error:
            log.warning(
                "could not verify referral for %s (%s) -- allowing, because "
                "access.allow_on_error is set",
                wallet, describe(exc),
            )
            return AccessDecision(True, f"indexer unavailable, allowed by config: {exc}")
        return AccessDecision(False, f"could not verify referral: {exc}")

    if not status.has_origin:
        return AccessDecision(
            False,
            f"{wallet} did not sign up under a referral or invite code",
            status,
        )

    # Wallets are matched against both routes: one address, whether it referred
    # the account or invited it.
    if status.referred_by_wallet and wallet_digest(status.referred_by_wallet) in allowed.wallets:
        return AccessDecision(
            True, f"referred by wallet {status.referred_by_wallet}", status
        )
    if status.invited_by_wallet and wallet_digest(status.invited_by_wallet) in allowed.wallets:
        return AccessDecision(
            True, f"invited by wallet {status.invited_by_wallet}", status
        )

    if status.referred_by_code and code_digest(status.referred_by_code) in allowed.codes:
        return AccessDecision(
            True, f"referred by code {status.referred_by_code}", status
        )

    # No route matched. `invited_by_code` is deliberately not one of them: the
    # indexer reports an internal id (`INV-4-004748`), not the shareable code an
    # owner holds, so there is nothing an owner could ever put on that list.
    # The inviter's wallet is matched above instead, which is stable.
    return AccessDecision(
        False,
        f"{wallet} arrived via {status.describe_origin()}, "
        "which is not the owner of this build",
        status,
    )

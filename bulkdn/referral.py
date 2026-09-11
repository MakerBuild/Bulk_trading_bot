"""Referral gating: run only for accounts that came in through a given owner.

BULK's indexer reports, for any wallet, how it arrived -- by two separate
routes that must both be checked:

    GET https://indexer.bulk.trade/v1/aura/wallet/<pubkey>
    -> {
         "referred_by_code":   "MAKER",        # shareable referral code
         "referred_by_wallet": "2xW5fXY7...",
         "access": {
           "invited_by_code_id": "...",        # single-use invite code
           "invited_by_wallet":  "2xW5fXY7...",
         },
       }

Accounts arrive by either, and in practice most arrive by invite: for the
owner's own wallet the split was 15 referrals against 32 redeemed invites. A
gate that only read `referred_by_*` would therefore refuse most of the people
it was meant to admit.

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

import logging
from dataclasses import dataclass, field

import requests

from .retry import TRANSIENT_EXCEPTIONS, retry

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
    # Individual invite codes. Supported, but they are single-use and reissued
    # weekly, so a list of them goes stale -- prefer `wallets`.
    invite_codes: list[str] = field(default_factory=list)
    # Accounts that may run regardless of who referred them.
    #
    # Without this the gate locks out its own author: the check asks "who
    # referred you", and a referrer was not referred by themselves, so the
    # owner's own wallets come back with a null referrer and are refused.
    # Checked before the indexer is contacted, so the owner can also run while
    # it is down.
    owner_wallets: list[str] = field(default_factory=list)
    # The gate only ever blocks startup, so refusing on an indexer outage
    # cannot strand open positions -- which is why it defaults to closed.
    allow_on_error: bool = False

    @property
    def enabled(self) -> bool:
        return self.require_referral

    def validate(self) -> None:
        if self.require_referral and not (
            self.codes or self.wallets or self.owner_wallets or self.invite_codes
        ):
            raise ValueError(
                "access.require_referral is on but no codes, wallets, invite_codes, "
                "or owner_wallets are listed -- that would refuse every account, "
                "including yours"
            )


@dataclass(frozen=True)
class ReferralStatus:
    """How a wallet came to be on BULK.

    Two independent routes, which the API keeps separate and so does this:

    **Referral code** -- `referred_by_*`, the shareable code (`MAKER`) someone
    typed when signing up.

    **Invite code** -- `access.invited_by_*`, a single-use code
    (`BULK-EDA-QQF`) issued from a limited weekly allowance.

    A wallet can arrive by either, so both are read. Matching on the inviter's
    *wallet* rather than the code is what keeps this maintainable: codes are
    consumed and reissued every week, while the wallet that issued them does
    not change.
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
    if not config.enabled:
        return AccessDecision(True, "referral gating is off")

    # Before the network call: the owner should not be locked out by their own
    # gate, nor by an indexer outage.
    if wallet in set(config.owner_wallets):
        return AccessDecision(True, "owner wallet")

    try:
        status = fetch_referral(wallet, base_url=base_url)
    except Exception as exc:  # noqa: BLE001 - every failure mode lands here
        if config.allow_on_error:
            log.warning(
                "could not verify referral for %s (%s) -- allowing, because "
                "access.allow_on_error is set",
                wallet, exc,
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
    owner_wallets = set(config.wallets)
    if status.referred_by_wallet and status.referred_by_wallet in owner_wallets:
        return AccessDecision(
            True, f"referred by wallet {status.referred_by_wallet}", status
        )
    if status.invited_by_wallet and status.invited_by_wallet in owner_wallets:
        return AccessDecision(
            True, f"invited by wallet {status.invited_by_wallet}", status
        )

    wanted_codes = {code.strip().upper() for code in config.codes if code.strip()}
    if status.referred_by_code and status.referred_by_code.strip().upper() in wanted_codes:
        return AccessDecision(
            True, f"referred by code {status.referred_by_code}", status
        )

    # Listing individual invite codes is supported but is the fragile option:
    # they are single-use and reissued weekly, so the list goes stale. Prefer
    # the inviter wallet above.
    wanted_invites = {code.strip().upper() for code in config.invite_codes if code.strip()}
    if status.invited_by_code and status.invited_by_code.strip().upper() in wanted_invites:
        return AccessDecision(
            True, f"invited by code {status.invited_by_code}", status
        )

    return AccessDecision(
        False,
        f"{wallet} arrived via {status.describe_origin()}, "
        "which is not on the allowed list",
        status,
    )

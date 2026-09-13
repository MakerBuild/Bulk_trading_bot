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
    # Individual invite codes, compared against `access.invited_by_code_id`.
    #
    # Almost certainly not what you want. That field holds an internal id
    # (`INV-3-031806` on the records checked), not the `BULK-XXX-XXX` code an
    # owner can see and share, so listing the codes you have will silently
    # match nothing. Use `wallets`, which matches the inviter.
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
        """Whether THIS CONFIG asks for gating. Not whether gating happens.

        A sealed build gates regardless of what the config says, so nothing
        outside `check_access` should branch on this -- guarding the call with
        it is the same bypass as emptying the allow-list.
        """
        return self.require_referral

    def validate(self) -> None:
        # Skipped when the build is sealed: the allow-list is compiled in, so
        # an empty config block is the normal case rather than a mistake that
        # would refuse everyone.
        if is_sealed():
            return
        if self.require_referral and not (
            self.codes or self.wallets or self.owner_wallets or self.invite_codes
        ):
            raise ValueError(
                "access.require_referral is on but no codes, wallets, invite_codes, "
                "or owner_wallets are listed -- that would refuse every account, "
                "including yours"
            )


# -- the sealed part of the gate --------------------------------------------
#
# Two changes from reading these out of settings.yaml.
#
# They are sha256 digests, not addresses, so the source does not hand a reader
# the value to substitute. And they live in code, so deleting a line in the
# config cannot widen who may run.
#
# When SEALED_WALLETS is non-empty the build is sealed and the whole `access`
# block in settings.yaml is ignored -- every field of it. Each one is a bypass
# otherwise: `wallets` and `codes` add allowed referrers, `owner_wallets` is an
# unconditional pass, `require_referral: false` switches the gate off, and
# `allow_on_error: true` turns a pulled network cable into a pass. A gate whose
# own config file can disable it is a gate with a documented bypass.
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
SEALED_INVITE_CODES: tuple[str, ...] = ()
# Accounts that run regardless of who referred them -- the owner's own trading
# account. Deliberately empty in the published build: an address here is public
# the moment the source is, and it is an unconditional pass, so it cannot be
# read from the config instead. To fill it in the build you run yourself:
#   python -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" <address>
SEALED_OWNER_WALLETS: tuple[str, ...] = (
    # The owner's own trading account. It is not its own referrer -- the
    # indexer reports it as having arrived by no referral at all -- so
    # without this entry the gate refuses the person who built it.
    "3fd0f416f9ebfe2f03c0d8a0d1b77e96ae71b6fef87e330e3ac2372ef2d5074f",
    # The referral wallet itself. It is the referrer, so the indexer reports it
    # as having been referred by nobody -- the same shape as any origin
    # account. It was in the config's owner_wallets before sealing; leaving it
    # out here locked the owner out of their own second wallet.
    "e87f0b26a48973d7ec3318929d8516debfa7d92dc2ca6d4557d7368f37c7041c",
    # A third account of the owner's. Like the trading one, the indexer reports
    # it as `source: redeemed, inviter_kind: admin` -- admitted by BULK rather
    # than by a wallet -- so there is nothing public for the gate to match on.
    "0bdac4b3d7aec10a53660957141834076f528e9d1a40b8a933299981455b6b6c",
)


# The owner's own referral list, admitted explicitly.
#
# Why a list and not a check: BULK's public index does not carry the referral
# of an account created on mainnet -- every attribution field comes back null
# even for a wallet the exchange's own site shows as referred (see
# docs/DESIGN.md). So the gate cannot ask "did this wallet come through me"
# and get a truthful answer, and these are named instead.
#
# Digests, not addresses, for the same reason as the rest of this file: the
# source ships, and a reader should not be handed the list. That hides it from
# reading, not from a search -- the set of BULK accounts is enumerable, so
# someone willing to hash all of them can match these. It is a list of who may
# run a trading bot, not a secret.
#
# The live check now admits exactly these same people -- the indexer reports
# `referred_by_wallet` for mainnet accounts, which it did not when this list was
# written. So the list has become the floor under that check rather than a
# substitute for it: it is consulted before the network call, and a sealed build
# refuses on an indexer outage, so without it an outage locks everyone out.
# To add one:
#   python -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" <address>
SEALED_REFERRAL_WALLETS: tuple[str, ...] = (
    "bccf04aa515043b6a3e1034630d9a0a46ccb7c3dc02fef20b52ae41a184f8c9a",
    "5467bf9c6c5308dabfd19878ccf346350c17f48560b604bd6fd5c22bfa0c3cb1",
    "da91c6eb9d3a153bc859cc22ff0bc83dc44e32fa9363647b4f17fd97ca87ca9b",
    "2b7fd137e9fc33c32e6d14d5537d665df8ad862705e15e8100d22b28dbc0df8d",
    "0bdac4b3d7aec10a53660957141834076f528e9d1a40b8a933299981455b6b6c",
    "9d3800163927b399fbb7b21ba9000f0d72e4aefae07d13de066000def25c038f",
    "22c3ac7ce01512a229216b6cb01ae18645f1173232d320331b5c42d488acc368",
    "f0bf4626285afa0681645efae55878a6edc3203c6695889a6f7c67c6c1243a27",
    "bbb68df0b18a9ceecc35ee743013ae12738dea42e89b84f8fee7d5fb7cdfadcc",
    "fedd548cbad6bedc956dc4e83752c240919ba7d62c00dc06f1e9eb18be5818c4",
    "df5010ff922ea9dd3a56ef2523f81461b6ecff52d51292417a3c1f7699ebb6a3",
    "1960947137c6a6f5713af5182cf4a0244a83348b8eb47c9345ab4d37fe4715b8",
    "0cd228ab98c8d8b76459a53ddaab0dab182d669f65c3606528bceda7e4a3320d",
    "7102e11a9d175df44db0226e1a4a6d88ab907a8dcccb44d195872b29c47728b0",
    "98b4e5f2d6ba1a0cab3be03a11848719d88bd54863a2828f726f1d1ff5e84c92",
    "71ca90f773cef279b7b2d12cffd414652726e3039db24da6fd7fe349dcd74752",
    "7dddb1bb09ea0dfbb636d8b253c809092b49b5b05ed86824bd33e1996333156a",
    "797aa5164d72aa17475c8d5f0795067fdd088314d4b66964b9b80943f3a2a5ff",
    "4e1b063d130616418529944f3b625ffbebec4fb2d677fd3b0475f295cd5daa06",
    "a9c52c4b5fd0c8ec435f6c2ffdb8302068be3ecffa2087fdf26e96b0db63946c",
    "aa0b78ddc7a2289c1be5aaf001ebc8f77b652bfff3af40f312099cdc5e18cad9",
    "e964d747da8c35c1e501fc4839da75a7fc2bcc0da1cf81eae5c9571b692ca1b0",
    "0af31d1d7338d0eeb7b59bf97c7c049f4f4fb54af56ce49c020493f27b000464",
    "acae1ce8b23f08b653f9b5a2aaef04437e63d07d543b98ba0715febd9d737b9d",
    "3fd0f416f9ebfe2f03c0d8a0d1b77e96ae71b6fef87e330e3ac2372ef2d5074f",
    "8f8387e0b02624200483985be847a1dcf86dceaba878446e680235da4bce7ef5",
    "845cc326b3178068d1496125b3ac707236ae38d24e18aee98ed84e0af8506a1b",
    "ad3bb6fd925e15772320aeae4e8337516389f32045ca116b9f4c595b96b09df8",
    "ea050b2cfcc87c7883312a8dcf060afa0284b4c264db47a08beefc1cc4f4293b",
    "4ffb81375344ad7304e9775b0a6a47485fc478fdab45e326642329bfc565671b",
    "a4caf1718253c908713e9c79367c458c4064ecb5adf55a22e132feea620f2242",
    "923048d05f1d4d33289d9d0437744d5bfd4df1d264f560dcf92ebdf31818fe47",
    "523f45334bf043bda19efd7c17c23cac5aecbbb9b2a4a03f8939fb8a35f665ca",
    "581e31267804b4fec253a6bbffd9cf844820ea3f5b9da537f63631cb7897b064",
    "87e7f300501382ac231766ac1a23015898e75c2484e8cbd2e47d59e264942490",
    "ac1eee7990aef4386e488ab9f296d6eb0d3d48f626bae1474a6e06bfa47eb66a",
    "df32139e2b8adbddf25138456d4c810b07aefb881b6c95808f082c831df4cbac",
    "a0305722b8cbe1bb9f5761f22337a23a3f3838492871920ceb5e7a566ef2d527",
    "1e6727ec8e624c10fe5a1ee1281e0be8b1b3bf7b9b693bd14dd8895bba35d1ba",
    "6cf5b0911dce96a06e540bab4bc6b97d1cd4162907427d7e13c86243ce027668",
    "2268d6b7300a4fdf491d7b16b987450d335c895a7ab8802cab474e1191d1f86f",
    "542757bf04ec78eb8409deb6aab0124b98074f9752c352dbec6dc4e137839dff",
    "a1761624214ce4250106ac66fff9082713713090dfde7517e08453903a5b6781",
    "0fd50d46581b9881de3cc5dc0c013277faa766aa6583e7466586d8459eea611d",
    "1469c3bcafc05e9ab186713ce6e4baa42b4b23715c2760fdd074a579afb0093a",
    "b26746fbd65ac5d689ee4628b9639c9d76367050d22e91dea4e8a88c071214b4",
)


def wallet_digest(value: str) -> str:
    """How a wallet is compared. Addresses are case-sensitive base58."""
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def code_digest(value: str) -> str:
    """How a code is compared. People type these by hand, so case is folded."""
    return hashlib.sha256(value.strip().upper().encode("utf-8")).hexdigest()


def is_sealed() -> bool:
    """Whether this build carries its own allow-list."""
    return bool(
        SEALED_WALLETS
        or SEALED_CODES
        or SEALED_INVITE_CODES
        or SEALED_REFERRAL_WALLETS
    )


@dataclass(frozen=True)
class _Allowed:
    """What the gate matches against, as digests."""

    wallets: frozenset[str]
    codes: frozenset[str]
    invite_codes: frozenset[str]
    owner_wallets: frozenset[str]
    # Named referrals. Admitted the same way as an owner wallet, kept apart so
    # the log says which of the two let someone in.
    referral_wallets: frozenset[str]
    allow_on_error: bool
    enabled: bool


def _allowed(config: AccessConfig) -> _Allowed:
    """The sealed values, or the config's when the build is not sealed.

    A sealed build ignores the config rather than merging with it. Merging
    would let the config add an allowed wallet, and there is no useful
    difference between adding one and replacing the list.
    """
    if is_sealed():
        return _Allowed(
            wallets=frozenset(SEALED_WALLETS),
            codes=frozenset(SEALED_CODES),
            invite_codes=frozenset(SEALED_INVITE_CODES),
            owner_wallets=frozenset(SEALED_OWNER_WALLETS),
            # Admitted without asking the indexer, so an outage cannot lock out
            # the people this build was made for.
            referral_wallets=frozenset(SEALED_REFERRAL_WALLETS),
            allow_on_error=False,
            enabled=True,
        )
    return _Allowed(
        wallets=frozenset(wallet_digest(w) for w in config.wallets if w.strip()),
        codes=frozenset(code_digest(c) for c in config.codes if c.strip()),
        invite_codes=frozenset(code_digest(c) for c in config.invite_codes if c.strip()),
        owner_wallets=frozenset(wallet_digest(w) for w in config.owner_wallets if w.strip()),
        # No config equivalent: a named list only exists in a sealed build.
        referral_wallets=frozenset(),
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

    # Before the network call: neither the owner nor a named referral should be
    # locked out by an indexer that has no record of them, or by an outage.
    # Reported apart so a support question can be answered from the log.
    digest = wallet_digest(wallet)
    if digest in allowed.referral_wallets:
        return AccessDecision(True, "on the build's referral list")
    if digest in allowed.owner_wallets:
        return AccessDecision(True, "owner wallet")

    try:
        status = fetch_referral(wallet, base_url=base_url)
    except Exception as exc:  # noqa: BLE001 - every failure mode lands here
        if allowed.allow_on_error:
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

    # Listing individual invite codes is supported but is the fragile option:
    # they are single-use and reissued weekly, so the list goes stale. Prefer
    # the inviter wallet above.
    if status.invited_by_code and code_digest(status.invited_by_code) in allowed.invite_codes:
        return AccessDecision(
            True, f"invited by code {status.invited_by_code}", status
        )

    return AccessDecision(
        False,
        f"{wallet} arrived via {status.describe_origin()}, "
        "which is not on the allowed list",
        status,
    )

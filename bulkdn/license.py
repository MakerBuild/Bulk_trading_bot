"""Per-account licence, signed by the author.

BULK exposes no referral data: there is nothing in the OpenAPI spec or the SDK
that says which code an account signed up under, and the documentation puts
referral activity on a web page in the referrer's own dashboard. So the bot
cannot ask the exchange whether its operator is a referral -- the answer has to
come from whoever can see that page, which is the author.

The shape that follows from it: the operator sends their master pubkey, the
author signs a licence naming it, and the bot verifies that signature against a
public key compiled in below. Ed25519 via PyNaCl, already present.

    {"account": "<master pubkey>", "code": "MAKER",
     "issued": <unix>, "expires": <unix>, "signature": "<base58>"}

The signature covers the payload serialised with sorted keys and no spaces, so
signer and verifier agree on the bytes without a schema.

**This is a lock on the front door of a house with the walls printed on the
key.** The bot ships as Python source, so anyone can delete the call to
`enforce`. It stops sharing, not cracking. Making it stand up to someone who
edits the source means shipping a binary, or moving something the bot cannot
run without onto a server the author controls.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import base58
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

LICENSE_FILE = "license.json"

# The author's licence-signing public key, base58. Replace with your own --
# `bulkdn license-keygen` prints the pair and tells you where to put it.
#
# This is deliberately not a trading key: it signs licences and nothing else,
# so it never needs to touch an account that holds funds.
AUTHOR_VERIFY_KEY = ""

REQUIRED_CODE = "MAKER"

# Fields covered by the signature. `signature` is excluded for obvious reasons.
SIGNED_FIELDS = ("account", "code", "issued", "expires")


class LicenseError(Exception):
    """Raised when a licence is missing, malformed, expired, or not ours."""


@dataclass(frozen=True)
class License:
    account: str
    code: str
    issued: int
    expires: int

    def payload(self) -> dict:
        return {
            "account": self.account,
            "code": self.code,
            "issued": self.issued,
            "expires": self.expires,
        }

    @property
    def expired(self) -> bool:
        return self.expires != 0 and time.time() > self.expires

    def remaining_days(self) -> float | None:
        """Days left, or None when the licence does not expire."""
        if self.expires == 0:
            return None
        return max(0.0, (self.expires - time.time()) / 86400)


def signing_bytes(payload: dict) -> bytes:
    """Canonical bytes for signing and verifying.

    Sorted keys and no whitespace, so two implementations agree without
    sharing a serialiser.
    """
    return json.dumps(
        {k: payload[k] for k in SIGNED_FIELDS}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


# -- author side ------------------------------------------------------------


def keygen() -> tuple[str, str]:
    """A fresh licence-signing pair as (secret base58, public base58)."""
    secret = SigningKey.generate()
    return (
        base58.b58encode(bytes(secret)).decode(),
        base58.b58encode(bytes(secret.verify_key)).decode(),
    )


def issue(
    *,
    account: str,
    signing_key: str,
    days: int = 30,
    code: str = REQUIRED_CODE,
    now: int | None = None,
) -> dict:
    """Sign a licence for one account. `days=0` means it never expires."""
    if not account:
        raise LicenseError("an account pubkey is required")
    try:
        if len(base58.b58decode(account)) != 32:
            raise ValueError
    except Exception as exc:
        raise LicenseError(f"{account!r} is not a base58 pubkey") from exc

    issued = int(now if now is not None else time.time())
    payload = {
        "account": account,
        "code": code,
        "issued": issued,
        "expires": 0 if days == 0 else issued + days * 86400,
    }
    secret = SigningKey(base58.b58decode(signing_key))
    signed = secret.sign(signing_bytes(payload))
    return {**payload, "signature": base58.b58encode(signed.signature).decode()}


# -- operator side ----------------------------------------------------------


def verify(envelope: dict, account: str, verify_key: str | None = None) -> License:
    """Check a licence against an account, or raise LicenseError.

    Every failure is distinguishable, because "it does not work" is useless to
    someone whose licence merely lapsed.
    """
    key = verify_key if verify_key is not None else AUTHOR_VERIFY_KEY
    if not key:
        raise LicenseError(
            "this build has no author key compiled in, so no licence can be "
            "checked -- see bulkdn/license.py"
        )

    missing = [f for f in (*SIGNED_FIELDS, "signature") if f not in envelope]
    if missing:
        raise LicenseError(f"licence is missing {', '.join(missing)}")

    try:
        licence = License(
            account=str(envelope["account"]),
            code=str(envelope["code"]),
            issued=int(envelope["issued"]),
            expires=int(envelope["expires"]),
        )
    except (TypeError, ValueError) as exc:
        raise LicenseError(f"malformed licence: {exc}") from exc

    try:
        VerifyKey(base58.b58decode(key)).verify(
            signing_bytes(licence.payload()), base58.b58decode(envelope["signature"])
        )
    except (BadSignatureError, ValueError) as exc:
        raise LicenseError(
            "licence signature does not check out -- it was not issued for this "
            "build, or it has been edited"
        ) from exc

    if licence.account != account:
        raise LicenseError(
            f"licence is for {licence.account}, but this bot signs as {account}"
        )
    if licence.code != REQUIRED_CODE:
        raise LicenseError(
            f"licence carries code {licence.code!r}, not {REQUIRED_CODE!r}"
        )
    if licence.expired:
        raise LicenseError("licence expired -- ask for a new one")

    return licence


def load(path: str = LICENSE_FILE) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise LicenseError(
            f"no {path} found. This build runs for accounts referred with code "
            f"{REQUIRED_CODE}; send your master pubkey to the author to get one."
        ) from exc
    except json.JSONDecodeError as exc:
        raise LicenseError(f"{path} is not valid JSON: {exc}") from exc


def enforce(account: str, path: str = LICENSE_FILE) -> License:
    """Gate: return the licence for `account`, or raise LicenseError."""
    return verify(load(path), account)

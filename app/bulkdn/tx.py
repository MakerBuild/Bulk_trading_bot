"""Signing and submitting a single action over HTTP.

The Python SDK's `place_orders` only accepts its four order dataclasses and
always signs for the signer's own account, so any action outside that set --
`createSubAccount`, `transfer`, `updateUserSettings` -- has to be assembled
here. This module holds the part all of them share: the transaction envelope,
the preimage layout, and the POST.

The preimage is what the API reference specifies, and nothing else:

    u64 action count || action bytes || u64 nonce || account[32] || domain byte

`account` is who is being acted on and `signer` is who authorises it. They
differ when a master acts on one of its sub-accounts.
"""

from __future__ import annotations

import struct
import time

import base58
from bulk_api.common.signer import SignatureDomain, TransactionSigner

from .retry import post_signed


def write_u64(value: int) -> bytes:
    return struct.pack("<Q", value)


def write_u32(value: int) -> bytes:
    return struct.pack("<I", value)


class TxResult(tuple):
    """What `sign_and_submit` returns: `(request, status, response)` plus a verdict.

    Still a 3-tuple, so every caller that unpacks `tx, status, body = ...`
    keeps working. The additions are attributes:

    * `uncertain` -- True when the outcome is NOT known. Some attempt may have
      reached the exchange without an answer (a read timeout, a dropped
      connection, a gateway 5xx), and the answer that did come back is not an
      acceptance. The usual shape: the first POST was applied, its response
      was lost, and the replay of the same signed bytes was refused because
      its nonce had already been used. Reading that refusal as "failed" is
      how a transfer gets repeated by hand. Callers should say UNKNOWN and
      tell the operator to check the balances before trying again.
    * `accepted` -- the exchange said "ok".

    An `accepted` result is never `uncertain`: an "ok" is an answer about this
    nonce, whichever attempt it came back on.
    """

    def __new__(cls, request: dict, status: int, response: dict, *, uncertain: bool = False):
        self = super().__new__(cls, (request, status, response))
        self.uncertain = bool(uncertain) and not accepted(status, response)
        return self

    @property
    def request(self) -> dict:
        return self[0]

    @property
    def status(self) -> int:
        return self[1]

    @property
    def response(self) -> dict:
        return self[2]

    @property
    def accepted(self) -> bool:
        return accepted(self.status, self.response)


def sign_and_submit(
    *,
    http_url: str,
    private_key: str,
    domain: SignatureDomain,
    action_bytes: bytes,
    action_json: dict,
    account: str | None = None,
    nonce: int | None = None,
    timeout: int = 30,
) -> TxResult:
    """Sign one action and POST it, returning (request, status, response).

    `account` defaults to the signer's own key. Pass a sub-account pubkey to act
    on it with the master's key.

    The result is a `TxResult`: it unpacks as the 3-tuple above, and its
    `uncertain` attribute is True when the outcome is unknown -- an attempt
    may have been applied without its answer arriving, and the final answer
    is not an acceptance. For a non-idempotent action (a transfer, a
    sub-account creation) that must be reported as UNKNOWN, never as a plain
    failure. See `TxResult`.

    If every attempt fails without any answer, `bulkdn.retry.RetryExhausted`
    is raised; its own `uncertain` attribute says the same thing about the
    attempts that were made.
    """
    signer = TransactionSigner(private_key)
    target = account or signer.public_key
    nonce = nonce if nonce is not None else int(time.time_ns())

    preimage = b"".join(
        [
            write_u64(1),  # one action in this transaction
            action_bytes,
            write_u64(nonce),
            base58.b58decode(target),
            bytes([domain.value]),
        ]
    )
    signature = signer.signing_key.sign(preimage).signature

    tx = {
        "actions": [action_json],
        # A JSON number, unlike the string the SDK sends for orders. Left as
        # it is on purpose: transfers and createSubAccount have been confirmed
        # live in exactly this form, and the string form has not been tried on
        # them. Python writes the integer exactly; a switch would be a change
        # to signed, money-moving requests made for tidiness alone.
        "nonce": nonce,
        "account": target,
        "signer": signer.public_key,
        "signature": base58.b58encode(signature).decode(),
    }

    # Retried as the same bytes, so a lost response is replayed under the same
    # nonce rather than becoming a second transaction. See bulkdn.retry.
    response, uncertain = post_signed(f"{http_url}/order", json=tx, timeout=timeout)
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text}
    if not isinstance(body, dict):
        body = {"raw": body}
    return TxResult(tx, response.status_code, body, uncertain=uncertain)


def accepted(status: int, body: dict) -> bool:
    return status == 200 and isinstance(body, dict) and body.get("status") == "ok"

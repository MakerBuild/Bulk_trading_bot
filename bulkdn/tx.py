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
import requests
from bulk_api.common.signer import SignatureDomain, TransactionSigner


def write_u64(value: int) -> bytes:
    return struct.pack("<Q", value)


def write_u32(value: int) -> bytes:
    return struct.pack("<I", value)


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
) -> tuple[dict, int, dict]:
    """Sign one action and POST it, returning (request, status, response).

    `account` defaults to the signer's own key. Pass a sub-account pubkey to act
    on it with the master's key.
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
        "nonce": nonce,
        "account": target,
        "signer": signer.public_key,
        "signature": base58.b58encode(signature).decode(),
    }

    response = requests.post(f"{http_url}/order", json=tx, timeout=timeout)
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text}
    return tx, response.status_code, body


def accepted(status: int, body: dict) -> bool:
    return status == 200 and body.get("status") == "ok"

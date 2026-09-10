"""`createSubAccount` transaction, hand-serialized.

The Python SDK does not sign this action -- `TransactionSigner.serialize_action`
has no `createSubAccount` case -- so the wincode bytes are built here.

The layout below matches a working implementation (`bulk-volume-bot`, in
`adapter/signer_ext.py`) that successfully creates sub-accounts against the live
API, so it is verified rather than guessed:

    u32 ordinal (27) || string name || optional f64 margin_amount

The trap worth recording: `marginSymbol` appears in the API reference's JSON
schema for this action but is **not** part of the signed binary preimage.
Including it -- as an earlier version of this file did, on the reasonable
assumption that the docs described the wire format -- shifts every subsequent
byte and the exchange rejects the transaction with `bad signature`. The Rust
`CreateSubAccount` struct, which carries only `name` and `margin_amount`, is the
accurate description of the signed bytes.

A zero margin amount encodes as absent (`0x00`), not as `Some(0.0)`.

Note that only the no-initial-margin path is confirmed working; the reference
implementation's config also leaves the margin at zero, so the `Some(amount)`
branch is untested against a live server.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from typing import Optional

import base58
import requests
from bulk_api.common.signer import SignatureDomain, TransactionSigner

CREATE_SUB_ACCOUNT_ORDINAL = 27


def _write_u64(value: int) -> bytes:
    return struct.pack("<Q", value)


def _write_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _write_u64(len(encoded)) + encoded


def _write_optional_f64(value: Optional[float]) -> bytes:
    """Tag byte then, if present, an unscaled little-endian f64.

    A zero amount is encoded as *absent*, not as `Some(0.0)` -- that is what the
    reference implementation does, and the tag byte is part of the signed bytes,
    so the distinction decides whether the signature verifies.
    """
    if value is None or float(value) <= 0:
        return bytes([0x00])
    return bytes([0x01]) + struct.pack("<d", float(value))


def serialize_create_sub_account(
    name: str, margin_amount: Optional[float] = None
) -> bytes:
    """Wincode bytes for one `createSubAccount` action.

    The signed payload is ordinal, name, optional margin amount -- and nothing
    else. `marginSymbol` appears in the API reference's JSON schema but is NOT
    part of the binary preimage; including it produces a `bad signature`
    rejection. This matches the `CreateSubAccount` struct in the Rust SDK, which
    has only `name` and `margin_amount`.
    """
    if not name or not (1 <= len(name) <= 32):
        raise ValueError("name must be 1-32 characters")

    return b"".join(
        [
            struct.pack("<I", CREATE_SUB_ACCOUNT_ORDINAL),
            _write_string(name),
            _write_optional_f64(margin_amount),
        ]
    )


@dataclass
class CreateSubAccountResult:
    name: str
    request_json: dict
    response_status: int
    response_json: dict

    @property
    def ok(self) -> bool:
        return self.response_status == 200 and self.response_json.get("status") == "ok"


def build_and_submit(
    *,
    http_url: str,
    private_key: str,
    domain: SignatureDomain,
    name: str,
    margin_amount: Optional[float] = None,
    nonce: Optional[int] = None,
) -> CreateSubAccountResult:
    """Sign and submit a `createSubAccount` transaction over HTTP.

    The master signs for itself (`account` == `signer` == the master pubkey),
    since a sub-account is created under the signing master, not under the
    child being created.
    """
    signer = TransactionSigner(private_key)
    nonce = nonce if nonce is not None else int(time.time_ns())

    action_bytes = serialize_create_sub_account(name, margin_amount)
    # Layout per the API spec: action count, actions, nonce, account, domain
    # byte (mainnet 1, testnet 2, devnet 3). The domain byte is always present.
    preimage = b"".join(
        [
            _write_u64(1),  # one action in this transaction
            action_bytes,
            _write_u64(nonce),
            base58.b58decode(signer.public_key),
            bytes([domain.value]),
        ]
    )
    signature = signer.signing_key.sign(preimage).signature

    # The JSON mirrors the signed bytes: name always, marginAmount only when
    # non-zero, and marginSymbol never -- it is not in the preimage, so sending
    # it would describe a transaction different from the one that was signed.
    action_json: dict = {"createSubAccount": {"name": name}}
    if margin_amount is not None and float(margin_amount) > 0:
        action_json["createSubAccount"]["marginAmount"] = float(margin_amount)

    tx = {
        "actions": [action_json],
        "nonce": nonce,
        "account": signer.public_key,
        "signer": signer.public_key,
        "signature": base58.b58encode(signature).decode(),
    }

    response = requests.post(f"{http_url}/order", json=tx, timeout=30)
    try:
        response_json = response.json()
    except ValueError:
        response_json = {"raw": response.text}

    return CreateSubAccountResult(
        name=name,
        request_json=tx,
        response_status=response.status_code,
        response_json=response_json,
    )

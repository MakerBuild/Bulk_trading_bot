"""Account-management transactions, hand-serialized.

The Python SDK signs neither of these -- `TransactionSigner.serialize_action`
has no `createSubAccount` or `transfer` case -- so the wincode bytes are built
here.

Both layouts are verified byte-for-byte against the official `bulk-keychain`
signing library rather than guessed, and `tests/test_subaccounts.py` pins the
bytes so they cannot drift back into a rejected shape:

    createSubAccount: u32 ordinal (27) || string name || optional f64 margin
    transfer:         u32 ordinal (29) || u32 kind || from[32] || to[32] || f64

**The trap both actions share**: `marginSymbol` appears in the API reference's
JSON schema but is **not** part of the signed binary preimage. Including it --
as an earlier version of this file did, on the reasonable assumption that the
docs described the wire format -- shifts every subsequent byte and the exchange
rejects the transaction with `bad signature`. The keychain `CreateSubAccount`
and `Transfer` structs, which carry no margin symbol, describe the signed bytes
accurately.

Amounts are plain little-endian f64, **not** fixed-point scaled by 1e8 the way
order prices and sizes are. For `createSubAccount` a zero margin encodes as
absent (`0x00`), not as `Some(0.0)` -- the tag byte is signed, so the two are
different transactions.

Both were confirmed against a live endpoint: `createSubAccount` with no initial
margin, and an internal `transfer`. The `createSubAccount` `Some(amount)` branch
is pinned by tests but has not been exercised against a live server.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import base58
from bulk_api.common.signer import SignatureDomain

from .tx import accepted, sign_and_submit, write_u32, write_u64

CREATE_SUB_ACCOUNT_ORDINAL = 27
TRANSFER_ORDINAL = 29

# Wire values for the transfer `kind` discriminant, serialized as u32.
TRANSFER_KINDS = {"internal": 0, "external": 1}


def _write_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return write_u64(len(encoded)) + encoded


def _write_pubkey(value: str) -> bytes:
    """Raw 32 bytes, with no length prefix.

    The length-prefixed form is exactly what the SDK's faucet action gets
    wrong: it writes u64(32) ahead of the key, which shifts every following
    byte and the exchange answers `bad signature`.
    """
    raw = base58.b58decode(value)
    if len(raw) != 32:
        raise ValueError(f"pubkey must decode to 32 bytes, got {len(raw)}")
    return raw


def _write_optional_f64(value: float | None) -> bytes:
    """Tag byte then, if present, an unscaled little-endian f64.

    A zero amount is encoded as *absent*, not as `Some(0.0)` -- that is what the
    reference implementation does, and the tag byte is part of the signed bytes,
    so the distinction decides whether the signature verifies.
    """
    if value is None or float(value) <= 0:
        return bytes([0x00])
    return bytes([0x01]) + struct.pack("<d", float(value))


def serialize_create_sub_account(
    name: str, margin_amount: float | None = None
) -> bytes:
    """Wincode bytes for one `createSubAccount` action.

    The signed payload is ordinal, name, optional margin amount -- and nothing
    else. See the module docstring for why `marginSymbol` must stay out.
    """
    if not name or not (1 <= len(name) <= 32):
        raise ValueError("name must be 1-32 characters")

    return b"".join(
        [
            write_u32(CREATE_SUB_ACCOUNT_ORDINAL),
            _write_string(name),
            _write_optional_f64(margin_amount),
        ]
    )


def serialize_transfer(
    from_pubkey: str, to_pubkey: str, margin_amount: float, kind: str = "internal"
) -> bytes:
    """Wincode bytes for one `transfer` action.

    Note `kind` is a u32 discriminant, not a single byte, and the amount is a
    plain f64. See the module docstring for the `marginSymbol` trap.
    """
    if kind not in TRANSFER_KINDS:
        raise ValueError(f"kind must be one of {sorted(TRANSFER_KINDS)}, got {kind!r}")
    if margin_amount <= 0:
        raise ValueError("margin_amount must be > 0")

    return b"".join(
        [
            write_u32(TRANSFER_ORDINAL),
            write_u32(TRANSFER_KINDS[kind]),
            _write_pubkey(from_pubkey),
            _write_pubkey(to_pubkey),
            struct.pack("<d", float(margin_amount)),
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
        return accepted(self.response_status, self.response_json)

    @property
    def sub_pubkey(self) -> str | None:
        """The new sub-account's pubkey, if the exchange reported one."""
        try:
            statuses = self.response_json["response"]["data"]["statuses"]
            return statuses[0]["createSubAccount"]["sub"]
        except (KeyError, IndexError, TypeError):
            return None


@dataclass
class TransferResult:
    from_pubkey: str
    to_pubkey: str
    margin_amount: float
    request_json: dict
    response_status: int
    response_json: dict

    @property
    def ok(self) -> bool:
        return accepted(self.response_status, self.response_json)


def build_and_submit(
    *,
    http_url: str,
    private_key: str,
    domain: SignatureDomain,
    name: str,
    margin_amount: float | None = None,
    nonce: int | None = None,
) -> CreateSubAccountResult:
    """Sign and submit a `createSubAccount` transaction over HTTP.

    The master signs for itself (`account` == `signer` == the master pubkey),
    since a sub-account is created under the signing master, not under the
    child being created.
    """
    # The JSON mirrors the signed bytes: name always, marginAmount only when
    # non-zero, and marginSymbol never.
    action_json: dict = {"createSubAccount": {"name": name}}
    if margin_amount is not None and float(margin_amount) > 0:
        action_json["createSubAccount"]["marginAmount"] = float(margin_amount)

    tx, status, response_json = sign_and_submit(
        http_url=http_url,
        private_key=private_key,
        domain=domain,
        action_bytes=serialize_create_sub_account(name, margin_amount),
        action_json=action_json,
        nonce=nonce,
    )
    return CreateSubAccountResult(
        name=name,
        request_json=tx,
        response_status=status,
        response_json=response_json,
    )


def submit_transfer(
    *,
    http_url: str,
    private_key: str,
    domain: SignatureDomain,
    from_pubkey: str,
    to_pubkey: str,
    margin_amount: float,
    kind: str = "internal",
    margin_symbol: str = "USDC",
    nonce: int | None = None,
) -> TransferResult:
    """Sign and submit a `transfer` transaction over HTTP.

    `margin_symbol` goes into the JSON only. The API schema requires it, but it
    contributes no signing bytes, so it cannot be recovered from the signature
    and has to be stated separately.
    """
    action_json = {
        "transfer": {
            "k": kind,
            "from": from_pubkey,
            "to": to_pubkey,
            "marginSymbol": margin_symbol,
            "marginAmount": float(margin_amount),
        }
    }
    tx, status, response_json = sign_and_submit(
        http_url=http_url,
        private_key=private_key,
        domain=domain,
        action_bytes=serialize_transfer(from_pubkey, to_pubkey, margin_amount, kind),
        action_json=action_json,
        nonce=nonce,
    )
    return TransferResult(
        from_pubkey=from_pubkey,
        to_pubkey=to_pubkey,
        margin_amount=float(margin_amount),
        request_json=tx,
        response_status=status,
        response_json=response_json,
    )

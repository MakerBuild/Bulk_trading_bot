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

import math
import struct
from dataclasses import dataclass

import base58
from bulk_api.common.signer import SignatureDomain

from .tx import accepted, sign_and_submit, write_u32, write_u64

CREATE_SUB_ACCOUNT_ORDINAL = 27
TRANSFER_ORDINAL = 29

# The sub-account name limit. What this file has always enforced, now counted
# in encoded bytes rather than characters -- see `serialize_create_sub_account`.
NAME_MAX_BYTES = 32

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

    A NEGATIVE amount is refused. It used to fall into the same `<= 0` branch
    as zero and be encoded as absent, so asking for -50 created the account
    with no margin and reported success: a typo turned into a different
    transaction than the one requested, silently. The JSON beside it dropped
    the field on the same test, so the two even agreed. Not-a-number and
    infinity are refused for the same reason -- neither is an amount anyone
    meant.
    """
    if value is None:
        return bytes([0x00])
    amount = float(value)
    if not math.isfinite(amount) or amount < 0:
        raise ValueError(f"margin amount must be a finite number >= 0, got {value!r}")
    if amount == 0:
        return bytes([0x00])
    return bytes([0x01]) + struct.pack("<d", amount)


def serialize_create_sub_account(
    name: str, margin_amount: float | None = None
) -> bytes:
    """Wincode bytes for one `createSubAccount` action.

    The signed payload is ordinal, name, optional margin amount -- and nothing
    else. See the module docstring for why `marginSymbol` must stay out.
    """
    # Bytes, not characters. The limit is on what goes on the wire -- the name
    # is written as length-prefixed UTF-8 just below -- and the two differ as
    # soon as a name leaves ASCII: 32 Cyrillic letters are 64 bytes, and
    # `len()` passed them. The menu only offers A-Z a-z 0-9 - _, where the two
    # agree; `--name` on the command line takes anything.
    if not name or not (1 <= len(name.encode("utf-8")) <= NAME_MAX_BYTES):
        raise ValueError(f"name must be 1-{NAME_MAX_BYTES} bytes of UTF-8")

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
    if not math.isfinite(float(margin_amount)) or margin_amount <= 0:
        raise ValueError(f"margin_amount must be > 0 and finite, got {margin_amount!r}")

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
    # True when the outcome is UNKNOWN: an attempt may have been applied
    # without its answer arriving, and the answer that did arrive is not an
    # acceptance. See `bulkdn.tx.TxResult`. Never True when `ok` is.
    uncertain: bool = False

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
    # As on `CreateSubAccountResult`. For a transfer this is the case that
    # matters most: a refusal after a lost response usually means the first
    # attempt moved the money and the replay's nonce was already spent.
    uncertain: bool = False

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

    Check `uncertain` on the result before reporting a failure. It is True
    when an attempt may have been applied without its answer arriving and
    the final answer is not an acceptance -- typically a replay refused
    because its nonce was already used, i.e. the account WAS created. Report
    that as UNKNOWN and look at the master's sub-accounts before trying again,
    or the retry makes a second one.
    """
    # The JSON mirrors the signed bytes: name always, marginAmount only when
    # non-zero, and marginSymbol never.
    action_json: dict = {"createSubAccount": {"name": name}}
    if margin_amount is not None and float(margin_amount) > 0:
        action_json["createSubAccount"]["marginAmount"] = float(margin_amount)

    result = sign_and_submit(
        http_url=http_url,
        private_key=private_key,
        domain=domain,
        action_bytes=serialize_create_sub_account(name, margin_amount),
        action_json=action_json,
        nonce=nonce,
    )
    tx, status, response_json = result
    return CreateSubAccountResult(
        name=name,
        request_json=tx,
        response_status=status,
        response_json=response_json,
        uncertain=bool(getattr(result, "uncertain", False)),
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

    Check `uncertain` on the result before reporting a failure: True means the
    outcome is unknown -- an attempt may have moved the money without its
    answer arriving, and the replay was then refused. Report it as UNKNOWN and
    read both balances before sending it again, or it is sent twice.
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
    result = sign_and_submit(
        http_url=http_url,
        private_key=private_key,
        domain=domain,
        action_bytes=serialize_transfer(from_pubkey, to_pubkey, margin_amount, kind),
        action_json=action_json,
        nonce=nonce,
    )
    tx, status, response_json = result
    return TransferResult(
        from_pubkey=from_pubkey,
        to_pubkey=to_pubkey,
        margin_amount=float(margin_amount),
        request_json=tx,
        response_status=status,
        response_json=response_json,
        uncertain=bool(getattr(result, "uncertain", False)),
    )

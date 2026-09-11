"""Byte layout of the hand-serialized createSubAccount action.

The layout is pinned against a known-working implementation, so these tests
exist to stop it drifting back into the shape that gets rejected.
"""

import struct

import base58

import pytest

from bulkdn.subaccounts import (
    CREATE_SUB_ACCOUNT_ORDINAL,
    TRANSFER_ORDINAL,
    serialize_create_sub_account,
    serialize_transfer,
)


def test_ordinal_is_first_four_bytes():
    data = serialize_create_sub_account("desk-1")
    assert struct.unpack("<I", data[:4])[0] == CREATE_SUB_ACCOUNT_ORDINAL == 27


def test_name_only_layout_is_exact():
    data = serialize_create_sub_account("desk-1")
    expected = (
        struct.pack("<I", 27)
        + struct.pack("<Q", 6)
        + b"desk-1"
        + b"\x00"  # margin amount absent
    )
    assert data == expected


def test_margin_symbol_is_absent_from_the_signed_bytes():
    """The regression that caused `bad signature`.

    marginSymbol is in the API reference's JSON schema but not in the binary
    preimage. If it ever creeps back in, the payload grows by at least the
    tag byte and every following byte shifts.
    """
    data = serialize_create_sub_account("desk-1")
    # ordinal(4) + len(8) + name(6) + margin tag(1)
    assert len(data) == 4 + 8 + 6 + 1
    assert b"USDC" not in data


def test_positive_margin_amount_is_tagged_and_unscaled():
    data = serialize_create_sub_account("desk-1", 1000.0)
    assert data[-9] == 0x01
    # Plain f64, not fixed-point scaled by 1e8.
    assert struct.unpack("<d", data[-8:])[0] == 1000.0


def test_zero_margin_encodes_as_absent_not_some_zero():
    # The tag byte is signed, so Some(0.0) and None are different transactions.
    assert serialize_create_sub_account("desk-1", 0.0) == serialize_create_sub_account("desk-1")
    assert serialize_create_sub_account("desk-1", 0.0)[-1] == 0x00


def test_none_margin_encodes_as_absent():
    assert serialize_create_sub_account("desk-1", None)[-1] == 0x00


def test_name_length_bounds():
    with pytest.raises(ValueError, match="1-32"):
        serialize_create_sub_account("")
    with pytest.raises(ValueError, match="1-32"):
        serialize_create_sub_account("x" * 33)
    serialize_create_sub_account("x" * 32)  # boundary is valid


def test_name_is_length_prefixed_utf8():
    data = serialize_create_sub_account("Sub1")
    assert struct.unpack("<Q", data[4:12])[0] == 4
    assert data[12:16] == b"Sub1"


# ---------------------------------------------------------------------------
# transfer
#
# The layout below was verified byte-for-byte against the official
# `bulk-keychain` 0.1.26 signing library, and separately against the live
# exchange, which accepts transfers signed this way.
#
# The golden hex itself is generated from this implementation, so it guards
# against drift rather than proving correctness a second time -- the addresses
# are synthetic, and bulk-keychain has no wheel for current Pythons, so it
# cannot be recomputed here. To restore it as an independent cross-check,
# install bulk-keychain and regenerate the literal from it.
#
# The literal embeds MASTER twice, so changing either address changes it.
# That is a regeneration, not a failure -- recompute it rather than reverting
# the address, and keep the field assertions below as the real check.
# ---------------------------------------------------------------------------

# These two are decoded to raw 32 bytes below, so they have to be real
# base58 rather than a readable placeholder. They are sha256 of the labels
# "bulkdn-example-master" and "bulkdn-example-subaccount" -- reproducible,
# and nobody's account.
MASTER = "DqciofFTMjwbGhwi3ox2kqN1F2P3HLYECDSC5hRytcUo"
SUB = "5GyLKfzb9xWjeLbF1PL9ygZZtNF3Hw3khYHxqD3VzM83"


def test_transfer_preimage_matches_keychain_reference():
    """Full 129-byte signing preimage.

    Pins the wrapper as well as the action -- action count, action, nonce,
    account, domain byte -- so drift in either layer fails here. The field
    breakdown is asserted separately by the tests below; this one is a single
    opaque literal on purpose, because splitting it by hand is how you
    introduce an off-by-one nibble.
    """
    from bulk_api.common.signer import SignatureDomain

    from bulkdn.config import SIGNATURE_DOMAIN_NAME
    from bulkdn.tx import write_u64

    preimage = (
        write_u64(1)
        + serialize_transfer(MASTER, SUB, 250.0)
        + write_u64(1704067200000)
        + base58.b58decode(MASTER)
        + bytes([SignatureDomain[SIGNATURE_DOMAIN_NAME].value])  # mainnet
    )
    assert len(preimage) == 129
    assert preimage.hex() == (
        "01000000000000001d00000000000000bec082f50bafed7094483093b03d66afe20be01242768e954ac30a895fdf5bb03f8627ea8803900cb63dd1abb234009c3379c48e971abed23f11592f34ef9a500000000000406f4000f451c28c010000bec082f50bafed7094483093b03d66afe20be01242768e954ac30a895fdf5bb001"
    )


def test_transfer_ordinal_and_kind_are_u32():
    data = serialize_transfer(MASTER, SUB, 1.0)
    assert struct.unpack("<I", data[:4])[0] == TRANSFER_ORDINAL == 29
    # A single-byte kind would shift both pubkeys and the amount.
    assert struct.unpack("<I", data[4:8])[0] == 0
    assert serialize_transfer(MASTER, SUB, 1.0, "external")[4:8] == struct.pack("<I", 1)


def test_transfer_layout_has_no_margin_symbol():
    """The regression that caused `bad signature` for createSubAccount.

    marginSymbol is in the API reference's JSON schema but not in the binary
    preimage, so the payload is exactly ordinal + kind + two keys + amount.
    """
    data = serialize_transfer(MASTER, SUB, 250.0)
    assert len(data) == 4 + 4 + 32 + 32 + 8
    assert b"USDC" not in data


def test_transfer_pubkeys_are_raw_32_bytes_not_length_prefixed():
    """The bug in the SDK's faucet action, kept out of this one.

    A u64(32) length prefix ahead of either key shifts every following byte.
    """
    data = serialize_transfer(MASTER, SUB, 250.0)
    assert data[8:40] == base58.b58decode(MASTER)
    assert data[40:72] == base58.b58decode(SUB)


def test_transfer_amount_is_unscaled_f64():
    data = serialize_transfer(MASTER, SUB, 250.0)
    # Plain f64, not fixed-point scaled by 1e8 the way order sizes are.
    assert struct.unpack("<d", data[-8:])[0] == 250.0


def test_transfer_rejects_bad_kind_and_nonpositive_amount():
    with pytest.raises(ValueError, match="kind must be"):
        serialize_transfer(MASTER, SUB, 1.0, "sideways")
    with pytest.raises(ValueError, match="must be > 0"):
        serialize_transfer(MASTER, SUB, 0.0)
    with pytest.raises(ValueError, match="must be > 0"):
        serialize_transfer(MASTER, SUB, -5.0)


def test_transfer_rejects_malformed_pubkey():
    with pytest.raises(ValueError, match="32 bytes"):
        serialize_transfer("tooshort", SUB, 1.0)

"""Byte layout of the hand-serialized createSubAccount action.

The layout is pinned against a known-working implementation, so these tests
exist to stop it drifting back into the shape that gets rejected.
"""

import struct

import pytest

from bulkdn.subaccounts import CREATE_SUB_ACCOUNT_ORDINAL, serialize_create_sub_account


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

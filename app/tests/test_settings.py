"""Leverage, via `updateUserSettings`.

The bytes come from the SDK's own serializer, so what is pinned here is the
part this module decides: that `m` travels as a JSON **object** rather than the
list of pairs the SDK's own `update_leverage` sends, and that one symbol goes
per transaction.

The object form is what the API reference describes and what the Rust reference
client declares (`HashMap<String, f64>` renamed to `m`). It was also confirmed
against `bulk-keychain`, whose signature over a keychain-built transaction
verifies against the preimage built from these bytes.
"""

import struct

import pytest

from bulkdn.settings import (
    UPDATE_USER_SETTINGS_ORDINAL,
    current_leverage,
    serialize_leverage,
    set_leverage,
)


def test_ordinal_and_single_entry_layout():
    data = serialize_leverage("BTC-USD", 5.0)
    assert struct.unpack("<I", data[:4])[0] == UPDATE_USER_SETTINGS_ORDINAL == 18
    # One map entry, then a length-prefixed symbol and a plain f64.
    assert struct.unpack("<Q", data[4:12])[0] == 1
    assert struct.unpack("<Q", data[12:20])[0] == len(b"BTC-USD")
    assert data[20:27] == b"BTC-USD"
    assert struct.unpack("<d", data[27:35])[0] == 5.0
    assert len(data) == 4 + 8 + 8 + 7 + 8


def test_leverage_is_an_unscaled_f64():
    """Not fixed-point scaled by 1e8 the way order prices and sizes are."""
    data = serialize_leverage("SOL-USD", 12.5)
    assert struct.unpack("<d", data[-8:])[0] == 12.5


def test_out_of_range_leverage_is_refused():
    for bad in (0.0, 0.9, 50.1, 1000.0):
        with pytest.raises(ValueError, match="leverage must be"):
            set_leverage(
                http_url="http://x",
                private_key="unused",
                domain=None,
                symbol="BTC-USD",
                leverage=bad,
            )


def test_json_sends_m_as_an_object_not_pairs(monkeypatch):
    """The SDK's own update_leverage sends `[[sym, lev]]`, which is not a map."""
    captured = {}

    def fake_submit(**kwargs):
        captured.update(kwargs)
        return {}, 200, {"status": "ok"}

    monkeypatch.setattr("bulkdn.settings.sign_and_submit", fake_submit)
    # `account` is supplied so no signer is constructed from a fake key.
    set_leverage(
        http_url="http://x",
        private_key="unused",
        domain=None,
        symbol="BTC-USD",
        leverage=5.0,
        account="MASTER",
    )
    assert captured["action_json"] == {"updateUserSettings": {"m": {"BTC-USD": 5.0}}}


def test_the_target_account_is_passed_through(monkeypatch):
    """A sub-account is set by the master signing for it."""
    captured = {}

    def fake_submit(**kwargs):
        captured.update(kwargs)
        return {}, 200, {"status": "ok"}

    monkeypatch.setattr("bulkdn.settings.sign_and_submit", fake_submit)
    result = set_leverage(
        http_url="http://x",
        private_key="unused",
        domain=None,
        symbol="SOL-USD",
        leverage=3.0,
        account="SUB1",
    )
    assert captured["account"] == "SUB1"
    assert result.account == "SUB1"
    assert result.ok is True


# -- reading current settings ----------------------------------------------


def test_current_leverage_reads_mapping_rows():
    state = {"leverageSettings": [{"symbol": "BTC-USD", "maxLeverage": 10.0}]}
    assert current_leverage(state) == {"BTC-USD": 10.0}


def test_current_leverage_reads_pair_rows():
    assert current_leverage({"leverageSettings": [["SOL-USD", 4.0]]}) == {"SOL-USD": 4.0}


def test_current_leverage_tolerates_absence_and_junk():
    assert current_leverage({}) == {}
    assert current_leverage({"leverageSettings": None}) == {}
    assert current_leverage({"leverageSettings": [{"symbol": "X"}, 5, []]}) == {}


# -- what a leg block may be called -----------------------------------------


def test_coin_named_leg_blocks_are_read_rather_than_refused(tmp_path):
    """`legs.btc`/`legs.sol` was the spelling before the blocks were named
    after accounts, and it used to be refused outright -- a file using it
    would otherwise have started with default sizes.

    Nothing defaults now: every block in the mapping is read, whatever it is
    called, because the name stopped meaning anything when accounts began
    being drawn per cycle. The refusal had become a trap of its own, since a
    market added from the menu was named after its coin.
    """
    import yaml

    from bulkdn.config import load_config

    path = tmp_path / "settings.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "legs": {
                    "btc": {"symbol": "BTC-USD", "size": 0.001, "max_order_size": 0.001},
                    "sol": {"symbol": "SOL-USD", "size": 2.0, "max_order_size": 2.0},
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_config(str(path), require_credentials=False)

    assert [leg.symbol for leg in config.active_legs] == ["BTC-USD", "SOL-USD"]


def test_one_leg_is_a_complete_config(tmp_path):
    """It used to be refused for naming no `sub_account`. One market is
    ordinary now -- which is what the old single mode was for."""
    import yaml

    from bulkdn.config import load_config

    path = tmp_path / "settings.yaml"
    path.write_text(
        yaml.safe_dump(
            {"legs": {"master_account": {"symbol": "BTC-USD", "size": 0.001,
                                         "max_order_size": 0.001}}}
        ),
        encoding="utf-8",
    )

    config = load_config(str(path), require_credentials=False)

    assert [leg.symbol for leg in config.active_legs] == ["BTC-USD"]

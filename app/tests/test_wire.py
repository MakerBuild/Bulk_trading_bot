"""One reading of the wire, shared by the fill stream and the history walk.

The two drifted apart. The history walk knew mainnet identifies a trade by
`slot` and `sequence` and sends no `tradeId`; the fill stream read only
`tradeId`, so on mainnet every streamed fill came with no id and the replay
guard -- "the only idempotency guard" for a fill delivered twice -- was off
without a word. And a side the stream read as a buy, the history walk read as
nothing.
"""

import logging

import pytest
from bulk_api.messages.trade import Fill

from bulkdn import fees, wire, ws_compat

# A fill as mainnet's history sends it: slot and sequence, no tradeId.
MAINNET_ROW = {
    "symbol": "BTC-USD", "orderId": "OID", "price": 100.0, "size": 0.01,
    "amount": 0.01, "isBuy": True, "timestamp": 1, "maker": "M", "taker": "T",
    "slot": 123456, "sequence": 7,
}


@pytest.fixture(autouse=True)
def patched(monkeypatch):
    ws_compat.apply_ws_compat()
    monkeypatch.setattr(ws_compat, "_no_trade_id_warned", False)


def test_a_streamed_fill_without_a_trade_id_takes_its_slot_and_sequence():
    fill = Fill.from_api(dict(MAINNET_ROW))
    assert fill.trade_id == "123456:7", "the replay guard keyed this fill on nothing"


def test_the_stream_and_the_history_name_a_trade_the_same_way():
    streamed = Fill.from_api(dict(MAINNET_ROW))
    assert fees._trade_key(dict(MAINNET_ROW)) == ("id", streamed.trade_id)
    # And a tradeId in the SDK's own format names the same trade.
    assert wire.trade_id({"tradeId": "123456:7"}) == streamed.trade_id


def test_a_trade_id_alone_is_still_read():
    row = {k: v for k, v in MAINNET_ROW.items() if k not in ("slot", "sequence")}
    assert Fill.from_api(dict(row, tradeId=99)).trade_id == "99"
    assert Fill.from_api(dict(row, tid="x:1")).trade_id == "x:1"


def test_a_fill_with_no_usable_id_is_reported_once(caplog):
    row = {k: v for k, v in MAINNET_ROW.items() if k not in ("slot", "sequence")}
    with caplog.at_level(logging.WARNING, logger="bulkdn.ws_compat"):
        for _ in range(3):
            assert Fill.from_api(dict(row)).trade_id is None
    said = [m for m in caplog.messages if "no trade id" in m]
    assert len(said) == 1


@pytest.mark.parametrize("flag, expected", [
    (True, True), (False, False), (1, True), (0, False),
    ("true", True), ("FALSE", False),
])
def test_the_history_reads_a_side_the_way_the_stream_does(flag, expected):
    row = dict(MAINNET_ROW, isBuy=flag)
    streamed = Fill.from_api(dict(row))
    assert fees._is_buy(row) is expected, "the history dropped a side the stream read"
    assert (streamed.side.name == "BUY") is expected


@pytest.mark.parametrize("word, expected", [("buy", True), ("ask", False), ("a", False)])
def test_side_words_are_one_list(word, expected):
    row = {"side": word}
    assert wire.is_buy(row) is expected
    assert fees._is_buy(row) is expected


def test_a_side_that_says_nothing_is_none_everywhere():
    assert wire.is_buy({"isBuy": 2}) is None
    assert fees._is_buy({}) is None


def test_the_fee_quote_and_the_account_share_one_unwrapper():
    body = {"takerBps": 3.5}
    assert wire.unwrap([{"feeTier": body}], "feeTier") == body
    assert wire.unwrap({"feeTier": body}, "feeTier") == body
    assert wire.unwrap(body, "feeTier") == body
    assert wire.unwrap([], "feeTier") == {}


def test_a_page_in_any_observed_shape_degrades_to_rows():
    assert wire.page([{"a": 1}, "junk"]) == ([{"a": 1}], None)
    assert wire.page({"data": [{"a": 1}], "page": {"nextCursor": "c"}}) == ([{"a": 1}], "c")
    assert wire.page({"data": [{"a": 1}], "page": None}) == ([{"a": 1}], None)
    assert wire.page(None) == ([], None)

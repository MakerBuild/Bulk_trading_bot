"""The position book decides every hedge, so its arithmetic is load-bearing."""

import time

from bulkdn.positions import PositionBook, SeenTrades

MASTER = "master-pubkey"
SUB1 = "sub1-pubkey"
BTC = "BTC-USD"


class FakePosition:
    def __init__(self, symbol, size):
        self.symbol = symbol
        self.size = size


def test_effective_includes_unconfirmed_fills():
    book = PositionBook(overlay_ttl_ms=5000)
    book.set_authoritative(MASTER, BTC, 0.0)
    book.apply_fill(MASTER, BTC, is_buy=True, size=0.1)

    assert book.effective(MASTER, BTC) == 0.1
    # Truth has not moved yet -- that is the whole point of the overlay.
    assert book.authoritative(MASTER, BTC) == 0.0


def test_authoritative_update_supersedes_the_overlay():
    book = PositionBook(overlay_ttl_ms=5000)
    book.apply_fill(MASTER, BTC, is_buy=True, size=0.1)
    assert book.effective(MASTER, BTC) == 0.1

    # Once the exchange confirms, the guess is discarded rather than added on
    # top -- keeping both is how a position gets double-counted. An overlay left
    # in place would read 0.2 here, so this value is the whole assertion.
    book.set_authoritative(MASTER, BTC, 0.1)
    assert book.effective(MASTER, BTC) == 0.1


def test_overlay_expires_so_a_lost_fill_cannot_drift_forever():
    book = PositionBook(overlay_ttl_ms=1)
    book.set_authoritative(MASTER, BTC, 0.0)
    book.apply_fill(MASTER, BTC, is_buy=True, size=0.1)
    time.sleep(0.01)
    assert book.effective(MASTER, BTC) == 0.0


def test_net_is_zero_when_hedged():
    book = PositionBook()
    book.set_authoritative(MASTER, BTC, 1.0)
    book.set_authoritative(SUB1, BTC, -1.0)
    assert book.net(MASTER, SUB1, BTC) == 0.0


def test_net_reflects_an_unhedged_partial_fill():
    book = PositionBook(overlay_ttl_ms=5000)
    book.set_authoritative(MASTER, BTC, 0.0)
    book.set_authoritative(SUB1, BTC, 0.0)
    book.apply_fill(MASTER, BTC, is_buy=True, size=0.1)
    assert book.net(MASTER, SUB1, BTC) == 0.1


def test_snapshot_zeroes_symbols_the_exchange_omits():
    book = PositionBook()
    book.set_authoritative(MASTER, BTC, 1.0)
    book.set_authoritative(MASTER, "SOL-USD", -5.0)

    # A closed position is omitted from the snapshot rather than sent as zero.
    book.apply_snapshot(MASTER, [FakePosition(BTC, 1.0)])

    assert book.authoritative(MASTER, BTC) == 1.0
    assert book.authoritative(MASTER, "SOL-USD") == 0.0


def test_snapshot_does_not_touch_the_other_account():
    book = PositionBook()
    book.set_authoritative(SUB1, BTC, -1.0)
    book.apply_snapshot(MASTER, [FakePosition(BTC, 1.0)])
    assert book.authoritative(SUB1, BTC) == -1.0


def test_full_account_envelope_is_unwrapped():
    """Regression: reading the envelope instead of the body made a funded
    account look flat -- no positions, no sub-accounts, no error."""
    from bulkdn.accounts import unwrap_full_account

    body = {"kind": "MasterEOA", "positions": [{"symbol": BTC, "size": 1.0}],
            "subAccounts": [{"pubkey": SUB1}]}

    assert unwrap_full_account({"fullAccount": body}) == body
    assert unwrap_full_account([{"fullAccount": body}]) == body
    assert unwrap_full_account(body) == body  # already flat


def test_unwrap_handles_empty_and_malformed_payloads():
    from bulkdn.accounts import unwrap_full_account

    assert unwrap_full_account([]) == {}
    assert unwrap_full_account(None) == {}
    assert unwrap_full_account("nonsense") == {}


def test_seen_trades_rejects_a_replayed_fill():
    seen = SeenTrades()
    assert seen.add_if_new(MASTER, "12000:3") is True
    assert seen.add_if_new(MASTER, "12000:3") is False


def test_seen_trades_is_scoped_per_account():
    """Maker and taker views of one execution share a tradeId.

    Both accounts genuinely moved, so both views must be applied -- a global
    set would silently drop the second one.
    """
    seen = SeenTrades()
    assert seen.add_if_new(MASTER, "12000:3") is True
    assert seen.add_if_new(SUB1, "12000:3") is True


def test_missing_trade_id_is_always_treated_as_new():
    # An exchange that has not been upgraded must not have every fill dropped.
    seen = SeenTrades()
    assert seen.add_if_new(MASTER, None) is True
    assert seen.add_if_new(MASTER, None) is True
    assert seen.add_if_new(MASTER, "") is True


def test_seen_trades_is_bounded():
    seen = SeenTrades(capacity=10)
    for i in range(50):
        seen.add_if_new(MASTER, f"1:{i}")
    assert len(seen) == 10
    # The most recent are retained; the oldest have been evicted.
    assert seen.add_if_new(MASTER, "1:49") is False
    assert seen.add_if_new(MASTER, "1:0") is True


def test_sell_fill_moves_position_down():
    book = PositionBook(overlay_ttl_ms=5000)
    book.set_authoritative(SUB1, BTC, 0.0)
    book.apply_fill(SUB1, BTC, is_buy=False, size=0.1)
    assert book.effective(SUB1, BTC) == -0.1

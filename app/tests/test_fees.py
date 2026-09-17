"""Fee schedule parsing and realised-spend totals.

The self-trade split is the part that matters: this strategy hedges between a
master and its own sub-account, and the fee documentation says those fills earn
no tier credit. A volume target that counted them would stop early on progress
that never happened.
"""

import itertools
from types import SimpleNamespace

import pytest

from bulkdn.fees import (
    FeeSchedule,
    Realised,
    Tier,
    account_fee_tier,
    fee_state,
    realised_for_tree,
)

MASTER = "EXAMPLE-MASTER-ACCOUNT-PUBKEY"
SUB = "EXAMPLE-SUBACCOUNT-PUBKEY"
OUTSIDER = "EXAMPLE-UNRELATED-PUBKEY"


_next_trade = itertools.count(1)


def fill(maker, taker, amount=1.0, price=100.0, fee=0.05, trade=None):
    """One row in the shape the API actually returns.

    A plain dict, and with no `tradeId`: mainnet sends `slot` and `sequence`
    separately, which is what made the SDK's strict history parser reject the
    whole page.

    Those two fields identify the TRADE, so every row gets its own by default.
    Pass the same `trade` to two rows to build the two sides of one trade --
    which is what a fill between two accounts of the tree looks like when both
    their histories are read.
    """
    seq = trade if trade is not None else next(_next_trade)
    return {
        "maker": maker,
        "taker": taker,
        "amount": amount,
        "price": price,
        "fee": fee,
        "symbol": "SOL-USD",
        "isBuy": True,
        "slot": 112226064,
        "sequence": seq,
    }


class FakeHttp:
    """Serves one page per account, then stops.

    Answers the raw `/account` POST rather than the SDK's `get_fills_page`,
    because that is what `fills_page` calls.
    """

    base_url = "https://example.test/api/v1"

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def install(self, monkeypatch):
        """Answer `fills_page`'s POST from `self.pages`."""

        class Response:
            def __init__(self, rows):
                self._rows = rows

            def raise_for_status(self):
                pass

            def json(self):
                return {"data": self._rows, "page": {"nextCursor": None}}

        def fake_post(url, json, timeout):
            user = json["user"]
            self.calls.append((user, json))
            return Response(self.pages.get(user, []))

        monkeypatch.setattr("bulkdn.fees.requests.post", fake_post)
        return self


# -- schedule ---------------------------------------------------------------


def test_tier_for_picks_the_highest_threshold_reached():
    schedule = FeeSchedule(
        scope="global",
        window_days=14,
        tiers=[
            Tier(0.0, 0.0, 3.5),
            Tier(1_000_000.0, 0.0, 3.3),
            Tier(10_000_000.0, 0.0, 3.0),
        ],
    )
    assert schedule.tier_for(0.0).taker_bps == 3.5
    assert schedule.tier_for(999_999.0).taker_bps == 3.5
    assert schedule.tier_for(1_000_000.0).taker_bps == 3.3
    assert schedule.tier_for(50_000_000.0).taker_bps == 3.0


def test_tier_for_returns_none_below_every_threshold():
    schedule = FeeSchedule("global", 14, [Tier(100.0, 0.0, 3.5)])
    assert schedule.tier_for(50.0) is None


def test_fee_state_accepts_both_key_spellings(monkeypatch):
    body = {
        "scopes": [
            {
                "instrument": "global",
                "active_policy": {
                    "window_days": 14,
                    "tiers": [{"threshold_volume": 0, "maker_bps": 1, "taker_bps": 2}],
                },
            },
            {
                "instrument": "BTC-USD",
                "active_policy": {
                    "window_days": 7,
                    "tiers": [{"thresholdVolume": 5, "makerBps": 3, "takerBps": 4}],
                },
            },
        ]
    }
    monkeypatch.setattr(
        "bulkdn.fees.requests.get",
        lambda *a, **k: SimpleNamespace(
            json=lambda: body, raise_for_status=lambda: None
        ),
    )
    out = fee_state("http://x")
    assert out["global"].tiers[0].taker_bps == 2
    assert out["BTC-USD"].tiers[0].maker_bps == 3
    assert out["BTC-USD"].window_days == 7


def test_account_fee_tier_returns_none_on_404(monkeypatch):
    monkeypatch.setattr(
        "bulkdn.fees.requests.post",
        lambda *a, **k: SimpleNamespace(status_code=404),
    )
    assert account_fee_tier("http://x", MASTER) is None


# The shape captured from mainnet-api1; the volume figures are made up, the
# field names and nesting are not -- those are the part that was wrong. The parser was
# written against a guessed shape -- a flat object -- and the real one wraps the
# quote in `feeTier` inside a list. Every field missed, every `or 0.0` supplied
# a zero, and the menu reported "taker 0.0 bps" against a real 3.5.
LIVE_FEE_TIER = [
    {
        "feeTier": {
            "scopeInstrument": None,
            "rollingVolume": 12500.0,
            "tierIndex": 0,
            "tierThreshold": 0.0,
            "makerBps": 0.0,
            "takerBps": 3.5,
            "windowDays": 14,
            "assessmentEpochDay": 20712,
            "assessmentCutoff": 1789516800000000000,
            "assessedAccountVolume": 12500.0,
            "makerNumerator": 0.0,
            "marketDenominator": 0.0,
            "makerSharePpm": 0,
            "makerShareRebateBps": 0.0,
            "effectiveMakerBps": 0.0,
        }
    }
]


def _quote(monkeypatch, body):
    monkeypatch.setattr(
        "bulkdn.fees.requests.post",
        lambda *a, **k: SimpleNamespace(
            status_code=200, json=lambda: body, raise_for_status=lambda: None
        ),
    )
    return account_fee_tier("http://x", MASTER)


def test_the_real_payload_shape_is_read(monkeypatch):
    quote = _quote(monkeypatch, LIVE_FEE_TIER)
    assert quote is not None
    assert quote.taker_bps == 3.5
    assert quote.maker_bps == 0.0
    assert quote.window_days == 14
    assert quote.rolling_volume == pytest.approx(12500.0)
    assert quote.tier_index == 0


def test_a_null_scope_reads_as_global(monkeypatch):
    """That is what the account-wide schedule looks like on the wire."""
    assert _quote(monkeypatch, LIVE_FEE_TIER).scope_instrument == "global"


def test_an_unwrapped_payload_still_works(monkeypatch):
    """In case the envelope goes away again; the fields are what matter."""
    flat = dict(LIVE_FEE_TIER[0]["feeTier"])
    assert _quote(monkeypatch, flat).taker_bps == 3.5


def test_a_payload_with_no_rates_is_no_quote_rather_than_free(monkeypatch):
    """A zero fee and an unreadable response must not look the same. One of
    them tells someone sizing a burn target that trading costs nothing."""
    assert _quote(monkeypatch, [{"feeTier": {"rollingVolume": 1.0}}]) is None
    assert _quote(monkeypatch, [{"somethingElse": {"takerBps": 3.5}}]) is None


def test_a_genuine_zero_is_still_reported(monkeypatch):
    """A real tier can reach zero; only a missing one is refused."""
    body = [{"feeTier": {"makerBps": 0.0, "takerBps": 0.0, "windowDays": 14}}]
    quote = _quote(monkeypatch, body)
    assert quote is not None and quote.taker_bps == 0.0 and quote.window_days == 14


def test_the_window_is_never_invented(monkeypatch):
    """`windowDays: 0` was the visible symptom -- no exchange runs a zero-day
    fee window, so it could only have come from a field that was not found."""
    quote = _quote(monkeypatch, LIVE_FEE_TIER)
    assert quote.window_days > 0


# -- realised ---------------------------------------------------------------


def test_a_trade_between_our_own_accounts_counts_once(monkeypatch):
    """It appears in both their histories. Summing the two views inflated the
    total by its whole notional -- the goal then read 5.7% short of the
    exchange's own figure on a live account."""
    both = [fill(MASTER, SUB, trade=7)]
    http = FakeHttp({MASTER: both, SUB: list(both)}).install(monkeypatch)
    totals = realised_for_tree(http, [MASTER, SUB])

    assert totals.fills == 2, "both rows were read"
    assert totals.volume_usd == 100.0, "the trade was counted twice"
    assert totals.self_trade_volume_usd == 100.0
    assert totals.qualifying_volume_usd == 100.0, "it counts, like any other trade"
    # Both sides were charged, and both are ours to pay.
    assert totals.fees_usd == 0.1


def test_the_fee_of_a_deduplicated_side_is_still_paid(monkeypatch):
    """Volume is the thing counted twice. Money is not."""
    both = [fill(MASTER, SUB, trade=9, fee=0.05)]
    http = FakeHttp({MASTER: both, SUB: list(both)}).install(monkeypatch)
    assert realised_for_tree(http, [MASTER, SUB]).fees_usd == 0.1


def test_two_separate_trades_are_both_counted(monkeypatch):
    """The fix must not collapse genuinely different trades."""
    http = FakeHttp({
        MASTER: [fill(MASTER, SUB), fill(MASTER, SUB)],
        SUB: [],
    }).install(monkeypatch)
    assert realised_for_tree(http, [MASTER, SUB]).volume_usd == 200.0


def test_external_counterparty_counts_toward_qualifying_volume(monkeypatch):
    http = FakeHttp({MASTER: [fill(MASTER, OUTSIDER)], SUB: []}).install(monkeypatch)
    totals = realised_for_tree(http, [MASTER, SUB])

    assert totals.self_trade_volume_usd == 0.0
    assert totals.qualifying_volume_usd == 100.0


def test_a_mixed_history_splits_correctly(monkeypatch):
    http = FakeHttp({
        MASTER: [fill(MASTER, SUB), fill(OUTSIDER, MASTER, amount=2.0)],
        SUB: [],
    }).install(monkeypatch)
    totals = realised_for_tree(http, [MASTER, SUB])

    assert totals.volume_usd == 300.0
    assert totals.self_trade_volume_usd == 100.0
    assert totals.qualifying_volume_usd == 300.0


def test_fills_without_a_trade_id_are_counted(monkeypatch):
    """The regression that emptied History and Progress.

    Mainnet sends `slot` and `sequence` separately and no `tradeId` at all.
    The SDK's history parser raises `tradeId must be <slot>:<sequence>` on
    that, which took out the whole page -- so fills are read as raw dicts and
    the id is never touched.
    """
    row = fill(MASTER, OUTSIDER)
    assert "tradeId" not in row

    http = FakeHttp({MASTER: [row], SUB: []}).install(monkeypatch)
    totals = realised_for_tree(http, [MASTER, SUB])

    assert totals.fills == 1
    assert totals.volume_usd == 100.0


def test_a_page_returned_as_a_bare_list_is_accepted(monkeypatch):
    """History queries answer {data, page}; a shape change should degrade to
    no rows rather than an exception."""
    from bulkdn.fees import fills_page

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return [fill(MASTER, OUTSIDER)]

    monkeypatch.setattr("bulkdn.fees.requests.post", lambda url, json, timeout: Response())
    rows, cursor = fills_page(FakeHttp({}), MASTER, limit=20, cursor=None)
    assert len(rows) == 1
    assert cursor is None


def test_totals_add():
    a = Realised(fills=1, fees_usd=1.0, volume_usd=10.0, self_trade_volume_usd=4.0)
    b = Realised(fills=2, fees_usd=2.0, volume_usd=20.0, self_trade_volume_usd=1.0)
    total = a + b
    assert (total.fills, total.fees_usd, total.volume_usd) == (3, 3.0, 30.0)
    assert total.qualifying_volume_usd == 30.0


def test_every_account_in_the_tree_is_walked(monkeypatch):
    http = FakeHttp({MASTER: [], SUB: []}).install(monkeypatch)
    realised_for_tree(http, [MASTER, SUB])
    assert [user for user, _ in http.calls] == [MASTER, SUB]


def test_from_fills_is_what_both_screens_use():
    """History and Progress must not total the same fills differently."""
    from bulkdn.fees import Realised

    rows = [fill(MASTER, SUB), fill(OUTSIDER, MASTER, amount=2.0)]
    totals = Realised.from_fills(rows, {MASTER, SUB})

    assert totals.fills == 2
    assert totals.volume_usd == 300.0
    assert totals.self_trade_volume_usd == 100.0
    assert totals.qualifying_volume_usd == 300.0
    assert totals.fees_usd == 0.1


def test_from_fills_on_an_empty_page_is_zero():
    from bulkdn.fees import Realised

    empty = Realised.from_fills([], {MASTER})
    assert (empty.fills, empty.volume_usd, empty.fees_usd) == (0, 0.0, 0.0)


def test_both_volume_definitions_are_reported():
    """The referral window counts a trade between our own accounts; the fee
    documentation says it creates no qualifying volume. Both cannot be checked
    yet, so the bot reports each rather than picking one silently."""
    from bulkdn.fees import Realised

    rows = [fill(MASTER, SUB, trade=1), fill(OUTSIDER, MASTER, amount=2.0, trade=2)]
    totals = Realised.from_fills(rows, {MASTER, SUB}, set())

    assert totals.volume_usd == 300.0
    assert totals.self_trade_volume_usd == 100.0
    assert totals.qualifying_volume_usd == 300.0, "the referral figure counts it"
    assert totals.tier_volume_usd == 200.0, "the documented tier rule does not"


def test_the_two_differ_only_by_the_self_traded_amount():
    from bulkdn.fees import Realised

    t = Realised(volume_usd=958_778.70, self_trade_volume_usd=55_120.10)
    assert t.qualifying_volume_usd - t.tier_volume_usd == pytest.approx(55_120.10)

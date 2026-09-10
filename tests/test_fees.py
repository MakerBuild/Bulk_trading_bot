"""Fee schedule parsing and realised-spend totals.

The self-trade split is the part that matters: this strategy hedges between a
master and its own sub-account, and the fee documentation says those fills earn
no tier credit. A volume target that counted them would stop early on progress
that never happened.
"""

from types import SimpleNamespace

from bulkdn.fees import (
    FeeSchedule,
    Realised,
    Tier,
    account_fee_tier,
    fee_state,
    realised_for_tree,
)

MASTER = "BR4SV1CRKygGWCsb1zF3g38Xc68b31WkEagdk8hVedB8"
SUB = "3AbM7XE9ikZW82rPUwRNnDovs3DMgvWgfPdckFnATSTe"
OUTSIDER = "9J8TUdEWrrcADK913r1Cs7DdqX63VdVU88imfDzT1ypt"


def fill(maker, taker, amount=1.0, price=100.0, fee=0.05):
    return SimpleNamespace(
        maker=maker, taker=taker, amount=amount, price=price, fee=fee, symbol="SOL-USD"
    )


class FakeHttp:
    """Serves one page per account, then stops."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_fills_page(self, user, **kwargs):
        self.calls.append((user, kwargs))
        return SimpleNamespace(data=self.pages.get(user, []), page=None, next_cursor=None)


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


# -- realised ---------------------------------------------------------------


def test_self_trades_are_excluded_from_qualifying_volume():
    """Both sides inside the tree: real spend, no tier credit."""
    http = FakeHttp({MASTER: [fill(MASTER, SUB)], SUB: [fill(MASTER, SUB)]})
    totals = realised_for_tree(http, [MASTER, SUB])

    assert totals.fills == 2
    assert totals.volume_usd == 200.0
    assert totals.self_trade_volume_usd == 200.0
    assert totals.qualifying_volume_usd == 0.0
    # Fees are still charged on a self-trade.
    assert totals.fees_usd == 0.1


def test_external_counterparty_counts_toward_qualifying_volume():
    http = FakeHttp({MASTER: [fill(MASTER, OUTSIDER)], SUB: []})
    totals = realised_for_tree(http, [MASTER, SUB])

    assert totals.self_trade_volume_usd == 0.0
    assert totals.qualifying_volume_usd == 100.0


def test_a_mixed_history_splits_correctly():
    http = FakeHttp({
        MASTER: [fill(MASTER, SUB), fill(OUTSIDER, MASTER, amount=2.0)],
        SUB: [],
    })
    totals = realised_for_tree(http, [MASTER, SUB])

    assert totals.volume_usd == 300.0
    assert totals.self_trade_volume_usd == 100.0
    assert totals.qualifying_volume_usd == 200.0


def test_totals_add():
    a = Realised(fills=1, fees_usd=1.0, volume_usd=10.0, self_trade_volume_usd=4.0)
    b = Realised(fills=2, fees_usd=2.0, volume_usd=20.0, self_trade_volume_usd=1.0)
    total = a + b
    assert (total.fills, total.fees_usd, total.volume_usd) == (3, 3.0, 30.0)
    assert total.qualifying_volume_usd == 25.0


def test_every_account_in_the_tree_is_walked():
    http = FakeHttp({MASTER: [], SUB: []})
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
    assert totals.qualifying_volume_usd == 200.0
    assert totals.fees_usd == 0.1


def test_from_fills_on_an_empty_page_is_zero():
    from bulkdn.fees import Realised

    empty = Realised.from_fills([], {MASTER})
    assert (empty.fills, empty.volume_usd, empty.fees_usd) == (0, 0.0, 0.0)

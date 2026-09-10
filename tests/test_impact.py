"""Impact-curve interpolation and the hedge slippage ceiling.

The curve is 250 readings at log-uniform size knots, so reading a size off it
is an interpolation in log space. Everything degrades to "no data" when a
market has no published curve, and a guard with no data must not fire.
"""

import math
from types import SimpleNamespace

from bulkdn.impact import Impact, ImpactBook, ImpactCurve, fetch

# 1 bps at size 0.001 rising linearly to 100 bps at size 10.
LOG_MIN = math.log(0.001)
LOG_RANGE = math.log(10.0) - LOG_MIN
LINEAR = ImpactCurve(
    log_min=LOG_MIN, log_range=LOG_RANGE, bps=[1 + 99 * i / 249 for i in range(250)]
)


def test_first_and_last_knots_read_exactly():
    assert LINEAR.bps_for(0.001) == 1.0
    assert LINEAR.bps_for(10.0) == 100.0


def test_the_log_midpoint_lands_mid_curve():
    """0.1 is the geometric mean of 0.001 and 10."""
    assert LINEAR.bps_for(0.1) == 50.5


def test_sizes_outside_the_curve_clamp_to_its_ends():
    """The curve says nothing beyond its own range, so it is not extrapolated."""
    assert LINEAR.bps_for(0.0000001) == 1.0
    assert LINEAR.bps_for(10_000.0) == 100.0


def test_non_positive_and_empty_return_none():
    assert LINEAR.bps_for(0.0) is None
    assert LINEAR.bps_for(-1.0) is None
    assert ImpactCurve(0.0, 0.0, []).bps_for(1.0) is None


def test_a_zero_range_curve_reads_its_single_value():
    assert ImpactCurve(0.0, 0.0, [7.0, 9.0]).bps_for(5.0) == 7.0


def test_buy_and_sell_sides_are_separate():
    impact = Impact(
        symbol="BTC-USD",
        timestamp=0,
        min_size=0.001,
        buy=ImpactCurve(LOG_MIN, LOG_RANGE, [10.0] * 250),
        sell=ImpactCurve(LOG_MIN, LOG_RANGE, [20.0] * 250),
    )
    assert impact.bps_for(0.1, is_buy=True) == 10.0
    assert impact.bps_for(0.1, is_buy=False) == 20.0


def test_from_api_parses_the_documented_shape():
    impact = Impact.from_api({
        "symbol": "SOL-USD",
        "timestamp": 1720000000000,
        "minSize": 0.5,
        "buyBps": {"logMin": 0.0, "logRange": 1.0, "bps": [1.0, 2.0]},
        "sellBps": {"logMin": 0.0, "logRange": 1.0, "bps": [3.0, 4.0]},
    })
    assert impact.symbol == "SOL-USD"
    assert impact.min_size == 0.5
    assert impact.buy.bps == [1.0, 2.0]


def test_fetch_returns_none_when_no_curve_is_published(monkeypatch):
    """404 is the documented answer until the executor publishes one."""
    monkeypatch.setattr(
        "bulkdn.impact.requests.get", lambda *a, **k: SimpleNamespace(status_code=404)
    )
    assert fetch("http://x", "BTC-USD") is None


def test_book_reports_no_data_for_an_unpublished_market(monkeypatch):
    monkeypatch.setattr(
        "bulkdn.impact.requests.get", lambda *a, **k: SimpleNamespace(status_code=404)
    )
    book = ImpactBook("http://x")
    book.refresh(["BTC-USD"])
    assert book.has("BTC-USD") is False
    assert book.bps_for("BTC-USD", 1.0, True) is None


def test_book_survives_an_unreachable_endpoint(monkeypatch):
    """Market data being down must not stop hedging."""
    def boom(*a, **k):
        raise ConnectionError("down")

    monkeypatch.setattr("bulkdn.impact.requests.get", boom)
    book = ImpactBook("http://x")
    book.refresh(["BTC-USD"])
    assert book.bps_for("BTC-USD", 1.0, True) is None


def test_book_reads_a_cached_curve():
    book = ImpactBook("http://x")
    book._curves["BTC-USD"] = Impact("BTC-USD", 0, 0.001, LINEAR, LINEAR)
    assert book.has("BTC-USD") is True
    assert book.bps_for("BTC-USD", 0.1, True) == 50.5
    assert book.bps_for("SOL-USD", 0.1, True) is None

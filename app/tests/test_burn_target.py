"""The fee target: what counts, and from when.

Two things this pins, both of which were wrong at some point.

The sign. BULK reports a charge as a NEGATIVE number on the fill -- a real
taker fill reads `"takerFee": -0.035017`, and a maker fill reads
`"makerFee": 0.0`, because passive execution is free here rather than rebated.
So a total of -3.0795 is $3.0795 SPENT. It was once read as money earned, and
the log printed `burned $-3.0795` at an operator who had in fact burned it.

The window. Totals came from the account's whole fill history, so once the
lifetime figure passed `burn_usd` the target was met before a single order was
placed, on that run and on every run after it -- a live session ended in one
second with `execution target reached: burned $-3.0795 of $3.00`. The target is
now the distance travelled since the run began: the history is read from the
moment the run started (`since_ms`), not as a lifetime total with the start's
lifetime total subtracted -- the walk stops at a page cap, and past it the two
totals stop growing and their difference stops meaning anything.
"""

import logging

import pytest

from bulkdn.config import ConfigError, ExecutionTarget
from bulkdn.fees import burned_usd
from bulkdn.state import StrategyState
from bulkdn.strategy import Strategy


class FakeTotals:
    def __init__(self, fees, volume=0.0, self_trade=0.0):
        self.fees_usd = fees
        self.qualifying_volume_usd = volume
        self.self_trade_volume_usd = self_trade


class FakeStrategy:
    """Only what _target_reached touches."""

    # The real ones, so the test drives the actual code path: the history read
    # is handed to a worker thread rather than run on the event loop, and the
    # accounts it reads are grouped by signing key the way the real run
    # groups them.
    _read_totals = Strategy._read_totals
    _totals_failed = Strategy._totals_failed
    _trees = Strategy._trees
    _spread_cost = Strategy._spread_cost
    _cost_text = Strategy._cost_text
    all_sessions = Strategy.all_sessions

    def __init__(self, target, state=None):
        class Config:
            pass

        self.config = Config()
        self.config.target = target
        self.state = state if state is not None else _started_at(0.0)

        # _target_reached reads these to address the fill history. Without
        # them the AttributeError is swallowed by the "never block trading on
        # this" handler, and every test passes by returning None.
        class Session:
            http = None
            pubkey = "EXAMPLE-PUBKEY"
            # Accounts under one key share a socket, and that is what tells
            # `_trees` which of them the exchange can see as related.
            client = None

        self.master = Session()
        self.sub1 = Session()

        class Feed:
            @staticmethod
            def reference_price(_symbol):
                return 80_000.0

        self.feed = Feed()
        # Every account the run trades. The history is read across all of
        # them, not the first pair -- in pool mode that pair is two of a
        # hundred and ten.
        self.sessions = {self.master.pubkey: self.master, self.sub1.pubkey: self.sub1}
        # No cached answer: each of these tests reads once and expects the
        # read to happen.
        self._target_answer = (0.0, None)

    async def reached(self):
        return await Strategy._target_reached(self)


def _started_at(fees, volume=0.0, self_trade=0.0):
    """A state whose run began with this much already on the account."""
    return StrategyState(
        baseline_fees_usd=fees,
        baseline_volume_usd=volume,
        baseline_self_trade_usd=self_trade,
        baseline_at=1.0,
    )


@pytest.fixture(autouse=True)
def totals(monkeypatch):
    """Replace the fill-history read with the number under test."""
    from bulkdn import strategy as strategy_module

    def fake(http, trees, since_ms=None):
        fake.reads += 1
        fake.since_ms = since_ms
        return fake.value

    fake.reads = 0
    fake.since_ms = None

    monkeypatch.setattr(strategy_module, "realised_for_trees", fake)
    return fake


async def reached(target_usd, fees_usd, totals, *, baseline=0.0):
    totals.value = FakeTotals(fees_usd)
    return await FakeStrategy(
        ExecutionTarget(cycles=0, burn_usd=target_usd), _started_at(baseline)
    ).reached()


# -- a charge is negative, and burning it is positive -------------------------


async def test_a_negative_total_is_money_spent():
    assert burned_usd(-3.0795) == pytest.approx(3.0795)


async def test_a_positive_total_is_not_spending():
    """Nothing on BULK produces one today -- maker fills are 0.0, not rebated --
    so it is floored rather than counted as negative spend."""
    assert burned_usd(1.5) == 0.0


async def test_the_reason_string_carries_no_minus_sign(totals):
    """What the operator reads at the end of a run."""
    reason = await reached(3.0, -3.5, totals)
    assert reason is not None
    assert "-" not in reason
    assert "$3.5000" in reason


# -- the target is reached by spending ---------------------------------------


async def test_spending_past_the_target_finishes(totals):
    assert await reached(3.0, -3.5, totals)


async def test_exactly_on_the_target_counts(totals):
    assert await reached(3.0, -3.0, totals)


async def test_short_of_the_target_keeps_going(totals):
    assert await reached(3.0, -2.99, totals) is None


async def test_a_positive_total_never_reaches_the_target(totals):
    """Not spending is not progress toward a spend target."""
    assert await reached(3.0, 100.0, totals) is None


# -- and it counts from the start of the run ----------------------------------


async def test_the_account_history_before_the_run_does_not_count(totals):
    """The reported bug: $3.08 burned last week ended the next run instantly.

    The walk is asked for fills from the run's start and nothing earlier.
    """
    await reached(3.0, 0.0, totals)
    assert totals.since_ms == pytest.approx(1.0 * 1000)


async def test_only_what_this_run_burned_counts(totals):
    """What the walk since the start returns is the whole of this run's spend:
    nothing is subtracted from it, so a history past the page cap cannot turn
    the difference into nonsense."""
    assert await reached(3.0, -2.0, totals, baseline=-3.0795) is None
    assert await reached(2.0, -2.0, totals, baseline=-3.0795)


async def test_without_a_baseline_nothing_is_judged(totals):
    """A run that could not read its starting point must not compare against
    the lifetime figure -- that is exactly the bug."""
    totals.value = FakeTotals(-100.0)
    strategy = FakeStrategy(ExecutionTarget(burn_usd=3.0), StrategyState())
    assert strategy.state.has_baseline is False
    assert await strategy.reached() is None


async def test_a_volume_target_also_counts_from_the_start(totals):
    totals.value = FakeTotals(0.0, volume=500.0)
    strategy = FakeStrategy(
        ExecutionTarget(volume_usd=1000.0), _started_at(0.0, volume=1000.0)
    )
    # 500 done since the start, of 1000.
    assert await strategy.reached() is None

    totals.value = FakeTotals(0.0, volume=1000.0)
    # The answer is cached for TARGET_FRESHNESS_S, because the read walks
    # every account's paginated fill history. In a run these two checks are
    # half a minute apart; here they are consecutive lines.
    strategy._target_answer = (0.0, None)
    assert await strategy.reached()


# -- self-trade is a distance too --------------------------------------------


async def test_the_self_trade_figure_counts_from_the_start(totals, caplog):
    """It sits beside progress to say how much of THIS run will not count
    toward the fee tier. It used to be the lifetime total printed next to a
    baselined one, which produced lines that cannot be true of any run:

        volume progress: $0.00 / $1000000.00 qualifying
        (of which $127113.97 traded between your own accounts)
    """
    totals.value = FakeTotals(0.0, volume=500.0, self_trade=200.0)
    strategy = FakeStrategy(
        ExecutionTarget(volume_usd=10_000.0),
        _started_at(0.0, volume=1000.0, self_trade=1000.0),
    )
    with caplog.at_level(logging.INFO):
        assert await strategy.reached() is None

    line = next(
        record.getMessage()
        for record in caplog.records
        if "volume progress" in record.getMessage()
    )
    # 500 done since the start, of which 200 was with ourselves.
    assert "$500.00 / $10000.00" in line
    assert "$200.00 traded between your own accounts" in line


async def test_the_part_can_never_be_larger_than_the_whole(totals):
    """The property the old line broke. $127,113.97 of $0.00 was exactly
    that, and it is what made the mismatch visible in the first place."""
    totals.value = FakeTotals(0.0, volume=500.0, self_trade=200.0)
    state = _started_at(0.0, volume=1000.0, self_trade=1000.0)
    await FakeStrategy(ExecutionTarget(volume_usd=10_000.0), state).reached()

    # Both read over the same window, so neither is a lifetime figure.
    assert totals.since_ms == pytest.approx(state.baseline_at * 1000)
    done = totals.value.qualifying_volume_usd
    ours = totals.value.self_trade_volume_usd
    assert 0.0 <= ours <= done


# -- clearing it --------------------------------------------------------------


def test_clearing_the_baseline_makes_the_next_run_measure_fresh():
    state = _started_at(-3.0795, volume=500.0, self_trade=100.0)
    assert state.has_baseline
    state.clear_baseline()
    assert not state.has_baseline
    assert state.baseline_fees_usd == 0.0
    assert state.baseline_volume_usd == 0.0
    assert state.baseline_self_trade_usd == 0.0


async def test_a_baseline_survives_a_save_and_load(tmp_path):
    """An interrupted run resumes its own count rather than starting over."""
    from bulkdn.state import StateStore

    store = StateStore(str(tmp_path / "state.json"))
    store.save(_started_at(-3.0795, volume=500.0))
    loaded = store.load()
    assert loaded.has_baseline
    assert loaded.baseline_fees_usd == pytest.approx(-3.0795)
    assert loaded.baseline_volume_usd == pytest.approx(500.0)


async def test_a_state_file_written_before_baselines_reads_as_unstarted():
    state = StrategyState.from_dict({"phase": "IDLE", "cycle_index": 2})
    assert state.has_baseline is False


# -- zero is off -------------------------------------------------------------


async def test_zero_disables_the_target(totals):
    assert await reached(0.0, -100.0, totals) is None


def test_zero_needs_no_fill_history():
    """Reading it costs an API call every cycle, so it must stay opt-in."""
    assert ExecutionTarget(burn_usd=0.0).measures_fills is False
    assert ExecutionTarget(burn_usd=3.0).measures_fills is True


# -- the config asks for a plain amount --------------------------------------


def test_a_negative_target_is_refused():
    """The sign belongs to the fills, not to the setting."""
    with pytest.raises(ConfigError, match="plain positive amount"):
        ExecutionTarget(burn_usd=-3.0).validate()


def test_the_other_targets_refuse_a_negative_too():
    with pytest.raises(ConfigError, match="cycles"):
        ExecutionTarget(cycles=-1).validate()
    with pytest.raises(ConfigError, match="volume_usd"):
        ExecutionTarget(volume_usd=-1.0).validate()


# -- and it is not read on every tick ---------------------------------------


async def test_a_second_check_inside_the_window_does_not_read_again(totals):
    """The read walks every account's paginated fill history -- seconds, for a
    pool of a hundred. The group dispatcher asks on every pass of its loop, so
    without this it would start those walks twice a second against an exchange
    that answered 429 to two accounts polling every five."""
    totals.value = FakeTotals(0.0, volume=100.0)
    strategy = FakeStrategy(
        ExecutionTarget(volume_usd=1000.0), _started_at(0.0, volume=0.0)
    )

    await strategy.reached()
    before = totals.reads
    for _ in range(20):
        await strategy.reached()

    assert totals.reads == before, "the history was walked again inside the window"


async def test_a_failed_read_is_held_only_briefly(totals, monkeypatch):
    """Holding "not reached" for the whole freshness window would turn one
    unreachable endpoint into a run slow to notice its own goal. Not holding
    it at all retried every second, and from Tokyo kept a 429 going."""
    import time

    from bulkdn.strategy import TARGET_FRESHNESS_S, TARGET_RETRY_S

    strategy = FakeStrategy(
        ExecutionTarget(volume_usd=1000.0), _started_at(0.0, volume=0.0)
    )

    async def boom():
        raise RuntimeError("indexer down")

    strategy._read_totals = boom
    assert await strategy.reached() is None
    read_at, answer = strategy._target_answer
    assert answer is None, "a failure was recorded as an answer"
    next_read_in = read_at + TARGET_FRESHNESS_S - time.monotonic()
    assert 0 < next_read_in <= TARGET_RETRY_S + 0.5


# -- the cost is split into what is ours to change --------------------------


def test_the_price_result_is_what_the_balances_lost_besides_fees():
    """Checked against a live run to the cent: $36.56 = $22.08 + $14.49.

    Maker buys at 100, the hedge sells at 99.9 on another account: 0.1 lost
    on price per unit, whatever the fees were.
    """
    from bulkdn.fees import Realised

    rows_maker = [{"isBuy": True, "amount": 2.0, "price": 100.0, "fee": 0.0,
                   "symbol": "BTC-USD", "slot": 1, "sequence": 0}]
    rows_hedge = [{"isBuy": False, "amount": 2.0, "price": 99.9, "fee": -0.07,
                   "symbol": "BTC-USD", "slot": 2, "sequence": 0}]
    total = Realised.from_fills(rows_maker, set()) + Realised.from_fills(rows_hedge, set())

    assert total.price_result_usd({"BTC-USD": 99.95}) == pytest.approx(-0.2)
    assert total.fees_usd == pytest.approx(-0.07)


def test_an_open_remainder_is_valued_at_the_price():
    from bulkdn.fees import Realised

    rows = [{"isBuy": True, "amount": 1.0, "price": 100.0, "fee": 0.0,
             "symbol": "BTC-USD", "slot": 1, "sequence": 0}]
    total = Realised.from_fills(rows, set())
    assert total.price_result_usd({"BTC-USD": 101.0}) == pytest.approx(1.0)


async def test_the_progress_line_shows_fees_and_spread_apart(totals, caplog):
    from bulkdn.fees import Realised

    value = Realised(fees_usd=-22.08, cash_usd=-14.49, base_by_symbol={"BTC-USD": 0.0})
    totals.value = value
    strategy = FakeStrategy(ExecutionTarget(volume_usd=1e9))
    with caplog.at_level(logging.INFO):
        await strategy.reached()
    line = next(r.getMessage() for r in caplog.records if "cost so far" in r.getMessage())
    assert "fees $22.08" in line
    assert "spread/slippage $14.49" in line
    assert "= $36.57" in line


async def test_the_reading_that_reaches_the_target_is_the_one_shown(totals):
    """The status block sat at 97.5% for twenty minutes after the log said
    the target was met: the reaching read returned before it was stored."""
    from bulkdn.fees import Realised

    totals.value = Realised(volume_usd=101_795.50, fees_usd=-17.30)
    strategy = FakeStrategy(ExecutionTarget(volume_usd=100_000.0))
    strategy._progress = (16.70, 97_541.91)
    assert await strategy.reached()
    assert strategy._progress == pytest.approx((17.30, 101_795.50))


# -- the status block's refresh never shows a lifetime as this run ------------
#
# 2026-09-25 17:02: the status loop started before recovery captured the
# baseline, so its first read walked every account's lifetime history. It
# landed forty seconds later, after the run had begun, and the log said
# "cost so far: $0.00" and then, one second on, the accounts' lifetime totals.


def _refreshing_strategy(read):
    from bulkdn.state import StrategyState
    from bulkdn.strategy import Strategy

    s = Strategy.__new__(Strategy)
    s.state = StrategyState()
    s._progress = None
    s._spread_cost = lambda totals: 0.0
    s._cost_text = lambda burned: f"${burned:,.2f}"
    s._read_totals = read
    return s


async def test_no_refresh_before_the_run_has_a_start():
    from bulkdn.fees import Realised

    calls = []

    async def read():
        calls.append(True)
        return Realised(fees_usd=-500.0, volume_usd=2_000_000.0)

    s = _refreshing_strategy(read)
    await s._refresh_totals()

    assert calls == [], "it read a lifetime history with nothing to count from"
    assert s._progress is None


async def test_a_read_that_began_before_the_baseline_is_dropped():
    from bulkdn.fees import Realised

    s = None
    reads = []

    async def read():
        reads.append(True)
        s.state.baseline_at = 1_790_000_100.0     # captured while this was reading
        return Realised(fees_usd=-500.0, volume_usd=2_000_000.0)

    s = _refreshing_strategy(read)
    s.state.baseline_at = 1_790_000_000.0
    await s._refresh_totals()

    assert reads, "the read never ran"
    assert s._progress is None, "a read over another window was shown as this run"

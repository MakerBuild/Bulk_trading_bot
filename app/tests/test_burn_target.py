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
now the distance travelled since the run began.
"""

import pytest

from bulkdn.config import ConfigError, ExecutionTarget
from bulkdn.fees import burned_usd
from bulkdn.state import StrategyState
from bulkdn.strategy import Strategy


class FakeTotals:
    def __init__(self, fees, volume=0.0):
        self.fees_usd = fees
        self.qualifying_volume_usd = volume
        self.self_trade_volume_usd = 0.0


class FakeStrategy:
    """Only what _target_reached touches."""

    # The real one, so the test drives the actual code path: the history read
    # is handed to a worker thread rather than run on the event loop.
    _read_totals = Strategy._read_totals

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

        self.master = Session()
        self.sub1 = Session()

    async def reached(self):
        return await Strategy._target_reached(self)


def _started_at(fees, volume=0.0):
    """A state whose run began with this much already on the account."""
    return StrategyState(
        baseline_fees_usd=fees, baseline_volume_usd=volume, baseline_at=1.0
    )


@pytest.fixture(autouse=True)
def totals(monkeypatch):
    """Replace the fill-history read with the number under test."""
    from bulkdn import strategy as strategy_module

    def fake(http, wallets):
        return fake.value

    monkeypatch.setattr(strategy_module, "realised_for_tree", fake)
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
    """The reported bug: $3.08 burned last week ended the next run instantly."""
    assert await reached(3.0, -3.0795, totals, baseline=-3.0795) is None


async def test_only_what_this_run_burned_counts(totals):
    """Started at -3.08, now at -5.08: this run has burned $2, not $5.08."""
    assert await reached(3.0, -5.0795, totals, baseline=-3.0795) is None
    assert await reached(2.0, -5.0795, totals, baseline=-3.0795)


async def test_without_a_baseline_nothing_is_judged(totals):
    """A run that could not read its starting point must not compare against
    the lifetime figure -- that is exactly the bug."""
    totals.value = FakeTotals(-100.0)
    strategy = FakeStrategy(ExecutionTarget(burn_usd=3.0), StrategyState())
    assert strategy.state.has_baseline is False
    assert await strategy.reached() is None


async def test_a_volume_target_also_counts_from_the_start(totals):
    totals.value = FakeTotals(0.0, volume=1500.0)
    strategy = FakeStrategy(
        ExecutionTarget(volume_usd=1000.0), _started_at(0.0, volume=1000.0)
    )
    # 1500 - 1000 = 500 done of 1000.
    assert await strategy.reached() is None

    totals.value = FakeTotals(0.0, volume=2000.0)
    assert await strategy.reached()


# -- clearing it --------------------------------------------------------------


def test_clearing_the_baseline_makes_the_next_run_measure_fresh():
    state = _started_at(-3.0795, volume=500.0)
    assert state.has_baseline
    state.clear_baseline()
    assert not state.has_baseline
    assert state.baseline_fees_usd == 0.0
    assert state.baseline_volume_usd == 0.0


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

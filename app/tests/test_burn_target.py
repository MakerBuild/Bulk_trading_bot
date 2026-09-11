"""A fee target that can point either way.

The resting side of each leg earns a maker rebate and the hedge pays a taker
fee, so a pair that fills mostly on the resting side runs a NEGATIVE fee total.
A live run showed `burn progress: $-1.4394 / $3.00` -- the figure moving away
from its target every cycle, and a run that would never end.

So `burn_usd` is a target for the total rather than for an amount spent, and it
carries a sign: +3 stops once $3 has been paid, -3 once $3 has been earned.
What matters here is that each is reached only from its own side, and that 0
still means no target at all.
"""

import pytest

from bulkdn.config import ConfigError, ExecutionTarget
from bulkdn.strategy import Strategy


class FakeTotals:
    def __init__(self, fees):
        self.fees_usd = fees
        self.qualifying_volume_usd = 0.0
        self.self_trade_volume_usd = 0.0


class FakeStrategy:
    """Only what _target_reached touches."""

    def __init__(self, target, fees):
        class Config:
            pass

        self.config = Config()
        self.config.target = target

        # _target_reached reads these to address the fill history. Without
        # them the AttributeError is swallowed by the "never block trading on
        # this" handler, and every test passes by returning None.
        class Session:
            http = None
            pubkey = "EXAMPLE-PUBKEY"

        self.master = Session()
        self.sub1 = Session()

    def reached(self):
        return Strategy._target_reached(self)


@pytest.fixture(autouse=True)
def totals_from_the_fixture(monkeypatch):
    """Replace the fill-history read with the number under test."""
    from bulkdn import strategy as strategy_module

    def fake(http, wallets):
        return fake.totals

    monkeypatch.setattr(strategy_module, "realised_for_tree", fake)
    return fake


def reached(target_usd, fees_usd, totals_from_the_fixture):
    totals_from_the_fixture.totals = FakeTotals(fees_usd)
    target = ExecutionTarget(cycles=0, burn_usd=target_usd)
    return FakeStrategy(target, fees_usd).reached()


# -- paying: the target is above ---------------------------------------------


def test_a_positive_target_is_reached_from_below(totals_from_the_fixture):
    assert reached(3.0, 3.5, totals_from_the_fixture)


def test_a_positive_target_is_not_reached_while_still_short(totals_from_the_fixture):
    assert reached(3.0, 2.99, totals_from_the_fixture) is None


def test_a_positive_target_is_not_reached_by_earning(totals_from_the_fixture):
    """The case from the live run: -1.44 against +3 is not progress."""
    assert reached(3.0, -1.4394, totals_from_the_fixture) is None


# -- earning: the target is below --------------------------------------------


def test_a_negative_target_is_reached_from_above(totals_from_the_fixture):
    assert reached(-3.0, -3.5, totals_from_the_fixture)


def test_a_negative_target_is_not_reached_while_still_short(totals_from_the_fixture):
    assert reached(-3.0, -2.99, totals_from_the_fixture) is None


def test_a_negative_target_is_not_reached_by_paying(totals_from_the_fixture):
    """A cycle that turned taker-heavy must not count as earning."""
    assert reached(-3.0, 5.0, totals_from_the_fixture) is None


def test_the_reason_names_both_figures(totals_from_the_fixture):
    reason = reached(-3.0, -3.5, totals_from_the_fixture)
    assert "-3.5" in reason and "-3" in reason


# -- zero is still off --------------------------------------------------------


def test_zero_disables_the_target(totals_from_the_fixture):
    assert reached(0.0, 100.0, totals_from_the_fixture) is None
    assert reached(0.0, -100.0, totals_from_the_fixture) is None


def test_zero_needs_no_fill_history():
    """Reading it costs an API call every cycle, so it must stay opt-in."""
    assert ExecutionTarget(burn_usd=0.0).measures_fills is False
    assert ExecutionTarget(burn_usd=3.0).measures_fills is True
    assert ExecutionTarget(burn_usd=-3.0).measures_fills is True


# -- the config accepts the sign ---------------------------------------------


def test_a_negative_burn_target_is_valid():
    ExecutionTarget(cycles=0, burn_usd=-3.0).validate()


def test_the_other_targets_still_refuse_a_negative():
    """Only burn has a meaning below zero."""
    with pytest.raises(ConfigError, match="cycles"):
        ExecutionTarget(cycles=-1).validate()
    with pytest.raises(ConfigError, match="volume_usd"):
        ExecutionTarget(volume_usd=-1.0).validate()

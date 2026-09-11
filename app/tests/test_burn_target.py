"""A fee target that counts in whichever direction the fees went.

The resting side of each leg earns a maker rebate and the hedge pays a taker
fee, so a pair that fills mostly on the resting side runs a NEGATIVE fee total.
A live run showed `burn progress: $-1.4394 / $3.00` -- the figure moving away
from its target every cycle, against a limit it could never reach.

The sign is a property of how the cycle happened to fill, not a decision the
operator should have to encode, so `burn_usd` stays a plain positive amount and
the comparison uses the distance from zero. $1.44 earned counts exactly as
$1.44 paid.
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

    def __init__(self, target):
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
def totals(monkeypatch):
    """Replace the fill-history read with the number under test."""
    from bulkdn import strategy as strategy_module

    def fake(http, wallets):
        return fake.value

    monkeypatch.setattr(strategy_module, "realised_for_tree", fake)
    return fake


def reached(target_usd, fees_usd, totals):
    totals.value = FakeTotals(fees_usd)
    return FakeStrategy(ExecutionTarget(cycles=0, burn_usd=target_usd)).reached()


# -- either direction counts -------------------------------------------------


def test_fees_paid_reach_the_target(totals):
    assert reached(3.0, 3.5, totals)


def test_fees_earned_reach_the_target(totals):
    """The live case: a maker-heavy pair runs negative and still finishes."""
    assert reached(3.0, -3.5, totals)


def test_the_two_directions_are_treated_alike(totals):
    assert bool(reached(3.0, 3.5, totals)) == bool(reached(3.0, -3.5, totals))


def test_exactly_on_the_target_counts(totals):
    assert reached(3.0, 3.0, totals)
    assert reached(3.0, -3.0, totals)


# -- and short is short ------------------------------------------------------


def test_short_of_the_target_while_paying(totals):
    assert reached(3.0, 2.99, totals) is None


def test_short_of_the_target_while_earning(totals):
    """-1.44 against 3 is the figure from the live run: not done yet."""
    assert reached(3.0, -1.4394, totals) is None


# -- zero is off -------------------------------------------------------------


def test_zero_disables_the_target(totals):
    assert reached(0.0, 100.0, totals) is None
    assert reached(0.0, -100.0, totals) is None


def test_zero_needs_no_fill_history():
    """Reading it costs an API call every cycle, so it must stay opt-in."""
    assert ExecutionTarget(burn_usd=0.0).measures_fills is False
    assert ExecutionTarget(burn_usd=3.0).measures_fills is True


# -- the config asks for a plain amount --------------------------------------


def test_a_negative_target_is_refused():
    """The sign belongs to the fills, not to the setting."""
    with pytest.raises(ConfigError, match="whichever way they went"):
        ExecutionTarget(burn_usd=-3.0).validate()


def test_the_other_targets_refuse_a_negative_too():
    with pytest.raises(ConfigError, match="cycles"):
        ExecutionTarget(cycles=-1).validate()
    with pytest.raises(ConfigError, match="volume_usd"):
        ExecutionTarget(volume_usd=-1.0).validate()

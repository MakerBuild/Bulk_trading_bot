"""Trading one market instead of two.

The two legs are not equally cheap. Measured across a two-hour live run,
hedging cost 1.24 bps on BTC-USD against 3.59 on ETH-USD, and the gap survives
excluding the self-trades that flatter the cheaper one. Concentrating on the
better market is worth a fifth to two fifths of the spread bill.

What makes it safe to do is that a leg is already a complete delta-neutral
pair: one account opens it, the other hedges it. Neutrality never depended on
there being two legs, only on there being two accounts.

The risk in a change like this is not the phase machine -- it is the nine
places that enumerated both legs to size, chase, cap and price them. Miss one
and a leg that has stopped trading is still being sized. So the tests below
care less about roles than about nothing downstream still naming the dropped
leg.
"""

import pytest

from bulkdn.config import Config, ConfigError, LegConfig, RiskConfig
from bulkdn.state import Phase
from bulkdn.strategy import build_chase_params, build_hedge_ceilings

BTC = "BTC-USD"
ETH = "ETH-USD"


def make(mode="multi", master_symbol=BTC, sub_symbol=ETH):
    return Config(
        sub1_pubkey="SUB",
        master_account=LegConfig(
            symbol=master_symbol, size=1.0, offset_bps=1.0,
            max_distance_bps=5.0, max_order_size=1.0,
        ),
        sub_account=LegConfig(
            symbol=sub_symbol, size=10.0, offset_bps=1.0,
            max_distance_bps=5.0, max_order_size=10.0,
        ),
        mode=mode,
        risk=RiskConfig(),
        private_key="x",
    )


# -- which legs are live ----------------------------------------------------


def test_multi_trades_both_legs():
    assert [leg.symbol for leg in make().active_legs] == [BTC, ETH]


def test_single_trades_only_the_master_leg():
    assert [leg.symbol for leg in make("single").active_legs] == [BTC]


def test_the_default_is_multi():
    """Everyone already running this has no `mode` line in their settings, and
    an update must not quietly halve what they trade."""
    assert Config.mode == "multi"


# -- and nothing downstream still names the dropped one ---------------------


def test_hedge_ceilings_cover_only_the_live_leg():
    assert set(build_hedge_ceilings(make("single"))) == {BTC}
    assert set(build_hedge_ceilings(make())) == {BTC, ETH}


def test_chase_params_cover_only_the_live_leg():
    """A dropped leg left in here would have its orders chased with nothing
    hedging the fills."""
    assert set(build_chase_params(make("single"))) == {BTC}
    assert set(build_chase_params(make())) == {BTC, ETH}


# -- the pair stays neutral on one leg --------------------------------------


class FakeSession:
    def __init__(self, pubkey):
        self.pubkey = pubkey


def roles_for(config, phase):
    from bulkdn.strategy import Strategy

    strategy = object.__new__(Strategy)
    strategy.config = config
    strategy.master = FakeSession("MASTER")
    strategy.sub1 = FakeSession("SUB1")
    return Strategy.roles_for(strategy, phase)


def test_one_leg_still_has_a_maker_and_a_hedger():
    """Which is what makes a single leg a pair rather than half of one."""
    roles = roles_for(make("single"), Phase.OPEN)
    assert len(roles) == 1
    assert roles[0].maker == "MASTER" and roles[0].taker == "SUB1"


def test_the_two_accounts_swap_on_the_way_out():
    roles = roles_for(make("single"), Phase.EXIT)
    assert roles[0].maker == "SUB1", "the account holding the short must cover it"
    assert roles[0].taker == "MASTER"
    assert roles[0].reduce_only is True


@pytest.mark.parametrize("phase", [Phase.OPEN, Phase.HOLD, Phase.EXIT])
def test_multi_mode_roles_are_exactly_what_they_were(phase):
    """The refactor that introduced single mode rewrote this function; multi
    is the behaviour thousands of cycles have already run on."""
    roles = roles_for(make(), phase)
    exiting = phase == Phase.EXIT
    assert [r.symbol for r in roles] == [BTC, ETH]
    assert roles[0].maker == ("SUB1" if exiting else "MASTER")
    assert roles[1].maker == ("MASTER" if exiting else "SUB1")
    assert all(r.reduce_only is exiting for r in roles)
    assert all(r.maker_is_buy for r in roles)


# -- and the rules about symbols follow the mode ----------------------------


def test_multi_still_refuses_two_legs_on_one_symbol():
    """Roles are held per symbol, so the second would overwrite the first and
    one leg would silently stop trading."""
    with pytest.raises(ConfigError, match="different symbols"):
        make(master_symbol=BTC, sub_symbol=BTC).validate(require_credentials=False)


def test_the_refusal_points_at_single_mode():
    """It is the thing the operator was reaching for."""
    with pytest.raises(ConfigError, match="mode: single"):
        make(master_symbol=BTC, sub_symbol=BTC).validate(require_credentials=False)


def test_single_does_not_care_what_the_unused_leg_names():
    make("single", master_symbol=BTC, sub_symbol=BTC).validate(require_credentials=False)


def test_an_unknown_mode_is_refused_rather_than_assumed():
    with pytest.raises(ConfigError, match="single"):
        make("solo").validate(require_credentials=False)

"""What the runtime hands the strategy in each mode.

The failure worth guarding here is a pairing that draws accounts the run has
no session for: the group would be drawn, the leg opened, and the first order
would go to a session that does not exist. So the pairing's pool is built from
the sessions themselves rather than from the config that produced them.
"""

import pytest

from bulkdn.config import Config, LegConfig, RiskConfig
from bulkdn.pairing import Pairing

BTC = "BTC-USD"
ETH = "ETH-USD"


class FakeSession:
    def __init__(self, name, pubkey):
        self.name = name
        self.pubkey = pubkey


def config(mode="multi", **kwargs):
    return Config(
        master_account=LegConfig(symbol=BTC, size=1.0, offset_bps=1.0,
                                 max_distance_bps=5.0, max_order_size=1.0),
        sub_account=LegConfig(symbol=ETH, size=1.0, offset_bps=1.0,
                              max_distance_bps=5.0, max_order_size=1.0),
        mode=mode,
        risk=RiskConfig(),
        private_key="x",
        **kwargs,
    )


# -- the session map -------------------------------------------------------


def test_the_pairing_draws_only_from_accounts_that_have_sessions():
    """A pubkey with no session would be drawn into a group and then have its
    first order sent to nothing."""
    sessions = [FakeSession(f"m{n}", f"key{n}") for n in range(6)]
    pairing = Pairing(pool=[s.pubkey for s in sessions], max_groups=2)

    known = {s.pubkey for s in sessions}
    for _ in range(2):
        _id, group = pairing.draw(BTC)
        assert set(group.accounts) <= known


def test_a_pool_of_one_account_cannot_pair():
    """Caught at startup rather than as a draw that never succeeds."""
    pairing = Pairing(pool=["only"], max_groups=5)
    assert pairing.draw(BTC) is None


# -- and the settings that shape it ----------------------------------------


def test_pool_mode_carries_its_caps():
    built = config("pool", max_groups=7, max_takers=3)
    built.validate(require_credentials=False)
    assert (built.max_groups, built.max_takers) == (7, 3)


def test_the_caps_default_to_five_and_one():
    built = config("pool")
    assert (built.max_groups, built.max_takers) == (5, 1)


@pytest.mark.parametrize("groups,takers", [(0, 1), (1, 0), (-1, 1)])
def test_a_cap_below_one_is_refused(groups, takers):
    from bulkdn.config import ConfigError

    built = config("pool", max_groups=groups, max_takers=takers)
    with pytest.raises(ConfigError):
        built.validate(require_credentials=False)


def test_single_and_multi_do_not_check_the_pool_caps():
    """They never draw a group, so a nonsense cap in the file is not their
    problem to report."""
    for mode in ("single", "multi"):
        config(mode, max_groups=0).validate(require_credentials=False)

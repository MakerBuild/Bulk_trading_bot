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
        markets=[
            LegConfig(symbol=BTC, size=1.0, offset_bps=1.0,
                      max_distance_bps=5.0, max_order_size=1.0),
            LegConfig(symbol=ETH, size=1.0, offset_bps=1.0,
                      max_distance_bps=5.0, max_order_size=1.0),
        ],
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


def test_the_caps_are_carried_through():
    built = config("multi", max_groups=7, max_takers=3)
    built.validate(require_credentials=False)
    assert (built.max_groups, built.max_takers) == (7, 3)


def test_the_caps_default_to_five_and_one():
    built = config("multi")
    assert (built.max_groups, built.max_takers) == (5, 1)


@pytest.mark.parametrize("groups,takers", [(0, 1), (1, 0), (-1, 1)])
def test_a_cap_below_one_is_refused(groups, takers):
    from bulkdn.config import ConfigError

    built = config("multi", max_groups=groups, max_takers=takers)
    with pytest.raises(ConfigError):
        built.validate(require_credentials=False)


def test_every_mode_checks_the_caps():
    """Both modes draw groups now -- they differ only in which accounts
    reached the pool -- so a nonsense cap is every mode's problem."""
    from bulkdn.config import ConfigError

    for mode in ("single", "multi"):
        with pytest.raises(ConfigError):
            config(mode, max_groups=0).validate(require_credentials=False)


# -- and what startup does to the accounts ----------------------------------
#
# Both of these read `(self.master, self.sub1)` -- the named pair from a time
# when there were only two accounts. A live run had six accounts in play and
# set leverage on two of them, then planned the cycle's size against those
# same two. The other four kept whatever leverage they carried, and the
# account that would actually run out of margin was never consulted.


class FakeClient:
    def __init__(self, master):
        self.account_pubkey = master


class PoolSession:
    def __init__(self, name, pubkey, client, margin, leverage):
        self.name = name
        self.pubkey = pubkey
        self.client = client
        self._margin = margin
        self._leverage = leverage

    def full_account(self):
        return {
            "margin": {"availableMargin": self._margin},
            "leverageSettings": [{"symbol": BTC, "leverage": self._leverage}],
        }


def pool_of(margins):
    """Six accounts under two keys, with the margins given."""
    clients = {1: FakeClient("k1-master"), 2: FakeClient("k2-master")}
    sessions = []
    for name, margin in margins.items():
        key = 1 if name.startswith("m1") else 2
        sessions.append(
            PoolSession(name, f"{name}-pub", clients[key], margin, leverage=10.0)
        )
    return sessions


def runtime_with(pool, config=None):
    from bulkdn.cli import Runtime

    runtime = object.__new__(Runtime)
    runtime.pool = pool
    runtime.config = config or globals()["config"]()
    runtime.symbols = [BTC]
    runtime.sessions = {s.pubkey: s for s in pool}
    runtime._key_by_master = {"k1-master": "k1", "k2-master": "k2"}
    return runtime


def test_leverage_is_set_on_every_account(monkeypatch):
    from bulkdn import cli

    built = config("multi")
    for leg in built.markets:
        leg.leverage = 40.0

    class Spec:
        max_leverage = 50.0

    class Feed:
        specs = {BTC: Spec(), ETH: Spec()}

    runtime = runtime_with(pool_of({
        "m1": 100.0, "m1s1": 100.0, "m1s2": 100.0,
        "m2": 100.0, "m2s1": 100.0, "m2s2": 100.0,
    }), built)
    runtime.feed = Feed()
    # Only BTC is enabled, so ETH must not be asked for.
    built.markets[1].enabled = False

    sent = []

    class Result:
        ok = True
        response_json = {}

    def set_leverage(*, http_url, private_key, domain, symbol, leverage, account):
        sent.append((account, private_key, leverage))
        return Result()

    monkeypatch.setattr(cli, "set_leverage", set_leverage)

    cli.Runtime.apply_leverage(runtime)

    assert [account for account, _key, _lev in sent] == [
        "m1-pub", "m1s1-pub", "m1s2-pub", "m2-pub", "m2s1-pub", "m2s2-pub",
    ]


def test_each_account_is_signed_for_by_its_own_key(monkeypatch):
    """A request signed by a key that does not own the account is refused,
    so iterating the pool with one key would have fixed nothing."""
    from bulkdn import cli

    built = config("multi")
    for leg in built.markets:
        leg.leverage = 40.0
    built.markets[1].enabled = False

    class Spec:
        max_leverage = 50.0

    class Feed:
        specs = {BTC: Spec()}

    runtime = runtime_with(pool_of({
        "m1": 100.0, "m1s1": 100.0, "m1s2": 100.0,
        "m2": 100.0, "m2s1": 100.0, "m2s2": 100.0,
    }), built)
    runtime.feed = Feed()

    sent = []

    class Result:
        ok = True
        response_json = {}

    monkeypatch.setattr(
        cli, "set_leverage",
        lambda *, http_url, private_key, domain, symbol, leverage, account: (
            sent.append((account, private_key)) or Result()
        ),
    )

    cli.Runtime.apply_leverage(runtime)

    for account, key in sent:
        assert key == ("k1" if account.startswith("m1") else "k2")


def test_the_margin_plan_sees_every_account(monkeypatch):
    """The thinnest account binds the cycle, and it is not always one of the
    first two. Six were in play and two were read."""
    from bulkdn import cli

    built = config("multi")
    built.markets[1].enabled = False

    class Spec:
        lot_size = 1e-06
        min_notional = 1.0
        tick_size = 0.01

    class Feed:
        specs = {BTC: Spec()}

        def http_price(self, _symbol):
            return 100_000.0

    runtime = runtime_with(pool_of({
        "m1": 500.0, "m1s1": 500.0, "m1s2": 500.0,
        "m2": 500.0, "m2s1": 500.0, "m2s2": 3.0,
    }), built)
    runtime.feed = Feed()

    seen = {}

    def plan_sizes(*, legs, specs, prices, available_margin, max_margin_fraction):
        seen.update(available_margin)
        raise RuntimeError("far enough")

    monkeypatch.setattr(cli, "plan_sizes", plan_sizes)
    monkeypatch.setattr(cli, "resolve_notionals", lambda **kwargs: None)

    with pytest.raises(RuntimeError, match="far enough"):
        cli.Runtime.apply_sizing(runtime)

    assert len(seen) == 6, f"only {sorted(seen)} were consulted"
    assert min(seen.values()) == 3.0, "the account that will run out was missed"


async def test_stopping_closes_every_socket_once():
    """By client, not by session: `master` and `sub1` sit on the SAME socket
    in a pool, so closing those two left every other key connected."""
    from bulkdn import cli

    closed = []

    class Session:
        def __init__(self, name, client):
            self.name = name
            self.client = client

        async def disconnect(self):
            closed.append(self.name)

    one, two = FakeClient("k1-master"), FakeClient("k2-master")
    runtime = object.__new__(cli.Runtime)
    runtime.pool = [
        Session("m1", one), Session("m1s1", one), Session("m1s2", one),
        Session("m2", two), Session("m2s1", two), Session("m2s2", two),
    ]

    await cli.Runtime.stop(runtime)

    assert closed == ["m1", "m2"], "one close per socket, and every socket"


async def test_starting_opens_every_socket_once():
    """`master` and `sub1` are BOTH on the first key's socket in a pool, so
    connecting those two never dialled any other key. A live flatten said so:
    `m2: cancel-all failed: not connected to WebSocket`, with no drop before
    it, because that socket had never come up."""
    from bulkdn import cli

    opened = []

    class Session:
        def __init__(self, name, client):
            self.name = name
            self.client = client

        async def connect(self):
            opened.append(self.name)

    one, two = FakeClient("k1-master"), FakeClient("k2-master")
    runtime = object.__new__(cli.Runtime)
    runtime.pool = [
        Session("m1", one), Session("m1s1", one), Session("m1s2", one),
        Session("m2", two), Session("m2s1", two), Session("m2s2", two),
    ]
    runtime.market_data = Session("market", FakeClient("market"))

    await cli.Runtime._connect_all(runtime)

    # The market data socket is its own, so fills never queue behind the book.
    assert opened == ["m1", "m2", "market"], "one dial per socket, and every socket"

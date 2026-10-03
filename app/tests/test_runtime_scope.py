"""What a Runtime may do to the Config it is given, and which accounts it holds.

Two things were wrong. A run resized the caller's own Config -- the sizing plan
and the per-cycle redraw both write into the legs -- so every caller had to
deep-copy before handing one over, and the menu's dry run once set the live
run's sizes. And status, check and flatten were built from the TRADING
runtime: single mode narrowed it to one master, so Close All left every other
master's positions open and then reset the state as if they were not there,
and the "needs two accounts" rule refused even to close a master that had no
sub-account yet.
"""

import asyncio

import base58
import pytest
from bulk_api.common.signer import TransactionSigner

from bulkdn import cli
from bulkdn.config import Config, ConfigError, LegConfig, RiskConfig

BTC = "BTC-USD"


def key(n: int) -> str:
    """A throwaway signing key: n repeated, which signs for nobody."""
    return base58.b58encode(bytes([n]) * 32).decode()


class Client:
    def __init__(self, master):
        self.account_pubkey = master


class Session:
    def __init__(self, name, pubkey, client):
        self.name = name
        self.pubkey = pubkey
        self.client = client
        self.http = None

    def full_account(self):
        return {"margin": {"availableMargin": 1000.0}}


class Feed:
    def __init__(self, *_a, **_k):
        self.specs = {BTC: type("Spec", (), {
            "lot_size": 1e-6, "min_notional": 1.0, "tick_size": 0.1, "max_leverage": 50,
        })()}

    def http_price(self, _symbol):
        return 100_000.0


@pytest.fixture
def wired(monkeypatch):
    """Runtime.__init__ as it really runs, with the network taken out.

    `subs` says how many sub-accounts each key's master owns.
    """
    seen = {}

    def build(subs):
        def build_pool(*, private_keys, **_kwargs):
            seen["keys"] = list(private_keys)
            pool = []
            for index, signing in enumerate(private_keys, start=1):
                master = TransactionSigner(signing).public_key
                client = Client(master)
                pool.append(Session(f"m{index}", master, client))
                pool += [
                    Session(f"m{index}s{n}", f"SUB-{index}-{n}", client)
                    for n in range(1, subs[index - 1] + 1)
                ]
            return pool

        monkeypatch.setattr(cli, "build_pool", build_pool)
        return seen

    monkeypatch.setattr(cli, "apply_ws_compat", lambda **_k: None)
    monkeypatch.setattr(cli, "market_data_session", lambda **_k: object())
    monkeypatch.setattr(cli, "MarketFeed", Feed)
    return build


TWO_KEYS = (key(1), key(2))


def config(mode="multi", keys=TWO_KEYS):
    return Config(
        markets=[LegConfig(symbol=BTC, notional_usd=100.0, max_order_notional_usd=100.0)],
        mode=mode,
        risk=RiskConfig(),
        private_keys=list(keys),
    )


# -- the caller's Config is left alone ----------------------------------------


def test_sizing_a_run_does_not_resize_the_callers_config(wired):
    wired([1, 1])
    given = config()
    runtime = cli.Runtime(given, dry_run=True)

    runtime.apply_sizing()

    assert runtime.config.markets[0].size > 0, "the run's own copy was sized"
    assert given.markets[0].size == 0.0, "the caller's Config was resized"
    assert given.markets[0].max_order_size == 0.0


def test_each_runtime_gets_its_own_copy(wired):
    wired([1, 1])
    given = config()
    first, second = cli.Runtime(given, dry_run=True), cli.Runtime(given, dry_run=True)
    assert first.config is not given and second.config is not first.config


# -- closing and looking use every key ----------------------------------------


def test_single_mode_trades_one_master(wired):
    seen = wired([1, 1])
    cli.Runtime(config("single"), dry_run=True)
    assert seen["keys"] == [key(1)]


def test_single_mode_still_closes_every_master(wired):
    seen = wired([1, 1])
    runtime = cli.Runtime(config("single"), dry_run=True, trading=False)
    assert seen["keys"] == [key(1), key(2)]
    assert len(runtime.pool) == 4


def test_a_master_without_a_sub_account_cannot_trade(wired):
    wired([0])
    with pytest.raises(ConfigError, match="at least two accounts"):
        cli.Runtime(config(keys=[key(1)]), dry_run=True)


def test_a_master_without_a_sub_account_can_still_be_closed(wired):
    wired([0])
    runtime = cli.Runtime(config(keys=[key(1)]), dry_run=True, trading=False)
    assert runtime.sub1 is None and len(runtime.pool) == 1


@pytest.mark.parametrize("command", ["flatten", "status", "check"])
def test_the_closing_and_looking_commands_ask_for_every_key(monkeypatch, command):
    asked = []

    class Stop(Exception):
        pass

    def runtime(*_a, trading=True, **_k):
        asked.append(trading)
        raise Stop

    monkeypatch.setattr(cli, "Runtime", runtime)
    run = {
        "flatten": lambda: cli.cmd_flatten(config(), dry_run=True),
        "status": lambda: cli.cmd_status(config()),
        "check": lambda: cli.cmd_check(config()),
    }[command]
    with pytest.raises(Stop):
        asyncio.run(run())
    assert asked == [False]


def test_check_says_a_lone_account_cannot_pair_rather_than_refusing(
    wired, monkeypatch, capsys
):
    wired([0])

    class Specs(Feed):
        def load_specs(self, strict=True):
            return None

    monkeypatch.setattr(cli, "MarketFeed", Specs)
    assert asyncio.run(cli.cmd_check(config(keys=[key(1)]))) == 1
    assert "a run needs two to pair" in capsys.readouterr().out

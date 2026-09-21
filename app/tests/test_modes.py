"""Which ACCOUNTS trade together, and which MARKETS they trade.

These were one setting and are now two, because they were never the same
question. `mode` used to mean "one market or two"; it now means "one master's
accounts, or every master's". The markets are a list, and each one carries a
switch.

The split matters for a reason that is not tidiness. A trade between two
accounts under one master is a self-trade: the exchange can see one owner on
both sides, and its own documentation excludes those from fee-tier volume. A
trade between accounts under two different masters is not a self-trade to
anyone, because nothing on the exchange links the two masters. That is what
`multi` buys, and it has nothing to do with how many markets are open.
"""

import pytest

from bulkdn.config import Config, ConfigError, LegConfig, RiskConfig

BTC = "BTC-USD"
ETH = "ETH-USD"
SOL = "SOL-USD"


def market(symbol, enabled=True):
    return LegConfig(
        symbol=symbol, size=1.0, offset_bps=1.0,
        max_distance_bps=5.0, max_order_size=1.0, enabled=enabled,
    )


def make(mode="multi", markets=(BTC, ETH), keys=("k1",), single_master=1):
    return Config(
        sub1_pubkey="SUB",
        markets=[m if isinstance(m, LegConfig) else market(m) for m in markets],
        mode=mode,
        single_master=single_master,
        risk=RiskConfig(),
        private_keys=list(keys),
    )


# -- the markets are a list, and each one has a switch ----------------------


def test_every_enabled_market_trades_in_either_mode():
    """The mode is about accounts. It does not drop a market any more."""
    for mode in ("single", "multi"):
        assert [leg.symbol for leg in make(mode).active_legs] == [BTC, ETH]


def test_more_than_two_markets_is_ordinary():
    assert len(make(markets=(BTC, ETH, SOL)).active_legs) == 3


def test_one_market_is_ordinary_too():
    """Which is what the old single mode was really for."""
    assert [leg.symbol for leg in make(markets=(BTC,)).active_legs] == [BTC]


def test_a_switched_off_market_keeps_its_settings():
    """Off rather than deleted, so turning it back on restores the numbers."""
    config = make(markets=(market(BTC), market(ETH, enabled=False)))

    assert [leg.symbol for leg in config.active_legs] == [BTC]
    assert [leg.symbol for leg in config.markets] == [BTC, ETH]


def test_everything_switched_off_is_refused():
    config = make(markets=(market(BTC, enabled=False),))

    with pytest.raises(ConfigError, match="switched off"):
        config.validate(require_credentials=False)


def test_two_live_markets_on_one_symbol_are_refused():
    """A leg is held per symbol, so the second would overwrite the first."""
    with pytest.raises(ConfigError, match="same symbol"):
        make(markets=(BTC, BTC)).validate(require_credentials=False)


def test_a_duplicate_that_is_switched_off_is_not_a_clash():
    make(markets=(market(BTC), market(BTC, enabled=False))).validate(
        require_credentials=False
    )


# -- the mode picks keys ----------------------------------------------------


def test_the_default_is_multi():
    """Nobody's settings file has to change to keep every account trading."""
    assert Config.mode == "multi"


def test_multi_plays_every_key():
    from bulkdn.cli import Runtime

    assert Runtime._keys_in_play(make("multi", keys=("k1", "k2", "k3"))) == [
        "k1", "k2", "k3",
    ]


def test_single_plays_the_chosen_master_only():
    from bulkdn.cli import Runtime

    config = make("single", keys=("k1", "k2", "k3"), single_master=2)

    assert Runtime._keys_in_play(config) == ["k2"]


def test_single_defaults_to_the_first_master():
    from bulkdn.cli import Runtime

    assert Runtime._keys_in_play(make("single", keys=("k1", "k2"))) == ["k1"]


def test_a_master_that_is_not_there_is_refused():
    """Better here than as an IndexError three calls further in."""
    config = make("single", keys=("k1", "k2"), single_master=5)

    with pytest.raises(ConfigError, match="holds 2 key"):
        config.validate(require_credentials=False)


def test_single_master_counts_from_one():
    with pytest.raises(ConfigError, match="line number"):
        make("single", single_master=0).validate(require_credentials=False)


def test_an_unknown_mode_is_refused_rather_than_assumed():
    with pytest.raises(ConfigError, match="single"):
        make("solo").validate(require_credentials=False)


# -- and the settings file, in both spellings -------------------------------


MARKETS_FILE = """
mode: {mode}
single_master: {master}
markets:
  - symbol: BTC-USD
    notional_usd: 4000
    max_order_notional_usd: 500
  - symbol: ETH-USD
    notional_usd: 1500
    max_order_notional_usd: 500
    enabled: {eth}
"""

LEGACY_FILE = """
mode: {mode}
legs:
  master_account:
    symbol: BTC-USD
    notional_usd: 4000
    max_order_notional_usd: 500
  sub_account:
    symbol: ETH-USD
    notional_usd: 1500
    max_order_notional_usd: 500
"""


def load(tmp_path, text):
    from bulkdn.config import load_config

    path = tmp_path / "settings.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(str(path), require_credentials=False, require_sub1=False)


def test_a_markets_list_is_read_in_order(tmp_path):
    config = load(tmp_path, MARKETS_FILE.format(mode="multi", master=1, eth="true"))

    assert [leg.symbol for leg in config.active_legs] == [BTC, ETH]
    assert config.markets[0].notional_usd == 4000


def test_a_market_can_be_switched_off_in_the_file(tmp_path):
    config = load(tmp_path, MARKETS_FILE.format(mode="multi", master=1, eth="false"))

    assert [leg.symbol for leg in config.active_legs] == [BTC]


def test_single_master_is_read(tmp_path):
    config = load(tmp_path, MARKETS_FILE.format(mode="single", master=2, eth="true"))

    assert config.mode == "single" and config.single_master == 2


def test_an_old_legs_block_still_works(tmp_path):
    """A subscriber's settings file must not stop the bot because its shape
    was superseded between one update and the next."""
    config = load(tmp_path, LEGACY_FILE.format(mode="multi"))

    assert [leg.symbol for leg in config.active_legs] == [BTC, ETH]
    assert config.markets[1].notional_usd == 1500


def test_an_old_file_saying_pool_is_read_as_multi(tmp_path):
    """`pool` was this mode's name while it was the third of three."""
    assert load(tmp_path, LEGACY_FILE.format(mode="pool")).mode == "multi"


def test_a_file_naming_no_market_at_all_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="markets"):
        load(tmp_path, "mode: multi\n")


# -- and from the command line ----------------------------------------------


@pytest.mark.parametrize("mode", ["single", "multi", "pool"])
def test_the_flag_accepts_every_mode_there_is(mode):
    from bulkdn.cli import build_parser

    assert build_parser().parse_args(["--mode", mode, "run"]).mode == mode


def test_the_flag_refuses_anything_else():
    from bulkdn.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["--mode", "solo", "run"])


def test_the_flags_old_name_lands_on_multi(tmp_path):
    from bulkdn.config import load_config

    path = tmp_path / "settings.yaml"
    path.write_text(LEGACY_FILE.format(mode="single"), encoding="utf-8")
    config = load_config(
        str(path), require_credentials=False, require_sub1=False, mode="pool"
    )

    assert config.mode == "multi"


# -- and a block may be called anything -------------------------------------
#
# The names used to mean something: they said which account opened which leg.
# They have not for a while -- accounts are drawn per cycle -- so a block is
# named whatever its author found clearest.
#
# `legs.btc` and `legs.sol` used to be refused outright, on the grounds that a
# file using them would silently start with default legs. Nothing defaults
# now, and the refusal had become a trap: a market added from the menu was
# named after its coin, so adding SOL-USD wrote a `sol:` block and the next
# start was turned away by a message about a rename from two versions ago.


COIN_NAMED = """
mode: multi
legs:
  btc:
    symbol: BTC-USD
    notional_usd: 100
  sol:
    symbol: SOL-USD
    notional_usd: 100
"""

THREE_MARKETS = """
mode: multi
legs:
  master_account:
    symbol: BTC-USD
    notional_usd: 100
  sub_account:
    symbol: ETH-USD
    notional_usd: 100
  sol_usd:
    symbol: SOL-USD
    notional_usd: 100
    enabled: false
"""


def test_a_block_named_after_its_coin_is_read(tmp_path):
    config = load(tmp_path, COIN_NAMED)

    assert [leg.symbol for leg in config.active_legs] == [BTC, SOL]


def test_a_third_market_can_be_added_to_a_legs_block(tmp_path):
    """Which is what the menu does to a file it did not write."""
    config = load(tmp_path, THREE_MARKETS)

    assert [leg.symbol for leg in config.markets] == [BTC, ETH, SOL]
    assert [leg.symbol for leg in config.active_legs] == [BTC, ETH]


def test_an_error_names_the_block_the_operator_will_find(tmp_path):
    broken = THREE_MARKETS.replace("    symbol: SOL-USD\n", "")

    with pytest.raises(ConfigError, match="legs.sol_usd"):
        load(tmp_path, broken)


def test_an_empty_legs_block_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="markets"):
        load(tmp_path, "mode: multi\nlegs: {}\n")

"""`time_in_force`: ALO, or the ALO_JOIN / ALO_SLIDE of API v1.0.20.

ALO refuses a maker order that would cross by the time it arrives, and the
chaser re-prices on its next pass. ALO_JOIN rests it at the best price on its
own side instead, ALO_SLIDE at the nearest tick that does not cross. ALO stays
the default -- every measured run used it, and a change of fill behaviour is
judged by comparing runs.
"""

import struct
import types
from enum import Enum

import pytest

from bulk_api.common import Side, TimeInForce
from bulk_api.common.signer import TransactionSigner
from bulk_api.messages.trade import LimitOrder

from bulkdn import accounts, strategy
from bulkdn.config import ConfigError, LegConfig, load_config
from bulkdn.strategy import build_chase_params

BASE = """\
markets:
  - symbol: BTC-USD
    notional_usd: 100
{extra}
"""


def load(tmp_path, extra=""):
    path = tmp_path / "settings.yaml"
    path.write_text(BASE.format(extra=extra), encoding="utf-8")
    return load_config(str(path), require_credentials=False)


# -- the setting --------------------------------------------------------------


def test_alo_is_the_default(tmp_path):
    assert load(tmp_path).markets[0].time_in_force == "ALO"


@pytest.mark.parametrize("written", ["ALO_JOIN", "alo_join", " Alo_Join "])
def test_join_is_read_however_it_is_cased(tmp_path, written):
    config = load(tmp_path, f"    time_in_force: {written}")
    assert config.markets[0].time_in_force == "ALO_JOIN"


def test_anything_else_is_refused_by_name(tmp_path):
    with pytest.raises(ConfigError, match="time_in_force"):
        load(tmp_path, "    time_in_force: GTC")


def test_it_reaches_the_chaser(tmp_path):
    config = load(tmp_path, "    time_in_force: ALO_SLIDE")
    assert build_chase_params(config)["BTC-USD"].time_in_force == "ALO_SLIDE"


def test_an_sdk_without_the_new_values_is_refused_before_the_run(monkeypatch, tmp_path):
    """The GitHub copy the installer falls back to predates them."""
    class OldTimeInForce(Enum):
        GTC = "GTC"
        IOC = "IOC"
        ALO = "ALO"

    monkeypatch.setattr(strategy, "TimeInForce", OldTimeInForce)
    config = load(tmp_path, "    time_in_force: ALO_JOIN")
    with pytest.raises(ConfigError, match="installer"):
        build_chase_params(config)

    assert build_chase_params(load(tmp_path))["BTC-USD"].time_in_force == "ALO"


# -- the SDK that ships with the bot -------------------------------------------


@pytest.mark.parametrize("tif, index", [
    (TimeInForce.ALO, 2), (TimeInForce.ALO_SLIDE, 3), (TimeInForce.ALO_JOIN, 4),
])
def test_the_vendored_sdk_signs_each_value_with_the_exchanges_index(tif, index):
    """Indices from the exchange's signing reference: 2=ALO, 3=ALO_SLIDE,
    4=ALO_JOIN. A wrong one is `bad signature` on every maker order."""
    action = LimitOrder(
        symbol="BTC-USD", side=Side.BUY, price=100_000.0, size=1.0, time_in_force=tif,
    ).to_api()
    assert action["l"]["tif"] == tif.value
    assert TransactionSigner.serialize_action(action)[36:40] == struct.pack("<I", index)


# -- the order --------------------------------------------------------------------


async def test_place_limit_sends_the_time_in_force_it_was_given():
    sent = []

    async def submit(actions, **kwargs):
        # Stamped as the real submit stamps them, so the order has an id.
        for action in actions:
            action.seqno, action.nonce = 1, 1
            action.pubkey = "11111111111111111111111111111111"
        sent.extend(actions)
        return []

    session = accounts.AccountSession(
        name="m1", pubkey="ZZZZ", client=types.SimpleNamespace(),
        http=types.SimpleNamespace(),
    )
    session.submit = submit
    await session.place_limit(
        symbol="BTC-USD", is_buy=True, price=100_000.0, size=0.01,
        time_in_force="ALO_JOIN",
    )
    (order,) = sent
    assert order.time_in_force is TimeInForce.ALO_JOIN


def test_the_shipped_template_keeps_alo():
    assert LegConfig.__dataclass_fields__["time_in_force"].default == "ALO"

"""A Strategy built through its own __init__, from inert parts.

Tests that exercise one method used to build a Strategy with
`object.__new__(Strategy)` and set by hand only the attributes that method
touched. Every attribute added to `__init__` afterwards was then missing from
those objects, and production code grew `getattr(self, name, default)` and
`self.__dict__.setdefault(...)` to survive them -- defaults that existed only
for the tests, and that could disagree with the real ones in `__init__`.

`bare_strategy()` runs the real `__init__`, so every attribute exists with its
real default, and the test then replaces whatever it needs to control:

    obj = bare_strategy()
    obj.sessions = {...}
    obj._roles_for_key = lambda key: ...
"""

import types

from bulkdn.config import Config, LegConfig, RiskConfig
from bulkdn.positions import PositionBook
from bulkdn.state import StrategyState
from bulkdn.strategy import Strategy


class _NoStore:
    """A state store that keeps nothing."""

    def save(self, state) -> None:
        pass

    async def save_async(self, state) -> None:
        pass


def bare_strategy(**attrs) -> Strategy:
    """A properly initialised Strategy over inert parts, with `attrs` set on it."""
    config = Config(
        markets=[
            LegConfig(
                symbol="BTC-USD", size=1.0, offset_bps=1.0,
                max_distance_bps=5.0, max_order_size=1.0,
            ),
        ],
        risk=RiskConfig(),
        private_key="x",
    )
    master = types.SimpleNamespace(name="master", pubkey="MASTER-PUB", dry_run=True)
    sub1 = types.SimpleNamespace(name="sub1", pubkey="SUB1-PUB", dry_run=True)
    strategy = Strategy(
        config=config,
        master=master,
        sub1=sub1,
        feed=types.SimpleNamespace(specs={}),
        book=PositionBook(),
        hedger=None,
        chaser=None,
        risk=None,
        store=_NoStore(),
        state=StrategyState(),
    )
    for name, value in attrs.items():
        setattr(strategy, name, value)
    return strategy

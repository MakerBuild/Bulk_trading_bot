"""Delta-neutral trading bot for BULK.

Runs a hedged BTC/SOL cycle across a master account and one sub-account, where
every fill on a resting limit order is immediately offset by a market order on
the other account.
"""

__version__ = "0.1.0"

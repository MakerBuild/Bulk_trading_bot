"""Delta-neutral trading bot for BULK.

Trades every market enabled in the settings across a pool of accounts: every
master key in private_key.local (one per line) and every sub-account under
each of them, found from the master at startup. `mode: single` narrows the pool
to one master's own tree. Each cycle draws a group from the pool -- one account
rests a limit order, one or more others hedge each of its fills with market
orders, in fixed shares -- and the group disbands when the cycle ends.
"""

__version__ = "0.1.0"

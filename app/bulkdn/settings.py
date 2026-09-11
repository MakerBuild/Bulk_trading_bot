"""Per-symbol leverage, via the `updateUserSettings` action.

Two reasons this does not use the SDK's `update_leverage`.

**It signs for the signer's own account only**, so it cannot reach a
sub-account. Sub-accounts copy the master's settings when they are created and
are independent from then on, so each one has to be set explicitly.

**It sends `m` as a list of pairs.** The API reference describes `m` as a map
of symbol to leverage, and the Rust reference client declares it as
`HashMap<String, f64>` renamed to `m` -- a JSON object. A JSON array of pairs
is not a map and would fail to deserialize. The signing bytes are still taken
from the SDK's serializer, which walks the pairs in order; only the JSON shape
is corrected here.

One symbol per transaction, deliberately. The signed bytes are the map's
entries in iteration order, and a multi-entry map has no order the sender and
the receiver are guaranteed to agree on.
"""

from __future__ import annotations

from dataclasses import dataclass

from bulk_api.common.signer import SignatureDomain, TransactionSigner

from .tx import accepted, sign_and_submit

UPDATE_USER_SETTINGS_ORDINAL = 18

# The action's own bound, from the API reference.
MIN_LEVERAGE = 1.0
MAX_LEVERAGE = 50.0


def serialize_leverage(symbol: str, leverage: float) -> bytes:
    """Wincode bytes for a single-symbol `updateUserSettings`.

    Produced by the SDK's own serializer rather than by hand, so the layout
    cannot drift from it.
    """
    return TransactionSigner.serialize_action(
        {"updateUserSettings": {"m": [(symbol, float(leverage))]}}
    )


@dataclass
class LeverageResult:
    account: str
    symbol: str
    leverage: float
    response_status: int
    response_json: dict

    @property
    def ok(self) -> bool:
        return accepted(self.response_status, self.response_json)


def set_leverage(
    *,
    http_url: str,
    private_key: str,
    domain: SignatureDomain,
    symbol: str,
    leverage: float,
    account: str | None = None,
    nonce: int | None = None,
) -> LeverageResult:
    """Set max leverage for one symbol on one account."""
    if not MIN_LEVERAGE <= leverage <= MAX_LEVERAGE:
        raise ValueError(
            f"leverage must be {MIN_LEVERAGE}-{MAX_LEVERAGE}, got {leverage}"
        )

    _, status, body = sign_and_submit(
        http_url=http_url,
        private_key=private_key,
        domain=domain,
        action_bytes=serialize_leverage(symbol, leverage),
        # Map, per the reference -- not the SDK's list of pairs.
        action_json={"updateUserSettings": {"m": {symbol: float(leverage)}}},
        account=account,
        nonce=nonce,
    )
    return LeverageResult(
        account=account or TransactionSigner(private_key).public_key,
        symbol=symbol,
        leverage=float(leverage),
        response_status=status,
        response_json=body,
    )


def current_leverage(account_state: dict) -> dict[str, float]:
    """Read `leverageSettings` out of a `fullAccount` body.

    The field is a list of per-symbol rows; shape varies between a mapping and
    a pair, so both are accepted rather than assuming one.
    """
    out: dict[str, float] = {}
    for row in account_state.get("leverageSettings") or []:
        if isinstance(row, dict):
            symbol = row.get("symbol") or row.get("s")
            value = row.get("maxLeverage", row.get("leverage", row.get("m")))
            if symbol is not None and value is not None:
                out[symbol] = float(value)
        elif isinstance(row, (list, tuple)) and len(row) == 2:
            out[str(row[0])] = float(row[1])
    return out

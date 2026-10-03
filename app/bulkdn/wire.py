"""How the exchange spells the things more than one module reads.

A fill arrives on the socket and again in the HTTP history, and an account is
read from `/account` by the session, the menu and the fee walk. Each reader
used to carry its own idea of the wire shape, and they drifted: the socket
accepted a side as 0/1 or "true" while the fee walk took only a bool, and the
fee walk knew that mainnet history identifies a trade by `slot` and `sequence`
while the socket read only `tradeId` -- which mainnet does not send, so the
fill stream's replay guard keyed every fill on nothing.

One spelling of each lives here, and every reader uses it.
"""

from __future__ import annotations

from typing import Any


def first(row: dict, *names: str, default: Any = None) -> Any:
    """The first of `names` present in `row` with a value, else `default`."""
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return default


# A `side` string, in the spellings an exchange tends to use.
SIDE_WORDS = {"buy": True, "b": True, "bid": True,
              "sell": False, "s": False, "a": False, "ask": False}


def is_buy(row: dict) -> bool | None:
    """True for a buy, False for a sell, None when the row does not say.

    `isBuy` (or its short form `b`) is the side of the account whose fill or
    history row it is. Taken as a bool, as 0/1, or as the strings "true" and
    "false"; failing those, a `side` word. Never guessed: a row that does not
    say is None, and each caller decides what that means for it.
    """
    flag = first(row, "isBuy", "b")
    if isinstance(flag, bool):
        return flag
    if isinstance(flag, (int, float)) and flag in (0, 1):
        return bool(flag)
    if isinstance(flag, str) and flag.strip().lower() in ("true", "false"):
        return flag.strip().lower() == "true"
    word = first(row, "side")
    if isinstance(word, str):
        return SIDE_WORDS.get(word.strip().lower())
    return None


def trade_id(row: dict) -> str | None:
    """The trade a fill or history row belongs to, as one string, or None.

    `slot` and `sequence` together, as `"<slot>:<sequence>"` -- the format the
    SDK's own `TradeId` parses `tradeId` from, so a row carrying either shape
    names the trade the same way. Mainnet history sends the two integers and
    no `tradeId`; they were verified unique across a live history (1830 rows,
    1465 trades, no collisions). Failing those, `tradeId` or its short form.
    """
    slot, sequence = row.get("slot"), row.get("sequence")
    if slot is not None and sequence is not None:
        return f"{slot}:{sequence}"
    value = first(row, "tradeId", "tid")
    if value is None or value == "":
        return None
    return str(value)


def unwrap(payload: Any, key: str) -> dict:
    """An `/account` answer flattened to the body under `key`.

    Observed shapes: `{key: {...}}`, `[{key: {...}}]`, and a bare `{...}`. All
    three are accepted so a change in envelope does not silently turn a
    funded account into an apparently empty one. Anything that is not a dict
    after unwrapping is `{}`.
    """
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict):
        return {}
    inner = payload.get(key)
    if isinstance(inner, dict):
        return inner
    return payload


def unwrap_full_account(payload: Any) -> dict:
    """Flatten a `/account` `fullAccount` response down to the account body.

    Reading the outer envelope instead silently yields no positions and no
    sub-accounts -- which looks exactly like a flat account.
    """
    return unwrap(payload, "fullAccount")


def page(body: Any) -> tuple[list[dict], str | None]:
    """The rows of a paged answer, and the cursor for the next page if any.

    A history query answers with `{data, page}`; a current-state query with a
    bare list. Both are accepted so a shape change degrades to "no rows"
    rather than an exception.
    """
    if isinstance(body, list):
        return [row for row in body if isinstance(row, dict)], None
    if not isinstance(body, dict):
        return [], None
    rows = [row for row in (body.get("data") or []) if isinstance(row, dict)]
    paging = body.get("page") or {}
    if not isinstance(paging, dict):
        return rows, None
    return rows, paging.get("nextCursor") or paging.get("next_cursor")

"""Account sessions and sub-account order routing.

Two problems with the stock SDK have to be solved here.

**Routing.** `BulkWebSocketClient.place_orders` hardcodes
`account = self.signer.public_key`, so it can only ever trade the signing
account. The wire protocol is more capable: `account` names the account being
acted on and `signer` names who authorises it, and a master is allowed to sign
for its own sub-accounts. `TransactionSigner.sign_transaction` reads both
fields straight off the transaction dict, so overriding the builder is enough --
the signing path itself is untouched.

**State separation.** Account stream updates are not tagged with the account
they belong to, so a single client subscribed to two accounts would merge
Master's and Sub1's positions into one book. Each account therefore gets its own
client, and handlers close over which one they belong to.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable, Sequence

import requests
from bulk_api import BulkWebSocketClient
from bulk_api.api.bulk_http import BulkHttpClient
from .retry import describe
from bulk_api.common import (
    OrderStatus,
    Side,
    SignatureDomain,
    TimeInForce,
    Topic,
    TransactionSigner,
)
from bulk_api.messages.trade import CancelAll, CancelOrder, LimitOrder, MarketOrder, OrderResponse

Action = Any  # LimitOrder | MarketOrder | CancelOrder | CancelAll

log = logging.getLogger(__name__)


class OrderRejected(Exception):
    """Raised when the exchange rejects an action inside a submitted batch."""

    def __init__(self, message: str, responses: Sequence[OrderResponse] | None = None):
        super().__init__(message)
        self.responses = list(responses or [])


class RoutedWsClient(BulkWebSocketClient):
    """A WS client bound to one account, which may differ from the signer.

    `account_pubkey` is the account being traded. For the master session it
    equals the signer's pubkey; for a sub-account session it is the child's
    pubkey and the master signs on its behalf.
    """

    def __init__(self, *args, account_pubkey: str | None = None, dry_run: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.account_pubkey = account_pubkey or (self.signer.public_key if self.signer else None)
        self.dry_run = dry_run
        self.last_message_at: float = time.monotonic()
        # Set only while `connect` has the signer hidden from the base class.
        self._hidden_signer = None

    # -- connection --------------------------------------------------------

    async def connect(self) -> bool:
        """Connect, subscribing to *this* client's account rather than the signer's.

        The base implementation auto-subscribes to `signer.public_key`, which is
        wrong for a sub-account session. Hiding the signer for the duration of
        the call suppresses that branch; the account subscription is then issued
        explicitly. On reconnect the stored subscription list is replayed
        instead, so this only applies to the first connect.
        """
        had_subscriptions = bool(self.subscriptions)
        # Hidden, not discarded: `_signing_key` still sees it, so a submission
        # that lands inside this window signs normally instead of failing with
        # "signer not configured". A live run hit exactly that while the
        # emergency stop tried to cancel orders during a reconnect.
        self._hidden_signer = self.signer
        self.signer = None
        try:
            connected = await super().connect()
        finally:
            self.signer = self._hidden_signer
            self._hidden_signer = None

        if connected:
            # A socket that has just come up has been silent for zero seconds.
            # The stale watchdog measures silence from this field, and leaving
            # it at the DEAD socket's last message makes a successful reconnect
            # still read as the failure it just fixed. That ended a three-hour
            # run one second after "WebSocket reconnected on attempt 1": the
            # heal worked, the re-check saw 31s of silence belonging to a socket
            # that no longer existed, and the kill switch fired anyway.
            self.last_message_at = time.monotonic()

        if connected and not had_subscriptions and self.account_pubkey:
            await self.subscribe_account(self.account_pubkey)
        return connected

    async def _handle_message(self, data: dict) -> None:
        # Liveness is tracked off raw traffic so the risk layer can tell a quiet
        # market from a dead socket.
        self.last_message_at = time.monotonic()
        await super()._handle_message(data)

    # -- routed submission -------------------------------------------------

    @property
    def _signing_key(self):
        """The key that signs, whether or not `connect` is hiding it."""
        return self.signer or self._hidden_signer

    async def submit(
        self,
        actions: Sequence[Action],
        timeout: float | None = None,
        nonce: int | None = None,
    ) -> list[OrderResponse]:
        """Sign and submit a batch of actions against this client's account.

        Mirrors the SDK's `place_orders` but sets `account` to the routed
        account while leaving `signer` as the key that actually signs. Actions
        also carry `pubkey`, which feeds the client-side order-ID hash -- it
        must be the routed account or the computed IDs won't match the
        exchange's.
        """
        signer = self._signing_key
        if not signer:
            raise RuntimeError("signer not configured")
        if not self.account_pubkey:
            raise RuntimeError("account_pubkey not configured")

        account = self.account_pubkey
        if nonce is None:
            nonce = int(time.time_ns())

        payload_actions = []
        for index, action in enumerate(actions):
            action.seqno = index
            action.nonce = nonce
            action.pubkey = account
            payload_actions.append(action.to_api())

        tx = {
            "actions": payload_actions,
            "nonce": f"{nonce}",
            "account": account,
            "signer": signer.public_key,
        }

        if self.dry_run:
            log.info(
                "[dry-run] would submit to %s: %s",
                short_pubkey(account),
                " | ".join(str(a) for a in actions),
            )
            return [
                OrderResponse(
                    order_id=_safe_order_id(action),
                    status=OrderStatus.RESTING,
                    message="dry-run",
                    meta={"dry_run": True},
                )
                for action in actions
            ]

        if not self.is_connected:
            raise RuntimeError("not connected to WebSocket")

        tx = signer.sign_transaction(tx, self.signature_domain)

        self.request_id += 1
        request_id = self.request_id
        request = {
            "method": "post",
            "request": {"type": "action", "payload": tx},
            "id": request_id,
        }

        future: asyncio.Future = asyncio.Future()
        self.pending_requests[request_id] = future
        try:
            await self.ws.send(json.dumps(request))
            return await asyncio.wait_for(
                future, timeout=timeout if timeout is not None else self.default_timeout
            )
        except Exception:
            # Only the failure path cleans up: on success the SDK's message
            # handler pops the entry when it resolves the future.
            self.pending_requests.pop(request_id, None)
            raise


@dataclass
class AccountSession:
    """One account: its WS client, its label, and its cached market specs."""

    name: str
    pubkey: str
    client: RoutedWsClient
    http: BulkHttpClient
    dry_run: bool = False
    reject_streak: int = 0
    # What the exchange said about the most recent counted rejection. The
    # streak alone says a kill switch fired; this says why, which is the part
    # anyone reading the halt an hour later actually needs.
    last_reject: str = ""
    # Symbols whose most recent submission never came back with an answer,
    # and when that happened. A request that times out may still have executed
    # -- that is what a timeout means here -- so until a fresh position read
    # says otherwise, any change in these symbols might be our own doing.
    # Without this the liquidation guard reads our own unacknowledged fill as
    # someone else closing the position, which is the opposite conclusion.
    unconfirmed: dict[str, float] = field(default_factory=dict)

    def symbols_in_doubt(self, within_s: float) -> set[str]:
        """Symbols with a submission whose outcome is still unknown."""
        now = time.monotonic()
        return {
            symbol
            for symbol, at in self.unconfirmed.items()
            if now - at <= within_s
        }

    def settled(self, symbol: str) -> None:
        """A fresh authoritative read covers whatever was in doubt here."""
        self.unconfirmed.pop(symbol, None)

    async def connect(self) -> None:
        if not await self.client.connect():
            raise RuntimeError(f"{self.name}: failed to connect to WebSocket")
        log.info("%s connected (account=%s)", self.name, short_pubkey(self.pubkey))

    async def reconnect(self, attempts: int = 3, delay: float = 2.0) -> bool:
        """Try to restore a dropped socket. True if the stream is back.

        Safe to call mid-cycle because nothing here depends on the socket
        having been continuous: subscriptions are replayed by `connect`,
        positions are re-read from HTTP by the reconciler, and the hedge rule
        is derived from those positions rather than from the fills it missed.
        A fill that landed while the socket was down therefore shows up as a
        position difference and is corrected once, not twice.
        """
        for attempt in range(1, attempts + 1):
            # Closing a socket that is already broken is expected to fail.
            with contextlib.suppress(Exception):
                await self.client.disconnect()
            try:
                if await self.client.connect():
                    log.warning(
                        "%s: WebSocket reconnected on attempt %d", self.name, attempt
                    )
                    return True
            except Exception as exc:  # noqa: BLE001 - every failure is the same here
                log.warning(
                    "%s: reconnect attempt %d/%d failed: %s",
                    self.name, attempt, attempts, describe(exc),
                )
            if attempt < attempts:
                await asyncio.sleep(delay)
        return False

    async def disconnect(self) -> None:
        try:
            await self.client.disconnect()
        except Exception as exc:  # pragma: no cover - shutdown best effort
            log.warning("%s: error during disconnect: %s", self.name, describe(exc))

    @property
    def is_connected(self) -> bool:
        return bool(self.client.is_connected)

    @property
    def last_message_age_s(self) -> float:
        return time.monotonic() - self.client.last_message_at

    def on(self, topic: Topic, handler: Callable) -> None:
        self.client.on(topic, handler)

    # -- order helpers -----------------------------------------------------

    async def submit(
        self, actions: Sequence[Action], count_rejects: bool = True, **kwargs
    ) -> list[OrderResponse]:
        """Submit a batch and raise if any action was rejected.

        Rejection streaks are tracked here rather than at the call sites so the
        risk layer has a single number to trip on. `count_rejects` exists for
        cancels, where a rejection usually just means there was nothing to
        cancel -- counting those would let routine cleanup trip the kill switch.

        `rejectedCrossing` is excluded for the same reason. Resting orders are
        ALO, so the exchange refuses one that would take instead of filling it;
        that is the protection working, and it happens whenever the book moves
        onto the target price between reading it and the order landing. Counting
        it would let an active market trip the kill switch.
        """
        try:
            responses = await self.client.submit(actions, **kwargs)
        except Exception:
            # No answer came back. The actions may have executed anyway, so
            # every symbol they touched is now in doubt until a position read
            # settles it. A rejection is not in doubt -- that IS an answer.
            at = time.monotonic()
            for action in actions:
                symbol = getattr(action, "symbol", None)
                if symbol:
                    self.unconfirmed[symbol] = at
            raise

        for action in actions:
            symbol = getattr(action, "symbol", None)
            if symbol:
                self.unconfirmed.pop(symbol, None)

        rejected = [r for r in responses if r.is_error()]
        if rejected:
            faults = [r for r in rejected if r.status != OrderStatus.REJECTED_CROSSING]
            detail = "; ".join(f"{r.status}: {r.message}" for r in rejected)
            if count_rejects and faults:
                self.reject_streak += 1
                # Only the counted ones. A crossing rejection is the ALO
                # protection working, and recording it as the cause of a halt
                # it did not contribute to would point at the wrong thing.
                self.last_reject = "; ".join(
                    f"{r.status}: {r.message}" for r in faults
                )
            raise OrderRejected(f"{self.name}: {detail}", responses)
        if count_rejects:
            self.reject_streak = 0
            self.last_reject = ""
        return responses

    async def place_limit(
        self,
        symbol: str,
        is_buy: bool,
        price: float,
        size: float,
        reduce_only: bool = False,
        cancel_oid: str | None = None,
    ) -> tuple[str, list[OrderResponse]]:
        """Place a resting limit order, optionally replacing an existing one.

        When `cancel_oid` is given the cancel and the placement go out in a
        single transaction, which is the only way to reprice on BULK -- `mod`
        changes an order's size, never its price.

        The order is ALO. Both legs rest inside the touch and are meant to earn
        the maker side, and ALO is the only time-in-force that says so to the
        exchange: per the taker speed bump, "Only ALO guarantees maker behavior
        before execution", and it is the only order type scheduled immediately.
        A GTC order is held for 25 ms even when it would rest, which on a chase
        loop is 25 ms added to every reprice. The cost is that a price the
        market has already reached is rejected rather than filled as a taker --
        which is the behaviour this strategy wants.
        """
        order = LimitOrder(
            symbol=symbol,
            side=Side.BUY if is_buy else Side.SELL,
            price=price,
            size=size,
            reduce_only=reduce_only,
            time_in_force=TimeInForce.ALO,
        )
        actions: list[Action] = []
        if cancel_oid:
            actions.append(CancelOrder(symbol=symbol, oid=cancel_oid))
        actions.append(order)

        responses = await self.submit(actions)
        # order_id() is a deterministic hash of the signed fields, so it is
        # known once seqno/nonce/pubkey have been stamped on by submit().
        return order.order_id(), responses

    async def aggressive_limit(
        self,
        symbol: str,
        is_buy: bool,
        price: float,
        size: float,
        reduce_only: bool = False,
    ) -> list[OrderResponse]:
        """Take liquidity, but only down to `price`. IOC: no remainder rests.

        A market order sweeps as deep as it needs to, which is how a hedge ends
        up matching this bot's own resting order on the other account. BULK's
        self-trade prevention does not cover that: it is per account, and the
        documentation is explicit that "orders from different sub-accounts under
        the same main account can trade against each other".

        Bounding the price is the only way to make that impossible rather than
        merely unlikely -- no offset is deep enough, because how far a hedge
        sweeps depends on the book at that instant, and it was measured reaching
        $28.74 past the touch on BTC.

        Whatever the bound leaves unfilled does not rest: IOC, because a hedge
        that quietly became a maker order would leave the pair unhedged while
        looking done. The remainder is picked up by the next trigger or by the
        reconciler, from the position -- which is where the hedge size comes
        from in the first place.
        """
        return await self.submit(
            [
                LimitOrder(
                    symbol=symbol,
                    side=Side.BUY if is_buy else Side.SELL,
                    price=price,
                    size=size,
                    reduce_only=reduce_only,
                    time_in_force=TimeInForce.IOC,
                )
            ]
        )

    async def market(
        self,
        symbol: str,
        is_buy: bool,
        size: float,
        reduce_only: bool = False,
    ) -> list[OrderResponse]:
        return await self.submit(
            [
                MarketOrder(
                    symbol=symbol,
                    side=Side.BUY if is_buy else Side.SELL,
                    size=size,
                    reduce_only=reduce_only,
                )
            ]
        )

    async def cancel(self, symbol: str, oid: str) -> list[OrderResponse]:
        # A cancel commonly loses a race with a fill; that is not a malfunction.
        return await self.submit(
            [CancelOrder(symbol=symbol, oid=oid)], count_rejects=False
        )

    async def cancel_all(self, symbols: Sequence[str]) -> list[OrderResponse]:
        return await self.submit(
            [CancelAll(symbols=list(symbols))], count_rejects=False
        )

    # -- state queries -----------------------------------------------------

    def full_account(self) -> dict:
        """HTTP account snapshot, unwrapped.

        Used at startup and during recovery, when the WS stream has not yet
        delivered a snapshot or is not trusted.

        The endpoint nests everything under a `fullAccount` key (and may wrap
        the whole thing in a single-element list), so the payload is flattened
        here. Reading the outer envelope instead silently yields no positions
        and no sub-accounts -- which looks exactly like a flat account.
        """
        return unwrap_full_account(self.http.get_full_account(self.pubkey))

    def open_orders(self) -> list[dict]:
        return self.http.get_open_orders(self.pubkey)


def build_sessions(
    *,
    private_key: str,
    sub1_pubkey: str,
    ws_url: str,
    http_url: str,
    domain: SignatureDomain,
    symbols: Sequence[str],
    dry_run: bool,
) -> tuple[AccountSession, AccountSession]:
    """Construct the master and Sub1 sessions from a single signing key.

    Both sessions share the master's key. They differ in `account_pubkey`,
    which is what routes orders to the right account.
    """
    signer = TransactionSigner(private_key)
    master_pubkey = signer.public_key

    if sub1_pubkey == master_pubkey:
        raise ValueError("sub1_pubkey must differ from the master account pubkey")

    # Takes the raw key, not the signer, and names the endpoint `base_url`.
    http = BulkHttpClient(
        base_url=http_url, private_key=private_key, signature_domain=domain
    )

    def make(name: str, pubkey: str) -> AccountSession:
        client = RoutedWsClient(
            url=ws_url,
            symbols=list(symbols),
            signer=signer,
            signature_domain=domain,
            account_pubkey=pubkey,
            dry_run=dry_run,
        )
        return AccountSession(
            name=name, pubkey=pubkey, client=client, http=http, dry_run=dry_run
        )

    return make("master", master_pubkey), make("sub1", sub1_pubkey)


class NoSubAccount(Exception):
    """The master has no sub-account to trade against."""


def discover_sub_account(
    *, private_key: str, http_url: str, timeout: int = 25
) -> str:
    """Find the master's sub-account, so it need not be configured by hand.

    A sub-account has no private key of its own -- it is created by, and signed
    for by, the master. Asking the operator to paste its pubkey is therefore
    busywork: the master's own account record already lists it, and that record
    is the authority. Reading it also means a config can never name a
    sub-account that does not belong to the key in use.

    The first child is taken when several exist. The strategy trades exactly
    one, and picking the first keeps repeat runs on the same account rather
    than moving positions around between them.
    """
    master_pubkey = TransactionSigner(private_key).public_key
    response = requests.post(
        f"{http_url}/account",
        json={"type": "fullAccount", "user": master_pubkey},
        timeout=timeout,
    )
    if response.status_code == 404:
        raise NoSubAccount(
            f"no BULK account exists for master {short_pubkey(master_pubkey)}. "
            "An account is created by depositing USDC on BULK; do that first."
        )
    response.raise_for_status()

    children = [
        entry["pubkey"]
        for entry in (unwrap_full_account(response.json()).get("subAccounts") or [])
        if isinstance(entry, dict) and entry.get("pubkey")
    ]
    if not children:
        raise NoSubAccount(
            f"master {short_pubkey(master_pubkey)} has no sub-account. "
            "Create one from the menu: Accounts Management -> Create New Subaccount."
        )

    if len(children) > 1:
        log.info(
            "master has %d sub-accounts; trading the first (%s)",
            len(children),
            short_pubkey(children[0]),
        )
    return children[0]


def verify_sub_account(master: AccountSession, sub1: AccountSession) -> None:
    """Fail fast unless Sub1 really is a child of the master.

    Trading an unrelated account would still sign correctly if that account had
    authorised the key, but the strategy's margin and risk assumptions only hold
    for a true sub-account, so this is checked rather than assumed.
    """
    master_state = master.full_account()
    children = {
        entry.get("pubkey")
        for entry in (master_state.get("subAccounts") or [])
        if isinstance(entry, dict)
    }
    if sub1.pubkey not in children:
        raise RuntimeError(
            f"{sub1.pubkey} is not a sub-account of master {master.pubkey}. "
            f"Known sub-accounts: {sorted(c for c in children if c) or 'none'}"
        )
    log.info("verified %s is a sub-account of %s", short_pubkey(sub1.pubkey), short_pubkey(master.pubkey))


def unwrap_full_account(payload: Any) -> dict:
    """Flatten a `/account` response down to the account body.

    Observed shapes: `{"fullAccount": {...}}`, `[{"fullAccount": {...}}]`, and
    a bare `{...}`. All three are accepted so a change in envelope does not
    silently turn a funded account into an apparently empty one.
    """
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("fullAccount")
    if isinstance(inner, dict):
        return inner
    return payload


def short_pubkey(pubkey: str | None) -> str:
    """`AAAAAA..BBBB`, for logs and menus. Shared so both render keys alike."""
    if not pubkey:
        return "?"
    return pubkey if len(pubkey) <= 12 else f"{pubkey[:6]}..{pubkey[-4:]}"


def _safe_order_id(action: Action) -> str | None:
    try:
        return action.order_id()
    except Exception:
        return None

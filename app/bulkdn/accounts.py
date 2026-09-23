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
from bulk_api.messages import SubscriptionRequest
from bulk_api.messages.trade import CancelAll, CancelOrder, LimitOrder, MarketOrder, OrderResponse

Action = Any  # LimitOrder | MarketOrder | CancelOrder | CancelAll

log = logging.getLogger(__name__)


class OrderRejected(Exception):
    """Raised when the exchange rejects an action inside a submitted batch."""

    def __init__(self, message: str, responses: Sequence[OrderResponse] | None = None):
        super().__init__(message)
        self.responses = list(responses or [])


class TransactionRejected(OrderRejected):
    """The exchange refused the whole transaction, not one action inside it.

    Bad signature, rate limit, malformed envelope: the reply to OUR request id
    came back with a status other than "ok". The SDK raises a bare
    `RuntimeError` for this, which is indistinguishable from a timeout or a
    dropped socket -- and `AccountSession.submit` read every such error as "no
    answer". So a key the exchange refused on every order never moved the
    reject streak, the kill switch that exists for exactly that never tripped,
    and the strategy looped place -> reject -> cancel for as long as anyone let
    it, each pass marking the symbol "in doubt" and asking for a position read.

    It is an `OrderRejected` because that is what callers already handle as
    "the exchange answered no": nothing was applied, and nothing is in doubt.
    `responses` is empty -- there are no per-action statuses -- and `detail`
    carries whatever the exchange said.
    """

    def __init__(self, message: str, detail: Any = None):
        super().__init__(message, [])
        self.detail = detail


_RATE_LIMIT_MARKERS = ("rate limit", "rate-limit", "ratelimit", "too many", "429", "throttl")


def is_rate_limited(exc: BaseException) -> bool:
    """Whether a refusal is the exchange throttling us rather than saying no.

    Judged from the text, which is all a refusal carries: the reply has no
    code for it that this bot has seen.
    """
    text = f"{exc} {getattr(exc, 'detail', '') or ''}".lower()
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


class NotSent(RuntimeError):
    """Refused on this side before anything reached the exchange.

    No socket, no signer, an account the socket does not carry. Unlike a
    timeout, the order certainly did not execute, so a hedge slice that
    fails this way is released rather than held in doubt -- held, a
    persistent fault here re-hedged, doubted and re-read forever without
    ever moving the reject streak.
    """


class SubmissionInDoubt(RuntimeError):
    """A request was failed on the socket without an answer addressed to it.

    The SDK's `_handle_error_response` fails EVERY pending request on a socket
    when an `error` frame arrives, whether or not the frame says which request
    it is about. One socket carries every account under a key, so an error
    caused by one account's order would otherwise be booked as a rejection on
    all of them -- and counted toward each one's kill switch.

    When the frame cannot be tied to a request id, nobody can say whose it
    was, so nobody is told "rejected". Everyone pending is told "in doubt"
    instead, which is the honest reading: the request may or may not have
    been applied, and a position read will settle it.
    """


class RoutedWsClient(BulkWebSocketClient):
    """A WS client for the accounts one key signs for.

    `account_pubkey` is the default account being traded. For the master it
    equals the signer's pubkey; for a sub-account it is the child's pubkey and
    the master signs on its behalf.

    `accounts` names every account this socket carries, and it is what makes a
    pool of a hundred accounts possible at all: the traded account travels in
    each transaction rather than being a property of the connection, so one
    master key needs one socket no matter how many sub-accounts hang off it.
    Ten masters is ten sockets; a socket per account would have been a hundred
    and ten, against an exchange that answered 429 to two accounts polling
    every five seconds.
    """

    def __init__(
        self,
        *args,
        account_pubkey: str | None = None,
        accounts: list[str] | None = None,
        dry_run: bool = False,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.account_pubkey = account_pubkey or (self.signer.public_key if self.signer else None)
        # Deduplicated, because subscribing twice to one account delivers every
        # fill on it twice and the fill path is only idempotent by trade id.
        wanted = list(accounts or ([self.account_pubkey] if self.account_pubkey else []))
        self.accounts = list(dict.fromkeys(pubkey for pubkey in wanted if pubkey))
        self.dry_run = dry_run
        self.last_message_at: float = time.monotonic()
        # Set only while an account update is being dispatched. See
        # `_handle_message`.
        self._dispatch_owner: str | None = None
        self._owner_warned = False
        # When an update last named an account this socket does not carry.
        # See `_warn_unknown_owner`.
        self._unknown_owner_warned_at: float | None = None

    # -- connection --------------------------------------------------------

    def _intended_subscriptions(self) -> list[SubscriptionRequest]:
        """Every account this socket carries, then the tickers.

        The same set, in the same order, that the base class and the old
        per-account loop issued between them on a first connect.
        """
        subs = [SubscriptionRequest("account", {"user": pubkey}) for pubkey in self.accounts]
        subs += [
            SubscriptionRequest("ticker", {"symbol": symbol})
            for symbol in (getattr(self, "symbols", None) or [])
        ]
        return subs

    async def connect(self) -> bool:
        """Connect, subscribing to *this* client's accounts rather than the signer's.

        The base implementation has two branches. With a stored subscription
        list it replays that list; with none -- a first connect -- it
        subscribes to `signer.public_key` and the tickers. The signer's own
        account is wrong for a socket carrying sub-accounts, so the first
        connect is steered into the replay branch instead, by recording the
        full intended set BEFORE the base call.

        This replaces hiding the signer for the duration of the call, which
        worked but was a trap: for as long as `connect` ran, `self.signer` was
        None, and a submission that landed in that window -- the emergency
        stop cancelling orders during a reconnect, seen live -- failed with
        "signer not configured" until a second field was added to paper over
        it. Nothing needs hiding now, so there is nothing to restore.

        Recording first also fixes a partial resubscription. The old path
        subscribed the accounts one `send` at a time after the base call, and
        each `send` appended to `subscriptions` as it went. A connect that died
        between two of them left the list holding only the accounts that got
        through -- and every later reconnect replays the list, so the missing
        accounts were never subscribed again. Fills on them arrived on no
        socket at all. Recorded up front, a failed first connect leaves the
        whole set behind for the next attempt to replay.
        """
        if not self.subscriptions:
            self.subscriptions = self._intended_subscriptions()

        connected = await super().connect()

        if connected:
            # A socket that has just come up has been silent for zero seconds.
            # The stale watchdog measures silence from this field, and leaving
            # it at the DEAD socket's last message makes a successful reconnect
            # still read as the failure it just fixed. That ended a three-hour
            # run one second after "WebSocket reconnected on attempt 1": the
            # heal worked, the re-check saw 31s of silence belonging to a socket
            # that no longer existed, and the kill switch fired anyway.
            self.last_message_at = time.monotonic()
        return connected

    async def _handle_message(self, data: dict) -> None:
        # Liveness is tracked off raw traffic so the risk layer can tell a quiet
        # market from a dead socket.
        self.last_message_at = time.monotonic()

        # Which account this update is about, for as long as it is being
        # dispatched. One socket carries every account under a key, and the
        # SDK was written when it carried one: handlers are registered per
        # account but the client fires all of them for every message, so a
        # fill on one account arrived as a fill on all of them.
        #
        # A live run with three accounts on a socket booked one $60 buy three
        # times, read the pair as long on both sides, and halted on a hedge
        # ceiling that was doing its job.
        #
        # Handlers run synchronously inside the dispatch below, so a plain
        # attribute is enough to carry this -- there is no interleaving to
        # lose it to.
        if isinstance(data, dict) and data.get("type") == "account":
            self._dispatch_owner = _owner_of(data, self.accounts)
            if self._dispatch_owner is None:
                stranger = _claimed_owner(data)
                if stranger is not None:
                    self._warn_unknown_owner(stranger)
                else:
                    self._warn_unattributable(data)
            try:
                await super()._handle_message(data)
            finally:
                self._dispatch_owner = None
            return

        await super()._handle_message(data)

    def _warn_unattributable(self, data: dict) -> None:
        """Say once that account updates arrive without naming their account.

        Only the field NAMES are logged. If this ever fires, those names say
        which spelling to add to `ACCOUNT_OWNER_KEYS`, and the run meanwhile
        falls back to re-reading positions over HTTP rather than guessing.
        """
        if self._owner_warned or len(self.accounts) <= 1:
            return
        self._owner_warned = True
        inner = data.get("data")
        log.warning(
            "account updates on this socket do not name their account, and it "
            "carries %d of them -- falling back to HTTP position reads. "
            "Fields seen: outer=%s inner=%s topic=%r",
            len(self.accounts),
            sorted(data),
            sorted(inner) if isinstance(inner, dict) else type(inner).__name__,
            data.get("topic"),
        )

    def _warn_unknown_owner(self, named: str) -> None:
        """Say that an update named an account this socket does not carry.

        It used to be believed. `_owner_of` returned whatever string sat under
        a `user`/`account`/`u` key, every session compared it with its own
        pubkey, none matched, and the update -- a fill, a position -- was
        dropped by all of them without a word. Now it reads as "cannot tell",
        which re-reads positions over HTTP, and this says why.

        Rate-limited rather than once-only: unlike the missing-field case, this
        is a fact about individual messages, and one that keeps happening is
        worth seeing more than once in a long run -- but not once per frame.
        """
        now = time.monotonic()
        last = self._unknown_owner_warned_at
        if last is not None and now - last < UNKNOWN_OWNER_WARN_EVERY_S:
            return
        self._unknown_owner_warned_at = now
        log.warning(
            "an account update names %s, which is not one of the %d account(s) "
            "on this socket -- not attributing it to any of them; positions "
            "will be re-read over HTTP",
            short_pubkey(named), len(self.accounts),
        )

    @property
    def message_owner(self) -> str | None:
        """The account the update being dispatched belongs to, if it says."""
        return self._dispatch_owner

    # -- answers to our requests -------------------------------------------

    async def _handle_post_response(self, data: dict) -> None:
        """Fail a refused transaction as a rejection, not as a bare error.

        The SDK resolves a non-"ok" reply with `RuntimeError("Order request
        failed: ...")`. The reply carries our request id, so it is an answer
        to exactly one request -- and an answer that says no. Raising it as
        `TransactionRejected` is what lets `AccountSession.submit` count it
        toward the reject streak instead of filing it with the timeouts.

        An "ok" reply is left to the SDK, which parses the per-action
        statuses.
        """
        response_data = data.get("data") if isinstance(data, dict) else None
        payload = response_data.get("payload") if isinstance(response_data, dict) else None
        status = payload.get("status") if isinstance(payload, dict) else None
        if status == "ok":
            await super()._handle_post_response(data)
            return

        future = self.pending_requests.pop(data.get("id"), None)
        log.error("transaction refused by the exchange: %s", response_data)
        if future is not None and not future.done():
            future.set_exception(
                TransactionRejected(
                    f"transaction refused: {_refusal_text(response_data)}",
                    response_data,
                )
            )
        await self._emit_event(Topic.ERROR, response_data)

    async def _handle_error_response(self, data: dict) -> None:
        """Fail only the request an `error` frame is about, when it says.

        The SDK fails every pending request on the socket with the frame's
        message. On a socket carrying every account under a key, that turned
        one account's refused order into a "failure" on every other account
        with a request in flight -- and with rejections now counted, it would
        have tripped their kill switches for something they did not do.

        With an id that matches a pending request, that request alone is
        rejected: it is a direct answer. Without one, nobody can say whose
        error it was, so every pending request is failed as IN DOUBT rather
        than rejected. Failing them at all, rather than leaving them to time
        out, keeps the SDK's reason for doing so: a submission waiting ten
        seconds for a reply that is not coming stalls a chase loop for ten
        seconds, and "in doubt" already makes the caller re-read positions.
        """
        error_data = data.get("error") if isinstance(data, dict) else None
        if not isinstance(error_data, dict):
            error_data = {}
        message = error_data.get("message") or "websocket error"
        request_id = data.get("id", error_data.get("id"))

        if request_id is not None:
            # Named, so it is about that request and no other. One that is no
            # longer waiting -- it already timed out -- is logged and left:
            # the requests still pending did nothing to earn it.
            future = self.pending_requests.pop(request_id, None)
            log.error("request %s refused on the socket: %s", request_id, message)
            if future is not None and not future.done():
                future.set_exception(
                    TransactionRejected(f"request refused: {message}", error_data)
                )
        else:
            log.error(
                "socket error naming no request we are waiting on (%s) -- %d "
                "pending request(s) on this socket are now in doubt, not rejected",
                message, len(self.pending_requests),
            )
            pending = list(self.pending_requests.items())
            self.pending_requests.clear()
            for _request_id, waiting in pending:
                if not waiting.done():
                    waiting.set_exception(
                        SubmissionInDoubt(f"unattributed socket error: {message}")
                    )
        await self._emit_event(Topic.ERROR, error_data)

    # -- routed submission -------------------------------------------------

    @property
    def _signing_key(self):
        """The key that signs.

        A property for the callers that predate `connect` no longer hiding
        the signer; it is simply `self.signer` now.
        """
        return self.signer

    async def submit(
        self,
        actions: Sequence[Action],
        timeout: float | None = None,
        nonce: int | None = None,
        account: str | None = None,
    ) -> list[OrderResponse]:
        """Sign and submit a batch of actions for one account.

        Mirrors the SDK's `place_orders` but sets `account` to the traded
        account while leaving `signer` as the key that actually signs. Actions
        also carry `pubkey`, which feeds the client-side order-ID hash -- it
        must be the traded account or the computed IDs won't match the
        exchange's.

        `account` defaults to this client's own, which is every caller that
        predates the pool. A shared socket passes it per call, because with
        several accounts on one connection the alternative is a mutable
        "current account" and orders landing on whichever one was set last.
        """
        signer = self._signing_key
        if not signer:
            raise NotSent("signer not configured")
        account = account or self.account_pubkey
        if not account:
            raise NotSent("account_pubkey not configured")
        if self.accounts and account not in self.accounts:
            # A socket signs only for the accounts its key was built for.
            # Sending for another would be rejected by the exchange, but the
            # useful moment to notice is here, where the account is named.
            raise NotSent(
                f"this connection does not carry {short_pubkey(account)}"
            )
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
            # A decimal string, as the SDK sends it on every signed path, WS
            # and HTTP alike. `bulkdn.tx` sends a number instead, on purpose;
            # see there.
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
            raise NotSent("not connected to WebSocket")

        tx = signer.sign_transaction(tx, self.signature_domain)

        self.request_id += 1
        request_id = self.request_id
        request = {
            "method": "post",
            "request": {"type": "action", "payload": tx},
            "id": request_id,
        }

        # From the running loop, not `asyncio.Future()`, which binds to
        # whatever `get_event_loop` returns -- deprecated outside a running
        # loop, and a different loop from the one resolving it if a caller
        # ever arrives through `asyncio.run` in a thread.
        future: asyncio.Future = asyncio.get_running_loop().create_future()
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

    async def reconnect(self, attempts: int = 6, delay: float = 2.0) -> bool:
        """Try to restore a dropped socket. True if the stream is back.

        Safe to call mid-cycle because nothing here depends on the socket
        having been continuous: subscriptions are replayed by `connect`,
        positions are re-read from HTTP by the reconciler, and the hedge rule
        is derived from those positions rather than from the fills it missed.
        A fill that landed while the socket was down therefore shows up as a
        position difference and is corrected once, not twice.

        The delay doubles, so the six attempts wait 2+4+8+16+30 = 60 seconds
        between them rather than the seven seconds three flat ones did. That is
        the WAITING alone. Each attempt can itself take up to the socket's open
        timeout (`ws_compat._OPEN_TIMEOUT`, 30s) when the far end accepts the
        TCP connection and then says nothing, so the worst case is 60 + 6 x 30,
        about four minutes, and a refused connection -- DNS, `HTTP 502` --
        fails in well under a second and leaves it near the one minute. The
        docstring used to promise "about a minute" flat, which was only ever
        the fast-failure case.

        The patience came from a subscriber's log: the socket dropped, and all
        three reconnects were refused by the exchange's own front end with
        `HTTP 502` inside seven seconds. A gateway is rarely back that quickly,
        and the run halted on an outage it had barely waited out -- cancelling
        its orders and leaving the operator to restart it by hand.

        Patience is nearly free here and impatience is not. The pair stays
        hedged while the socket is down -- the reconciler works over HTTP -- so
        a minute of trying costs a minute of not trading, while giving up costs
        a halt.

        **The budget belongs to the socket, not to the session.** Every account
        under a key shares one client, and each of their sessions used to spend
        its own six attempts on it, one after another: eleven accounts on a
        dead socket was eleven budgets, up to three-quarters of an hour, before
        the supervisor could conclude what the first one already knew. Now the
        attempts run once per client and every session on it waits for, and
        shares, the same outcome. See `reconnect_client`.
        """
        return await reconnect_client(
            self.client, label=self.name, attempts=attempts, delay=delay
        )

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

    def owns_this_update(self) -> bool | None:
        """Is the account update being dispatched this account's?

        True or False when the exchange names the account, and None when it
        does not and the socket carries more than one -- which is a genuine
        "cannot tell", not a no. A caller that treats None as either answer
        is guessing; the handlers re-read positions over HTTP instead.

        A socket carrying a single account has nothing to confuse: whatever
        arrives on it is that account's, which is how every run before pools
        worked and why this was never noticed.
        """
        owner = getattr(self.client, "message_owner", None)
        if owner is not None:
            return owner == self.pubkey
        if len(getattr(self.client, "accounts", None) or ()) <= 1:
            return True
        return None

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
            # Named explicitly: one socket may carry several accounts, and the
            # session is the only thing that knows which of them this is.
            kwargs.setdefault("account", self.pubkey)
            responses = await self.client.submit(actions, **kwargs)
        except NotSent as exc:
            # Nothing left this process, so nothing is in doubt -- and a fault
            # that stops every order is exactly what the streak is for.
            self._settle(actions)
            if count_rejects:
                self.reject_streak += 1
                self.last_reject = str(exc)
            raise
        except OrderRejected as exc:
            # The whole transaction was refused -- bad signature, rate limit,
            # a malformed envelope. That is an answer, and a definite one:
            # nothing in it was applied, so nothing is in doubt.
            #
            # It used to land in the branch below with the timeouts, which
            # left the reject streak untouched. A key the exchange refused on
            # every order therefore never tripped the kill switch built for
            # it, and the strategy looped place -> reject -> cancel, each pass
            # asking for a position read that could only say "nothing
            # happened".
            self._settle(actions)
            if count_rejects and not is_rate_limited(exc):
                self.reject_streak += 1
                self.last_reject = str(exc)
            elif count_rejects:
                # Throttled, not refused for cause. The streak halts the run
                # and closes every account at market, and a burst of 429s on a
                # shared socket reached five inside five seconds of chasing --
                # an outage of a few seconds is no reason to do that.
                log.warning("%s: throttled by the exchange: %s", self.name, describe(exc))
            raise
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

        self._settle(actions)

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

    def _settle(self, actions: Sequence[Action]) -> None:
        """An answer came back, so these symbols are no longer in doubt."""
        for action in actions:
            symbol = getattr(action, "symbol", None)
            if symbol:
                self.unconfirmed.pop(symbol, None)

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

        try:
            responses = await self.submit(actions)
        except OrderRejected as exc:
            # A rejection of the BATCH is not necessarily a rejection of the
            # order. In a cancel+replace the cancel half is refused whenever
            # the old order filled first -- a race this strategy loses
            # routinely -- and the new order beside it may well have been
            # accepted and be resting. Reported only as "rejected", the
            # chaser forgot it: an order on the book that nothing tracked,
            # free to fill the leg past its size.
            #
            # So the exception says what became of the order itself: its id,
            # and whether its own status was an acceptance. A whole-
            # transaction refusal has no per-action statuses and is never
            # `placed`. Attached rather than returned, because every other
            # caller of `submit` relies on a rejection raising.
            exc.order_id = _safe_order_id(order)
            exc.placed = _order_accepted(order, actions, exc.responses)
            raise
        # order_id() is a deterministic hash of the signed fields, so it is
        # known once seqno/nonce/pubkey have been stamped on by submit().
        return order.order_id(), responses

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


# How long the outcome of one reconnect speaks for the socket. The supervisor
# heals sessions one after another, so the siblings of a session that just
# gave up ask about the same socket within milliseconds; ten seconds covers
# that pass with room to spare and is well short of the next one.
RECONNECT_SHARE_S = 10.0

# Bumped whenever ANY socket reconnects. A failure is shared with the next
# caller only while this is unchanged: one socket coming back is proof the
# network did, and the supervisor retries the ones that failed before it for
# exactly that reason. A shared failure must not refuse that retry.
_reconnect_generation = 0


async def reconnect_client(
    client, *, label: str, attempts: int = 6, delay: float = 2.0
) -> bool:
    """Reconnect one socket once, however many sessions ask.

    Three cases, in order:

    * A reconnect of this client is already running: wait for it and take
      its answer. Sessions healed concurrently join one attempt sequence
      rather than each tearing down the socket the other is building.
    * One finished moments ago (`RECONNECT_SHARE_S`): a success stands if the
      socket is still up, so a sibling asking next does not disconnect the
      socket that was just restored; a failure stands if no socket anywhere
      has come back since, so a sibling asking next does not spend another
      full budget against the same dead endpoint.
    * Otherwise run the attempts.

    The state lives on the client object because the client IS the socket --
    which is also how `Strategy._trees` groups sessions by key.
    """
    running = getattr(client, "_bulkdn_reconnect", None)
    if running is not None and not running.done():
        log.info("%s: joining the reconnect already under way on this socket", label)
        return await _await_shared(client, running)

    outcome = getattr(client, "_bulkdn_reconnect_outcome", None)
    if outcome is not None:
        ok, generation, at = outcome
        if time.monotonic() - at < RECONNECT_SHARE_S:
            if ok and client.is_connected:
                log.info("%s: this socket was reconnected moments ago", label)
                return True
            if not ok and generation == _reconnect_generation:
                log.warning(
                    "%s: this socket gave up reconnecting moments ago and "
                    "nothing has come back since -- not retrying it again",
                    label,
                )
                return False

    task = asyncio.ensure_future(_reconnect_attempts(client, label, attempts, delay))
    client._bulkdn_reconnect = task
    client._bulkdn_reconnect_waiters = 0
    return await _await_shared(client, task)


async def _await_shared(client, task: asyncio.Future) -> bool:
    """Wait on a shared reconnect without letting one waiter cancel it for all.

    Shielded, so a session whose own caller is cancelled does not take the
    reconnect away from the siblings waiting on it. Counted, so when the LAST
    waiter is cancelled -- shutdown -- the attempts stop too, rather than
    running on in the background and reopening a socket after the run ended.
    """
    client._bulkdn_reconnect_waiters = getattr(client, "_bulkdn_reconnect_waiters", 0) + 1
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if client._bulkdn_reconnect_waiters <= 1 and not task.done():
            task.cancel()
        raise
    finally:
        client._bulkdn_reconnect_waiters -= 1


async def _reconnect_attempts(client, label: str, attempts: int, delay: float) -> bool:
    for attempt in range(1, attempts + 1):
        # Closing a socket that is already broken is expected to fail.
        with contextlib.suppress(Exception):
            await client.disconnect()
        try:
            if await client.connect():
                log.warning("%s: WebSocket reconnected on attempt %d", label, attempt)
                _record_reconnect(client, True)
                return True
        except Exception as exc:  # noqa: BLE001 - every failure is the same here
            log.warning(
                "%s: reconnect attempt %d/%d failed: %s",
                label, attempt, attempts, describe(exc),
            )
        if attempt < attempts:
            # Doubling, capped: a gateway that is down stays down for
            # longer than a socket that merely blipped, and hammering it
            # every two seconds neither helps it nor us.
            await asyncio.sleep(min(delay * 2 ** (attempt - 1), 30.0))
    # Recorded only on a finished run. A reconnect cancelled at shutdown
    # proved nothing about the socket and leaves no verdict behind.
    _record_reconnect(client, False)
    return False


def _record_reconnect(client, ok: bool) -> None:
    global _reconnect_generation
    if ok:
        _reconnect_generation += 1
    client._bulkdn_reconnect_outcome = (ok, _reconnect_generation, time.monotonic())


def build_pool(
    *,
    private_keys: Sequence[str],
    ws_url: str,
    http_url: str,
    domain: SignatureDomain,
    symbols: Sequence[str],
    dry_run: bool,
    discover=None,
) -> list[AccountSession]:
    """A session for every account under every key, in key order.

    One socket per key, shared by all of that key's accounts. The traded
    account travels in each transaction rather than being a property of the
    connection, so ten masters with ten sub-accounts apiece need ten sockets
    and not a hundred and ten -- against an exchange that answered 429 to two
    accounts polling every five seconds.

    Sessions are named for where they sit rather than for what they are:
    `m2s3` is the third sub-account of the second key. A pool of a hundred
    accounts is read in a log, and "sub1" repeated ten times is unreadable.

    Key order is the operator's, taken from the file. An account that appears
    under two keys is kept once, at its first appearance: the same account
    twice in a pool is an account that can be paired with itself, which is not
    a hedge.
    """
    discover = discover or discover_accounts
    sessions: list[AccountSession] = []
    seen: set[str] = set()

    for index, private_key in enumerate(private_keys, start=1):
        master_pubkey, children = discover(
            private_key=private_key, http_url=http_url
        )
        signer = TransactionSigner(private_key)
        # Takes the raw key, not the signer, and names the endpoint `base_url`.
        http = BulkHttpClient(
            base_url=http_url, private_key=private_key, signature_domain=domain
        )
        owned = [(f"m{index}", master_pubkey)] + [
            (f"m{index}s{n}", pubkey) for n, pubkey in enumerate(children, start=1)
        ]
        fresh = [(name, pubkey) for name, pubkey in owned if pubkey not in seen]
        if not fresh:
            continue
        client = RoutedWsClient(
            url=ws_url,
            symbols=list(symbols),
            signer=signer,
            signature_domain=domain,
            account_pubkey=master_pubkey,
            accounts=[pubkey for _name, pubkey in fresh],
            dry_run=dry_run,
        )
        for name, pubkey in fresh:
            seen.add(pubkey)
            sessions.append(
                AccountSession(
                    name=name, pubkey=pubkey, client=client, http=http, dry_run=dry_run
                )
            )

    return sessions


# Where the exchange names the account an update is about. The field is not
# in the SDK's model at all -- it parses fills into a dataclass with no room
# for it -- so this reads the raw message before parsing.
#
# In practice it is `topic`, and none of the payload spellings appear: a live
# socket carrying three accounts sent outer=[data, topic, type] and an inner
# payload of position and margin fields naming nobody. The others are kept
# because they cost nothing and a second endpoint may differ.
ACCOUNT_OWNER_KEYS = ("user", "account", "subAccount", "pubkey", "owner", "u")


def _owner_of(message: dict, accounts: Sequence[str] = ()) -> str | None:
    """The account an account-update is about, or None if it cannot be told.

    `topic` is what the exchange actually answers with, and it is matched
    against the accounts this socket subscribed to rather than parsed. The
    subscription is `{"type": "account", "user": <pubkey>}`, so the pubkey is
    in there; how it is wrapped -- a prefix, a separator, a case -- is the
    exchange's business and not something to encode a guess about. Matching
    what we asked for is exact where it matters and indifferent to the rest.

    A payload field is believed only if it names one of `accounts`. It used to
    be believed whatever it said: a string under `user`, `account` or `u` that
    was some other key -- a counterparty, a referrer, a field that merely
    shares the name -- came back as the owner, every session on the socket
    compared it with its own pubkey, and the update was dropped by all of them
    in silence. Naming someone we do not carry is now "cannot tell", which the
    handlers answer with an HTTP re-read; `_claimed_owner` lets the client say
    so in the log.

    With no `accounts` to check against there is nothing to validate, and the
    named value is returned as before. Every `RoutedWsClient` carries at least
    one account, so that is only ever a caller outside a socket.
    """
    topic = message.get("topic")
    if isinstance(topic, str) and topic:
        for pubkey in accounts:
            if pubkey and pubkey in topic:
                return pubkey

    named = _claimed_owner(message)
    if named is None:
        return None
    if accounts and named not in accounts:
        return None
    return named


def _claimed_owner(message: dict) -> str | None:
    """Whatever a payload field says the owner is, checked against nothing."""
    layers = [message]
    inner = message.get("data")
    if isinstance(inner, dict):
        layers.append(inner)
    for layer in layers:
        for key in ACCOUNT_OWNER_KEYS:
            value = layer.get(key)
            if isinstance(value, str) and value:
                return value
    return None


# How often to repeat the warning about updates naming an account this socket
# does not carry. Once a minute is enough to see that it keeps happening in a
# long run without one line per frame.
UNKNOWN_OWNER_WARN_EVERY_S = 60.0


def _refusal_text(response_data: Any) -> str:
    """The exchange's own words for a refused transaction, briefly.

    Used as the rejection message, which ends up in `last_reject` and so in
    the halt reason. The full reply is logged separately at ERROR.
    """
    if isinstance(response_data, dict):
        payload = response_data.get("payload")
        for source in (payload, response_data):
            if isinstance(source, dict):
                for key in ("message", "error", "reason", "status"):
                    value = source.get(key)
                    if value:
                        return str(value)
    return str(response_data)


class NoSubAccount(Exception):
    """The master has no sub-account, so a pair cannot be formed."""


def discover_accounts(
    *, private_key: str, http_url: str, timeout: int = 25
) -> tuple[str, list[str]]:
    """The master this key signs for, and every sub-account under it.

    A sub-account has no key of its own -- it is created by, and signed for by,
    the master -- so the master's own record is the authority on which accounts
    a key can trade. Reading it means a pool can never contain an account the
    key cannot sign for, which is a failure that would otherwise surface as a
    rejected order in the middle of a cycle.

    Order is the exchange's. It is stable across runs, which matters because
    the names built from it end up in the log.
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
    return master_pubkey, children


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


def _order_accepted(
    order: Action, actions: Sequence[Action], responses: Sequence[OrderResponse]
) -> bool:
    """Whether the exchange's status for `order` within a batch was an acceptance.

    Found by order id where the response carries one, and otherwise by
    position -- responses come back one per action, in order -- but only when
    the counts agree, since a positional match against a short list would
    read some other action's status as this one's.
    """
    oid = _safe_order_id(order)
    mine = None
    if oid:
        mine = next((r for r in responses if getattr(r, "order_id", None) == oid), None)
    if mine is None and len(responses) == len(actions):
        mine = responses[next(i for i, a in enumerate(actions) if a is order)]
    return mine is not None and not mine.is_error()

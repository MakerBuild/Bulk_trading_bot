"""Edges found in an audit of the plumbing modules.

Each section names what used to happen. None of them is dramatic on its own;
each is a place where the code said one thing and did another quietly --
retried a typo three times, reported a transfer that went through as failed,
printed a proxy password, booked a buy as a sell.
"""

import logging
import math
import os

import pytest
import requests

# -- retry: what counts as transient ----------------------------------------


@pytest.mark.parametrize("exc", [
    requests.exceptions.InvalidURL("no host"),
    requests.exceptions.MissingSchema("no scheme"),
    requests.exceptions.TooManyRedirects("loop"),
    requests.exceptions.InvalidJSONError("bad body"),
    FileNotFoundError("settings.yaml"),
    PermissionError("locked"),
    OSError("anything else"),
])
def test_deterministic_failures_are_not_retried(exc):
    """`OSError` in the list made all of these 'transient': requests' own
    exceptions subclass it. Each fails the same way every time."""
    from bulkdn.retry import retry

    calls = []

    @retry("probe", attempts=3, delay=0)
    def failing():
        calls.append(1)
        raise exc

    with pytest.raises(type(exc)):
        failing()
    assert len(calls) == 1


@pytest.mark.parametrize("exc", [
    requests.ConnectionError("dropped"),
    requests.Timeout("slow"),
    requests.exceptions.ChunkedEncodingError("cut off"),
    ConnectionResetError("reset"),
    ConnectionAbortedError("aborted"),
    BrokenPipeError("pipe"),
    TimeoutError("socket timeout"),
])
def test_transport_faults_still_are(exc):
    from bulkdn.retry import is_transient

    assert is_transient(exc)


def test_a_5xx_raised_by_raise_for_status_is_retried_and_a_404_is_not():
    from bulkdn.retry import is_transient

    def http_error(status):
        response = requests.Response()
        response.status_code = status
        return requests.HTTPError(f"HTTP {status}", response=response)

    assert is_transient(http_error(502))
    assert is_transient(http_error(429))
    assert not is_transient(http_error(404))
    assert not is_transient(http_error(400))


# -- tx: a replayed transfer whose outcome is unknown -------------------------


class Reply:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


# Real base58 -- the preimage decodes the account. sha256 of a label, as in
# test_subaccounts: reproducible, and nobody's.
MASTER = "DqciofFTMjwbGhwi3ox2kqN1F2P3HLYECDSC5hRytcUo"
SUB = "5GyLKfzb9xWjeLbF1PL9ygZZtNF3Hw3khYHxqD3VzM83"


def transfer(monkeypatch, replies):
    """Run `submit_transfer` against scripted POST outcomes."""
    from bulk_api.common.signer import SignatureDomain

    from bulkdn import subaccounts
    from bulkdn.config import SIGNATURE_DOMAIN_NAME

    script = list(replies)
    sent = []

    def post(url, json, timeout):
        sent.append(json)
        outcome = script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    class Signer:
        public_key = MASTER

        class signing_key:
            @staticmethod
            def sign(_preimage):
                class Signed:
                    signature = b"\x01" * 64
                return Signed()

        def __init__(self, _key):
            pass

    monkeypatch.setattr("bulkdn.tx.TransactionSigner", Signer)
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr("bulkdn.retry.time.sleep", lambda _s: None)
    result = subaccounts.submit_transfer(
        http_url="https://example.test",
        private_key="unused",
        domain=SignatureDomain[SIGNATURE_DOMAIN_NAME],
        from_pubkey=MASTER,
        to_pubkey=SUB,
        margin_amount=25.0,
    )
    return result, sent


REFUSED = Reply(200, {"status": "error", "message": "nonce already used"})
ACCEPTED = Reply(200, {"status": "ok"})


def test_a_refusal_after_a_lost_response_is_unknown_not_failed(monkeypatch):
    """The first POST moved the money and its answer was lost; the replay of
    the same bytes was refused for reusing the nonce. Reported as failed, an
    operator sends it again -- and moves the money twice."""
    result, sent = transfer(monkeypatch, [requests.ReadTimeout("lost"), REFUSED])

    assert not result.ok
    assert result.uncertain is True
    assert sent[0] == sent[1], "the replay was not the same signed bytes"


def test_a_refusal_on_the_first_answer_is_a_plain_failure(monkeypatch):
    result, _ = transfer(monkeypatch, [REFUSED])
    assert not result.ok
    assert result.uncertain is False


def test_an_acceptance_after_a_retry_is_certain(monkeypatch):
    result, _ = transfer(monkeypatch, [requests.ReadTimeout("lost"), ACCEPTED])
    assert result.ok
    assert result.uncertain is False


def test_a_request_that_never_left_does_not_make_the_answer_uncertain(monkeypatch):
    result, _ = transfer(monkeypatch, [requests.ConnectTimeout("no route"), REFUSED])
    assert result.uncertain is False


def test_a_gateway_error_as_the_last_word_is_uncertain(monkeypatch):
    """A 502 may have been forwarded before the gateway gave up."""
    result, _ = transfer(monkeypatch, [Reply(502, {}), Reply(502, {}), Reply(502, {})])
    assert result.uncertain is True


def test_no_answer_at_all_says_uncertain_on_the_exception(monkeypatch):
    from bulkdn.retry import RetryExhausted

    with pytest.raises(RetryExhausted) as caught:
        transfer(monkeypatch, [requests.ReadTimeout("lost")] * 3)
    assert caught.value.uncertain is True


def test_the_tx_result_still_unpacks_as_a_triple(monkeypatch):
    from bulkdn.tx import TxResult

    result = TxResult({"nonce": "1"}, 200, {"status": "ok"}, uncertain=True)
    request, status, body = result
    assert (status, body["status"]) == (200, "ok")
    assert result.uncertain is False, "an acceptance is never uncertain"


def test_the_nonce_travels_in_the_form_confirmed_live(monkeypatch):
    """A JSON integer, written exactly. The string form the SDK uses for orders
    has not been tried on transfers, and this is a money-moving request."""
    import json

    _, sent = transfer(monkeypatch, [ACCEPTED])
    assert isinstance(sent[0]["nonce"], int)
    assert json.loads(json.dumps(sent[0]))["nonce"] == sent[0]["nonce"]


# -- subaccounts: amounts and names ------------------------------------------


@pytest.mark.parametrize("amount", [-50.0, -0.01, math.nan, math.inf])
def test_a_negative_or_non_finite_margin_is_refused(amount):
    """-50 used to encode as 'no margin' and succeed."""
    from bulkdn.subaccounts import serialize_create_sub_account

    with pytest.raises(ValueError, match="margin amount"):
        serialize_create_sub_account("desk-1", amount)


def test_zero_and_none_still_mean_absent():
    from bulkdn.subaccounts import serialize_create_sub_account

    assert serialize_create_sub_account("desk-1", 0.0)[-1] == 0x00
    assert serialize_create_sub_account("desk-1", None)[-1] == 0x00


def test_the_name_limit_is_counted_in_bytes():
    """32 Cyrillic letters are 64 bytes on the wire, and len() passed them."""
    from bulkdn.subaccounts import NAME_MAX_BYTES, serialize_create_sub_account

    with pytest.raises(ValueError, match="bytes"):
        serialize_create_sub_account("я" * 17)  # 17 characters, 34 bytes
    serialize_create_sub_account("я" * 16)  # 32 bytes exactly
    serialize_create_sub_account("x" * NAME_MAX_BYTES)


def test_transfer_refuses_non_finite():
    from bulkdn.subaccounts import serialize_transfer

    with pytest.raises(ValueError):
        serialize_transfer(MASTER, SUB, math.nan)


# -- ws_compat: a fill without a side ----------------------------------------


@pytest.fixture
def patched():
    from bulkdn.ws_compat import apply_ws_compat

    apply_ws_compat()


def test_a_fill_without_a_side_is_not_booked_as_a_sell(patched, caplog):
    """It defaulted to isBuy=False: a buy with no flag was booked as a sell of
    the same size, and the book read the leg off by twice the fill."""
    from bulk_api.messages.trade import Fill

    with caplog.at_level(logging.ERROR, logger="bulkdn.ws_compat"):
        fill = Fill.from_api({"sym": "BTC-USD", "sz": 0.5, "px": 100.0, "tid": "t1"})

    assert fill.size == 0.0, "an unsided fill was booked"
    assert fill.side_missing is True
    assert fill.symbol == "BTC-USD"
    assert any("without a side" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("fields, is_buy", [
    ({"b": True}, True),
    ({"isBuy": False}, False),
    ({"side": "buy"}, True),
    ({"side": "SELL"}, False),
])
def test_a_stated_side_is_still_read(patched, fields, is_buy):
    from bulk_api.common import Side
    from bulk_api.messages.trade import Fill

    fill = Fill.from_api({"sym": "BTC-USD", "sz": 0.5, **fields})
    assert fill.size == 0.5
    assert (fill.side == Side.BUY) is is_buy
    assert fill.side_missing is False


def test_the_connect_patch_stays_inside_the_sdk(patched):
    """Only the SDK's own reference is replaced; `websockets.connect` for the
    rest of the process is websockets'."""
    from bulk_api.api import bulk_ws
    from websockets.asyncio import client

    from bulkdn.ws_compat import _connect_with_ssl_fallback

    assert bulk_ws.ws_connect is _connect_with_ssl_fallback
    assert client.connect is not _connect_with_ssl_fallback


# -- proxy: the password never reaches a message -----------------------------


@pytest.mark.parametrize("url", [
    "user:hunter2@proxy.example.com:1080",           # no scheme
    "ftp://user:hunter2@proxy.example.com:1080",     # bad scheme
    "socks5h://user:hunter2@:1080",                  # no host
    "http://user:hunter2@proxy.example.com",         # no port
    "http://user:hunter2@proxy.example.com:port",    # port not a number
])
def test_a_refused_proxy_address_does_not_print_its_password(url):
    from bulkdn import proxy

    with pytest.raises(proxy.ProxyError) as caught:
        proxy.validate(url)
    assert "hunter2" not in str(caught.value)


def test_redaction_holds_for_an_address_without_a_scheme():
    from bulkdn.proxy import redacted

    assert "hunter2" not in redacted("user:hunter2@proxy.example.com:1080")


# -- keystore: the temp file ---------------------------------------------------


def _fast(monkeypatch):
    """Argon2id at the shipped parameters takes most of a second per call."""
    from bulkdn import keystore

    params = keystore.KdfParams(ops=1, mem=8 * 1024 * 1024)
    original = keystore.encrypt
    monkeypatch.setattr(
        keystore, "encrypt", lambda secret, password, params_=None: original(secret, password, params)
    )


def test_a_locked_destination_is_retried(tmp_path, monkeypatch):
    """What state.py already does: a scanner holding the file for a moment
    used to fail the save outright on Windows."""
    from bulkdn import keystore

    _fast(monkeypatch)
    monkeypatch.setattr(keystore.time, "sleep", lambda _s: None)
    real_replace = os.replace
    calls = []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("[WinError 5] Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(keystore.os, "replace", flaky)
    path = tmp_path / "private_key.local"
    keystore.save(str(path), "SEED", "pw")

    assert keystore.load(str(path), "pw") == "SEED"
    assert len(calls) == 3
    assert sorted(p.name for p in tmp_path.iterdir()) == ["private_key.local"]


def test_a_failed_save_leaves_no_temp_file_behind(tmp_path, monkeypatch):
    from bulkdn import keystore

    _fast(monkeypatch)
    monkeypatch.setattr(keystore.time, "sleep", lambda _s: None)

    def locked(src, dst):
        raise PermissionError("[WinError 5] Access is denied")

    monkeypatch.setattr(keystore.os, "replace", locked)
    with pytest.raises(PermissionError):
        keystore.save(str(tmp_path / "private_key.local"), "SEED", "pw")
    assert list(tmp_path.iterdir()) == [], "an encrypted copy was left lying there"


# -- referral: naming the owner ----------------------------------------------


def test_an_invite_is_not_credited_to_whoever_referred_the_account(monkeypatch):
    """Invited by the owner, referred by someone else: the referral code is
    the someone else's, and it used to be printed as the owner's."""
    from bulkdn import referral
    from bulkdn.referral import AccessConfig, check_access

    monkeypatch.setattr(referral, "SEALED_WALLETS", ())
    monkeypatch.setattr(referral, "SEALED_CODES", ())
    both = {
        "referred_by_code": "STRANGERS-CODE",
        "referred_by_wallet": "STRANGER-WALLET",
        "access": {"invited_by_code_id": "INV-1", "invited_by_wallet": "OWNER-WALLET"},
    }

    class Answer:
        status_code = 200

        def json(self):
            return both

    monkeypatch.setattr(requests, "get", lambda url, timeout: Answer())
    decision = check_access("W", AccessConfig(require_referral=True, wallets=["OWNER-WALLET"]))

    assert decision.allowed
    assert "invited by" in decision.reason
    assert "STRANGERS-CODE" not in decision.reason


# -- fees: dedup, fee spellings, and the page cap ------------------------------


def test_rows_without_slot_and_sequence_are_not_all_one_trade():
    """(None, None) was every such row's key, so all but the first were
    dropped as repeats."""
    from bulkdn.fees import Realised

    rows = [
        {"maker": "A", "taker": "X", "amount": 1.0, "price": 100.0, "fee": -0.1, "tradeId": "1"},
        {"maker": "A", "taker": "X", "amount": 1.0, "price": 100.0, "fee": -0.1, "tradeId": "2"},
        {"maker": "A", "taker": "Y", "amount": 2.0, "price": 100.0, "fee": -0.1},
        {"maker": "A", "taker": "Z", "amount": 3.0, "price": 100.0, "fee": -0.1},
    ]
    totals = Realised.from_fills(rows, {"A"}, set())
    assert totals.volume_usd == 700.0


def test_the_same_trade_seen_from_both_sides_without_ids_still_counts_once():
    from bulkdn.fees import Realised

    seen = set()
    row = {"maker": "A", "taker": "B", "amount": 1.0, "price": 100.0, "timestamp": 5}
    a = Realised.from_fills([dict(row, makerFee=0.0, isBuy=True)], {"A", "B"}, seen, user="A")
    b = Realised.from_fills([dict(row, takerFee=-0.05, isBuy=False)], {"A", "B"}, seen, user="B")
    assert (a + b).volume_usd == 100.0
    assert (a + b).fees_usd == pytest.approx(-0.05)


def test_maker_and_taker_fee_spellings_are_read():
    """A row with `takerFee` and no `fee` used to read as free."""
    from bulkdn.fees import Realised

    row = {"maker": "M", "taker": "T", "amount": 1.0, "price": 100.0,
           "makerFee": 0.0, "takerFee": -0.035, "slot": 1, "sequence": 1}
    assert Realised.from_fills([row], {"T"}, user="T").fees_usd == pytest.approx(-0.035)
    assert Realised.from_fills([row], {"M"}, user="M").fees_usd == 0.0


def test_hitting_the_page_cap_is_said_and_marked(monkeypatch, caplog):
    from bulkdn import fees

    def endless(http, user, limit, cursor):
        n = int(cursor or 0)
        return [{"amount": 1.0, "price": 1.0, "slot": n, "sequence": 0}], str(n + 1)

    monkeypatch.setattr(fees, "fills_page", endless)
    fees._TRUNCATION_WARNED.discard("CAPPED")
    with caplog.at_level(logging.WARNING, logger="bulkdn.fees"):
        total = fees.realised_for_account(None, "CAPPED", set(), max_pages=3)

    assert total.truncated is True
    assert any("lower bound" in r.getMessage() for r in caplog.records)


def test_a_walk_since_a_time_stops_at_it_when_pages_are_newest_first(monkeypatch):
    from bulkdn import fees

    # Stamped in nanoseconds, as the exchange stamps fills; the cutoff is in
    # milliseconds. Compared raw, every row read as newer than any cutoff.
    t0 = 1_790_000_000_000  # ms

    def ns(ms_after):
        return (t0 + ms_after) * 1_000_000

    pages = {
        None: ([{"amount": 1, "price": 1, "slot": 3, "sequence": 0, "timestamp": ns(300)},
                {"amount": 1, "price": 1, "slot": 2, "sequence": 0, "timestamp": ns(200)}], "p2"),
        "p2": ([{"amount": 1, "price": 1, "slot": 1, "sequence": 0, "timestamp": ns(150)},
                {"amount": 1, "price": 1, "slot": 0, "sequence": 0, "timestamp": ns(50)}], "p3"),
        "p3": ([{"amount": 1000, "price": 1, "slot": 9, "sequence": 0, "timestamp": ns(10)}], None),
    }
    asked = []

    def page(http, user, limit, cursor):
        asked.append(cursor)
        return pages[cursor]

    monkeypatch.setattr(fees, "fills_page", page)
    total = fees.realised_for_account(None, "U", set(), since_ms=t0 + 100)

    assert total.volume_usd == 3.0
    assert asked == [None, "p2"], "it walked past the point it was asked to stop at"

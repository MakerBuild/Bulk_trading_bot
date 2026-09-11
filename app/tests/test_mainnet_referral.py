"""Reading a referral that the public record does not carry.

A wallet created on mainnet comes back from /v1/aura/wallet with every
attribution field null, even while app.bulk.trade shows "You were referred by
MAKER" for that same wallet. The real record lives at /v1/aura/mainnet/referrals
and answers 401 without a key.

So the lookup is consulted only when a key is configured, and only when the
public record said nothing -- and it can never make things worse than a build
with no key at all, because every failure leaves the public answer standing.

The response shape is not published. These tests pin the tolerance rather than
a schema: the app bundle calls the fields `referrer_wallet` and `referral_code`
where the public record calls them `referred_by_wallet` and `referred_by_code`,
so both spellings are accepted until a real response settles it.
"""

import pytest
import requests

from bulkdn import referral
from bulkdn.referral import fetch_referral

WALLET = "EXAMPLE-MAINNET-WALLET"
OWNER = "EXAMPLE-OWNER-WALLET"

# What /v1/aura/wallet returns for a mainnet account: nothing to go on.
BARE = {
    "wallet": WALLET,
    "referral_code": None,
    "referred_by_wallet": None,
    "referred_by_code": None,
    "access": {"has_access": True, "source": "redeemed", "invited_by_wallet": None},
}


class Response:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


@pytest.fixture
def calls(monkeypatch):
    """Serve the public record, and whatever the test says for the key-gated one."""
    log = []

    def get(url, **kwargs):
        log.append({"url": url, "headers": kwargs.get("headers") or {}})
        if "mainnet/referrals" in url:
            return get.mainnet
        return Response(200, BARE)

    get.mainnet = Response(401, {"error": "missing x-aura-referral-api-key"})
    get.log = log
    monkeypatch.setattr(requests, "get", get)
    return get


@pytest.fixture(autouse=True)
def no_key(monkeypatch):
    monkeypatch.setattr(referral, "REFERRAL_API_KEY", "")


def with_key(monkeypatch, key="test-key"):
    monkeypatch.setattr(referral, "REFERRAL_API_KEY", key)


# -- when it is consulted ----------------------------------------------------


def test_without_a_key_the_gated_endpoint_is_never_called(calls):
    status = fetch_referral(WALLET)
    assert not status.has_origin
    assert not any("mainnet/referrals" in c["url"] for c in calls.log)


def test_with_a_key_an_empty_public_record_is_followed_up(calls, monkeypatch):
    with_key(monkeypatch)
    calls.mainnet = Response(200, {"referrer_wallet": OWNER, "referral_code": "MAKER"})

    status = fetch_referral(WALLET)
    assert status.referred_by_wallet == OWNER
    assert status.referred_by_code == "MAKER"
    assert status.has_origin


def test_the_key_travels_as_the_header_the_api_asks_for(calls, monkeypatch):
    with_key(monkeypatch, "abc123")
    calls.mainnet = Response(200, {"referrer_wallet": OWNER})

    fetch_referral(WALLET)
    gated = next(c for c in calls.log if "mainnet/referrals" in c["url"])
    assert gated["headers"]["x-aura-referral-api-key"] == "abc123"


def test_a_public_record_that_already_answers_is_not_followed_up(calls, monkeypatch):
    """One call, not two, for the accounts the public record does cover."""
    with_key(monkeypatch)

    def get(url, **kwargs):
        calls.log.append({"url": url, "headers": kwargs.get("headers") or {}})
        return Response(200, {**BARE, "referred_by_wallet": OWNER})

    monkeypatch.setattr(requests, "get", get)
    status = fetch_referral(WALLET)
    assert status.referred_by_wallet == OWNER
    assert not any("mainnet/referrals" in c["url"] for c in calls.log)


# -- it can never make things worse ------------------------------------------


def test_a_401_leaves_the_public_answer_standing(calls, monkeypatch):
    """An expired key must degrade to a keyless build, not refuse everyone."""
    with_key(monkeypatch)
    calls.mainnet = Response(401, {"error": "missing x-aura-referral-api-key"})
    assert not fetch_referral(WALLET).has_origin


def test_a_server_error_leaves_the_public_answer_standing(calls, monkeypatch):
    with_key(monkeypatch)
    calls.mainnet = Response(500)
    assert not fetch_referral(WALLET).has_origin


def test_an_exception_leaves_the_public_answer_standing(calls, monkeypatch):
    with_key(monkeypatch)

    def get(url, **kwargs):
        if "mainnet/referrals" in url:
            raise requests.ConnectionError("down")
        return Response(200, BARE)

    monkeypatch.setattr(requests, "get", get)
    assert not fetch_referral(WALLET).has_origin


def test_an_empty_body_changes_nothing(calls, monkeypatch):
    with_key(monkeypatch)
    calls.mainnet = Response(200, {})
    assert not fetch_referral(WALLET).has_origin


# -- tolerance, until a real response settles the shape ----------------------


@pytest.mark.parametrize("payload", [
    {"referrer_wallet": OWNER},
    {"referred_by_wallet": OWNER},
    {"referrerWallet": OWNER},
    {"referrer": OWNER},
    {"referral": {"referrer_wallet": OWNER}},
    {"data": {"referrer_wallet": OWNER}},
])
def test_the_referrer_is_found_under_several_spellings(calls, monkeypatch, payload):
    with_key(monkeypatch)
    calls.mainnet = Response(200, payload)
    assert fetch_referral(WALLET).referred_by_wallet == OWNER


def test_a_code_alone_is_enough_to_count_as_an_origin(calls, monkeypatch):
    with_key(monkeypatch)
    calls.mainnet = Response(200, {"referral_code": "MAKER"})
    status = fetch_referral(WALLET)
    assert status.referred_by_code == "MAKER"
    assert status.has_origin

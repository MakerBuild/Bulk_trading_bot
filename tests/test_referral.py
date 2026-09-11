"""Referral gating.

Response shapes here mirror what the live indexer actually returns:
a wallet with a referrer, a wallet without one, and a rejected address.
"""

import pytest
import requests

from bulkdn.config import ConfigError, _access_from_dict
from bulkdn.referral import AccessConfig, check_access, fetch_referral

WALLET = "C5RhFpsbsgvFpNJPLTbywMjfgejeHLg17e4VH7h7JgNx"
REFERRER = "GDMm3PDx6ZMY5gUUKuSAGeVpWw4YL22ZxRaPMDVSrNUv"

REFERRED = {
    "wallet": WALLET,
    "referral_code": "DISCORD",
    "referred_by_wallet": REFERRER,
    "referred_by_code": "VAULT",
    "referred_qualified": True,
}
NO_REFERRER = {
    "wallet": WALLET,
    "referral_code": None,
    "referred_by_wallet": None,
    "referred_by_code": None,
    "referred_qualified": None,
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="{}"):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _serve(monkeypatch, response):
    monkeypatch.setattr(
        requests, "get", lambda url, timeout: response
    )


# -- fetch -----------------------------------------------------------------


def test_fetch_parses_a_referred_wallet(monkeypatch):
    _serve(monkeypatch, FakeResponse(200, REFERRED))
    status = fetch_referral(WALLET)
    assert status.referred_by_code == "VAULT"
    assert status.referred_by_wallet == REFERRER
    assert status.own_code == "DISCORD"
    assert status.has_referrer


def test_fetch_parses_a_wallet_with_no_referrer(monkeypatch):
    _serve(monkeypatch, FakeResponse(200, NO_REFERRER))
    assert not fetch_referral(WALLET).has_referrer


def test_malformed_address_is_not_retried(monkeypatch):
    """A 400 means the address is wrong; it will be wrong next time too."""
    calls = []

    def fake_get(url, timeout):
        calls.append(1)
        return FakeResponse(400, None, text="bad request")

    monkeypatch.setattr(requests, "get", fake_get)
    with pytest.raises(Exception, match="rejected"):
        fetch_referral("nonsense")
    assert len(calls) == 1


def test_non_json_is_an_error(monkeypatch):
    _serve(monkeypatch, FakeResponse(200, None, text="<html>"))
    with pytest.raises(Exception, match="non-JSON"):
        fetch_referral(WALLET)


# -- decisions -------------------------------------------------------------


def test_disabled_gate_allows_everything():
    decision = check_access(WALLET, AccessConfig(require_referral=False))
    assert decision.allowed


def test_allows_a_matching_code(monkeypatch):
    _serve(monkeypatch, FakeResponse(200, REFERRED))
    decision = check_access(
        WALLET, AccessConfig(require_referral=True, codes=["VAULT"])
    )
    assert decision.allowed
    assert "VAULT" in decision.reason


def test_code_match_is_case_insensitive(monkeypatch):
    """People type these by hand."""
    _serve(monkeypatch, FakeResponse(200, REFERRED))
    decision = check_access(
        WALLET, AccessConfig(require_referral=True, codes=["  vault "])
    )
    assert decision.allowed


def test_allows_a_matching_referrer_wallet(monkeypatch):
    _serve(monkeypatch, FakeResponse(200, REFERRED))
    decision = check_access(
        WALLET, AccessConfig(require_referral=True, wallets=[REFERRER])
    )
    assert decision.allowed


def test_wallet_match_is_exact(monkeypatch):
    """base58 is case-sensitive -- a near-match is a different key."""
    _serve(monkeypatch, FakeResponse(200, REFERRED))
    decision = check_access(
        WALLET, AccessConfig(require_referral=True, wallets=[REFERRER.lower()])
    )
    assert not decision.allowed


def test_denies_a_different_code(monkeypatch):
    _serve(monkeypatch, FakeResponse(200, REFERRED))
    decision = check_access(
        WALLET, AccessConfig(require_referral=True, codes=["SOMETHINGELSE"])
    )
    assert not decision.allowed
    assert "not on the allowed list" in decision.reason


def test_denies_a_wallet_with_no_referrer(monkeypatch):
    _serve(monkeypatch, FakeResponse(200, NO_REFERRER))
    decision = check_access(
        WALLET, AccessConfig(require_referral=True, codes=["VAULT"])
    )
    assert not decision.allowed
    assert "did not sign up" in decision.reason


def test_indexer_outage_denies_by_default(monkeypatch):
    """Fail closed: the gate only blocks startup, so it cannot strand positions."""
    def boom(url, timeout):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr("bulkdn.retry.time.sleep", lambda _: None)

    decision = check_access(
        WALLET, AccessConfig(require_referral=True, codes=["VAULT"])
    )
    assert not decision.allowed
    assert "could not verify" in decision.reason


def test_indexer_outage_can_be_configured_to_allow(monkeypatch):
    def boom(url, timeout):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr("bulkdn.retry.time.sleep", lambda _: None)

    decision = check_access(
        WALLET,
        AccessConfig(require_referral=True, codes=["VAULT"], allow_on_error=True),
    )
    assert decision.allowed


# -- config ----------------------------------------------------------------


def test_config_off_by_default():
    assert not _access_from_dict({}).enabled


def test_config_accepts_a_single_unwrapped_code():
    cfg = _access_from_dict({"require_referral": True, "code": "VAULT"})
    assert cfg.codes == ["VAULT"]


def test_enabled_with_no_allowlist_is_an_error():
    """Would refuse every account, including the owner's."""
    with pytest.raises(ConfigError, match="no codes or wallets"):
        _access_from_dict({"require_referral": True})


def test_non_mapping_is_an_error():
    with pytest.raises(ConfigError, match="must be a mapping"):
        _access_from_dict(["nope"])

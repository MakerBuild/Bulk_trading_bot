"""Referral gating.

Response shapes here mirror what the live indexer actually returns:
a wallet with a referrer, a wallet without one, and a rejected address.
"""

import pytest
import requests

from bulkdn import referral
from bulkdn.config import ConfigError, _access_from_dict
from bulkdn.referral import AccessConfig, check_access, fetch_referral


@pytest.fixture(autouse=True)
def unsealed(monkeypatch):
    """Most of this file exercises the configurable gate.

    A sealed build ignores settings.yaml by design, so these tests would all be
    testing the seal instead of what they were written for. Sealing has its own
    tests in test_sealed_gate.py.
    """
    monkeypatch.setattr(referral, "SEALED_WALLETS", ())
    monkeypatch.setattr(referral, "SEALED_CODES", ())
    monkeypatch.setattr(referral, "SEALED_INVITE_CODES", ())
    monkeypatch.setattr(referral, "SEALED_OWNER_WALLETS", ())

WALLET = "EXAMPLE-REFERRED-WALLET"
REFERRER = "EXAMPLE-REFERRER-WALLET"

REFERRED = {
    "wallet": WALLET,
    "referral_code": "EXAMPLE-OWN-CODE",
    "referred_by_wallet": REFERRER,
    "referred_by_code": "EXAMPLE-REFERRER-CODE",
    "referred_qualified": True,
}
NO_REFERRER = {
    "wallet": WALLET,
    "referral_code": None,
    "referred_by_wallet": None,
    "referred_by_code": None,
    "referred_qualified": None,
}
# Arrived by invite rather than referral: referred_by_* stay null and the
# origin lives under `access`. Most accounts look like this.
INVITED = {
    "wallet": WALLET,
    "referral_code": None,
    "referred_by_wallet": None,
    "referred_by_code": None,
    "access": {
        "has_access": True,
        "invited_by_code_id": "BULK-EDA-QQF",
        "invited_by_wallet": REFERRER,
    },
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
    assert status.referred_by_code == "EXAMPLE-REFERRER-CODE"
    assert status.referred_by_wallet == REFERRER
    assert status.own_code == "EXAMPLE-OWN-CODE"
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
        WALLET, AccessConfig(require_referral=True, codes=["EXAMPLE-REFERRER-CODE"])
    )
    assert decision.allowed
    assert "EXAMPLE-REFERRER-CODE" in decision.reason


def test_code_match_is_case_insensitive(monkeypatch):
    """People type these by hand."""
    _serve(monkeypatch, FakeResponse(200, REFERRED))
    decision = check_access(
        WALLET, AccessConfig(require_referral=True, codes=["  example-referrer-code "])
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
        WALLET, AccessConfig(require_referral=True, codes=["EXAMPLE-REFERRER-CODE"])
    )
    assert not decision.allowed
    assert "did not sign up" in decision.reason


def test_one_wallet_admits_both_referred_and_invited(monkeypatch):
    """The point of matching on wallet: two routes, one configured address.

    Most accounts arrive by invite, not referral -- for the owner's wallet the
    split was 15 referred against 32 invited -- so a gate reading only
    referred_by_* would refuse most of the people it should admit.
    """
    config = AccessConfig(require_referral=True, wallets=[REFERRER])

    _serve(monkeypatch, FakeResponse(200, REFERRED))
    referred = check_access(WALLET, config)

    _serve(monkeypatch, FakeResponse(200, INVITED))
    invited = check_access(WALLET, config)

    assert referred.allowed and "referred by wallet" in referred.reason
    assert invited.allowed and "invited by wallet" in invited.reason


def test_invited_wallet_is_denied_for_a_different_inviter(monkeypatch):
    _serve(monkeypatch, FakeResponse(200, INVITED))
    decision = check_access(
        WALLET, AccessConfig(require_referral=True, wallets=["SomeoneElse"])
    )
    assert not decision.allowed
    assert "invite code BULK-EDA-QQF" in decision.reason


def test_an_explicit_invite_code_is_accepted(monkeypatch):
    _serve(monkeypatch, FakeResponse(200, INVITED))
    decision = check_access(
        WALLET,
        AccessConfig(require_referral=True, invite_codes=["bulk-eda-qqf"]),
    )
    assert decision.allowed
    assert "invited by code" in decision.reason


def test_missing_access_block_is_not_an_error(monkeypatch):
    """Older records have no `access` key at all."""
    _serve(monkeypatch, FakeResponse(200, REFERRED))
    status = fetch_referral(WALLET)
    assert status.invited_by_wallet is None
    assert status.has_referrer


def test_neither_route_is_denied_with_a_clear_reason(monkeypatch):
    _serve(monkeypatch, FakeResponse(200, NO_REFERRER))
    decision = check_access(
        WALLET, AccessConfig(require_referral=True, wallets=[REFERRER])
    )
    assert not decision.allowed
    assert "referral or invite" in decision.reason


def test_owner_wallet_runs_without_a_referral(monkeypatch):
    """The gate must not lock out its own author.

    A referrer was not referred by themselves, so the owner's wallet has a null
    referrer and every code check fails for it.
    """
    _serve(monkeypatch, FakeResponse(200, NO_REFERRER))
    decision = check_access(
        WALLET,
        AccessConfig(require_referral=True, codes=["EXAMPLE-REFERRER-CODE"], owner_wallets=[WALLET]),
    )
    assert decision.allowed
    assert decision.reason == "owner wallet"


def test_owner_wallet_skips_the_indexer_entirely(monkeypatch):
    """So the owner can still run while the indexer is down."""
    def boom(url, timeout):
        raise AssertionError("the indexer must not be contacted for an owner wallet")

    monkeypatch.setattr(requests, "get", boom)
    decision = check_access(
        WALLET, AccessConfig(require_referral=True, owner_wallets=[WALLET])
    )
    assert decision.allowed


def test_owner_list_alone_satisfies_validation():
    """An owner-only build is a legitimate configuration, not an empty allowlist."""
    cfg = _access_from_dict({"require_referral": True, "owner_wallet": WALLET})
    assert cfg.owner_wallets == [WALLET]


def test_indexer_outage_denies_by_default(monkeypatch):
    """Fail closed: the gate only blocks startup, so it cannot strand positions."""
    def boom(url, timeout):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr("bulkdn.retry.time.sleep", lambda _: None)

    decision = check_access(
        WALLET, AccessConfig(require_referral=True, codes=["EXAMPLE-REFERRER-CODE"])
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
        AccessConfig(require_referral=True, codes=["EXAMPLE-REFERRER-CODE"], allow_on_error=True),
    )
    assert decision.allowed


# -- config ----------------------------------------------------------------


def test_config_off_by_default():
    assert not _access_from_dict({}).enabled


def test_config_accepts_a_single_unwrapped_code():
    cfg = _access_from_dict({"require_referral": True, "code": "EXAMPLE-REFERRER-CODE"})
    assert cfg.codes == ["EXAMPLE-REFERRER-CODE"]


def test_enabled_with_no_allowlist_is_an_error():
    """Would refuse every account, including the owner's."""
    with pytest.raises(ConfigError, match="no codes, wallets, invite_codes"):
        _access_from_dict({"require_referral": True})


def test_non_mapping_is_an_error():
    with pytest.raises(ConfigError, match="must be a mapping"):
        _access_from_dict(["nope"])

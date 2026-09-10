"""Per-account licence.

The point of the signature is that the operator holds the file and cannot
usefully change it. So the tests that matter are the tampering ones: every
field the licence asserts must be pinned by the signature, and every distinct
failure must be distinguishable, since "it does not work" tells someone whose
licence merely lapsed nothing useful.

What is NOT tested, because it is not true: that this cannot be bypassed. The
bot ships as source and the gate is a function call.
"""

import json
import time

import pytest

from bulkdn import license as licensing

ACCOUNT = "BR4SV1CRKygGWCsb1zF3g38Xc68b31WkEagdk8hVedB8"
OTHER = "3AbM7XE9ikZW82rPUwRNnDovs3DMgvWgfPdckFnATSTe"


@pytest.fixture
def author():
    secret, public = licensing.keygen()
    return secret, public


@pytest.fixture
def licence(author):
    secret, _ = author
    return licensing.issue(account=ACCOUNT, signing_key=secret, days=30)


def test_a_licence_verifies_for_its_account(author, licence):
    _, public = author
    result = licensing.verify(licence, ACCOUNT, public)
    assert result.account == ACCOUNT
    assert result.code == "MAKER"
    assert 29 < result.remaining_days() <= 30


def test_it_does_not_verify_for_another_account(author, licence):
    _, public = author
    with pytest.raises(licensing.LicenseError, match="but this bot signs as"):
        licensing.verify(licence, OTHER, public)


def test_rewriting_the_account_breaks_the_signature(author, licence):
    """The obvious attack: put your own pubkey in someone else's licence."""
    _, public = author
    forged = {**licence, "account": OTHER}
    with pytest.raises(licensing.LicenseError, match="does not check out"):
        licensing.verify(forged, OTHER, public)


def test_extending_the_expiry_breaks_the_signature(author, licence):
    _, public = author
    forged = {**licence, "expires": licence["expires"] + 10_000_000}
    with pytest.raises(licensing.LicenseError, match="does not check out"):
        licensing.verify(forged, ACCOUNT, public)


def test_a_licence_signed_by_someone_else_is_refused(author):
    """Anyone can run keygen; only the author's key opens this build."""
    _, public = author
    stranger, _ = licensing.keygen()
    forged = licensing.issue(account=ACCOUNT, signing_key=stranger, days=30)
    with pytest.raises(licensing.LicenseError, match="does not check out"):
        licensing.verify(forged, ACCOUNT, public)


def test_a_different_code_is_refused(author):
    secret, public = author
    other_code = licensing.issue(
        account=ACCOUNT, signing_key=secret, days=30, code="SOMEONE-ELSE"
    )
    with pytest.raises(licensing.LicenseError, match="not 'MAKER'"):
        licensing.verify(other_code, ACCOUNT, public)


def test_an_expired_licence_says_so(author):
    """Distinct from a bad signature: this one is renewable."""
    secret, public = author
    stale = licensing.issue(
        account=ACCOUNT,
        signing_key=secret,
        days=1,
        now=int(time.time()) - 3 * 86400,
    )
    with pytest.raises(licensing.LicenseError, match="expired"):
        licensing.verify(stale, ACCOUNT, public)


def test_days_zero_never_expires(author):
    secret, public = author
    perpetual = licensing.issue(account=ACCOUNT, signing_key=secret, days=0)
    result = licensing.verify(perpetual, ACCOUNT, public)
    assert result.expires == 0
    assert result.expired is False
    assert result.remaining_days() is None


def test_missing_fields_are_named(author, licence):
    _, public = author
    for field in ("account", "code", "issued", "expires", "signature"):
        broken = {k: v for k, v in licence.items() if k != field}
        with pytest.raises(licensing.LicenseError, match=field):
            licensing.verify(broken, ACCOUNT, public)


def test_a_build_with_no_author_key_refuses_everything(licence):
    """An unconfigured build must fail closed, not open."""
    with pytest.raises(licensing.LicenseError, match="no author key"):
        licensing.verify(licence, ACCOUNT, "")


def test_issuing_for_a_non_pubkey_is_refused(author):
    secret, _ = author
    for bad in ("", "not-base58!!", "abc"):
        with pytest.raises(licensing.LicenseError):
            licensing.issue(account=bad, signing_key=secret)


def test_signing_bytes_are_order_independent():
    """Signer and verifier must agree without sharing a serialiser."""
    payload = {"account": ACCOUNT, "code": "MAKER", "issued": 1, "expires": 2}
    shuffled = {"expires": 2, "code": "MAKER", "account": ACCOUNT, "issued": 1}
    assert licensing.signing_bytes(payload) == licensing.signing_bytes(shuffled)


# -- files ------------------------------------------------------------------


def test_a_missing_file_explains_how_to_get_one(tmp_path):
    with pytest.raises(licensing.LicenseError, match="MAKER"):
        licensing.load(str(tmp_path / "license.json"))


def test_a_corrupt_file_is_reported_as_such(tmp_path):
    path = tmp_path / "license.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(licensing.LicenseError, match="not valid JSON"):
        licensing.load(str(path))


def test_enforce_reads_the_file_and_checks_it(tmp_path, author, licence, monkeypatch):
    _, public = author
    monkeypatch.setattr(licensing, "AUTHOR_VERIFY_KEY", public)
    path = tmp_path / "license.json"
    path.write_text(json.dumps(licence), encoding="utf-8")

    assert licensing.enforce(ACCOUNT, str(path)).account == ACCOUNT
    with pytest.raises(licensing.LicenseError):
        licensing.enforce(OTHER, str(path))

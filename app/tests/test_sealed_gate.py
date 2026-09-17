"""The gate when the build carries its own owner identity.

Reading that out of settings.yaml makes every field of it a bypass: `wallets`
and `codes` add allowed referrers, `require_referral: false` switches the gate
off, and `allow_on_error: true` turns a pulled network cable into a pass. So a
sealed build ignores that block entirely, and each of those is a test here.

This is a speed bump, not enforcement -- the source ships and the check is one
edit away from gone. What these tests pin is that the edit has to be to the
program, not to a config file.
"""

import pytest
import requests

from bulkdn import referral
from bulkdn.referral import AccessConfig, check_access

OWNER = "EXAMPLE-OWNER-REFERRAL-WALLET"
OWNER_CODE = "EXAMPLE-OWNER-CODE"
OUTSIDER = "EXAMPLE-SOMEONE-ELSES-WALLET"
CALLER = "EXAMPLE-CALLER-WALLET"


@pytest.fixture(autouse=True)
def sealed(monkeypatch):
    monkeypatch.setattr(referral, "SEALED_WALLETS",
                        (referral.wallet_digest(OWNER),))
    monkeypatch.setattr(referral, "SEALED_CODES",
                        (referral.code_digest(OWNER_CODE),))


def serve(monkeypatch, payload):
    class Response:
        status_code = 200

        def json(self):
            return payload

        def raise_for_status(self):
            pass

    monkeypatch.setattr(requests, "get", lambda *a, **k: Response())


def referred_by(wallet=None, code=None):
    return {
        "wallet": CALLER,
        "referral_code": None,
        "referred_by_wallet": wallet,
        "referred_by_code": code,
        "referred_qualified": True,
    }


# -- the config cannot widen the gate ---------------------------------------


def test_the_sealed_wallet_still_admits(monkeypatch):
    """The baseline: sealing did not break the thing it protects."""
    serve(monkeypatch, referred_by(wallet=OWNER))
    assert check_access(CALLER, AccessConfig()).allowed


def test_deleting_the_wallet_from_the_config_changes_nothing(monkeypatch):
    serve(monkeypatch, referred_by(wallet=OWNER))
    empty = AccessConfig(require_referral=True, wallets=[], codes=[])
    assert check_access(CALLER, empty).allowed


def test_substituting_another_wallet_in_the_config_admits_nobody(monkeypatch):
    """The substitution this is named for: swap the address, keep the gate."""
    serve(monkeypatch, referred_by(wallet=OUTSIDER))
    swapped = AccessConfig(require_referral=True, wallets=[OUTSIDER])
    assert not check_access(CALLER, swapped).allowed


def test_adding_a_wallet_to_the_config_admits_nobody(monkeypatch):
    serve(monkeypatch, referred_by(wallet=OUTSIDER))
    widened = AccessConfig(require_referral=True, wallets=[OWNER, OUTSIDER])
    assert not check_access(CALLER, widened).allowed


def test_adding_a_code_to_the_config_admits_nobody(monkeypatch):
    serve(monkeypatch, referred_by(code="SOMEONE-ELSES-CODE"))
    widened = AccessConfig(require_referral=True, codes=["SOMEONE-ELSES-CODE"])
    assert not check_access(CALLER, widened).allowed


# -- the config cannot switch the gate off ----------------------------------


def test_require_referral_false_does_not_disable_a_sealed_build(monkeypatch):
    serve(monkeypatch, referred_by(wallet=OUTSIDER))
    off = AccessConfig(require_referral=False)
    assert not check_access(CALLER, off).allowed


def test_an_empty_access_block_does_not_disable_a_sealed_build(monkeypatch):
    serve(monkeypatch, referred_by(wallet=OUTSIDER))
    assert not check_access(CALLER, AccessConfig()).allowed


def test_allow_on_error_cannot_turn_an_outage_into_a_pass(monkeypatch):
    """Otherwise the bypass is: set the flag, unplug the network."""
    def refuse(*_a, **_k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "get", refuse)
    lenient = AccessConfig(require_referral=True, allow_on_error=True)
    assert not check_access(CALLER, lenient).allowed


# -- what the source gives away ---------------------------------------------


def test_the_address_is_not_in_the_source():
    """A reader gets a digest, not the value to substitute."""
    import pathlib
    import re

    source = pathlib.Path(referral.__file__).read_text(encoding="utf-8")
    for sealed_value in referral.SEALED_WALLETS:
        assert re.fullmatch(r"[0-9a-f]{64}", sealed_value), "not a sha256 digest"

    # And nothing address-shaped sits beside them. Written as a search rather
    # than a comparison against the real address, so this test does not carry
    # the value it exists to keep out of the file.
    #
    # The digests are removed first: hex is a subset of the base58 alphabet, so
    # a digest matches the pattern for an address and would mask a real one.
    block = source[source.index("SEALED_WALLETS"):source.index("def wallet_digest")]
    block = re.sub(r"[0-9a-f]{64}", "", block)
    assert not re.search(r"[1-9A-HJ-NP-Za-km-z]{32,44}", block)


def test_digests_fold_case_for_codes_but_not_for_wallets():
    """base58 is case-sensitive: a near-match is a different key, not a typo."""
    assert referral.code_digest(" example ") == referral.code_digest("EXAMPLE")
    assert referral.wallet_digest("ABC") != referral.wallet_digest("abc")
    assert referral.wallet_digest(" ABC ") == referral.wallet_digest("ABC")


def test_an_unsealed_build_still_reads_the_config(monkeypatch):
    """Someone forking this for their own use is not locked out of configuring it."""
    monkeypatch.setattr(referral, "SEALED_WALLETS", ())
    monkeypatch.setattr(referral, "SEALED_CODES", ())
    monkeypatch.setattr(referral, "SEALED_OPERATORS", ())
    assert not referral.is_sealed()

    serve(monkeypatch, referred_by(wallet=OUTSIDER))
    theirs = AccessConfig(require_referral=True, wallets=[OUTSIDER])
    assert check_access(CALLER, theirs).allowed


# -- the direct operator list -----------------------------------------------
#
# Admitting one account that signed up under someone else's link. The
# alternative was adding that account's referrer to SEALED_WALLETS, which is a
# list of REFERRERS: it would have admitted everyone that person has referred
# and everyone they refer later, none of whom the owner ever sees.

NAMED = "EXAMPLE-NAMED-OPERATOR"


@pytest.fixture
def named_only(monkeypatch):
    monkeypatch.setattr(referral, "SEALED_WALLETS", ())
    monkeypatch.setattr(referral, "SEALED_CODES", ())
    monkeypatch.setattr(
        referral, "SEALED_OPERATORS", (referral.wallet_digest(NAMED),)
    )


def test_a_named_wallet_is_admitted(named_only, monkeypatch):
    serve(monkeypatch, referred_by(wallet=OUTSIDER))
    assert check_access(NAMED, AccessConfig()).allowed


def test_naming_one_wallet_seals_the_build(named_only):
    """Otherwise the config would still be read, and the config is editable by
    whoever has the archive."""
    assert referral.is_sealed()


def test_a_named_wallet_runs_while_the_indexer_is_down(named_only, monkeypatch):
    """The owner has already answered, in the build, the question the indexer
    would be asked."""
    def boom(*a, **k):
        raise TimeoutError()

    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr("bulkdn.retry.time.sleep", lambda _: None)
    assert check_access(NAMED, AccessConfig()).allowed


def test_everyone_else_still_goes_through_the_indexer(named_only, monkeypatch):
    """Naming one account must not widen the gate for anyone else."""
    serve(monkeypatch, referred_by(wallet=OUTSIDER))
    assert not check_access(CALLER, AccessConfig()).allowed


def test_the_config_cannot_name_an_operator(monkeypatch):
    """The one field that is never read from settings.yaml. Every other is,
    for a fork -- but a line anybody could add to admit themselves is not a
    gate, and the config ships in the archive."""
    monkeypatch.setattr(referral, "SEALED_WALLETS", ())
    monkeypatch.setattr(referral, "SEALED_CODES", ())
    monkeypatch.setattr(referral, "SEALED_OPERATORS", ())

    serve(monkeypatch, referred_by(wallet=OUTSIDER))
    # There is no such field; the closest thing is `wallets`, which names
    # referrers, so naming the CALLER there admits nobody.
    theirs = AccessConfig(require_referral=True, wallets=[CALLER])
    assert not check_access(CALLER, theirs).allowed


def test_a_named_operator_is_reported_as_such(named_only, monkeypatch):
    """The log has to say which route admitted an account, or an audit after
    the fact cannot tell a referral from a name in the build."""
    serve(monkeypatch, referred_by(wallet=OUTSIDER))
    reason = check_access(NAMED, AccessConfig()).reason
    assert "directly" in reason and NAMED in reason


def test_the_named_list_holds_digests_not_addresses():
    """Same rule as SEALED_WALLETS: a reader gets a digest, not the value."""
    import pathlib
    import re

    for value in referral.SEALED_OPERATORS:
        assert re.fullmatch(r"[0-9a-f]{64}", value), "not a sha256 digest"

    source = pathlib.Path(referral.__file__).read_text(encoding="utf-8")
    block = source[source.index("SEALED_OPERATORS"):source.index("def wallet_digest")]
    block = re.sub(r"[0-9a-f]{64}", "", block)
    assert not re.search(r"[1-9A-HJ-NP-Za-km-z]{32,44}", block)

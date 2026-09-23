"""The menu with more than one master key in private_key.local.

It used to read `config.private_key`, which is the first line of that file, so
an operator who had added a second master saw one master in the balance table,
one master's fills in the history, and one master's trading in the progress
figures the execution target is judged against. Nothing said the others were
missing; they simply were not there.

No real keys here. The signer is stubbed, so every string below is a string.
"""

import pytest

from bulkdn import menu
from bulkdn.fees import Realised, realised_for_trees

# Two masters, one with two sub-accounts and one with a single sub.
TREES = {
    "key-one": ["one-sub-a", "one-sub-b"],
    "key-two": ["two-sub-a"],
}


def _owner(pubkey: str) -> str:
    """Which key signs for this account."""
    for key, subs in TREES.items():
        if pubkey == f"{key}-master" or pubkey in subs:
            return key
    raise AssertionError(f"no key owns {pubkey}")


class FakeSigner:
    def __init__(self, private_key):
        self.public_key = f"{private_key}-master"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def exchange(monkeypatch):
    """Answers `/account` for these two trees, and 404 for anything else."""
    import bulk_api.common.signer as signer_mod

    monkeypatch.setattr(signer_mod, "TransactionSigner", FakeSigner)

    def post(url, json=None, timeout=None):
        user = (json or {}).get("user")
        for key, subs in TREES.items():
            if user == f"{key}-master":
                return FakeResponse({"subAccounts": [{"pubkey": s} for s in subs]})
        return FakeResponse({}, status_code=404)

    monkeypatch.setattr(menu.requests, "post", post)


class StubConfig:
    http_url = "https://x/api/v1"
    signature_domain_name = "MAINNET"

    def __init__(self, keys):
        self.private_keys = list(keys)
        self.private_key = self.private_keys[0]


# -- what the menu can see --------------------------------------------------


def test_every_key_gets_a_tree():
    trees = menu._trees(StubConfig(TREES))

    assert [tree.master for tree in trees] == ["key-one-master", "key-two-master"]
    assert [tree.index for tree in trees] == [1, 2]
    assert trees[0].subs == ("one-sub-a", "one-sub-b")


def test_every_account_under_every_key_is_listed():
    accounts = menu._accounts(StubConfig(TREES))

    assert [pubkey for _, pubkey in accounts] == [
        "key-one-master",
        "one-sub-a",
        "one-sub-b",
        "key-two-master",
        "two-sub-a",
    ]


def test_one_key_is_not_numbered():
    """The common case still reads the way it always did."""
    accounts = menu._accounts(StubConfig(["key-one"]))

    assert [label for label, _ in accounts] == ["master", "sub", "sub"]


def test_several_keys_are_numbered():
    """Two rows both saying `master` would be two rows about one account."""
    labels = [label for label, _ in menu._accounts(StubConfig(TREES))]

    assert labels == ["master 1", "sub 1", "sub 1", "master 2", "sub 2"]


def test_a_key_with_no_account_names_itself():
    config = StubConfig([*TREES, "key-three"])

    with pytest.raises(menu.NoAccountTree) as raised:
        menu._trees(config)

    assert "key 3" in str(raised.value), "which of the three is not obvious"


# -- what margin can and cannot do ------------------------------------------


def _balances(monkeypatch, amounts, *, confirm):
    monkeypatch.setattr(menu, "_transferable", lambda _c, pk: amounts[pk])
    monkeypatch.setattr(menu, "_confirm", lambda _what: confirm)
    monkeypatch.setattr(menu, "_pause", lambda: None)


def test_margin_is_never_planned_across_two_masters(monkeypatch):
    """A transfer is signed by the key owning both ends, so this cannot work.

    Levelling the whole pool to one figure would need an on-chain withdrawal
    and deposit. Planning it anyway would only queue transfers the exchange
    rejects -- one rejection per account, with the operator left to work out
    why the number never moved.
    """
    _balances(monkeypatch, {
        "key-one-master": 100.0,
        "one-sub-a": 0.0,
        "one-sub-b": 0.0,
        "key-two-master": 0.0,
        "two-sub-a": 60.0,
    }, confirm=False)

    weighed = []
    real_settle = menu._settle

    def watch(rows):
        weighed.append(rows)
        return real_settle(rows)

    monkeypatch.setattr(menu, "_settle", watch)

    menu._balance_subaccounts(StubConfig(TREES))

    assert weighed, "no tree was balanced at all"
    for rows in weighed:
        owners = {_owner(pubkey) for _, pubkey, _ in rows}
        assert len(owners) == 1, f"{owners} weighed against each other"


def test_a_transfer_is_signed_by_the_key_that_owns_it(monkeypatch):
    _balances(monkeypatch, {
        "key-one-master": 90.0,
        "one-sub-a": 0.0,
        "one-sub-b": 0.0,
        "key-two-master": 40.0,
        "two-sub-a": 0.0,
    }, confirm=True)

    sent = []

    class Result:
        ok = True
        response_json = {}

    def submit_transfer(*, http_url, private_key, domain, from_pubkey,
                        to_pubkey, margin_amount):
        sent.append((private_key, from_pubkey, to_pubkey))
        return Result()

    import bulkdn.subaccounts as subaccounts_mod

    monkeypatch.setattr(subaccounts_mod, "submit_transfer", submit_transfer)

    menu._balance_subaccounts(StubConfig(TREES))

    assert len(sent) == 3, "each tree should have levelled its own accounts"
    for private_key, src, dst in sent:
        assert _owner(src) == private_key == _owner(dst)


# -- what counts as trading with yourself -----------------------------------


def _one_page(monkeypatch, rows):
    """Every account answers with the same single page of fills."""
    import bulkdn.fees as fees_mod

    monkeypatch.setattr(
        fees_mod, "fills_page", lambda http, user, limit, cursor: (rows, None)
    )

    class Http:
        base_url = "https://x/api/v1"

    return Http()


def _fill(maker, taker, notional):
    return [{
        "amount": 1.0, "price": notional, "fee": -0.1,
        "maker": maker, "taker": taker, "slot": 7, "sequence": 1,
    }]


TWO_TREES = [["key-one-master", "one-sub-a"], ["key-two-master", "two-sub-a"]]


def test_a_trade_between_two_masters_is_not_a_self_trade(monkeypatch):
    """Which is the whole reason for holding more than one key.

    The exchange links a sub-account to the master that created it. Nothing
    links two masters to each other, so it cannot see that this trade had the
    same owner on both sides -- and the tier it quotes reflects that. Scoring
    it as a self-trade here would report a tier volume the exchange does not
    agree with.
    """
    http = _one_page(monkeypatch, _fill("one-sub-a", "two-sub-a", 1000.0))

    totals = realised_for_trees(http, TWO_TREES)

    assert totals.self_trade_volume_usd == 0.0
    assert totals.tier_volume_usd == totals.volume_usd == 1000.0


def test_a_trade_inside_one_tree_still_is_one(monkeypatch):
    http = _one_page(monkeypatch, _fill("key-one-master", "one-sub-a", 1000.0))

    totals = realised_for_trees(http, TWO_TREES)

    assert totals.self_trade_volume_usd == 1000.0
    assert totals.tier_volume_usd == 0.0


def test_a_cross_key_trade_is_still_counted_only_once(monkeypatch):
    """We see both views of it, which is exactly why it can be doubled."""
    http = _one_page(monkeypatch, _fill("one-sub-a", "two-sub-a", 1000.0))

    totals = realised_for_trees(http, [["one-sub-a"], ["two-sub-a"]])

    assert totals.volume_usd == 1000.0, "counted from both histories"
    # Both sides were charged, and both are ours to pay.
    assert totals.fees_usd == pytest.approx(-0.2)


def test_no_trees_is_no_trading(monkeypatch):
    assert realised_for_trees(_one_page(monkeypatch, []), []) == Realised()


# -- collecting margin back to the master -----------------------------------


def test_collect_sweeps_each_sub_into_its_own_master(monkeypatch):
    """Every sub under every key, each into the master that owns it."""
    _balances(monkeypatch, {
        "one-sub-a": 10.009,
        "one-sub-b": 0.0,
        "two-sub-a": 25.0,
    }, confirm=True)

    sent = []

    class Result:
        ok = True
        response_json = {}

    def submit_transfer(*, http_url, private_key, domain, from_pubkey,
                        to_pubkey, margin_amount):
        sent.append((private_key, from_pubkey, to_pubkey, margin_amount))
        return Result()

    import bulkdn.subaccounts as subaccounts_mod

    monkeypatch.setattr(subaccounts_mod, "submit_transfer", submit_transfer)

    menu._collect_to_master(StubConfig(TREES))

    assert sent == [
        # Floored: 10.01 is more than the account holds.
        ("key-one", "one-sub-a", "key-one-master", 10.0),
        ("key-two", "two-sub-a", "key-two-master", 25.0),
    ]


def test_collect_submits_nothing_unconfirmed(monkeypatch):
    _balances(monkeypatch, {"one-sub-a": 5.0, "one-sub-b": 5.0, "two-sub-a": 5.0},
              confirm=False)

    import bulkdn.subaccounts as subaccounts_mod

    def submit_transfer(**_kw):
        raise AssertionError("submitted without a yes")

    monkeypatch.setattr(subaccounts_mod, "submit_transfer", submit_transfer)

    menu._collect_to_master(StubConfig(TREES))

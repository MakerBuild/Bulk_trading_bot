"""Every account under every key, as one pool of sessions.

The thing that makes a pool of a hundred accounts possible is that the traded
account travels in each transaction rather than being a property of the
connection. Ten master keys with ten sub-accounts apiece therefore need ten
sockets, not a hundred and ten -- against an exchange that answered 429 to two
accounts polling every five seconds.

No real keys here. `TransactionSigner` is stubbed, so the strings below are
strings.
"""

import pytest

from bulkdn import accounts as accounts_mod
from bulkdn.accounts import build_pool

SYMBOLS = ["BTC-USD"]


class FakeSigner:
    """Derives a pubkey from the key text, so tests can predict both."""

    def __init__(self, private_key):
        self.public_key = f"{private_key}-pub"


@pytest.fixture(autouse=True)
def no_real_crypto_or_sockets(monkeypatch):
    monkeypatch.setattr(accounts_mod, "TransactionSigner", FakeSigner)
    monkeypatch.setattr(accounts_mod, "BulkHttpClient", lambda **kwargs: object())

    class FakeWs:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.accounts = list(dict.fromkeys(kwargs.get("accounts") or []))

    monkeypatch.setattr(accounts_mod, "RoutedWsClient", FakeWs)


def discovery(tree):
    """`tree` maps a key to the sub-accounts under it."""
    def discover(*, private_key, http_url, timeout=25):
        return f"{private_key}-pub", tree[private_key]
    return discover


def pool(tree, keys=None):
    return build_pool(
        private_keys=keys if keys is not None else list(tree),
        ws_url="wss://x",
        http_url="https://x",
        domain=None,
        symbols=SYMBOLS,
        dry_run=True,
        discover=discovery(tree),
    )


# -- what ends up in it -----------------------------------------------------


def test_the_master_and_all_its_children():
    sessions = pool({"k1": ["a", "b", "c"]})
    assert [s.pubkey for s in sessions] == ["k1-pub", "a", "b", "c"]


def test_several_keys_in_the_order_the_file_gave_them():
    sessions = pool({"k1": ["a"], "k2": ["b"]}, keys=["k2", "k1"])
    assert [s.pubkey for s in sessions] == ["k2-pub", "b", "k1-pub", "a"]


def test_names_say_where_an_account_sits():
    """A hundred accounts get read in a log, and "sub1" ten times over is
    unreadable."""
    sessions = pool({"k1": ["a", "b"], "k2": ["c"]})
    assert [s.name for s in sessions] == ["m1", "m1s1", "m1s2", "m2", "m2s1"]


def test_a_master_with_ten_subs_is_eleven_sessions():
    sessions = pool({"k1": [f"s{n}" for n in range(10)]})
    assert len(sessions) == 11


# -- and how many sockets it opens ------------------------------------------


def test_one_socket_per_key_not_per_account():
    sessions = pool({"k1": ["a", "b", "c"], "k2": ["d", "e"]})
    assert len({id(s.client) for s in sessions}) == 2


def test_the_socket_carries_every_account_it_signs_for():
    sessions = pool({"k1": ["a", "b"]})
    assert sessions[0].client.accounts == ["k1-pub", "a", "b"]


def test_ten_masters_of_ten_is_a_hundred_and_ten_accounts_on_ten_sockets():
    tree = {f"k{i}": [f"k{i}s{n}" for n in range(10)] for i in range(10)}
    sessions = pool(tree)
    assert len(sessions) == 110
    assert len({id(s.client) for s in sessions}) == 10


# -- an account must not appear twice ---------------------------------------


def test_the_same_account_under_two_keys_is_kept_once():
    """A pool holding one account twice can pair it with itself, which is not
    a hedge and would sit there netting to nothing."""
    sessions = pool({"k1": ["shared"], "k2": ["shared"]})
    assert [s.pubkey for s in sessions] == ["k1-pub", "shared", "k2-pub"]


def test_a_repeated_account_is_not_subscribed_twice():
    """Two subscriptions deliver every fill twice, and the fill path is only
    idempotent by trade id."""
    sessions = pool({"k1": ["shared"], "k2": ["shared"]})
    # Per socket, not per session: sessions on one socket share its list, and
    # counting that list once per session counts it as many times as there are
    # accounts on it.
    clients = {id(s.client): s.client for s in sessions}.values()
    carried = [pubkey for client in clients for pubkey in client.accounts]
    assert len(carried) == len(set(carried))


def test_a_key_whose_accounts_are_all_already_present_opens_no_socket():
    sessions = pool({"k1": ["a"], "k1-dup": ["a"]}, keys=["k1", "k1"])
    assert [s.pubkey for s in sessions] == ["k1-pub", "a"]
    assert len({id(s.client) for s in sessions}) == 1


# -- what the totals need from it -------------------------------------------


def _trees_of(sessions):
    """`Strategy._trees` over a pool, without building a whole Strategy."""
    from bulkdn.strategy import Strategy

    class Fake:
        _trees = Strategy._trees
        all_sessions = Strategy.all_sessions

    fake = Fake()
    fake.sessions = {s.pubkey: s for s in sessions}
    return fake._trees()


def test_the_run_groups_its_accounts_by_signing_key():
    """The fill history is totalled per tree, and the socket is what says which.

    A self-trade is one the exchange can see both sides of as one holder. It
    can see that inside a master's tree and it cannot see it across two keys,
    so totalling the pool as a single tree would subtract every cross-key
    hedge from the volume the fee tier is read against -- which is most of
    the trading pool mode exists to do.
    """
    sessions = pool({"k1": ["k1-s1"], "k2": ["k2-s1", "k2-s2"]})

    assert _trees_of(sessions) == [
        ["k1-pub", "k1-s1"],
        ["k2-pub", "k2-s1", "k2-s2"],
    ]


def test_one_key_is_one_tree():
    sessions = pool({"k1": ["k1-s1", "k1-s2"]})

    assert _trees_of(sessions) == [["k1-pub", "k1-s1", "k1-s2"]]

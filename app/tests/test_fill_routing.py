"""A fill belongs to one account, not to every account on the socket.

Every account under one key shares a socket -- that is what makes a pool of a
hundred accounts cost ten connections instead of a hundred and ten. The SDK
was written when a socket carried one account: handlers are registered per
account, but the client fires ALL of them for every message that arrives.

So with three accounts on a socket, one $60 buy was booked three times, once
per account. A live run shows it plainly:

    fill on m1:   BUY 0.00069700 role=maker
    fill on m1s1: BUY 0.00069700 role=taker
    fill on m1s2: BUY 0.00069700 role=?          <- not even in the group

The book then read the pair as long on BOTH sides, the required hedge came to
twice the leg, and the run halted on a hedge ceiling that was doing its job.
The exchange's own records afterwards said what had really happened: m1 held
0.000615 and m1s1 held 0.000697, each its own order and nothing else.
"""

import pytest

from bulkdn.accounts import AccountSession, _owner_of

MASTER = "MASTER-PUB"
SUB1 = "SUB1-PUB"
SUB2 = "SUB2-PUB"


class FakeClient:
    """Stands in for the shared socket."""

    def __init__(self, accounts, owner=None):
        self.accounts = list(accounts)
        self.message_owner = owner


def session(pubkey, client):
    return AccountSession(name=pubkey, pubkey=pubkey, client=client, http=None)


# -- reading the account out of a message -----------------------------------


@pytest.mark.parametrize("key", ["user", "account", "subAccount", "pubkey", "owner"])
def test_the_account_is_found_wherever_it_is_named(key):
    """The field is not in the SDK's model at all, so this reads the raw
    message -- and the raw message's spelling is the exchange's business."""
    assert _owner_of({"type": "account", "data": {key: SUB1}}) == SUB1


def test_it_is_found_on_the_outer_envelope_too():
    assert _owner_of({"type": "account", "user": SUB1, "data": {}}) == SUB1


def test_a_message_naming_nobody_returns_nothing():
    assert _owner_of({"type": "account", "data": {"symbol": "BTC-USD"}}) is None


def test_an_empty_name_does_not_count_as_one():
    assert _owner_of({"type": "account", "data": {"user": ""}}) is None


# -- and deciding whether an update is ours ---------------------------------


def test_an_update_naming_this_account_is_ours():
    client = FakeClient([MASTER, SUB1, SUB2], owner=SUB1)

    assert session(SUB1, client).owns_this_update() is True


def test_an_update_naming_another_account_is_not():
    """This is the whole bug: without it, all three sessions said yes."""
    client = FakeClient([MASTER, SUB1, SUB2], owner=SUB1)

    assert session(MASTER, client).owns_this_update() is False
    assert session(SUB2, client).owns_this_update() is False


def test_exactly_one_of_them_claims_each_update():
    client = FakeClient([MASTER, SUB1, SUB2], owner=SUB2)

    claimed = [
        pubkey for pubkey in (MASTER, SUB1, SUB2)
        if session(pubkey, client).owns_this_update()
    ]

    assert claimed == [SUB2]


def test_a_socket_carrying_one_account_needs_no_name():
    """Which is every run before pools, and why this was never noticed."""
    client = FakeClient([MASTER], owner=None)

    assert session(MASTER, client).owns_this_update() is True


def test_an_unnamed_update_on_a_shared_socket_is_undecidable():
    """None, not False. A caller that reads it as either answer is guessing,
    and the handlers re-read positions over HTTP instead."""
    client = FakeClient([MASTER, SUB1, SUB2], owner=None)

    assert session(SUB1, client).owns_this_update() is None


# -- and end to end, through the handler that booked it three times ---------


class Fill:
    def __init__(self, symbol, is_buy, size, trade_id):
        from bulk_api.common.enums import Side

        self.symbol = symbol
        self.side = Side.BUY if is_buy else Side.SELL
        self.size = size
        self.price = 85_959.5
        self.trade_id = trade_id


def three_on_one_socket(tmp_path):
    """A strategy whose three accounts share one socket, as a pool key does."""
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    from test_strategy import BTC, FakeFeed, FakeSession, make_config

    from bulkdn.hedger import Hedger
    from bulkdn.positions import PositionBook
    from bulkdn.state import Phase, StateStore, StrategyState
    from bulkdn.strategy import Strategy, build_hedge_ceilings

    config = make_config()
    book = PositionBook(overlay_ttl_ms=5000)
    client = FakeClient(["m1", "m1s1", "m1s2"])
    sessions = {}
    for name in ("m1", "m1s1", "m1s2"):
        one = FakeSession(name, name)
        one.client = client
        sessions[name] = one

    feed = FakeFeed()
    strategy = Strategy(
        config=config,
        master=sessions["m1"],
        sub1=sessions["m1s1"],
        feed=feed,
        book=book,
        hedger=Hedger(
            book=book,
            sessions=sessions,
            specs=feed.specs,
            max_hedge_size=build_hedge_ceilings(config),
        ),
        chaser=None,
        risk=None,
        store=StateStore(str(tmp_path / "state.json")),
        state=StrategyState(phase=Phase.OPEN),
        sessions=list(sessions.values()),
    )
    return strategy, book, sessions, client, BTC


def deliver(strategy, sessions, fill, owner):
    """One message reaching every handler on the socket, as the client does.

    That is the shape of the bug: the client holds one handler per account
    and fires all of them, so each session is offered every message.
    """
    for name, one in sessions.items():
        one.client.message_owner = owner
        # What a real session answers: the account named, or "cannot tell"
        # when nothing is named and the socket carries several.
        one.owns = None if owner is None else (owner == name)
        strategy._make_fill_handler(one)(fill)


def test_one_fill_is_booked_on_one_account(tmp_path):
    """The live failure: three accounts, one $60 buy, booked three times."""
    strategy, book, sessions, _client, btc = three_on_one_socket(tmp_path)

    deliver(strategy, sessions, Fill(btc, True, 0.000697, "t1"), owner="m1s1")

    assert book.effective("m1s1", btc) == pytest.approx(0.000697)
    assert book.effective("m1", btc) == 0.0
    assert book.effective("m1s2", btc) == 0.0


def test_two_orders_on_two_accounts_stay_apart(tmp_path):
    """What the exchange's own records said afterwards: m1 held its order and
    m1s1 held its own, and neither held the other's."""
    strategy, book, sessions, _client, btc = three_on_one_socket(tmp_path)

    deliver(strategy, sessions, Fill(btc, True, 0.000697, "t1"), owner="m1s1")
    deliver(strategy, sessions, Fill(btc, True, 0.000615, "t2"), owner="m1")

    assert book.effective("m1s1", btc) == pytest.approx(0.000697)
    assert book.effective("m1", btc) == pytest.approx(0.000615)
    assert book.effective("m1s2", btc) == 0.0


def test_an_unattributable_fill_leaves_the_book_alone(tmp_path):
    """And asks for a fresh read instead. A fill applied to the wrong account
    is worse than one applied late: nothing disagrees with it until the
    reconciler runs."""
    strategy, book, sessions, _client, btc = three_on_one_socket(tmp_path)
    strategy._synced_at = 12345.0

    deliver(strategy, sessions, Fill(btc, True, 0.000697, "t1"), owner=None)

    assert book.effective("m1", btc) == 0.0
    assert book.effective("m1s1", btc) == 0.0
    assert book.effective("m1s2", btc) == 0.0
    assert strategy._synced_at == 0.0, "it did not ask for a fresh read"
    assert strategy._unattributed_fills == 3

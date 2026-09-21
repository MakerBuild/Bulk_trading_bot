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

    deliver(strategy, sessions, Fill(btc, True, 0.000697, "t1"), owner=None)

    assert book.effective("m1", btc) == 0.0
    assert book.effective("m1s1", btc) == 0.0
    assert book.effective("m1s2", btc) == 0.0
    assert strategy._book_suspect is True, "it did not mark the book"
    assert strategy._unattributed_fills == 3


# -- the topic is where the exchange actually names it ----------------------
#
# Learned from a live socket, which answered:
#
#   outer=['data', 'topic', 'type']
#   inner=['authorizedAgentWallets', 'feeTiers', 'kind', 'leverageSettings',
#          'margin', 'name', 'openOrders', 'positions', 'reserve',
#          'subAccounts', 'type']
#
# Not one of the payload spellings, and `name` is the account's label rather
# than its pubkey. The subscription is `{"type": "account", "user": <pubkey>}`,
# so the pubkey is in the topic; how it is wrapped is the exchange's business.


def test_the_topic_names_the_account():
    message = {"type": "account", "topic": f"account:{SUB1}", "data": {}}

    assert _owner_of(message, [MASTER, SUB1, SUB2]) == SUB1


@pytest.mark.parametrize("topic", [
    "account:{pubkey}", "account.{pubkey}", "{pubkey}", "account|{pubkey}|v1",
])
def test_the_wrapping_around_it_does_not_matter(topic):
    """Matched against what we subscribed to, rather than parsed. A format
    this does not anticipate is a format it still reads."""
    message = {"type": "account", "topic": topic.format(pubkey=SUB2), "data": {}}

    assert _owner_of(message, [MASTER, SUB1, SUB2]) == SUB2


def test_a_topic_naming_none_of_our_accounts_is_not_a_match():
    message = {"type": "account", "topic": "account:SOMEONE-ELSE", "data": {}}

    assert _owner_of(message, [MASTER, SUB1, SUB2]) is None


def test_the_payload_still_wins_where_it_says_so():
    """A second endpoint may name it in the payload, as the first did not."""
    message = {"type": "account", "topic": "account", "data": {"user": SUB1}}

    assert _owner_of(message, [MASTER, SUB1, SUB2]) == SUB1


# -- and a book we know is wrong is not traded on ---------------------------


async def test_no_hedge_is_sent_while_the_book_is_suspect(tmp_path):
    """What $375 off-hedge cost: the worker hedges straight off the queue and
    never consulted the freshness clock, so marking the book stale-dated did
    nothing. Six market orders went out against a book that never moved, each
    one enlarging the imbalance it was correcting."""
    import asyncio

    strategy, _book, _sessions, _client, btc = three_on_one_socket(tmp_path)
    strategy._book_suspect = True

    hedged = []
    strategy.hedger.hedge = lambda *a, **k: hedged.append(a)

    reads = []

    async def failing_read(max_age_s=0.0):
        reads.append(max_age_s)
        raise RuntimeError("exchange unreachable")

    strategy._sync_positions = failing_read
    strategy._hedge_queue.put_nowait(btc)

    worker = asyncio.create_task(strategy._hedge_worker())
    await asyncio.sleep(0.05)
    strategy._stop.set()
    await worker

    assert reads, "it did not even try to re-read"
    assert not hedged, "it hedged off a book it had been told was wrong"
    assert strategy._book_suspect is True, "the suspicion was cleared anyway"


async def test_the_hedge_resumes_once_the_read_succeeds(tmp_path):
    import asyncio

    strategy, _book, _sessions, _client, btc = three_on_one_socket(tmp_path)
    strategy._book_suspect = True

    hedged = []

    async def hedge(roles, mark_price=None):
        hedged.append(roles.symbol)

    strategy.hedger.hedge = hedge

    async def good_read(max_age_s=0.0):
        return None

    strategy._sync_positions = good_read
    strategy._hedge_queue.put_nowait(btc)

    worker = asyncio.create_task(strategy._hedge_worker())
    await asyncio.sleep(0.05)
    strategy._stop.set()
    await worker

    assert hedged == [btc]
    assert strategy._book_suspect is False


# -- and the leg a fill belongs to is the one its ACCOUNT is trading --------
#
# Roles looked up by SYMBOL are the configured pair's. A group's accounts are
# drawn from the pool, so a fill on a group maker matched neither side of that
# pair: it logged `role=?` and queued the bare symbol, the worker resolved
# that back to the configured pair, found it flat, and hedged nothing.
#
# A live run:
#
#   === g1:BTC-USD OPEN: maker=m2 taker=m1s2 ===
#   === g2:BTC-USD OPEN: maker=m2s2 taker=m2s1 ===
#   fill on m2:   BUY 0.00062700 role=?
#   fill on m2s2: BUY 0.00062700 role=?
#   exposure: m2=+0.00073400 m2s2=+0.00087600 net=+0.00161000 $138.16
#
# and that $138 sat directional for three minutes with no hedge ever sent.
# It only ever worked when a group happened to contain the named pair.


def with_group(tmp_path, maker, takers):
    from bulkdn.pairing import Group

    strategy, book, sessions, client, btc = three_on_one_socket(tmp_path)
    group = Group(
        symbol=btc, maker=maker, takers=tuple(takers),
        shares=tuple([1.0 / len(takers)] * len(takers)),
    )
    strategy._groups["g1:" + btc] = group
    return strategy, book, sessions, btc


def test_an_account_resolves_to_the_group_it_is_in(tmp_path):
    strategy, _book, _sessions, btc = with_group(tmp_path, "m1s2", ["m1"])

    assert strategy._key_for_account("m1s2", btc) == f"g1:{btc}"
    assert strategy._key_for_account("m1", btc) == f"g1:{btc}"


def test_an_account_in_no_group_resolves_to_nothing(tmp_path):
    strategy, _book, _sessions, btc = with_group(tmp_path, "m1s2", ["m1"])

    assert strategy._key_for_account("m1s1", btc) is None


def test_a_fill_on_a_group_maker_queues_that_group(tmp_path):
    """Not the bare symbol. The worker resolves the bare symbol to the
    configured pair, which in a pool holds nothing."""
    strategy, _book, sessions, btc = with_group(tmp_path, "m1s2", ["m1"])

    deliver(strategy, sessions, Fill(btc, True, 0.000734, "t1"), owner="m1s2")

    queued = []
    while not strategy._hedge_queue.empty():
        queued.append(strategy._hedge_queue.get_nowait())
    assert queued == [f"g1:{btc}"], "the fill was filed under the wrong leg"


def test_the_reconciler_looks_at_the_groups(tmp_path):
    """It runs off `_live_roles`, which was the configured pair by symbol --
    two accounts out of however many, and usually not the two holding
    anything."""
    strategy, _book, _sessions, btc = with_group(tmp_path, "m1s2", ["m1"])

    live = strategy._live_roles()

    assert [roles.maker for roles in live] == ["m1s2"]
    assert [roles.key for roles in live] == [f"g1:{btc}"]


def test_without_groups_it_is_still_the_configured_legs(tmp_path):
    """Nothing about a pool changes what a plain pair does."""
    strategy, _book, _sessions, _client, btc = three_on_one_socket(tmp_path)

    assert [roles.symbol for roles in strategy._live_roles()] == strategy.symbols


# -- and a split hedge has more than one hedger -----------------------------
#
# `roles.taker` is the FIRST of them. Retiring only its reservation left the
# other slices in `in_flight` for ever: net exposure read as short by the
# leftovers, the hedger corrected it, that order reserved and was not retired
# either, and the leg thrashed -- 700 alternating fills of about a dollar
# each on one account in five minutes, every one paying a taker fee.


def split_group(tmp_path):
    """One maker covered by three hedgers, as `max_takers: 3` draws."""
    from bulkdn.pairing import Group

    strategy, book, sessions, _client, btc = three_on_one_socket(tmp_path)
    strategy._groups["g1:" + btc] = Group(
        symbol=btc, maker="m1", takers=("m1s1", "m1s2", "m2s1"),
        shares=(0.5, 0.3, 0.2),
    )
    return strategy, book, sessions, btc


def test_every_hedger_retires_its_own_reservation(tmp_path):
    strategy, _book, sessions, btc = split_group(tmp_path)
    key = f"g1:{btc}"

    # Three slices reserved, as `_slice` would when the hedge goes out.
    for signed in (-0.0005, -0.0003, -0.0002):
        strategy.hedger.in_flight.add(key, signed)
    assert strategy.hedger.in_flight.total(key) == pytest.approx(-0.001)

    # Each lands on its own account.
    sessions["m1s1"].owns = True
    strategy._make_fill_handler(sessions["m1s1"])(Fill(btc, False, 0.0005, "a"))
    sessions["m1s2"].owns = True
    strategy._make_fill_handler(sessions["m1s2"])(Fill(btc, False, 0.0003, "b"))

    assert strategy.hedger.in_flight.total(key) == pytest.approx(-0.0002), (
        "a hedger other than the first left its reservation standing"
    )


def test_a_second_hedger_is_not_labelled_unknown(tmp_path, caplog):
    """`role=?` against an account doing exactly its job is how the thrash
    stayed invisible in a log full of it."""
    import logging

    strategy, _book, sessions, btc = split_group(tmp_path)
    sessions["m1s2"].owns = True

    with caplog.at_level(logging.INFO, logger="bulkdn.strategy"):
        strategy._make_fill_handler(sessions["m1s2"])(Fill(btc, False, 0.0003, "b"))

    printed = " | ".join(caplog.messages)
    assert "role=taker" in printed, printed
    assert "role=?" not in printed


# -- and the phase the guard is given comes from the group ------------------


def test_the_guard_is_told_each_accounts_own_phase(tmp_path):
    """It used to be told the SYMBOL's phase, read off a leg that nothing
    drove once accounts came from a pool -- so it was told IDLE, always."""
    from bulkdn.state import Phase

    strategy, _book, _sessions, btc = with_group(tmp_path, "m1s2", ["m1"])
    strategy.state.leg(f"g1:{btc}", btc).phase = Phase.HOLD

    phases = strategy._phases_by_account()

    assert phases == {("m1s2", btc): Phase.HOLD, ("m1", btc): Phase.HOLD}


def test_an_account_in_no_group_is_not_watched(tmp_path):
    strategy, _book, _sessions, btc = with_group(tmp_path, "m1s2", ["m1"])

    assert ("m1s1", btc) not in strategy._phases_by_account()


def test_no_groups_means_nothing_to_watch(tmp_path):
    """A position opened by this run cannot exist before a group exists to
    open it."""
    strategy, _book, _sessions, _client, _btc = three_on_one_socket(tmp_path)

    assert strategy._phases_by_account() == {}


# -- and the status block shows the legs that exist -------------------------


class _FlatRisk:
    """The status block asks risk for a dollar figure; here it is zero."""

    def net_exposure_usd(self, _symbol):
        return 0.0



def test_the_status_block_names_the_groups(tmp_path):
    """It read the leg keyed by the SYMBOL, which nothing drives once the
    accounts come from a pool -- so it said `BTC-USD IDLE cycle 0` for an
    hour while fifteen cycles completed underneath it."""
    from bulkdn.state import Phase

    strategy, _book, _sessions, btc = with_group(tmp_path, "m1s2", ["m1"])
    leg = strategy.state.leg(f"g1:{btc}", btc)
    leg.phase = Phase.HOLD
    leg.cycle_index = 15
    strategy._progress = (0.78, 0.0)
    strategy.risk = _FlatRisk()

    printed = "\n".join(strategy.status_lines())

    assert f"g1:{btc} HOLD cycle 15" in printed, printed
    assert "IDLE cycle 0" not in printed


def test_with_no_group_it_says_waiting(tmp_path):
    """Rather than reporting a phase for a leg that is not being traded."""
    strategy, _book, _sessions, _client, btc = three_on_one_socket(tmp_path)
    strategy._progress = (0.0, 0.0)
    strategy.risk = _FlatRisk()

    printed = "\n".join(strategy.status_lines())

    assert f"{btc} waiting" in printed, printed

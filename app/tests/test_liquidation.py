"""Detecting a position closed by someone other than the bot.

Two failure modes matter equally here. Missing a real liquidation leaves the
surviving leg as outright directional exposure. Crying liquidation on a normal
entry closes a healthy pair and stops the run for nothing -- so most of these
tests are about *not* firing.
"""

from bulkdn.liquidation import LiquidationGuard
from bulkdn.marketdata import MarketSpec
from bulkdn.positions import PositionBook
from bulkdn.state import Phase

MASTER = "master-pubkey"
SUB1 = "sub1-pubkey"
BTC = "BTC-USD"
SOL = "SOL-USD"

SPECS = {
    BTC: MarketSpec(BTC, tick_size=0.5, lot_size=0.001, min_notional=1.0),
    SOL: MarketSpec(SOL, tick_size=0.01, lot_size=0.1, min_notional=50.0),
}
ACCOUNTS = [MASTER, SUB1]


def guard() -> LiquidationGuard:
    return LiquidationGuard(specs=SPECS, names={MASTER: "master", SUB1: "sub1"})


def book_with(positions: dict) -> PositionBook:
    book = PositionBook()
    for (account, symbol), size in positions.items():
        book.set_authoritative(account, symbol, size)
    return book


# -- must fire -------------------------------------------------------------


def test_position_wiped_during_hold_is_reported():
    g = guard()
    book = book_with({(MASTER, BTC): 1.0, (SUB1, BTC): -1.0})
    assert g.check(book, Phase.HOLD, ACCOUNTS) == []

    # Sub1's short is force-closed.
    book.set_authoritative(SUB1, BTC, 0.0)
    events = g.check(book, Phase.HOLD, ACCOUNTS)

    assert len(events) == 1
    assert events[0].account == SUB1
    assert events[0].symbol == BTC
    assert events[0].fully_closed
    assert "sub1" in events[0].describe()


def test_partial_reduction_is_reported():
    """Partial liquidation still breaks the pair."""
    g = guard()
    book = book_with({(MASTER, BTC): 1.0, (SUB1, BTC): -1.0})
    g.check(book, Phase.HOLD, ACCOUNTS)

    book.set_authoritative(SUB1, BTC, -0.4)
    events = g.check(book, Phase.HOLD, ACCOUNTS)

    assert len(events) == 1
    assert not events[0].fully_closed
    assert (events[0].previous, events[0].current) == (-1.0, -0.4)


def test_reduction_during_open_is_reported():
    """OPEN only ever adds, so a drop there is external too."""
    g = guard()
    book = book_with({(MASTER, BTC): 0.5})
    g.check(book, Phase.OPEN, ACCOUNTS)

    book.set_authoritative(MASTER, BTC, 0.0)
    assert len(g.check(book, Phase.OPEN, ACCOUNTS)) == 1


def test_a_short_side_reports_its_direction():
    g = guard()
    book = book_with({(SUB1, BTC): -2.0})
    g.check(book, Phase.HOLD, ACCOUNTS)

    book.set_authoritative(SUB1, BTC, -0.5)
    event = g.check(book, Phase.HOLD, ACCOUNTS)[0]
    assert event.previous == -2.0
    assert event.current == -0.5


def test_a_wiped_short_is_not_reported_as_a_long():
    """With the peak stored unsigned, a fully closed short reads as +size,
    because the direction would have to come from a position that is now zero.
    """
    g = guard()
    book = book_with({(SUB1, BTC): -1.0})
    g.check(book, Phase.HOLD, ACCOUNTS)

    book.set_authoritative(SUB1, BTC, 0.0)
    event = g.check(book, Phase.HOLD, ACCOUNTS)[0]
    assert event.previous == -1.0
    assert "-1.00000000 -> +0.00000000" in event.describe()


def test_each_symbol_is_tracked_separately():
    g = guard()
    book = book_with({(MASTER, BTC): 1.0, (MASTER, SOL): 10.0})
    g.check(book, Phase.HOLD, ACCOUNTS)

    book.set_authoritative(MASTER, SOL, 0.0)
    events = g.check(book, Phase.HOLD, ACCOUNTS)
    assert [e.symbol for e in events] == [SOL]


# -- must not fire ---------------------------------------------------------


def test_growing_into_a_position_is_not_a_liquidation():
    """The whole of a normal entry, one partial fill at a time."""
    g = guard()
    book = PositionBook()
    for size in (0.2, 0.5, 0.8, 1.0):
        book.set_authoritative(MASTER, BTC, size)
        assert g.check(book, Phase.HOLD, ACCOUNTS) == []


def test_exit_phase_never_reports():
    """EXIT reduces positions on purpose; that is its whole job."""
    g = guard()
    book = book_with({(MASTER, BTC): 1.0, (SUB1, BTC): -1.0})
    g.check(book, Phase.HOLD, ACCOUNTS)

    book.set_authoritative(MASTER, BTC, 0.0)
    book.set_authoritative(SUB1, BTC, 0.0)
    assert g.check(book, Phase.EXIT, ACCOUNTS) == []


def test_dust_below_one_lot_does_not_fire():
    """Rounding leaves sub-lot residue; that is not a liquidation."""
    g = guard()
    book = book_with({(MASTER, BTC): 1.0})
    g.check(book, Phase.HOLD, ACCOUNTS)

    book.set_authoritative(MASTER, BTC, 1.0 - SPECS[BTC].lot_size / 2)
    assert g.check(book, Phase.HOLD, ACCOUNTS) == []


def test_one_side_filled_before_the_other_is_not_a_liquidation():
    """Mid-entry the maker holds size and the taker has not hedged yet.

    That imbalance is normal and must not be mistaken for a liquidation --
    which is why the rule is "a position shrank", not "the sides disagree".
    """
    g = guard()
    book = book_with({(MASTER, BTC): 0.5, (SUB1, BTC): 0.0})
    assert g.check(book, Phase.HOLD, ACCOUNTS) == []


def test_an_event_is_reported_once_not_every_tick():
    g = guard()
    book = book_with({(MASTER, BTC): 1.0})
    g.check(book, Phase.HOLD, ACCOUNTS)

    book.set_authoritative(MASTER, BTC, 0.0)
    assert len(g.check(book, Phase.HOLD, ACCOUNTS)) == 1
    assert g.check(book, Phase.HOLD, ACCOUNTS) == []
    assert g.check(book, Phase.HOLD, ACCOUNTS) == []


def test_reset_clears_peaks_between_cycles():
    """A completed EXIT took everything to zero; the next cycle starts fresh."""
    g = guard()
    book = book_with({(MASTER, BTC): 1.0})
    g.check(book, Phase.HOLD, ACCOUNTS)

    book.set_authoritative(MASTER, BTC, 0.0)
    g.reset()
    assert g.check(book, Phase.HOLD, ACCOUNTS) == []


def test_exit_still_tracks_peaks_for_the_next_phase():
    """Positions opened during EXIT-adjacent activity should not read as a
    drop once an accumulating phase resumes."""
    g = guard()
    book = book_with({(MASTER, BTC): 2.0})
    g.check(book, Phase.EXIT, ACCOUNTS)

    book.set_authoritative(MASTER, BTC, 0.5)
    assert len(g.check(book, Phase.HOLD, ACCOUNTS)) == 1


def test_unconfirmed_fills_do_not_trigger_it():
    """The optimistic overlay can briefly show a fill the exchange has not
    applied. Deciding a liquidation happened on that basis would be a false
    alarm with an expensive response, so only confirmed positions are read."""
    g = guard()
    book = PositionBook(overlay_ttl_ms=5000)
    book.set_authoritative(MASTER, BTC, 1.0)
    g.check(book, Phase.HOLD, ACCOUNTS)

    # A sell overlay drags `effective` down, but not `authoritative`.
    book.apply_fill(MASTER, BTC, is_buy=False, size=0.9)
    assert book.effective(MASTER, BTC) < 0.2
    assert g.check(book, Phase.HOLD, ACCOUNTS) == []


# -- resetting peaks for one leg, not for a market --------------------------


def test_a_finishing_leg_does_not_clear_another_groups_peaks():
    """Two groups can share a market once accounts are pooled. Clearing the
    whole symbol would erase the high-water mark of a group still holding a
    position, and its next real shrink -- a liquidation -- would go unseen,
    which is the one thing this guard exists to catch."""
    from bulkdn.liquidation import LiquidationGuard
    from bulkdn.marketdata import MarketSpec

    spec = MarketSpec(symbol="BTC-USD", tick_size=0.5, lot_size=0.001, min_notional=10.0)
    guard = LiquidationGuard(specs={"BTC-USD": spec}, names={})
    guard._peak[("mine", "BTC-USD")] = 0.5
    guard._peak[("theirs", "BTC-USD")] = 0.7

    guard.reset_symbol("BTC-USD", accounts=("mine",))

    assert ("mine", "BTC-USD") not in guard._peak
    assert guard._peak[("theirs", "BTC-USD")] == 0.7, "another group's peak was cleared"


def test_without_accounts_it_still_clears_the_whole_symbol():
    """Which is what the halt path and a single configured pair both mean."""
    from bulkdn.liquidation import LiquidationGuard
    from bulkdn.marketdata import MarketSpec

    spec = MarketSpec(symbol="BTC-USD", tick_size=0.5, lot_size=0.001, min_notional=10.0)
    guard = LiquidationGuard(specs={"BTC-USD": spec}, names={})
    guard._peak[("a", "BTC-USD")] = 0.5
    guard._peak[("b", "BTC-USD")] = 0.7
    guard._peak[("a", "ETH-USD")] = 0.9

    guard.reset_symbol("BTC-USD")

    assert not [k for k in guard._peak if k[1] == "BTC-USD"]
    assert guard._peak[("a", "ETH-USD")] == 0.9, "another market was cleared"

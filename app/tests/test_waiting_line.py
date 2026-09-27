"""The line a leg writes while it waits on a fill.

The chaser logs only when it moves the order. On a quiet market an order on the
touch can sit for minutes, and a live operator read that silence as a hung bot
and stopped it three times in five minutes. The line says the bot is waiting,
how long, and what ends the wait.
"""

from bulkdn.strategy import waiting_line


def line(label="exit", *, price=2710.37, budget_s=1800.0, group=True):
    return waiting_line(
        "g1:ETH-USD", label,
        left=0.0369, price=price, waited_s=185.0, budget_s=budget_s, group=group,
    )


def test_it_names_the_leg_the_order_and_the_time_waited():
    text = line()
    assert text.startswith("g1:ETH-USD exit: waiting for a fill")
    assert "0.0369 left" in text
    assert "order resting at 2710.37" in text
    assert "3.1 min in" in text


def test_an_exit_says_it_closes_at_market_when_time_runs_out():
    assert "at 30 min the rest closes at market" in line("exit")


def test_an_open_says_it_keeps_what_has_filled():
    assert "at 30 min it keeps what has filled and moves on" in line("open")


def test_a_configured_leg_says_the_run_halts():
    """Not a pool group: there is no other group to carry on beside it."""
    assert "at 30 min the run halts" in line(group=False)


def test_no_limit_is_said_rather_than_implied():
    assert "no time limit is set" in line(budget_s=0.0)


def test_no_order_yet_is_not_shown_as_a_price():
    assert "no order resting yet" in line(price=None)

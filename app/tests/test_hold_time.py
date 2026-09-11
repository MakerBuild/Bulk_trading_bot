"""`hold_minutes` as a range rather than a fixed number.

A fixed hold gives every cycle the same length, which is a shape visible in the
fill history. A range removes it. The config accepts both spellings, so most of
what matters here is that the old one -- a plain number -- keeps meaning
exactly what it did, and that a malformed range is refused at load time rather
than silently becoming a hold of zero.
"""

import pytest
import yaml

from bulkdn.config import ConfigError, HoldTime


def parsed(text: str) -> HoldTime:
    """Parse the way settings.yaml would, so YAML's own typing is in play."""
    return HoldTime.parse(yaml.safe_load(f"hold_minutes: {text}")["hold_minutes"])


def test_a_plain_number_is_a_range_of_zero_width():
    hold = parsed("0.5")
    assert (hold.low, hold.high) == (0.5, 0.5)
    assert not hold.is_range
    # Every draw is the same number, so an existing config does not start
    # behaving differently because of this change.
    assert {hold.pick() for _ in range(50)} == {0.5}


def test_a_range_is_written_low_first_with_a_dash():
    hold = parsed("0.5-1")
    assert (hold.low, hold.high) == (0.5, 1.0)
    assert hold.is_range


def test_a_list_spells_the_same_range():
    assert parsed("[0.5, 1]") == HoldTime(0.5, 1.0)


def test_draws_land_inside_the_range_and_vary():
    hold = parsed("0.5-1")
    draws = [hold.pick() for _ in range(200)]
    assert all(0.5 <= d <= 1.0 for d in draws)
    # The point of the range is that cycles differ; identical draws would mean
    # the randomness was lost somewhere.
    assert len(set(draws)) > 1


def test_integer_minutes_survive_yaml_typing():
    """`hold_minutes: 5` arrives as an int, not a float."""
    assert parsed("5") == HoldTime(5.0, 5.0)


def test_a_backwards_range_is_refused():
    with pytest.raises(ConfigError, match="runs backwards"):
        parsed("1-0.5")


def test_a_negative_hold_is_refused():
    with pytest.raises(ConfigError, match=">= 0"):
        HoldTime.parse(-1.0)


def test_nonsense_is_refused_rather_than_read_as_zero():
    for bad in ("abc", "0.5-", "0.5-1-2"):
        with pytest.raises(ConfigError):
            parsed(bad)
    # A bare dash is not valid YAML, so it only reaches the parser directly.
    with pytest.raises(ConfigError):
        HoldTime.parse("-")


def test_a_boolean_is_refused():
    """`hold_minutes: yes` is a mistake, not a one-minute hold."""
    with pytest.raises(ConfigError, match="number or a range"):
        parsed("yes")


def test_a_list_of_one_is_refused():
    with pytest.raises(ConfigError, match="exactly two values"):
        parsed("[0.5]")


def test_it_prints_the_way_it_was_written():
    assert str(parsed("0.5-1")) == "0.5-1"
    assert str(parsed("0.5")) == "0.5"
    assert str(parsed("5")) == "5"

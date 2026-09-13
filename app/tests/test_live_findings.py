"""Three faults the first live sessions produced, and their fixes.

None of them showed up in the test suite, and two of them hid each other: an
unparsed order status dropped the reply to an order submission, the awaiting
call timed out, and the timeout logged as a blank line because only `str(exc)`
was printed and `asyncio.TimeoutError` has nothing to say.
"""

import asyncio
import logging

import pytest

from bulkdn.marketdata import MarketSpec
from bulkdn.retry import describe
from bulkdn.strategy import Strategy

# -- 1. an order status the SDK spells differently ---------------------------


@pytest.fixture(autouse=True)
def patched():
    from bulkdn.ws_compat import apply_ws_compat

    apply_ws_compat()


@pytest.mark.parametrize("spelling", ["cancelledIoc", "cancelledIOC", "CANCELLEDIOC"])
def test_the_status_the_exchange_actually_sends_is_accepted(spelling):
    """Live: the exchange sends `cancelledIoc`, the SDK matches `cancelledIOC`.
    A capitalisation difference, raised from `_handle_post_response` -- so the
    reply to an order submission was dropped and the caller timed out."""
    from bulk_api.common.enums import OrderStatus

    assert OrderStatus.from_string(spelling) is OrderStatus.CANCELLED_IOC


@pytest.mark.parametrize(
    "spelling, expected",
    [
        ("resting", "RESTING"),
        ("filled", "FILLED"),
        ("partiallyFilled", "PARTIALLY_FILLED"),
        ("rejectedCrossing", "REJECTED_CROSSING"),
        ("cancelledReduceOnly", "CANCELLED_REDUCEONLY"),
    ],
)
def test_the_statuses_that_already_worked_still_do(spelling, expected):
    from bulk_api.common.enums import OrderStatus

    assert OrderStatus.from_string(spelling).name == expected


def test_an_unknown_terminal_status_still_delivers_the_response():
    """Dropping the reply costs a hedge. Both of these are terminal by their
    prefix, so nothing downstream acts on which one it was."""
    from bulk_api.common.enums import OrderStatus

    assert OrderStatus.from_string("cancelledSomethingNew") is OrderStatus.CANCELLED
    assert OrderStatus.from_string("rejectedSomethingNew") is OrderStatus.REJECTED_INVALID


def test_an_unknown_status_that_is_not_terminal_is_refused():
    """The one case where guessing is worse than raising: reading a live order
    as finished would have the chaser place a second one."""
    from bulk_api.common.enums import OrderStatus

    with pytest.raises(ValueError, match="Unknown order status"):
        OrderStatus.from_string("somethingEntirelyNew")


# -- 2. an exception is worth reading -----------------------------------------


def test_a_message_less_exception_still_names_itself():
    """`hedge for BTC-USD failed: ` was the entire report of a failed hedge."""
    assert describe(asyncio.TimeoutError()) == "TimeoutError"
    assert str(asyncio.TimeoutError()) == "", "the premise of this test"


def test_a_message_is_kept_when_there_is_one():
    assert describe(ValueError("size below lot")) == "ValueError: size below lot"


def test_the_strategy_logs_the_type(caplog):
    class Fake:
        _persist = Strategy._persist

        class store:
            @staticmethod
            def save(_):
                raise PermissionError(5, "Access is denied")

        state = None

    with caplog.at_level(logging.ERROR):
        Fake()._persist()
    assert "PermissionError" in caplog.text


def test_no_log_call_passes_a_bare_exception():
    """The twelve sites that printed a blank."""
    import pathlib
    import re

    offenders = []
    for path in pathlib.Path(Strategy.__module__.replace(".", "/")).parent.glob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"log\.(error|warning|critical)\(.*,\s*exc\)\s*$", line):
                offenders.append(f"{path.name}:{i}")
    assert not offenders, f"these log an exception without its type: {offenders}"


# -- 3. dust is not an unhedged position --------------------------------------


class FakeFeed:
    specs = {"BTC-USD": MarketSpec("BTC-USD", tick_size=0.001, lot_size=1e-06,
                                   min_notional=1.0)}

    def reference_price(self, symbol):
        return 76_700.0


def verdict(net):
    class Fake:
        feed = FakeFeed()
        _net_verdict = Strategy._net_verdict

    return Fake()._net_verdict("BTC-USD", net)


def test_the_live_dust_is_not_called_unhedged():
    """From the log: net +0.00000300 BTC, $0.23, reported as NOT hedged -- one
    line above the code calling the same amount unhedgeable."""
    text = verdict(0.000003)
    assert "NOT hedged" not in text
    assert "dust" in text and "cannot be closed" in text


def test_below_a_lot_says_nothing_at_all():
    assert verdict(1e-09) == ""


def test_a_real_imbalance_is_still_called_out():
    """$76 of naked BTC has to read as loudly as it did before."""
    assert verdict(0.001) == " -- NOT hedged"


def test_the_threshold_is_the_market_minimum_not_the_lot():
    spec = FakeFeed.specs["BTC-USD"]
    just_under = (spec.min_notional * 0.9) / 76_700.0
    just_over = (spec.min_notional * 1.1) / 76_700.0
    assert "dust" in verdict(just_under)
    assert verdict(just_over) == " -- NOT hedged"


# -- 4. TLS is verified, not bypassed -----------------------------------------
#
# The bypass fired on every run on Windows, and the reason recorded in the code
# was that BULK's certificate had expired. It had not. Measured against all
# three hosts: the system store rejects them, certifi accepts them, valid to
# 22 Nov 2026. What expired is a root in the Windows store. `requests` never
# noticed because it ships certifi; the socket used the system store and gave
# up on verification instead.


def test_the_verified_context_actually_verifies():
    import ssl as ssl_module

    from bulkdn.ws_compat import verified_context

    ctx = verified_context()
    assert ctx.verify_mode is ssl_module.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_it_trusts_what_requests_trusts():
    """The whole point: the two halves of the bot agree on a trust store."""
    import certifi

    from bulkdn.ws_compat import verified_context

    ctx = verified_context()
    loaded = {c["subject"] for c in ctx.get_ca_certs()}
    assert loaded, "no certificate authorities were loaded"
    bundle = ssl_bundle_count(certifi.where())
    assert len(loaded) > bundle * 0.5, "certifi's bundle does not look loaded"


def ssl_bundle_count(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read().count("BEGIN CERTIFICATE")


def test_a_missing_certifi_degrades_rather_than_crashing(monkeypatch):
    """No worse than the position before this existed."""
    import builtins
    import ssl as ssl_module

    from bulkdn import ws_compat

    real_import = builtins.__import__

    def no_certifi(name, *args, **kwargs):
        if name == "certifi":
            raise ImportError("no certifi")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_certifi)
    ctx = ws_compat.verified_context()
    assert ctx.verify_mode is ssl_module.CERT_REQUIRED


def test_the_bypass_is_still_reachable_but_no_longer_the_default():
    """Kept for an operator who genuinely cannot connect -- and only then."""
    import ssl as ssl_module

    from bulkdn.ws_compat import _insecure_context, verified_context

    assert _insecure_context().verify_mode is ssl_module.CERT_NONE
    assert verified_context().verify_mode is ssl_module.CERT_REQUIRED


def test_install_bat_names_certifi():
    """It arrives with requests today, which is not a promise."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    assert "certifi" in (root / "install.bat").read_text(encoding="utf-8")

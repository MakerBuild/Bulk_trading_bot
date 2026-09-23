"""The menu refusing to act on anything it was not actually told.

Every test here is one way the menu used to spend money, or write a limit, on
an answer nobody gave:

* Ctrl+C at a prompt read as "0" -- a real value. It set a burn target of 0
  (no limit) and submitted a sub-account called "0".
* A failed balance read came back as 0.0, so Balance planned transfers INTO
  an account whose balance nobody knew.
* A transfer that timed out mid-plan ended the loop with a traceback and no
  word about which of the others had gone through.
* A dry run's sizing wrote into the one config the menu kept, and the next
  live run traded the rehearsal's sizes.
"""

import asyncio
import builtins

import pytest

from bulkdn import cli, menu
from bulkdn.config import Config, ExecutionTarget, LegConfig, RiskConfig
from bulkdn.retry import RetryExhausted


def feed(monkeypatch, answers):
    """Scripted input. An exception instance in the list is raised instead."""
    it = iter(answers)

    def answer(*_):
        value = next(it)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(builtins, "input", answer)


def real_config(**overrides):
    values = {
        "markets": [
            LegConfig(symbol="BTC-USD", notional_usd=100.0, max_order_notional_usd=100.0),
            LegConfig(symbol="ETH-USD", notional_usd=100.0, max_order_notional_usd=100.0),
        ],
        "risk": RiskConfig(),
        "private_keys": ["k1"],
    }
    values.update(overrides)
    return Config(**values)


TARGET_FILE = """\
execution_target:
  cycles: 0
  burn_usd: 3               # stop once $3 of fees has been spent
  volume_usd: 0
"""


# -- Ctrl+C cancels; it is not an answer ------------------------------------


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), EOFError()])
def test_ctrl_c_at_a_target_prompt_writes_nothing(monkeypatch, tmp_path, interrupt):
    path = tmp_path / "settings.yaml"
    path.write_text(TARGET_FILE, encoding="utf-8")
    config = real_config(target=ExecutionTarget(cycles=0, burn_usd=3.0))
    feed(monkeypatch, [interrupt])

    with pytest.raises(menu.Cancelled):
        menu._edit_target(config, str(path), "burn_usd", "burn target")

    assert path.read_text(encoding="utf-8") == TARGET_FILE, "a cancel wrote a value"
    assert config.target.burn_usd == 3.0


def test_the_menu_reports_a_cancel_and_carries_on(monkeypatch, tmp_path, capsys):
    path = tmp_path / "settings.yaml"
    path.write_text(TARGET_FILE, encoding="utf-8")
    config = real_config(target=ExecutionTarget(cycles=0, burn_usd=3.0))
    # Configuration -> burn target -> Ctrl+C, then exit.
    feed(monkeypatch, ["5", "2", KeyboardInterrupt(), "0"])

    assert menu.run_menu(config, str(path)) == 0
    assert "nothing was changed" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == TARGET_FILE


def test_end_of_input_at_the_top_menu_exits_rather_than_spinning(monkeypatch):
    feed(monkeypatch, [EOFError()])
    assert menu.run_menu(real_config(), "settings.yaml") == 0


@pytest.mark.parametrize("value", ["inf", "nan", "-inf", "1e400"])
def test_a_non_finite_target_is_refused_at_the_prompt(monkeypatch, tmp_path, capsys, value):
    path = tmp_path / "settings.yaml"
    path.write_text(TARGET_FILE, encoding="utf-8")
    feed(monkeypatch, [value])

    menu._edit_target(real_config(), str(path), "burn_usd", "burn target")

    assert "non-negative number" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == TARGET_FILE


# -- creating a sub-account -------------------------------------------------


def one_tree(monkeypatch):
    tree = menu.Tree(index=1, private_key="k1", master="MASTER-PUBKEY-XYZ", subs=())
    monkeypatch.setattr(menu, "_trees", lambda _config: [tree])
    monkeypatch.setattr(menu, "_pause", lambda: None)


def watch_creates(monkeypatch, code=0):
    sent = []

    async def create(config, name, margin, *, private_key=None):
        sent.append(name)
        return code

    monkeypatch.setattr(menu, "cmd_create_subaccount", create)
    return sent


def test_ctrl_c_at_the_name_prompt_creates_nothing(monkeypatch):
    one_tree(monkeypatch)
    sent = watch_creates(monkeypatch)
    feed(monkeypatch, [KeyboardInterrupt()])

    with pytest.raises(menu.Cancelled):
        menu._create_subaccount(real_config())
    assert sent == [], "a cancel submitted a createSubAccount"


def test_creating_one_needs_a_yes(monkeypatch):
    one_tree(monkeypatch)
    sent = watch_creates(monkeypatch)
    feed(monkeypatch, ["trader-2", "no"])

    menu._create_subaccount(real_config())
    assert sent == []


def test_a_bad_name_is_refused_before_anything_is_signed(monkeypatch, capsys):
    one_tree(monkeypatch)
    sent = watch_creates(monkeypatch)
    feed(monkeypatch, ["has space"])

    menu._create_subaccount(real_config())
    assert sent == []
    assert "nothing was sent" in capsys.readouterr().out


@pytest.mark.parametrize(
    "code, said, not_said",
    [
        (0, "ready to use", "not created"),
        (1, "not created", "ready to use"),
        (cli.EXIT_UNKNOWN, "NOT CONFIRMED", "ready to use"),
    ],
)
def test_the_outcome_reported_is_the_one_that_happened(monkeypatch, capsys, code, said, not_said):
    one_tree(monkeypatch)
    sent = watch_creates(monkeypatch, code=code)
    feed(monkeypatch, ["trader-2", "yes"])

    menu._create_subaccount(real_config())

    printed = capsys.readouterr().out
    assert sent == ["trader-2"]
    assert said in printed
    assert not_said not in printed


# -- a balance that could not be read is not zero ---------------------------


class Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_a_failed_balance_read_is_unknown_not_empty(monkeypatch):
    monkeypatch.setattr(menu.requests, "post", lambda *a, **k: Response(502))
    assert menu._transferable(real_config(), "SOME-PUBKEY-123") is None


def test_a_network_error_on_a_balance_read_is_unknown_too(monkeypatch):
    def down(*a, **k):
        raise menu.requests.ConnectionError("unreachable")

    monkeypatch.setattr(menu.requests, "post", down)
    assert menu._transferable(real_config(), "SOME-PUBKEY-123") is None


def test_an_unreadable_account_is_left_out_of_the_balance_plan(monkeypatch, capsys):
    tree = menu.Tree(index=1, private_key="k1", master="M-master",
                     subs=("S-sub-aaaa", "U-sub-bbbb"))
    monkeypatch.setattr(menu, "_trees", lambda _config: [tree])
    amounts = {"M-master": 100.0, "S-sub-aaaa": 0.0, "U-sub-bbbb": None}
    monkeypatch.setattr(menu, "_transferable", lambda _c, pk: amounts[pk])
    monkeypatch.setattr(menu, "_confirm", lambda _what: False)
    monkeypatch.setattr(menu, "_pause", lambda: None)

    weighed = []
    real_settle = menu._settle

    def watch(rows):
        weighed.append(rows)
        return real_settle(rows)

    monkeypatch.setattr(menu, "_settle", watch)

    menu._balance_subaccounts(real_config())

    assert weighed, "the readable accounts should still be balanced"
    planned = {pubkey for rows in weighed for _, pubkey, _ in rows}
    assert "U-sub-bbbb" not in planned, "money was planned into an unknown balance"
    assert "could not read" in capsys.readouterr().out


def test_collect_skips_an_unreadable_sub(monkeypatch, capsys):
    tree = menu.Tree(index=1, private_key="k1", master="M-master",
                     subs=("S-sub-aaaa", "U-sub-bbbb"))
    monkeypatch.setattr(menu, "_trees", lambda _config: [tree])
    amounts = {"S-sub-aaaa": 10.0, "U-sub-bbbb": None}
    monkeypatch.setattr(menu, "_transferable", lambda _c, pk: amounts[pk])
    asked = []
    monkeypatch.setattr(menu, "_confirm", lambda what: asked.append(what) or False)
    monkeypatch.setattr(menu, "_pause", lambda: None)

    menu._collect_to_master(real_config())

    assert "unreadable" in capsys.readouterr().out
    assert asked == ["Submit 1 transfer(s) to the master account."]


# -- a plan that fails partway still says what happened ---------------------


class Result:
    def __init__(self, ok, status=200, uncertain=False):
        self.ok = ok
        self.response_status = status
        self.response_json = {"status": "ok" if ok else "error"}
        self.uncertain = uncertain


def plan_of(count):
    tree = menu.Tree(index=1, private_key="k1", master="M-master", subs=())
    return [(tree, f"SRC-{i}-account", "DST-account-x", 10.0) for i in range(count)]


def scripted_transfers(monkeypatch, outcomes):
    import bulkdn.subaccounts as subaccounts_mod

    calls = []
    it = iter(outcomes)

    def submit_transfer(**kwargs):
        calls.append(kwargs["from_pubkey"])
        outcome = next(it)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(subaccounts_mod, "submit_transfer", submit_transfer)
    return calls


def test_a_transfer_that_times_out_stops_the_plan_with_a_summary(monkeypatch, capsys):
    lost = RetryExhausted("POST /order", 3, TimeoutError("read timed out"), uncertain=True)
    calls = scripted_transfers(monkeypatch, [Result(True), lost, Result(True)])

    menu._submit_plan(real_config(), plan_of(3))

    printed = capsys.readouterr().out
    assert len(calls) == 2, "kept sending into a network that had just failed"
    assert "1 done, 0 refused, 1 unknown, 1 not sent" in printed
    assert "UNKNOWN" in printed and "BEFORE trying again" in printed


def test_a_refused_transfer_does_not_stop_the_others(monkeypatch, capsys):
    calls = scripted_transfers(
        monkeypatch, [Result(False, status=400), Result(True), Result(True)]
    )

    menu._submit_plan(real_config(), plan_of(3))

    assert len(calls) == 3
    assert "2 done, 1 refused, 0 unknown, 0 not sent" in capsys.readouterr().out


def test_a_refusal_after_a_lost_answer_is_unknown_not_rejected(monkeypatch):
    """The replay of an applied transfer is refused for its spent nonce. That
    refusal is not evidence nothing moved -- it is the opposite."""
    _, outcome, _ = cli.submit_signed(
        lambda **_: Result(False, status=400, uncertain=True)
    )
    assert outcome == cli.UNKNOWN


def test_a_plain_refusal_is_rejected():
    _, outcome, _ = cli.submit_signed(lambda **_: Result(False, status=400))
    assert outcome == cli.REJECTED


def test_never_reaching_the_exchange_is_not_unknown():
    def unreachable(**_):
        raise RetryExhausted("POST /order", 3, ConnectionRefusedError(), uncertain=False)

    _, outcome, _ = cli.submit_signed(unreachable)
    assert outcome == cli.UNSENT


# -- one run's sizes do not leak into the next ------------------------------


def test_a_dry_run_cannot_resize_the_live_run_after_it(monkeypatch):
    config = real_config()
    seen = []

    async def fake_run(run_config, dry_run):
        seen.append((dry_run, run_config.markets[0].size, run_config))
        # What apply_sizing and the per-cycle redraw do to the config.
        for leg in run_config.markets:
            leg.size = 0.00001
            leg.max_order_size = 0.00001
            leg.notional_usd = 1.0
        return 0

    monkeypatch.setattr(menu, "cmd_run", fake_run)
    monkeypatch.setattr(menu, "_pause", lambda: None)

    feed(monkeypatch, ["1"])
    menu._start(config)
    feed(monkeypatch, ["2", "yes"])
    menu._start(config)

    (dry, _, first), (live, size_seen_by_live, second) = seen
    assert dry is True and live is False
    assert first is not config and second is not config and first is not second
    assert size_seen_by_live == 0.0, "the live run inherited the dry run's sizes"
    assert config.markets[0].notional_usd == 100.0, "the menu's own config was changed"


def test_status_and_flatten_get_their_own_copy_too(monkeypatch):
    config = real_config()
    given = []

    async def record(run_config, *a, **k):
        given.append(run_config)
        return 0

    monkeypatch.setattr(menu, "cmd_status", record)
    monkeypatch.setattr(menu, "cmd_flatten", record)
    monkeypatch.setattr(menu, "_pause", lambda: None)

    menu._active_strategy(config)
    feed(monkeypatch, ["1"])
    menu._close_all(config)

    assert len(given) == 2
    assert all(run_config is not config for run_config in given)


# -- the words on the screen ------------------------------------------------


def test_the_live_confirmation_names_the_real_stop_conditions(monkeypatch):
    config = real_config(target=ExecutionTarget(cycles=0, burn_usd=3.0, volume_usd=0.0))
    asked = []
    monkeypatch.setattr(menu, "_confirm", lambda what: asked.append(what) or False)
    monkeypatch.setattr(menu, "_pause", lambda: None)
    feed(monkeypatch, ["2"])

    menu._start(config)

    assert "unlimited" not in asked[0]
    assert "$3.00 of fees burned" in asked[0]
    assert "multi" in asked[0]


def test_no_limit_at_all_is_said_plainly():
    config = real_config(target=ExecutionTarget(cycles=0))
    assert "runs until you press S" in menu._stop_conditions(config)


def test_several_limits_read_as_whichever_first():
    config = real_config(target=ExecutionTarget(cycles=5, burn_usd=2.0, volume_usd=1000.0))
    text = menu._stop_conditions(config)
    assert "5 cycle(s)" in text and "whichever comes first" in text


def test_the_stop_banner_names_the_close_item_by_its_real_number():
    expected = f"{menu.MAIN_ITEMS.index(menu.CLOSE_ALL_LABEL) + 1}. Close All Positions"
    assert any(expected in line for line in cli._stop_banner())


@pytest.mark.parametrize(
    "line",
    [
        "  - symbol: ETH-USD",
        '  - symbol: "ETH-USD"',
        "    symbol: 'ETH-USD'   # the dear one",
        "    symbol:   ETH-USD#note",
    ],
)
def test_a_market_is_found_however_its_symbol_line_is_spelled(line):
    lines = ["markets:", line, "    enabled: true", "hold_minutes: 1"]
    assert menu._block_at(lines, "ETH-USD") == (1, 3)


def test_a_similar_symbol_is_not_mistaken_for_it():
    assert menu._block_at(["  - symbol: ETH-USDC"], "ETH-USD") is None


def test_a_damaged_key_file_is_not_reported_as_encrypted(tmp_path):
    f = tmp_path / "k.local"
    f.write_text('{"version": 999, "kdf": "argon2id", "box": "x"}', encoding="utf-8")
    assert menu._key_state(str(f)) == "UNREADABLE"


# -- the exit codes the CLI gives ------------------------------------------


def test_a_config_error_inside_runtime_is_one_line_not_a_traceback(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: real_config())
    monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(cli.proxy, "configure", lambda *a, **k: None)

    class TooFew:
        def __init__(self, *a, **k):
            raise cli.ConfigError("multi mode needs at least two accounts to pair")

    monkeypatch.setattr(cli, "Runtime", TooFew)

    for command in (["status"], ["check"], ["run"], ["flatten"]):
        assert cli.main(command) == 1
        err = capsys.readouterr().err
        assert "configuration error: multi mode needs at least two" in err
        assert "Traceback" not in err


def test_status_covers_switched_off_markets(monkeypatch, capsys):
    config = real_config()
    config.markets[1].enabled = False
    made = []

    class Book:
        def authoritative(self, account, symbol):
            return 0.0

    class Store:
        def load(self):
            from bulkdn.state import StrategyState

            return StrategyState()

    class Session:
        name = "m1"
        pubkey = "M1-PUBKEY"
        client = object()

        def open_orders(self):
            return []

    class Status:
        def __init__(self, cfg, dry_run, symbols=None):
            made.append(symbols)
            self.symbols = symbols
            self.pool = [Session()]
            self.book = Book()
            self.store = Store()
            self.feed = type("F", (), {"load_specs": lambda self: None})()

    monkeypatch.setattr(cli, "Runtime", Status)
    monkeypatch.setattr(cli, "sync_positions_http", lambda pool, book: None)

    assert asyncio.run(cli.cmd_status(config)) == 0
    assert made == [["BTC-USD", "ETH-USD"]]
    assert "(switched off)" in capsys.readouterr().out


# -- Target Progress reads this run from the run's start -----------------------


def test_this_run_is_read_from_the_runs_start(monkeypatch, tmp_path, capsys):
    """The run now records a start time and zero baselines; subtracting those
    from a lifetime walk printed the lifetime totals as 'this run'."""
    import types

    import bulkdn.fees as fees_mod
    from bulkdn import menu
    from bulkdn.fees import Realised
    from bulkdn.state import StateStore, StrategyState

    state_file = tmp_path / "state.json"
    state = StrategyState()
    state.baseline_at = 1_790_000_000.0
    StateStore(str(state_file)).save(state)

    calls = []

    def realised(http, trees, since_ms=None, **kw):
        calls.append(since_ms)
        if since_ms is None:
            return Realised(fills=900, fees_usd=-500.0, volume_usd=2_800_000.0)
        return Realised(fills=90, fees_usd=-20.0, volume_usd=110_000.0)

    monkeypatch.setattr(fees_mod, "realised_for_trees", realised)
    monkeypatch.setattr(fees_mod, "account_fee_tier", lambda *a, **k: None)
    monkeypatch.setattr(fees_mod, "fee_state", lambda *a, **k: {})
    monkeypatch.setattr(menu, "_trees", lambda c: [types.SimpleNamespace(accounts=["m1"], master="m1")])
    monkeypatch.setattr(menu, "_http", lambda c: object())
    monkeypatch.setattr(menu, "_pause", lambda: None)
    config = types.SimpleNamespace(
        state_file=str(state_file), http_url="http://x",
        target=types.SimpleNamespace(burn_usd=0.0, volume_usd=100_000.0),
    )

    menu._target_progress(config)
    out = capsys.readouterr().out.split("THIS RUN")[1]

    assert calls == [None, 1_790_000_000_000.0]
    assert "$110,000.00" in out, "this run showed something else"
    assert "2,800,000" not in out, "the lifetime total was printed as this run"

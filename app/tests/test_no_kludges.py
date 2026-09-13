"""Four things that were wrong, pinned so they cannot come back.

Each of these passed review once by looking reasonable. What they have in
common is that nothing failed when they were introduced -- so the fix is only
half the work, and this file is the other half.
"""

import ast
import asyncio
import inspect
import pathlib
import re

import pytest

from bulkdn import menu, strategy

PKG = pathlib.Path(strategy.__file__).parent
SOURCES = {p.stem: p.read_text(encoding="utf-8") for p in PKG.glob("*.py")}


# -- 1. no synchronous network call on the event loop ------------------------
#
# `realised_for_tree` is synchronous `requests`, and the target check runs
# between cycles, inside the loop. Called directly it froze everything for the
# length of the walk -- measured at 2.9-3.8s against a 575-fill account, and
# the walk is paginated a thousand fills at a time, so it grows with history.
# During it the chaser placed nothing and arriving fills sat unprocessed.


def test_the_fill_history_is_read_off_the_event_loop():
    source = SOURCES["strategy"]
    tree = ast.parse(source)
    direct = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == "realised_for_tree":
                line = source.splitlines()[node.lineno - 1]
                if "to_thread" not in line:
                    direct.append(node.lineno)
    assert not direct, (
        f"strategy.py:{direct} calls realised_for_tree directly. It is "
        "synchronous requests; route it through asyncio.to_thread."
    )


@pytest.mark.parametrize(
    "name", ["_target_reached", "progress_detail", "capture_target_baseline"]
)
def test_every_history_reader_is_a_coroutine(name):
    """If one of these is made sync again, its caller silently gets a
    coroutine object instead of a result -- which is truthy, so the target
    would read as reached on the first check."""
    assert inspect.iscoroutinefunction(getattr(strategy.Strategy, name))


async def test_the_loop_keeps_turning_during_a_history_read(monkeypatch):
    """The property that matters, not just the shape of the code."""
    import time as real_time

    from bulkdn import strategy as strategy_module

    def slow(http, wallets):
        real_time.sleep(0.3)  # a blocking read, as requests would be
        raise RuntimeError("not the point of this test")

    monkeypatch.setattr(strategy_module, "realised_for_tree", slow)

    class Fake:
        _read_totals = strategy.Strategy._read_totals

        class _S:
            http = None
            pubkey = "EXAMPLE"

        master = sub1 = _S()

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    spinner = asyncio.create_task(ticker())
    with pytest.raises(RuntimeError):
        await Fake()._read_totals()
    spinner.cancel()

    # A blocked loop would leave this at ~0; a live one gets many turns.
    assert ticks > 5, f"the event loop only advanced {ticks} times -- it was blocked"


# -- 2. the settings file is named, not guessed at ---------------------------
#
# `_delete` matched any `.yaml` path and rewrote it from the settings template.
# Only settings.yaml reaches it today, and nothing said so, so the next yaml
# added to the erase list would have been silently replaced rather than
# deleted.


def test_delete_does_not_dispatch_on_a_file_extension():
    """Read from the parsed code, not the text: the docstring names the old
    behaviour on purpose, and a grep would match that and never fail."""
    fn = next(
        node for node in ast.walk(ast.parse(SOURCES["menu"]))
        if isinstance(node, ast.FunctionDef) and node.name == "_delete"
    )
    suffix_reads = [
        n.lineno for n in ast.walk(fn)
        if isinstance(n, ast.Attribute) and n.attr == "suffix"
    ]
    assert not suffix_reads, (
        f"_delete reads path.suffix at line(s) {suffix_reads} -- it is deciding "
        "what a file is from its extension again"
    )


def test_an_unrelated_yaml_is_deleted_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = tmp_path / "settings.yaml"
    settings.write_text("mine", encoding="utf-8")
    other = tmp_path / "something-else.yaml"
    other.write_text("unrelated", encoding="utf-8")

    result = menu._delete(other, settings=settings)

    assert not other.exists(), "it should have been deleted"
    assert "deleted" in result
    assert settings.read_text(encoding="utf-8") == "mine", "and left alone"


def test_the_settings_file_is_matched_however_it_is_spelled(tmp_path, monkeypatch):
    """One side comes from --config and the other from a listing, so the two
    spellings of the same file have to compare equal."""
    monkeypatch.chdir(tmp_path)
    from bulkdn.config import SETTINGS_TEMPLATE

    template = tmp_path / SETTINGS_TEMPLATE
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text("shipped", encoding="utf-8")
    settings = tmp_path / "settings.yaml"
    settings.write_text("mine", encoding="utf-8")

    result = menu._delete(settings.resolve(), settings=pathlib.Path("settings.yaml"))

    assert "reset" in result
    assert settings.read_text(encoding="utf-8") == "shipped"


# -- 3. no reaching across a module for an underscored name ------------------


def test_cli_uses_the_public_strategy_interface():
    hits = re.findall(r"\bstrategy\._\w+", SOURCES["cli"])
    assert not hits, f"cli.py reaches into strategy internals: {sorted(set(hits))}"


@pytest.mark.parametrize("name", ["stop_reason", "progress_detail", "request_stop"])
def test_the_public_names_exist(name):
    assert hasattr(strategy.Strategy, name)


def test_stop_reason_reports_what_request_stop_recorded():
    class Fake:
        _stop = asyncio.Event()
        _stop_requested = None
        request_stop = strategy.Strategy.request_stop
        stop_reason = strategy.Strategy.stop_reason

    f = Fake()
    assert f.stop_reason is None
    f.request_stop("keyboard")
    assert f.stop_reason == "keyboard"


def test_no_module_imports_a_private_name_from_another():
    offenders = []
    for mod, source in SOURCES.items():
        for m in re.finditer(r"from \.(\w+) import ([^\n(]+)", source):
            for name in (n.strip() for n in m.group(2).split(",")):
                if name.startswith("_"):
                    offenders.append(f"{mod} <- {m.group(1)}.{name}")
    assert not offenders, offenders


# -- 4. cli and menu are not mutually dependent ------------------------------


def test_menu_depends_on_cli_and_not_the_other_way_round():
    """menu is a front end over the commands, so it may import them. cli
    importing menu back made the pair circular, and the price was eight
    `from .cli import ...` lines hidden inside menu's function bodies."""
    cli_source = SOURCES["cli"]
    top_level = cli_source[: cli_source.index("\ndef ")]
    assert "from .menu import" not in top_level, (
        "cli imports menu at module scope again -- the cycle is back"
    )


def test_menu_imports_cli_once_at_the_top():
    source = SOURCES["menu"]
    tree = ast.parse(source)
    deferred = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.ImportFrom) and sub.module == "cli":
                    deferred.append(f"{node.name}:{sub.lineno}")
    assert not deferred, f"deferred cli imports are back in menu: {deferred}"
    assert re.search(r"^from \.cli import", source, re.M), "menu should import cli plainly"


def test_both_modules_import_in_either_order():
    """A cycle often only shows up from one entry point."""
    import importlib
    import sys

    for first, second in (("bulkdn.cli", "bulkdn.menu"), ("bulkdn.menu", "bulkdn.cli")):
        for mod in ("bulkdn.cli", "bulkdn.menu"):
            sys.modules.pop(mod, None)
        importlib.import_module(first)
        importlib.import_module(second)


# -- 5. a failed state write must not abandon open positions -----------------


def test_the_strategy_never_saves_state_unguarded():
    """Every save in strategy.py goes through _persist, which logs and carries
    on. A run that dies on a file-permission error leaves hedged positions open
    with nothing running to close them -- which is what happened live."""
    tree = ast.parse(SOURCES["strategy"])
    unguarded = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr == "save"
                and isinstance(f.value, ast.Attribute)
                and f.value.attr == "store"
            ):
                unguarded.append(node.lineno)
    inside_persist = [
        n.lineno
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef) and fn.name == "_persist"
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
    ]
    stray = [line for line in unguarded if line not in inside_persist]
    assert not stray, (
        f"strategy.py:{stray} calls store.save directly -- use _persist, so a "
        "locked file cannot kill a run with positions open"
    )


async def test_a_locked_state_file_does_not_stop_a_leg(tmp_path):
    """The behaviour, not just the shape."""
    from bulkdn.state import StateStore, StrategyState

    class Boom(StateStore):
        def save(self, state):
            raise PermissionError(5, "Access is denied")

    class Fake:
        _persist = strategy.Strategy._persist
        store = Boom(str(tmp_path / "state.json"))
        state = StrategyState()

    Fake()._persist()  # must not raise

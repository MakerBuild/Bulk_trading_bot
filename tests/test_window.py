"""Console title progress. Cosmetic, so the only real requirement is that it
never raises -- including on platforms with no console title to set."""

from bulkdn.window import WindowTitle


def _title(win: WindowTitle) -> str:
    """Rebuild what update() would set, without touching the OS."""
    progress = f"{win.cycle}/{win.cycles}" if win.cycles else str(win.cycle)
    text = f"{win.name} [{progress}] {win.phase}"
    if win.note:
        text += f" | {win.note}"
    return text


def test_starts_at_zero_with_a_known_phase():
    win = WindowTitle(cycles=3)
    assert _title(win) == "bulkdn [0/3] starting"


def test_tracks_cycle_and_phase():
    win = WindowTitle(cycles=3)
    win.set_cycle(2)
    win.set_phase("HOLD")
    assert _title(win) == "bulkdn [2/3] HOLD"


def test_unbounded_run_shows_a_bare_count():
    win = WindowTitle(cycles=0)
    win.set_cycle(7)
    assert _title(win) == "bulkdn [7] starting"


def test_note_is_appended():
    win = WindowTitle(cycles=1)
    win.set_note("burn $1.20 / $100.00")
    assert _title(win).endswith("| burn $1.20 / $100.00")


def test_halt_reason_is_truncated():
    """A long reason would push the phase off the end of the tab."""
    win = WindowTitle(cycles=1)
    win.halted("x" * 200)
    assert win.phase == "HALTED"
    assert len(win.note) == 40


def test_updates_never_raise_on_any_platform():
    win = WindowTitle(cycles=2)
    win.set_cycle(1, cycles=5)
    win.set_phase("EXIT")
    win.set_note("something")
    win.halted("reason")
    assert win.cycles == 5

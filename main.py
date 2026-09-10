"""Entry point: `python main.py`.

A launcher, not an implementation. Everything lives in the `bulkdn` package,
and `python -m bulkdn.cli` and the `bulkdn` console script reach the same
`main`, so there is one place to change and three ways to call it.

Arguments pass straight through, so `python main.py run --live` works the same
as `bulkdn run --live`. With no arguments the interactive menu opens.

The import is guarded because the first thing a new operator does wrong is run
this with the system Python instead of the project's. That fails deep inside
the SDK with a message about a missing name, which says nothing about the
actual cause.
"""

import os
import sys

VENV_PYTHON = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    ".venv",
    "Scripts" if os.name == "nt" else "bin",
    "python.exe" if os.name == "nt" else "python",
)


def _explain(problem: str) -> int:
    """Say which interpreter is running and how to use the right one."""
    print(f"\n{problem}\n", file=sys.stderr)
    print(f"  running:  {sys.executable}", file=sys.stderr)

    if os.path.exists(VENV_PYTHON) and not sys.executable.startswith(
        os.path.dirname(os.path.dirname(VENV_PYTHON))
    ):
        print(f"  expected: {VENV_PYTHON}\n", file=sys.stderr)
        print("Run it with the project's interpreter:\n", file=sys.stderr)
        print(f"  {VENV_PYTHON} main.py", file=sys.stderr)
        return 1

    # Right interpreter, wrong SDK build. The PyPI wheel omits the trailing
    # signature-domain byte the API requires, so it cannot sign anything the
    # exchange will accept -- see the Setup section of the README.
    print("\nThe SDK must come from GitHub, not PyPI:\n", file=sys.stderr)
    print(
        '  python -m pip install --no-deps "git+https://github.com/Bulk-trade/'
        'bulk-client.git@3a6506e#subdirectory=crates/api-python"',
        file=sys.stderr,
    )
    return 1


def _launch() -> int:
    try:
        from bulkdn.cli import main
    except ImportError as exc:
        missing = getattr(exc, "name", "") or str(exc)
        if "bulkdn" in missing:
            return _explain(f"Cannot find the bulkdn package: {exc}")
        return _explain(f"The BULK SDK is missing or too old: {exc}")
    return main()


if __name__ == "__main__":
    sys.exit(_launch())

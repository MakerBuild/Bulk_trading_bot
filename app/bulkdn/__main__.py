"""Package entry point: `python -m bulkdn`, which is what run.bat calls.

Everything lives in `bulkdn.cli`; this only guards the import, because the
first thing a new operator does wrong is run the bot with the system Python
instead of the project's. That fails deep inside the SDK with a message about
a missing name, which says nothing about the actual cause.

Arguments pass straight through, so `python -m bulkdn run --live` works the
same as the console script. With no arguments the interactive menu opens.
"""

import os
import sys

# app/, which holds the virtualenv alongside this package. Not the project
# root -- that is one level further up, and holds run.bat and settings.yaml.
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PYTHON = os.path.join(
    _APP_DIR,
    ".venv",
    "Scripts" if os.name == "nt" else "bin",
    "python.exe" if os.name == "nt" else "python",
)


def _explain(problem: str) -> int:
    """Say which interpreter is running and how to use the right one."""
    print(f"\n{problem}\n", file=sys.stderr)
    print(f"  running:  {sys.executable}", file=sys.stderr)

    venv_root = os.path.dirname(os.path.dirname(VENV_PYTHON))
    if os.path.exists(VENV_PYTHON) and not sys.executable.startswith(venv_root):
        print(f"  expected: {VENV_PYTHON}\n", file=sys.stderr)
        print("Start it with the launcher instead:\n", file=sys.stderr)
        print("  run.bat", file=sys.stderr)
        return 1

    # Right interpreter, wrong SDK build. The PyPI wheel omits the trailing
    # signature-domain byte the API requires, so it cannot sign anything the
    # exchange will accept -- see the Setup section of the README.
    print("\nDependencies are missing. Install them with:\n", file=sys.stderr)
    print("  install.bat", file=sys.stderr)
    return 1


def main() -> int:
    try:
        from bulkdn.cli import main as cli_main
    except ImportError as exc:
        missing = getattr(exc, "name", "") or str(exc)
        if "bulkdn" in missing:
            return _explain(f"Cannot find the bulkdn package: {exc}")
        return _explain(f"The BULK SDK is missing or too old: {exc}")
    return cli_main()


if __name__ == "__main__":
    sys.exit(main())

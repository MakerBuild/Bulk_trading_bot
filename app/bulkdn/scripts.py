"""What the operator's scripts and files are called on this system.

The same bot ships `install.bat`, `run.bat` and `update.bat` for Windows and
`install.sh`, `run.sh` and `update.sh` for Linux. A message that tells someone
what to run has to name the one that exists on their machine -- `install.bat`
typed into a Linux shell is a "command not found" in the middle of recovering
from a problem.
"""

from __future__ import annotations

import os

WINDOWS = os.name == "nt"


def script(name: str) -> str:
    """`install` -> `install.bat` on Windows, `./install.sh` elsewhere."""
    return f"{name}.bat" if WINDOWS else f"./{name}.sh"


def venv_python() -> str:
    """The virtualenv's interpreter, relative to the bot's folder."""
    return r"app\.venv\Scripts\python.exe" if WINDOWS else "app/.venv/bin/python"


def venv_folder() -> str:
    return r"app\.venv" if WINDOWS else "app/.venv"

"""Entry point: `python main.py`.

A launcher, not an implementation. Everything lives in the `bulkdn` package,
and `python -m bulkdn.cli` and the `bulkdn` console script reach the same
`main`, so there is one place to change and three ways to call it.

Arguments pass straight through, so `python main.py run --live` works the same
as `bulkdn run --live`. With no arguments the interactive menu opens.
"""

import sys

from bulkdn.cli import main

if __name__ == "__main__":
    sys.exit(main())

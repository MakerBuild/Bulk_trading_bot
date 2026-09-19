# Vendored BULK SDK

`bulk_client-0.1.2-py3-none-any.whl` is the exchange SDK, built from
[Bulk-trade/bulk-client](https://github.com/Bulk-trade/bulk-client) at commit
`3a6506e` -- the same commit `install.bat` pins in its fallback.

It ships with the bot because it is the only piece of the install that comes
from GitHub. Everything else is on PyPI, which answers reliably from the
networks this is handed out on; github.com does not. Measured while writing
this: three consecutive `pip install` clones of the SDK failed at twenty-one
seconds each, with PyPI working fine throughout. That failure is what a new
operator sees first, and it looks like a broken bot rather than a bad route.

The wheel is pure Python -- `py3-none-any`, thirty-one `.py` files, no compiled
extension -- so one file covers every machine the bot runs on, at 49 KB. Its
contents were compared byte for byte against a working install before it was
committed.

The SDK is Apache 2.0, which permits redistribution; the licence is beside it
in `LICENSE-bulk-client.txt`. The upstream repository carries no NOTICE file,
and nothing here is modified.

## When the SDK moves

Two things name the version and both have to move together, or an operator who
has the wheel and one who fell back to GitHub end up on different code:

* the filename pinned in `install.bat`,
* the commit in the GitHub fallback in the same file.

To rebuild from a checkout of the new commit:

```
pip wheel --no-deps -w app/vendor ./crates/api-python
```

Delete the old wheel in the same commit. An unzipped copy updates by copying
files over the top and never deletes, so a wheel left behind would sit there
looking current.

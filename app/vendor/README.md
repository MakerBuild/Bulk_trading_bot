# Vendored BULK SDK

`bulk_client-0.1.2+bulkdn.1-py3-none-any.whl` is the exchange SDK, built from
[Bulk-trade/bulk-client](https://github.com/Bulk-trade/bulk-client) at commit
`3a6506e` -- the same commit `install.bat` pins in its fallback -- with one
upstream commit applied on top. See "Local patch" below.

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
in `LICENSE-bulk-client.txt`. The upstream repository carries no NOTICE file.

## Local patch

The wheel is `3a6506e` plus the Python half of upstream commit `96c3252` ("Add
ALO slide and join client support", 2026-09-21), and nothing else: three files,
fourteen lines, adding the `ALO_SLIDE` and `ALO_JOIN` time-in-force values the
exchange accepts from API v1.0.20 (signing indices 3 and 4):

* `bulk_api/common/enums.py` -- `TimeInForce.ALO_SLIDE`, `TimeInForce.ALO_JOIN`;
* `bulk_api/common/signer.py` -- `TIME_IN_FORCE_MAP` gains `ALO_SLIDE: 3`,
  `ALO_JOIN: 4`, their lowercase forms and `postOnly`/`postOnlySlide`/`postOnlyJoin`;
* `bulk_api/messages/trade.py` -- the same two in its own `TIME_IN_FORCE_MAP`.

The local version `+bulkdn.1` says so, and makes the installer's hash check see
a new wheel. Everything else in the wheel is byte for byte the old one.
`app/dev/patch_sdk_wheel.py` rebuilds it from the plain 0.1.2 wheel.

Why not upstream `main` as it stands: later commits also append a `slippage`
field to the signed bytes of a plain **market** order, which the exchange's own
signing reference does not list for it. Every hedge is a market order; a
signature the exchange reads differently is `bad signature` on all of them.
Moving the whole SDK forward is a separate change, with its own check.

The GitHub fallback still installs plain `3a6506e`, which lacks the patch. A
market set to `ALO_JOIN` or `ALO_SLIDE` is refused before the run on such an
install, with a message to run the installer again; `ALO` works on both.

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

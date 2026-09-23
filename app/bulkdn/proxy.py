"""Routing every connection through a proxy.

BULK is unreachable from some countries, and a bot that cannot open a socket is
not a trading problem to debug -- it is a network one. This puts the proxy in
one place, in a file next to the key, so nothing about it is buried in a config
the operator has to understand.

**Set through the environment, deliberately.** Both transports read the same
standard variables on their own:

* `requests` -- used for every HTTP call, including order submission
* `websockets` -- via `urllib.request.getproxies()`, see `websockets.proxy`

Passing a proxy argument down instead would mean threading it through the SDK's
client constructors, which this bot does not own, and through every
`requests.post` call site. The environment reaches code we cannot edit, which
is most of what actually opens a socket here.

**http, https and socks5 all work.** socks5h is worth preferring where the
provider offers it: the `h` sends the hostname to the proxy for resolution, so
DNS is not resolved locally -- and in a country that blocks by DNS, a local
lookup is the part that fails first.
"""

from __future__ import annotations

import logging
import os
import pathlib
import urllib.parse

from .retry import describe

log = logging.getLogger(__name__)

PROXY_FILE = "proxy.local"

# Written by install.bat as well; the two are checked against each other by
# test_proxy, the same way the key template is.
PROXY_TEMPLATE = """\
# Optional. Leave this file as it is unless BULK is blocked where you are.
#
# Put ONE proxy address on the line below -- no quotes, nothing else. Examples:
#
#     http://user:password@proxy.example.com:8080
#     socks5h://user:password@proxy.example.com:1080
#     socks5h://proxy.example.com:1080
#
# socks5h is the one to ask your provider for: the "h" means the proxy resolves
# the hostname, so the lookup does not happen on your machine -- which is what
# fails first where DNS is what does the blocking.
#
# Everything the bot does goes through it: orders, positions, prices and the
# account stream. This file never leaves your machine, but it usually holds a
# password, so treat it like the key file.
"""

# Names both libraries look at. `requests` reads the lower-case form first and
# the upper-case second; `urllib.request.getproxies` reads the lower-case form
# only, and `websockets` gives `wss`/`ws` priority over `https`/`http`, which is
# why those are set too rather than relying on the https entry alone.
_ENV_NAMES = (
    "http_proxy", "HTTP_PROXY",
    "https_proxy", "HTTPS_PROXY",
    "ws_proxy", "WS_PROXY",
    "wss_proxy", "WSS_PROXY",
    "all_proxy", "ALL_PROXY",
)


class ProxyError(Exception):
    """The proxy line is present but unusable."""


def load(path: str = PROXY_FILE) -> str | None:
    """The proxy address from `path`, or None when none is configured.

    Absent file, empty file and a file with only comments all mean "no proxy",
    because that is the normal state for most operators and it must not be an
    error.
    """
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning(
            "could not read %s (%s) -- continuing without a proxy", path, describe(exc)
        )
        return None

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not lines:
        return None
    if len(lines) > 1:
        raise ProxyError(
            f"{path} has {len(lines)} non-comment lines; it must hold exactly one "
            "proxy address"
        )
    return validate(lines[0], source=path)


def validate(url: str, *, source: str = PROXY_FILE) -> str:
    """Check the address is something both libraries can use, and return it.

    Rejecting a bad address here is the whole point: the alternative is the
    proxy being silently ignored and the bot connecting directly, which in a
    blocked country looks like the exchange being down.
    """
    # Checked on the raw text, before parsing. `urlsplit("host:1080")` reads
    # the host as the SCHEME, because that is what a colon means to it -- so
    # the missing-scheme case would otherwise be reported as
    # "'proxy.example.com' is not a proxy scheme", which tells someone who
    # simply forgot the `http://` nothing at all.
    # Every message below names the address through `redacted`. These reach
    # the screen and the log file, and the log file is what operators send on
    # when asking for help -- with the proxy password in it, verbatim, before.
    shown = redacted(url)
    if "://" not in url:
        raise ProxyError(
            f"{source}: '{shown}' has no scheme. Write it as "
            "http://host:port or socks5h://host:port"
        )

    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        # `.port` raises on a non-numeric port, with the port text in the
        # message -- raised from here it would escape as a bare ValueError.
        raise ProxyError(f"{source}: '{shown}' has a port that is not a number") from None
    if parsed.scheme not in ("http", "https", "socks5", "socks5h", "socks4", "socks4a"):
        raise ProxyError(
            f"{source}: '{parsed.scheme}' is not a proxy scheme this understands. "
            "Use http, https, socks5 or socks5h"
        )
    if not parsed.hostname:
        raise ProxyError(f"{source}: '{shown}' has no host")
    if port is None:
        raise ProxyError(
            f"{source}: '{shown}' has no port. Proxies need one, e.g. :1080"
        )
    if parsed.scheme.startswith("socks"):
        _require_socks_support(source)
    return url


def _require_socks_support(source: str) -> None:
    """Fail loudly rather than connecting directly without the proxy.

    Both libraries need a third-party package for SOCKS and neither says so
    usefully at the point of failure. install.bat installs them; a hand-built
    environment might not have.
    """
    missing = []
    try:
        import python_socks  # noqa: F401
    except ImportError:
        missing.append("python-socks (for the account stream)")
    try:
        import socks  # noqa: F401
    except ImportError:
        missing.append("PySocks (for HTTP calls)")
    if missing:
        raise ProxyError(
            f"{source} asks for a SOCKS proxy but {' and '.join(missing)} "
            "is not installed. Run install.bat again, or:\n"
            "    app\\.venv\\Scripts\\python.exe -m pip install python-socks PySocks"
        )


def redacted(url: str) -> str:
    """The address with any password replaced, for logs and screens.

    Works on the text rather than on `urlsplit`'s fields, because it has to
    hold for exactly the addresses `validate` is about to refuse: one with no
    scheme (`user:pw@host:1080`, where `urlsplit` reads `user` as the scheme
    and finds no password at all), one with a port that is not a number
    (where `.port` raises). Everything between the scheme and the LAST `@` is
    credentials; whatever follows the first `:` in it is the password.
    """
    scheme, sep, rest = url.partition("://")
    if not sep:
        scheme, rest = "", url
    userinfo, at, hostpart = rest.rpartition("@")
    if not at or ":" not in userinfo:
        return url
    user = userinfo.split(":", 1)[0]
    prefix = f"{scheme}://" if sep else ""
    return f"{prefix}{user}:***@{hostpart}"


def apply(url: str | None) -> None:
    """Put the proxy where both libraries will find it.

    A missing proxy clears the variables rather than leaving whatever the shell
    had: "no proxy configured" should mean the same thing however the bot was
    started.
    """
    if url is None:
        for name in _ENV_NAMES:
            os.environ.pop(name, None)
        return
    for name in _ENV_NAMES:
        os.environ[name] = url
    log.info("routing every connection through %s", redacted(url))


def configure(path: str = PROXY_FILE) -> str | None:
    """Read the file and apply it. Returns the proxy in use, if any."""
    url = load(path)
    apply(url)
    return url

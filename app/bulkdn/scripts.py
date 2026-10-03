"""What the operator's scripts and files are called on this system -- and the
few answers those scripts get from Python rather than work out for themselves.

The same bot ships `install.bat`, `run.bat` and `update.bat` for Windows and
`install.sh`, `run.sh` and `update.sh` for Linux. A message that tells someone
what to run has to name the one that exists on their machine -- `install.bat`
typed into a Linux shell is a "command not found" in the middle of recovering
from a problem.

Run as `python -m bulkdn.scripts <question>` (with `app` on PYTHONPATH), it
answers the scripts:

    proxy               the proxy line from proxy.local, as the bot reads it
    pip-proxy           the same, in the form pip accepts
    sdk-current WHEEL   exit 0 when this exact wheel is what is installed
    sdk-stamp WHEEL     record that it is, after installing it
    templates           write private_key.local and proxy.local if missing
    signing-check       sign and verify a transaction the way the bot will

The proxy used to be read five different ways -- install.bat took the LAST
usable line, update.bat the same but kept trailing spaces and took an
indented `  # note` for an address, the shell scripts took the FIRST line, and
the bot refused any file with two. So git and pip could go through one proxy
and the bot through another, or none. They now all ask here.

Standard library only, apart from `templates` and `signing-check`, which run
after the dependencies are installed: install.bat asks for the proxy before
anything is.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import pathlib
import sys

WINDOWS = os.name == "nt"


def script(name: str) -> str:
    """`install` -> `install.bat` on Windows, `./install.sh` elsewhere."""
    return f"{name}.bat" if WINDOWS else f"./{name}.sh"


def venv_python() -> str:
    """The virtualenv's interpreter, relative to the bot's folder."""
    return r"app\.venv\Scripts\python.exe" if WINDOWS else "app/.venv/bin/python"


def venv_folder() -> str:
    return r"app\.venv" if WINDOWS else "app/.venv"


# -- the proxy --------------------------------------------------------------


class ProxyLineError(Exception):
    """proxy.local holds something that is not one proxy address."""


def proxy_line(path: str = "proxy.local") -> str | None:
    """The one address in proxy.local, by the rule bulkdn.proxy.load uses.

    Every line is stripped; blank lines and lines starting with `#` -- after
    the strip, so an indented note is still a note -- are skipped; exactly one
    may remain. test_scripts holds this and bulkdn.proxy.load to the same
    answers.
    """
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not lines:
        return None
    if len(lines) > 1:
        raise ProxyLineError(
            f"{path} has {len(lines)} non-comment lines; it must hold exactly one "
            "proxy address"
        )
    return _validated(lines[0], path)


def _validated(line: str, path: str) -> str:
    """The bot's own check of the address, where the bot's code can run.

    It cannot on a first install -- bulkdn.proxy needs `requests`, which is not
    there yet -- and then the line is used as written; the bot checks it again
    when it starts. The one thing waved through is SOCKS support not being
    installed yet: git and pip do not need it, and installing it is what the
    script is about to do.
    """
    try:
        from . import proxy
    except ImportError:
        return line
    try:
        return proxy.validate(line, source=path)
    except proxy.ProxyError:
        socks = line.partition("://")[0].startswith("socks")
        missing = not all(
            importlib.util.find_spec(name) for name in ("python_socks", "socks")
        )
        if socks and missing:
            return line
        raise


def pip_proxy(line: str) -> str:
    """The address as pip will take it.

    pip's vendored urllib3 raises `PoolKey.__new__() got an unexpected keyword
    argument key_proxy_ssl_context` on any socks address, so a SOCKS proxy is
    handed to it as the same host and port with an http scheme -- which the
    providers this is used with serve on the same port.
    """
    scheme, sep, rest = line.partition("://")
    if sep and scheme.startswith("socks"):
        return f"http://{rest}"
    return line


# -- the SDK wheel ------------------------------------------------------------


def _stamp() -> pathlib.Path:
    """Where the installed wheel's hash is kept: inside the virtualenv."""
    return pathlib.Path(sys.prefix) / "bulk_client.sha256"


def _digest(wheel: str) -> str:
    return hashlib.sha256(pathlib.Path(wheel).read_bytes()).hexdigest()


def sdk_current(wheel: str) -> bool:
    """Whether this exact wheel is what the virtualenv has installed.

    The SDK's version string does not change between commits, so pip cannot
    tell one build from another and the installers used `--force-reinstall`
    every time -- under a running bot, during an update. The wheel's hash is
    what changes, so that is what is compared.
    """
    try:
        recorded = _stamp().read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return (
        recorded == _digest(wheel)
        and importlib.util.find_spec("bulk_api") is not None
    )


def sdk_stamp(wheel: str) -> None:
    _stamp().write_text(_digest(wheel) + "\n", encoding="utf-8")


# -- after the dependencies are in -------------------------------------------


def write_templates() -> None:
    """The key and proxy files, from the constants the bot itself uses.

    install.bat used to echo its own copy of both texts, line by line, and a
    test checked the two had not drifted; now there is one copy.
    """
    from .config import PRIVATE_KEY_FILE, PRIVATE_KEY_TEMPLATE
    from .proxy import PROXY_FILE, PROXY_TEMPLATE

    for name, text, note in (
        (PRIVATE_KEY_FILE, PRIVATE_KEY_TEMPLATE, "for your key"),
        (PROXY_FILE, PROXY_TEMPLATE, "(only needed if BULK is blocked where you are)"),
    ):
        path = pathlib.Path(name)
        if not path.exists():
            path.write_text(text, encoding="utf-8")
            print(f"  Created {name} {note}.")


def signing_check() -> None:
    """Sign a transaction the way the bot will, and check what came out.

    The installers' check used to import one name from the SDK and call that
    "Signing check: OK" -- which the PyPI build passes, though it cannot sign
    anything the exchange accepts: it leaves off the signature-domain byte.
    This builds a real transaction for a throwaway key, checks the domain byte
    is the last thing signed, and verifies the signature.
    """
    from bulk_api.common import SignatureDomain
    from bulk_api.common.signer import TransactionSigner

    from .config import SIGNATURE_DOMAIN_NAME

    domain = SignatureDomain[SIGNATURE_DOMAIN_NAME]
    signer = TransactionSigner.generate_account()
    actions = [{"updateUserSettings": {"m": [("BTC-USD", 5.0)]}}]
    message = TransactionSigner.serialize_transaction(actions, 1, signer.public_key, domain)
    if message[-1:] != bytes([domain.value]):
        raise RuntimeError(
            "the SDK does not sign the signature-domain byte -- this is the PyPI "
            "build, which the exchange refuses"
        )
    tx = signer.sign_transaction(
        {"actions": actions, "nonce": 1, "account": signer.public_key}, domain
    )
    tx["signer"] = signer.public_key
    if not signer.verify(tx, domain):
        raise RuntimeError("a signature the SDK just made does not verify")


# -- the command line the scripts call ----------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    question = args[0] if args else ""
    try:
        if question in ("proxy", "pip-proxy"):
            line = proxy_line()
            if line:
                print(line if question == "proxy" else pip_proxy(line))
            return 0
        if question == "sdk-current" and len(args) == 2:
            return 0 if sdk_current(args[1]) else 1
        if question == "sdk-stamp" and len(args) == 2:
            sdk_stamp(args[1])
            return 0
        if question == "templates":
            write_templates()
            return 0
        if question == "signing-check":
            signing_check()
            print("  Signing check: OK -- a test transaction was signed and verified.")
            return 0
    except Exception as exc:  # noqa: BLE001 - the scripts need a sentence and a code
        print(f"  {exc}", file=sys.stderr)
        return 2
    print(f"unknown question: {' '.join(args)!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

"""Password-encrypted private key at rest.

`private_key.local` may hold either a bare base58 seed or the envelope below.
Both are read; only the envelope is written.

    {"version": 1, "kdf": "argon2id", "ops": .., "mem": .., "salt": "..",
     "box": ".."}

Argon2id derives a 32-byte key from the password and the stored salt, and
XSalsa20-Poly1305 (`SecretBox`) encrypts the seed under it. Both come from
PyNaCl, which is already present -- it is what the SDK signs transactions with,
so this adds no dependency and no home-made cryptography.

The KDF parameters travel in the file rather than being hardcoded here, so a
file written today still opens after they are raised.

Poly1305 authenticates the ciphertext, so a wrong password fails to open the
box rather than yielding a plausible-looking wrong key. That is what makes
"wrong password" reportable at all.

**An empty password uses a constant that is printed in this source file.** It
stops a key being read by eye or scraped out of a backup, and stops nothing
else: anyone holding the file and this repository can open it. It exists
because a default is expected at the prompt, and the prompt says so.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass

import nacl.pwhash
import nacl.secret
import nacl.utils
from nacl.exceptions import CryptoError

from .state import REPLACE_ATTEMPTS, REPLACE_RETRY_DELAY_S

PASSWORD_ENV = "BULK_KEY_PASSWORD"

# Not a secret, and not pretending to be one. See the module docstring.
DEFAULT_PASSWORD = "bulkdn-default"

ENVELOPE_VERSION = 1
_ARGON = nacl.pwhash.argon2id


class KeystoreError(Exception):
    """Raised when a key file cannot be read, opened, or written."""


@dataclass(frozen=True)
class KdfParams:
    ops: int = _ARGON.OPSLIMIT_MODERATE
    mem: int = _ARGON.MEMLIMIT_MODERATE


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode())


def _derive(password: str, salt: bytes, params: KdfParams) -> bytes:
    return _ARGON.kdf(
        nacl.secret.SecretBox.KEY_SIZE,
        password.encode("utf-8"),
        salt,
        opslimit=params.ops,
        memlimit=params.mem,
    )


def encrypt(secret: str, password: str, params: KdfParams | None = None) -> dict:
    """Wrap a base58 seed in the envelope."""
    if not secret:
        raise KeystoreError("nothing to encrypt")
    params = params or KdfParams()
    salt = nacl.utils.random(_ARGON.SALTBYTES)
    box = nacl.secret.SecretBox(_derive(password, salt, params))
    return {
        "version": ENVELOPE_VERSION,
        "kdf": "argon2id",
        "ops": params.ops,
        "mem": params.mem,
        "salt": _b64(salt),
        "box": _b64(box.encrypt(secret.encode("utf-8"))),
    }


def decrypt(envelope: dict, password: str) -> str:
    """Open an envelope, or raise KeystoreError."""
    version = envelope.get("version")
    if version != ENVELOPE_VERSION:
        raise KeystoreError(
            f"unsupported key file version {version!r}; this build writes "
            f"version {ENVELOPE_VERSION}"
        )
    if envelope.get("kdf") != "argon2id":
        raise KeystoreError(f"unsupported kdf {envelope.get('kdf')!r}")

    try:
        params = KdfParams(ops=int(envelope["ops"]), mem=int(envelope["mem"]))
        salt = _unb64(envelope["salt"])
        sealed = _unb64(envelope["box"])
    except (KeyError, ValueError, TypeError) as exc:
        raise KeystoreError(f"malformed key file: {exc}") from exc

    box = nacl.secret.SecretBox(_derive(password, salt, params))
    try:
        return box.decrypt(sealed).decode("utf-8")
    except CryptoError as exc:
        # Poly1305 rejected it: wrong password, or the file was altered.
        raise KeystoreError("wrong password, or the key file is corrupt") from exc


def parse(text: str) -> dict | None:
    """The envelope in `text`, or None when it is a bare key.

    A plaintext file is a single base58 line, so anything that is not JSON with
    a `box` field is treated as one.
    """
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        envelope = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return envelope if isinstance(envelope, dict) and "box" in envelope else None


def read_plaintext_all(text: str) -> list[str]:
    """Every line that is neither blank nor a comment, in file order.

    One key per line. The order is kept because it is the operator's -- the
    first key is the one a single-account command uses, and shuffling it would
    silently change which account a `status` or a `transfer` talks about.

    Duplicates are dropped rather than refused: the same key twice is a
    copy-paste, not an instruction to trade an account against itself, and
    that is exactly what a pool would do with it.
    """
    keys: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and stripped not in keys:
            keys.append(stripped)
    return keys


def read_plaintext(text: str) -> str:
    """First line that is neither blank nor a comment."""
    keys = read_plaintext_all(text)
    return keys[0] if keys else ""


def resolve_password(prompt: str = "Enter password to decrypt private key") -> str:
    """Password from the environment, or from the terminal.

    The environment is checked first so an unattended run needs no terminal.
    An empty answer means the default, which the prompt states.
    """
    from_env = os.environ.get(PASSWORD_ENV)
    if from_env is not None:
        return from_env or DEFAULT_PASSWORD

    if not sys.stdin.isatty():
        raise KeystoreError(
            "the key file is encrypted and there is no terminal to ask on -- "
            f"set {PASSWORD_ENV}"
        )

    import getpass

    entered = getpass.getpass(f"{prompt} (empty for default): ")
    return entered or DEFAULT_PASSWORD


def load_all(path: str, password: str | None = None) -> list[str]:
    """Every key in the file, decrypting it when it is an envelope.

    Returns [] when the file is absent, which is not an error: keys may be
    coming from the environment instead.

    One envelope covers all of them rather than one envelope each. A file of
    keys where some are encrypted and some are not is a file whose protection
    is whatever the weakest line offers, and it invites the reading where a
    missing password prompt means the key was safe.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return []

    envelope = parse(text)
    if envelope is None:
        return read_plaintext_all(text)
    opened = decrypt(envelope, password if password is not None else resolve_password())
    return read_plaintext_all(opened)


def load(path: str, password: str | None = None) -> str:
    """The first key in the file, or "" when there is none.

    Kept because most commands act on one account. `load_all` is what the
    account pool uses.
    """
    keys = load_all(path, password)
    return keys[0] if keys else ""


def save_all(path: str, secrets: list[str], password: str) -> None:
    """Encrypt several keys into one envelope."""
    save(path, "\n".join(secrets), password)


def save(path: str, secret: str, password: str) -> None:
    """Write the envelope, replacing whatever was there.

    Written through a temporary file in the same directory and moved into
    place, so an interrupted write cannot leave a key file that is neither the
    old one nor the new one.
    """
    envelope = encrypt(secret, password)
    # Prove it opens before the plaintext is replaced.
    if decrypt(envelope, password) != secret:
        raise KeystoreError("re-reading the new key file did not return the key")

    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    # A fresh, uniquely named temp file rather than a fixed
    # `.private_key.local.tmp`. With a fixed name two saves at once -- the
    # menu and a second window, say -- wrote the same file and one moved the
    # other's half-written envelope into place; and a save that died left
    # that name behind for the next one to trip over. `mkstemp` also creates
    # it readable by the owner only on POSIX, so the envelope is never
    # briefly world-readable before the chmod below.
    fd, temp = tempfile.mkstemp(
        dir=directory, prefix=f".{os.path.basename(path)}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(envelope, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temp, path)
    except BaseException:
        # Including KeyboardInterrupt: an encrypted key left lying next to
        # the real one under a temp name is a copy nobody knows to erase.
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise

    # Best effort: on POSIX drop the group and other bits. Windows ignores it.
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def _replace_with_retry(temp: str, path: str) -> None:
    """`os.replace`, retried while Windows has the destination locked.

    The same fix `StateStore._replace_with_retry` carries, for the same
    reason: on Windows the rename fails with a sharing violation whenever
    another process has the destination open -- OneDrive syncing it, an
    antivirus scanning a file that just changed, the search indexer. A key
    file is exactly what a scanner looks at. The lock lasts milliseconds, and
    the temp file is complete and fsynced before the first attempt, so a
    retry risks nothing. Same schedule too, so there is one number to tune.
    """
    delay = REPLACE_RETRY_DELAY_S
    for attempt in range(1, REPLACE_ATTEMPTS + 1):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS:
                raise
            time.sleep(delay)
            delay *= 2


def is_encrypted(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as handle:
            return parse(handle.read()) is not None
    except FileNotFoundError:
        return False

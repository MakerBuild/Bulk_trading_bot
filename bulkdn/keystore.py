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
from dataclasses import dataclass

import nacl.pwhash
import nacl.secret
import nacl.utils
from nacl.exceptions import CryptoError

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


def read_plaintext(text: str) -> str:
    """First line that is neither blank nor a comment."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


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


def load(path: str, password: str | None = None) -> str:
    """Read a key file, decrypting it when it is an envelope.

    Returns "" when the file is absent, which is not an error: the key may be
    coming from the environment instead.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return ""

    envelope = parse(text)
    if envelope is None:
        return read_plaintext(text)
    return decrypt(envelope, password if password is not None else resolve_password())


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
    temp = os.path.join(directory, f".{os.path.basename(path)}.tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(envelope, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)

    # Best effort: on POSIX drop the group and other bits. Windows ignores it.
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def is_encrypted(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as handle:
            return parse(handle.read()) is not None
    except FileNotFoundError:
        return False

"""Encrypted key file.

The properties that matter: the seed does not appear in the file, a wrong
password is refused rather than silently returning something else, a bare
plaintext file still works, and a failed write never leaves the file in a
state that is neither the old key nor the new one.

Every test passes an explicit password, so nothing here can reach a prompt.
"""

import base64
import json

import pytest

from bulkdn import keystore

SEED = "4XmiBPzjsmugYJtYFmgh8GWYKQEjt2CtT1MqfZKi8pm4tevpthqRePiACfNoUz4DWtxsxtVYHzBYD8PR7qHC21Kc"

# Argon2id at the shipped parameters takes most of a second per call, which
# would dominate the suite. These are only ever used to exercise the envelope.
FAST = keystore.KdfParams(ops=1, mem=8 * 1024 * 1024)


def seal(password: str) -> dict:
    return keystore.encrypt(SEED, password, FAST)


def test_round_trip():
    assert keystore.decrypt(seal("hunter2"), "hunter2") == SEED


def test_the_seed_is_not_in_the_file():
    assert SEED not in json.dumps(seal("hunter2"))


def test_a_wrong_password_is_refused_not_guessed():
    """Poly1305 authenticates, so a near miss cannot return a plausible key."""
    envelope = seal("hunter2")
    with pytest.raises(keystore.KeystoreError, match="wrong password"):
        keystore.decrypt(envelope, "hunter3")
    with pytest.raises(keystore.KeystoreError, match="wrong password"):
        keystore.decrypt(envelope, "")


def test_two_encryptions_of_one_key_differ():
    """Fresh salt and nonce each time, so the file is not a fingerprint."""
    assert seal("hunter2")["box"] != seal("hunter2")["box"]
    assert seal("hunter2")["salt"] != seal("hunter2")["salt"]


def test_kdf_parameters_travel_with_the_file():
    """A file written today must still open after the defaults are raised."""
    envelope = seal("hunter2")
    assert envelope["ops"] == FAST.ops
    assert envelope["mem"] == FAST.mem
    assert keystore.decrypt(envelope, "hunter2") == SEED


def test_tampering_is_detected():
    envelope = seal("hunter2")
    raw = bytearray(base64.b64decode(envelope["box"]))
    raw[-1] ^= 0x01
    envelope["box"] = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(keystore.KeystoreError):
        keystore.decrypt(envelope, "hunter2")


def test_unknown_version_and_kdf_are_refused():
    envelope = seal("hunter2")
    with pytest.raises(keystore.KeystoreError, match="version"):
        keystore.decrypt({**envelope, "version": 99}, "hunter2")
    with pytest.raises(keystore.KeystoreError, match="kdf"):
        keystore.decrypt({**envelope, "kdf": "pbkdf2"}, "hunter2")


def test_a_malformed_envelope_reports_itself():
    with pytest.raises(keystore.KeystoreError, match="malformed"):
        keystore.decrypt({"version": 1, "kdf": "argon2id", "box": "x"}, "pw")


def test_encrypting_nothing_is_refused():
    with pytest.raises(keystore.KeystoreError, match="nothing to encrypt"):
        keystore.encrypt("", "pw")


# -- parsing ---------------------------------------------------------------


def test_parse_tells_an_envelope_from_a_bare_key():
    assert keystore.parse(json.dumps(seal("pw"))) is not None
    assert keystore.parse(SEED) is None
    assert keystore.parse("") is None
    # JSON, but not ours.
    assert keystore.parse('{"hello": 1}') is None
    assert keystore.parse("{not json") is None


def test_plaintext_reader_skips_comments_and_blanks():
    assert keystore.read_plaintext(f"# a note\n\n{SEED}\n") == SEED
    assert keystore.read_plaintext("# only a comment\n") == ""


# -- files -----------------------------------------------------------------


def test_save_then_load(tmp_path):
    path = str(tmp_path / "private_key.local")
    keystore.save(path, SEED, "pw")
    assert keystore.is_encrypted(path)
    assert keystore.load(path, "pw") == SEED


def test_a_plaintext_file_still_loads(tmp_path):
    """Existing installs keep working without being converted first."""
    path = tmp_path / "private_key.local"
    path.write_text(f"# header\n{SEED}\n", encoding="utf-8")
    assert keystore.is_encrypted(str(path)) is False
    assert keystore.load(str(path)) == SEED


def test_a_missing_file_is_empty_not_an_error(tmp_path):
    """The key may be coming from the environment instead."""
    assert keystore.load(str(tmp_path / "absent")) == ""
    assert keystore.is_encrypted(str(tmp_path / "absent")) is False


def test_save_leaves_no_temporary_behind(tmp_path):
    path = str(tmp_path / "private_key.local")
    keystore.save(path, SEED, "pw")
    assert [p.name for p in tmp_path.iterdir()] == ["private_key.local"]


def test_re_encrypting_under_a_new_password(tmp_path):
    path = str(tmp_path / "private_key.local")
    keystore.save(path, SEED, "old")
    keystore.save(path, keystore.load(path, "old"), "new")
    assert keystore.load(path, "new") == SEED
    with pytest.raises(keystore.KeystoreError):
        keystore.load(path, "old")


# -- password resolution ---------------------------------------------------


def test_the_environment_wins_over_the_prompt(monkeypatch):
    monkeypatch.setenv(keystore.PASSWORD_ENV, "from-env")
    assert keystore.resolve_password() == "from-env"


def test_an_empty_environment_value_means_the_default(monkeypatch):
    monkeypatch.setenv(keystore.PASSWORD_ENV, "")
    assert keystore.resolve_password() == keystore.DEFAULT_PASSWORD


def test_no_terminal_and_no_environment_is_an_error(monkeypatch):
    monkeypatch.delenv(keystore.PASSWORD_ENV, raising=False)
    monkeypatch.setattr(keystore.sys.stdin, "isatty", lambda: False)
    with pytest.raises(keystore.KeystoreError, match=keystore.PASSWORD_ENV):
        keystore.resolve_password()


# -- the interactive command -----------------------------------------------


class KeyOnly:
    """Stands in for Config: cmd_encrypt_key reads only this field."""

    def __init__(self, key=SEED):
        self.private_key = key


def run_encrypt(monkeypatch, tmp_path, answers, key=SEED):
    """Drive cmd_encrypt_key with scripted passwords, in a scratch directory."""
    from bulkdn import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("bulkdn.config.PRIVATE_KEY_FILE", "private_key.local")
    monkeypatch.setattr(keystore.sys.stdin, "isatty", lambda: True)
    supplied = iter(answers)
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: next(supplied))
    return cli.cmd_encrypt_key(KeyOnly(key))


def test_mismatched_passwords_write_nothing(monkeypatch, tmp_path):
    """The guard that matters: a typo must not lock the key away."""
    assert run_encrypt(monkeypatch, tmp_path, ["one", "two"]) == 1
    assert not (tmp_path / "private_key.local").exists()


def test_a_confirmed_password_encrypts(monkeypatch, tmp_path):
    assert run_encrypt(monkeypatch, tmp_path, ["secret", "secret"]) == 0
    path = str(tmp_path / "private_key.local")
    assert keystore.is_encrypted(path)
    assert keystore.load(path, "secret") == SEED


def test_an_empty_password_uses_the_default(monkeypatch, tmp_path):
    assert run_encrypt(monkeypatch, tmp_path, ["", ""]) == 0
    path = str(tmp_path / "private_key.local")
    assert keystore.load(path, keystore.DEFAULT_PASSWORD) == SEED


def test_no_key_is_refused(monkeypatch, tmp_path):
    assert run_encrypt(monkeypatch, tmp_path, [], key="") == 1


def test_without_a_terminal_it_refuses_rather_than_echoing(monkeypatch, tmp_path):
    """getpass would fall back to an echoing read, putting the password on screen."""
    from bulkdn import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(keystore.sys.stdin, "isatty", lambda: False)
    assert cli.cmd_encrypt_key(KeyOnly()) == 1
    assert not (tmp_path / "private_key.local").exists()

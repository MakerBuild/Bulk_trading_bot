"""Several master keys in one key file.

The pool the strategy will draw pairs from is every account under every key,
so the first thing that has to hold several is the file. One envelope covers
all of them rather than one each: a file where some lines are encrypted and
some are not is protected to the standard of its weakest line, and it invites
the reading where the absence of a password prompt means the key was safe.

None of these tests use a real key. They are strings that look like keys.
"""

import pytest

from bulkdn import keystore
from bulkdn.config import Config, LegConfig

A = "key-alpha"
B = "key-bravo"
C = "key-charlie"


# -- reading a plaintext file ----------------------------------------------


def test_one_key_per_line_in_file_order():
    assert keystore.read_plaintext_all(f"{A}\n{B}\n{C}\n") == [A, B, C]


def test_blank_lines_and_comments_are_not_keys():
    text = f"# the main one\n{A}\n\n   \n# and the spare\n{B}\n"
    assert keystore.read_plaintext_all(text) == [A, B]


def test_the_same_key_twice_is_a_copy_paste():
    """A pool would otherwise pair an account against itself, which is not a
    hedge and not what anyone meant by writing it down twice."""
    assert keystore.read_plaintext_all(f"{A}\n{B}\n{A}\n") == [A, B]


def test_order_is_kept_because_the_first_key_means_something():
    """status, transfer and create-subaccount act on one account, and it is
    this one. Sorting the list would silently change which."""
    assert keystore.read_plaintext_all(f"{C}\n{A}\n{B}\n")[0] == C


def test_a_single_key_file_reads_exactly_as_it_always_did():
    assert keystore.read_plaintext(f"{A}\n") == A
    assert keystore.read_plaintext_all(f"{A}\n") == [A]


def test_an_empty_file_is_no_keys_rather_than_one_empty_key():
    assert keystore.read_plaintext_all("\n#only a comment\n") == []
    assert keystore.read_plaintext("") == ""


# -- and an encrypted one ---------------------------------------------------


def test_every_key_survives_the_round_trip(tmp_path):
    path = str(tmp_path / "private_key.local")
    keystore.save_all(path, [A, B, C], "hunter2")

    assert keystore.load_all(path, "hunter2") == [A, B, C]


def test_one_envelope_covers_them_all(tmp_path):
    """Not one envelope per key: mixed protection is the weakest of them."""
    path = str(tmp_path / "private_key.local")
    keystore.save_all(path, [A, B], "hunter2")

    body = (tmp_path / "private_key.local").read_text(encoding="utf-8")
    assert body.count('"box"') == 1
    assert A not in body and B not in body, "a key was left readable on disk"


def test_the_wrong_password_opens_nothing(tmp_path):
    path = str(tmp_path / "private_key.local")
    keystore.save_all(path, [A, B], "hunter2")

    with pytest.raises(keystore.KeystoreError):
        keystore.load_all(path, "hunter3")


def test_load_still_returns_just_the_first(tmp_path):
    path = str(tmp_path / "private_key.local")
    keystore.save_all(path, [A, B, C], "hunter2")

    assert keystore.load(path, "hunter2") == A


def test_a_missing_file_is_no_keys_rather_than_an_error(tmp_path):
    """They may be coming from the environment instead."""
    assert keystore.load_all(str(tmp_path / "nothing.local")) == []


# -- how the config carries them -------------------------------------------


def config(**kwargs):
    return Config(
        master_account=LegConfig(symbol="BTC-USD", size=1.0, offset_bps=1.0,
                                 max_distance_bps=5.0),
        sub_account=LegConfig(symbol="ETH-USD", size=1.0, offset_bps=1.0,
                              max_distance_bps=5.0),
        **kwargs,
    )


def test_one_key_given_alone_still_fills_the_pool():
    """Everything that existed before this names a single key."""
    built = config(private_key=A)
    assert built.private_keys == [A]
    assert built.private_key == A


def test_a_pool_given_alone_still_names_a_first_key():
    built = config(private_keys=[A, B])
    assert built.private_key == A


def test_the_two_cannot_disagree():
    built = config(private_key=A, private_keys=[A, B, C])
    assert built.private_key == A
    assert built.private_keys == [A, B, C]


def test_no_keys_at_all_is_not_an_error_here():
    """`check` and the menu load a config without credentials on purpose."""
    built = config()
    assert built.private_keys == []
    assert built.private_key == ""

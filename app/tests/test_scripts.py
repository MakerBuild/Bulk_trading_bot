"""The answers the install and update scripts get from Python.

The proxy was read five ways -- install.bat took the last usable line,
update.bat too but kept trailing spaces and took an indented `# note` for an
address, the shell scripts took the first line, and the bot refused a file
with two -- so git and pip could go through one proxy and the bot another.
"""

import pytest

from bulkdn import proxy, scripts

CASES = [
    "",
    "# only comments\n#\n",
    "http://proxy.example.com:8080\n",
    "   socks5h://user:pa!ss@proxy.example.com:1080   \n",
    "# note\n  # an indented note is still a note\nhttp://proxy.example.com:8080\n",
    "\r\nhttp://proxy.example.com:8080\r\n\r\n",
]


@pytest.mark.parametrize("text", CASES)
def test_the_scripts_read_the_same_address_as_the_bot(tmp_path, text):
    path = tmp_path / "proxy.local"
    path.write_text(text, encoding="utf-8")
    assert scripts.proxy_line(str(path)) == proxy.load(str(path))


def test_two_addresses_are_refused_by_both(tmp_path):
    path = tmp_path / "proxy.local"
    path.write_text("http://a.example.com:1\nhttp://b.example.com:2\n", encoding="utf-8")
    with pytest.raises(proxy.ProxyError):
        proxy.load(str(path))
    with pytest.raises(scripts.ProxyLineError):
        scripts.proxy_line(str(path))


def test_a_bad_address_stops_the_script(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "proxy.local").write_text("proxy.example.com:8080\n", encoding="utf-8")
    assert scripts.main(["proxy"]) != 0
    assert "no scheme" in capsys.readouterr().err


def test_no_proxy_prints_nothing_and_succeeds(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert scripts.main(["proxy"]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("line,expected", [
    ("socks5h://u:p@h.example.com:1080", "http://u:p@h.example.com:1080"),
    ("socks5://h.example.com:1080", "http://h.example.com:1080"),
    ("http://h.example.com:8080", "http://h.example.com:8080"),
])
def test_pip_gets_the_same_host_over_http(line, expected):
    assert scripts.pip_proxy(line) == expected


def test_the_wheel_is_current_only_after_it_is_stamped(tmp_path, monkeypatch):
    monkeypatch.setattr(scripts.sys, "prefix", str(tmp_path))
    wheel = tmp_path / "sdk.whl"
    wheel.write_bytes(b"one build")
    assert not scripts.sdk_current(str(wheel))
    scripts.sdk_stamp(str(wheel))
    assert scripts.sdk_current(str(wheel))
    wheel.write_bytes(b"another build, same version string")
    assert not scripts.sdk_current(str(wheel)), "a changed wheel was not noticed"


def test_the_signing_check_signs_and_verifies():
    scripts.signing_check()


def test_the_signing_check_catches_a_build_without_the_domain_byte(monkeypatch):
    from bulk_api.common.signer import TransactionSigner

    real = TransactionSigner.serialize_transaction

    def pypi(actions, nonce, account, domain):
        return real(actions, nonce, account, domain)[:-1]

    monkeypatch.setattr(TransactionSigner, "serialize_transaction", staticmethod(pypi))
    with pytest.raises(RuntimeError, match="domain byte"):
        scripts.signing_check()


def test_the_templates_are_the_bots_own(tmp_path, monkeypatch):
    from bulkdn.config import PRIVATE_KEY_FILE, PRIVATE_KEY_TEMPLATE

    monkeypatch.chdir(tmp_path)
    assert scripts.main(["templates"]) == 0
    assert (tmp_path / PRIVATE_KEY_FILE).read_text(encoding="utf-8") == PRIVATE_KEY_TEMPLATE
    assert (tmp_path / proxy.PROXY_FILE).read_text(encoding="utf-8") == proxy.PROXY_TEMPLATE

"""Routing everything through a proxy, for operators where BULK is blocked.

The failure this guards against is not a crash. It is the proxy being quietly
ignored: the bot then connects directly, the connection is refused or hangs, and
that looks exactly like the exchange being down. So the tests are mostly about
a bad address being refused loudly and a good one reaching both transports.
"""

import os
import pathlib

import pytest

from bulkdn import proxy

GOOD = "socks5h://user:secret@proxy.example.com:1080"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in proxy._ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def write(tmp_path, body):
    path = tmp_path / proxy.PROXY_FILE
    path.write_text(body, encoding="utf-8")
    return str(path)


# -- no proxy is the normal case ---------------------------------------------


def test_a_missing_file_means_no_proxy(tmp_path):
    assert proxy.load(str(tmp_path / "absent.local")) is None


def test_the_shipped_template_means_no_proxy(tmp_path):
    """It is all comments, so a fresh install configures nothing."""
    assert proxy.load(write(tmp_path, proxy.PROXY_TEMPLATE)) is None


def test_an_empty_file_means_no_proxy(tmp_path):
    assert proxy.load(write(tmp_path, "\n\n   \n")) is None


def test_applying_nothing_clears_the_environment(monkeypatch):
    """However the bot was started, 'no proxy' has to mean the same thing."""
    monkeypatch.setenv("HTTPS_PROXY", "http://left-over:8080")
    proxy.apply(None)
    assert "HTTPS_PROXY" not in os.environ


# -- a proxy reaches both transports -----------------------------------------


def test_it_is_set_where_requests_looks():
    proxy.apply(GOOD)
    assert os.environ["HTTPS_PROXY"] == GOOD
    assert os.environ["https_proxy"] == GOOD


def test_it_is_set_where_websockets_looks():
    """websockets reads urllib.request.getproxies, and gives wss priority."""
    import urllib.request

    proxy.apply(GOOD)
    assert os.environ["WSS_PROXY"] == GOOD
    # The resolver itself, rather than the spelling: it lower-cases the names
    # it finds, so what matters is that it ends up in there at all.
    assert GOOD in urllib.request.getproxies().values()


def test_the_websocket_layer_actually_picks_it_up():
    """The property that matters: the library's own resolver finds it."""
    from websockets.proxy import get_proxy
    from websockets.uri import parse_uri

    proxy.apply(GOOD)
    assert get_proxy(parse_uri("wss://mainnet-ws1.bulk.trade")) == GOOD


def test_configure_reads_the_file_and_applies_it(tmp_path):
    path = write(tmp_path, f"{proxy.PROXY_TEMPLATE}\n{GOOD}\n")
    assert proxy.configure(path) == GOOD
    assert os.environ["HTTPS_PROXY"] == GOOD


# -- a bad address is refused, never ignored ---------------------------------


@pytest.mark.parametrize(
    "line, complaint",
    [
        ("proxy.example.com:1080", "no scheme"),
        ("ftp://proxy.example.com:1080", "not a proxy scheme"),
        ("http://:8080", "no host"),
        ("http://proxy.example.com", "no port"),
    ],
)
def test_a_malformed_address_raises(tmp_path, line, complaint):
    with pytest.raises(proxy.ProxyError, match=complaint):
        proxy.load(write(tmp_path, line))


def test_two_addresses_are_refused(tmp_path):
    """Silently taking the first would be a coin flip over which one is live."""
    with pytest.raises(proxy.ProxyError, match="exactly one"):
        proxy.load(write(tmp_path, f"{GOOD}\nhttp://other.example.com:8080\n"))


def test_socks_without_its_packages_is_refused(tmp_path, monkeypatch):
    """Otherwise the address is accepted and the connection goes direct."""
    import builtins

    real_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name in ("python_socks", "socks"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(proxy.ProxyError, match="python-socks"):
        proxy.load(write(tmp_path, GOOD))


@pytest.mark.parametrize(
    "url",
    [
        "http://proxy.example.com:8080",
        "https://proxy.example.com:8443",
        "socks5://proxy.example.com:1080",
        "socks5h://user:pw@proxy.example.com:1080",
    ],
)
def test_the_schemes_a_provider_hands_out_are_accepted(url):
    assert proxy.validate(url) == url


# -- the password does not get printed ---------------------------------------


def test_the_password_is_hidden_in_logs():
    shown = proxy.redacted(GOOD)
    assert "secret" not in shown
    assert "proxy.example.com:1080" in shown and "user" in shown


def test_an_address_without_a_password_is_shown_whole():
    plain = "socks5h://proxy.example.com:1080"
    assert proxy.redacted(plain) == plain


# -- the file is the operator's, and erasable --------------------------------


def test_install_bat_writes_the_same_template():
    """Two copies of this text exist -- batch cannot read a Python constant --
    so they are checked against each other rather than trusted to stay equal."""
    root = pathlib.Path(__file__).resolve().parents[2]
    batch = (root / "install.bat").read_text(encoding="utf-8")

    echoed = []
    for line in batch.splitlines():
        stripped = line.strip()
        if "proxy.local echo" not in stripped:
            continue
        text = stripped.split("proxy.local echo", 1)[1]
        echoed.append("" if text.strip() == "." else text.strip().replace("^", ""))

    expected = [line.strip() for line in proxy.PROXY_TEMPLATE.splitlines()]
    assert echoed == expected, "install.bat and PROXY_TEMPLATE have drifted"


def test_erasing_the_proxy_empties_it_rather_than_deleting(tmp_path, monkeypatch):
    """Same reason as the key file: it is where the next one gets pasted."""
    from bulkdn import menu

    monkeypatch.chdir(tmp_path)
    path = tmp_path / proxy.PROXY_FILE
    path.write_text(f"{proxy.PROXY_TEMPLATE}\n{GOOD}\n", encoding="utf-8")

    result = menu._delete(path)

    assert path.exists()
    assert path.read_text(encoding="utf-8") == proxy.PROXY_TEMPLATE
    assert "secret" not in path.read_text(encoding="utf-8")
    assert "emptied" in result

"""Meta-test: the offline guard in conftest.py actually blocks network access."""

from __future__ import annotations

import socket

import pytest
from hypothesis import settings
from support.netguard import NetworkAccessError


def test_socket_connect_is_blocked() -> None:
    sock = socket.socket()
    try:
        with pytest.raises(NetworkAccessError, match="test_socket_connect_is_blocked"):
            sock.connect(("127.0.0.1", 9))
    finally:
        sock.close()


def test_create_connection_is_blocked() -> None:
    with pytest.raises(NetworkAccessError, match="test_create_connection_is_blocked"):
        socket.create_connection(("127.0.0.1", 9))


def test_getaddrinfo_is_blocked() -> None:
    with pytest.raises(NetworkAccessError, match="test_getaddrinfo_is_blocked"):
        socket.getaddrinfo("example.com", 80)


def test_gethostbyname_is_blocked() -> None:
    with pytest.raises(NetworkAccessError, match="test_gethostbyname_is_blocked"):
        socket.gethostbyname("example.com")


def test_gethostbyname_ex_is_blocked() -> None:
    with pytest.raises(NetworkAccessError, match="test_gethostbyname_ex_is_blocked"):
        socket.gethostbyname_ex("example.com")


def test_gethostbyaddr_is_blocked() -> None:
    with pytest.raises(NetworkAccessError, match="test_gethostbyaddr_is_blocked"):
        socket.gethostbyaddr("127.0.0.1")


def test_socketpair_still_round_trips() -> None:
    a, b = socket.socketpair()
    try:
        a.sendall(b"x")
        assert b.recv(1) == b"x"
    finally:
        a.close()
        b.close()


def test_network_access_error_is_assertion_error() -> None:
    assert issubclass(NetworkAccessError, AssertionError)


def test_yfinance_download_is_blocked() -> None:
    yfinance = pytest.importorskip("yfinance")
    with pytest.raises(NetworkAccessError):
        yfinance.download("SPY")


def test_ccxt_fetch_is_blocked() -> None:
    ccxt = pytest.importorskip("ccxt")
    with pytest.raises(NetworkAccessError):
        ccxt.Exchange.fetch(object(), "https://example.com")


def test_hypothesis_profiles_are_registered() -> None:
    ci = settings.get_profile("ci")
    dev = settings.get_profile("dev")
    assert ci.derandomize is True
    assert ci.max_examples == 200
    assert dev.max_examples == 50

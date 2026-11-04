"""测试进程的联网禁令（tests/conftest.py）：自动测试不得连接本机以外的地址。"""
import socket

import pytest


def test_external_connection_is_blocked_and_recorded(external_network_attempts):
    with pytest.raises(AssertionError, match="example.invalid"):
        socket.create_connection(("example.invalid", 80), timeout=1)
    sock = socket.socket()
    try:
        with pytest.raises(AssertionError, match="192.0.2.1"):
            sock.connect(("192.0.2.1", 80))
    finally:
        sock.close()
    assert len(external_network_attempts) == 2
    external_network_attempts.clear()  # 这两次是本测试故意触发的，不算违规


def test_loopback_is_allowed(external_network_attempts):
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(("127.0.0.1", server.getsockname()[1]), timeout=5):
            pass
    assert socket.getaddrinfo("localhost", 80)
    assert external_network_attempts == []

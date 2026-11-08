"""Hermetic boundary regressions, including real local IPC and HTTP clients."""
import asyncio
import builtins
import socket
import tempfile
from pathlib import Path

import httpx
import pytest

from _pytest_network_guard import ExternalNetworkBlocked, install

MESSAGE = 'External network disabled in tests'


def forbidden(*args, **kwargs):
    raise AssertionError('Underlying network was reached')


@pytest.mark.parametrize('host', [
    'secret.example', '192.0.2.1', '::ffff:192.0.2.1', 'localhost.',
    '127.1', '2130706433', '127.0.0.1.evil', '::1%secret', '', b'\xff', None,
])
@pytest.mark.parametrize('method', ['getaddrinfo', 'gethostbyname', 'gethostbyname_ex', 'gethostbyaddr'])
def test_external_resolver_denied_before_underlying(monkeypatch, host, method):
    monkeypatch.setattr(socket, method, forbidden)
    install(monkeypatch)
    args = (host, 443) if method == 'getaddrinfo' else (host,)
    with pytest.raises(ExternalNetworkBlocked) as exc:
        getattr(socket, method)(*args)
    assert str(exc.value) == MESSAGE


@pytest.mark.parametrize('kind', [socket.SOCK_STREAM, socket.SOCK_DGRAM])
@pytest.mark.parametrize('method', ['connect', 'connect_ex', 'sendto', 'sendmsg'])
@pytest.mark.parametrize('family,host', [(socket.AF_INET, '192.0.2.1'), (socket.AF_INET6, '2001:db8::1')])
def test_direct_external_socket_denied_before_underlying(monkeypatch, method, family, host, kind):
    if not hasattr(socket.socket, method):
        pytest.skip('sendmsg unavailable')
    monkeypatch.setattr(socket.socket, method, forbidden)
    install(monkeypatch)
    with socket.socket(family, kind) as client:
        address = (host, 443)
        args = (b'secret', address) if method == 'sendto' else (address,)
        if method == 'sendmsg':
            args = ([b'secret'], [], 0, address)
        with pytest.raises(ExternalNetworkBlocked) as exc:
            getattr(client, method)(*args)
        assert str(exc.value) == MESSAGE


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('host', ['secret.example', '192.0.2.1'])
def test_ordinary_http_denied_without_resolving_or_connecting(monkeypatch, asynchronous, host):
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    install(monkeypatch)
    url = f'http://{host}/private?token=secret'

    async def request():
        async with httpx.AsyncClient(trust_env=False) as client:
            await client.get(url, headers={'Authorization': 'secret'})

    with pytest.raises((ExternalNetworkBlocked, builtins.ExceptionGroup)) as exc:
        if asynchronous:
            asyncio.run(request())
        else:
            with httpx.Client(trust_env=False) as client:
                client.get(url, headers={'Authorization': 'secret'})
    errors = exc.value.exceptions if isinstance(exc.value, builtins.ExceptionGroup) else [exc.value]
    assert all(isinstance(error, ExternalNetworkBlocked) and str(error) == MESSAGE for error in errors)


@pytest.mark.parametrize('host', ['127.0.0.1', 'localhost'])
def test_real_loopback_tcp_works(host):
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        server.listen()
        with socket.create_connection((host, server.getsockname()[1]), timeout=1) as client:
            connection, _ = server.accept()
            with connection:
                client.sendall(b'ok')
                assert connection.recv(2) == b'ok'


def test_real_loopback_udp_works():
    with socket.socket(type=socket.SOCK_DGRAM) as server:
        server.settimeout(1)
        server.bind(('127.0.0.1', 0))
        with socket.socket(type=socket.SOCK_DGRAM) as client:
            client.sendto(b'ok', server.getsockname())
        assert server.recvfrom(2)[0] == b'ok'


def test_real_unix_socket_works():
    with tempfile.TemporaryDirectory(dir='/tmp') as directory, socket.socket(socket.AF_UNIX) as server:
        path = str(Path(directory) / 'local.sock')
        server.bind(path)
        server.listen()
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(path)
            connection, _ = server.accept()
            connection.close()


def test_mock_transport_unaffected():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
        assert client.get('https://synthetic.example').status_code == 200


def test_real_ipv6_loopback_works():
    with socket.socket(socket.AF_INET6) as server:
        try:
            server.bind(('::1', 0))
        except OSError:
            pytest.skip('IPv6 loopback unavailable')
        server.listen()
        with socket.create_connection(('::1', server.getsockname()[1]), timeout=1):
            connection, _ = server.accept()
            connection.close()


def test_reverse_nameinfo_denied_before_underlying(monkeypatch):
    monkeypatch.setattr(socket, 'getnameinfo', forbidden)
    install(monkeypatch)
    with pytest.raises(ExternalNetworkBlocked, match=f'^{MESSAGE}$'):
        socket.getnameinfo(('192.0.2.1', 443), 0)

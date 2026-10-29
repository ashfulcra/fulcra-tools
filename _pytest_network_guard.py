"""Test-only Python socket boundary, not an OS or child-process sandbox.

Only numeric loopback and exact localhost are permitted. No arbitrary hostname
is resolved to decide permission. Tests replacing these methods must supply
hermetic substitutes. Imported aliases, native clients and child processes are
outside this Python monkeypatch boundary; collection-time I/O is also outside
its function-scoped lifetime.
"""
from __future__ import annotations

import ipaddress
import socket


class ExternalNetworkBlocked(RuntimeError):
    """Static message deliberately excludes addresses and credential material."""


def _check_host(host):
    if isinstance(host, bytes):
        try:
            host = host.decode('ascii')
        except UnicodeError:
            raise ExternalNetworkBlocked('External network disabled in tests') from None
    if host == 'localhost':
        return
    try:
        # Scoped IPv6 literals are intentionally rejected: fixture endpoints need
        # no interface suffix, and ambiguous spellings should fail closed.
        if not isinstance(host, str) or '%' in host:
            raise ValueError
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise ExternalNetworkBlocked('External network disabled in tests')


def install(monkeypatch):
    """Patch ordinary sync/async Python networking before resolver/socket I/O."""
    for name in ('getaddrinfo', 'gethostbyname', 'gethostbyname_ex', 'gethostbyaddr'):
        original = getattr(socket, name)

        def resolve(host, *args, _original=original, **kwargs):
            _check_host(host)
            return _original(host, *args, **kwargs)

        monkeypatch.setattr(socket, name, resolve)

    original_nameinfo = socket.getnameinfo

    def nameinfo(address, flags):
        if not isinstance(address, tuple) or not address:
            raise ExternalNetworkBlocked('External network disabled in tests')
        _check_host(address[0])
        return original_nameinfo(address, flags)

    monkeypatch.setattr(socket, 'getnameinfo', nameinfo)

    for name in ('connect', 'connect_ex', 'sendto', 'sendmsg'):
        if not hasattr(socket.socket, name):
            continue
        original = getattr(socket.socket, name)

        def outbound(sock, *args, _name=name, _original=original, **kwargs):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                if _name in ('connect', 'connect_ex'):
                    address = args[0] if args else kwargs.get('address')
                elif _name == 'sendto':
                    address = args[-1] if len(args) >= 2 else kwargs.get('address')
                else:
                    address = args[3] if len(args) > 3 else kwargs.get('address')
                    # Omitted destination uses a previously guarded connection.
                    if address is None:
                        return _original(sock, *args, **kwargs)
                if not isinstance(address, tuple) or not address:
                    raise ExternalNetworkBlocked('External network disabled in tests')
                _check_host(address[0])
            return _original(sock, *args, **kwargs)

        monkeypatch.setattr(socket.socket, name, outbound)

"""In-process guard canaries: no children, real DNS or real transport calls."""
import asyncio
import os
import socket
import subprocess

import httpx
import pytest


def test_commands_are_blocked(netflix_isolation):
    for entry in (subprocess.run, subprocess.Popen, os.system, os.popen):
        with pytest.raises(netflix_isolation["error"], match="^Netflix test external action blocked$"):
            entry("must-not-start")


def test_connections_and_dns_are_blocked(netflix_isolation):
    calls = [
        lambda: socket.create_connection(("synthetic.invalid", 443)),
        lambda: socket.getaddrinfo("synthetic.invalid", 443),
        lambda: socket.gethostbyname("synthetic.invalid"),
        lambda: socket.gethostbyname_ex("synthetic.invalid"),
        lambda: socket.gethostbyaddr("192.0.2.1"),
        lambda: socket.getnameinfo(("192.0.2.1", 443), 0),
    ]
    with socket.socket() as sock:
        calls.extend([
            lambda: sock.connect(("192.0.2.1", 443)),
            lambda: sock.connect_ex(("192.0.2.1", 443)),
            lambda: sock.sendto(b"synthetic", ("192.0.2.1", 443)),
        ])
        for call in calls:
            with pytest.raises(netflix_isolation["error"], match="^Netflix test external action blocked$"):
                call()


def test_real_httpx_transports_are_blocked(netflix_isolation):
    request = httpx.Request("GET", "https://synthetic.invalid")
    with httpx.HTTPTransport() as transport:
        with pytest.raises(netflix_isolation["error"], match="^Netflix test external action blocked$"):
            transport.handle_request(request)

    async def probe():
        async with httpx.AsyncHTTPTransport() as transport:
            with pytest.raises(netflix_isolation["error"], match="^Netflix test external action blocked$"):
                await transport.handle_async_request(request)

    asyncio.run(probe())


def test_auth_is_synthetic_and_saved_credentials_blocked(ni, netflix_isolation):
    from fulcra_api.core import FulcraAPI
    from fulcra_api.credentials import FulcraCredentials
    from fulcra_collect import credentials
    from fulcra_common.client import BaseFulcraClient

    token = BaseFulcraClient().get_token()
    assert token == netflix_isolation["token"]
    assert FulcraAPI(credentials=FulcraCredentials(access_token=token)).get_fulcra_userid() == (
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    )
    if hasattr(ni, "get_token"):
        assert ni.get_token() == token
    with pytest.raises(netflix_isolation["error"], match="^Netflix test external action blocked$"):
        credentials.get_user_secret_fresh("synthetic")

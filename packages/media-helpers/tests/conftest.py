"""Shared test fixtures."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest


class RecordingTransport(httpx.MockTransport):
    """MockTransport that records every request it sees."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []

        def wrapper(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            self.bodies.append(request.content)
            return handler(request)

        super().__init__(wrapper)


@pytest.fixture
def recording_transport():
    def make(handler: Callable[[httpx.Request], httpx.Response]) -> RecordingTransport:
        return RecordingTransport(handler)
    return make


@pytest.fixture(autouse=True)
def isolated_collect_source_secrets(monkeypatch):
    """A new daemon final source fence must never reach the host Keychain."""
    from fulcra_collect import credentials
    # Preserve the real credential API: health/OAuth fixtures may install a
    # narrower in-memory backend and must own the values they write/read.
    monkeypatch.setattr(credentials, "_uses_macos_keychain", lambda: False)
    monkeypatch.setattr(credentials.keyring, "get_password", lambda *_: None)
    def unexpected_write(*_):
        pytest.fail("credential write requires an isolated test backend")
    monkeypatch.setattr(credentials.keyring, "set_password", unexpected_write)
    monkeypatch.setattr(credentials.keyring, "delete_password", unexpected_write)


@pytest.fixture(autouse=True)
def trakt_caller_containment(request, monkeypatch, tmp_path):
    """Actual legacy SDK auth may spawn children: contain BEFORE caller fixtures."""
    if not (request.node.path.name.startswith('test_trakt')
            or request.node.name.startswith(('test_trakt', 'test_run_trakt'))):
        return
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(tmp_path / 'contained-profile'))
    import os
    import socket
    import subprocess
    def forbidden(*args, **kwargs):
        pytest.fail('Trakt test attempted uncontained child/auth/network')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(os, 'system', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    from pathlib import Path
    from fulcra_collect import config, trakt_source, db, config_leases, quick_record_favorites
    from fulcra_media import collect_twins
    def private(path):
        resolved = Path(path).absolute()
        if not resolved.is_relative_to(tmp_path.absolute()):
            pytest.fail('Trakt test attempted default-root/config/legacy-file access')
        return path
    original_profile = config.profile_path
    for name in ('config_dir', 'profile_path', '_config_path'):
        original = getattr(config, name)
        def checked(*args, _original=original, **kwargs):
            # profile_path is pure; reject before config_dir mkdir/chmod.
            private(original_profile())
            return private(_original(*args, **kwargs))
        monkeypatch.setattr(config, name, checked)
    # Modules importing config_dir by value must receive this run's guard,
    # rather than retaining a prior fixture closure after first import.
    for module in (db, config_leases, quick_record_favorites, collect_twins):
        monkeypatch.setattr(module, 'config_dir', config.config_dir)
    original_owner = trakt_source.Owner.__init__
    def owner_init(self, store, *, _root=None):
        private(_root if _root is not None else trakt_source._os_user_root())
        original_owner(self, store, _root=_root)
    monkeypatch.setattr(trakt_source.Owner, '__init__', owner_init)
    original_file = trakt_source.FileStore.__init__
    def file_init(self, path):
        private(path)
        original_file(self, path)
    monkeypatch.setattr(trakt_source.FileStore, '__init__', file_init)
    from fulcra_common.client import BaseFulcraClient
    monkeypatch.setattr(BaseFulcraClient, 'get_token', lambda _: os.environ['FULCRA_ACCESS_TOKEN'])

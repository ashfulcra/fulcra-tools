"""Startup options through real serve(), with every startup effect faked."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("options,expected_menu", [
    ({}, ["menubar"]),
    ({"launch_menubar": False}, []),
], ids=["default", "no-menubar"])
def test_serve_menubar_option_preserves_other_startup_and_shutdown(
    collect_home, monkeypatch, options, expected_menu,
):
    """Catch unconditional or scheduled menu launch, or skipped core startup."""
    def forbidden(*args, **kwargs):
        raise AssertionError("external startup dependency reached")

    # Fence native/auth/network/child paths before loading startup dependencies.
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    import keyring
    for name in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(keyring, name, forbidden)
    from fulcra_collect import credentials
    for name in ("get_user_secret", "get_user_secret_fresh", "get_secret", "set_secret"):
        monkeypatch.setattr(credentials, name, forbidden)
    monkeypatch.setattr(credentials, "_uses_macos_keychain", forbidden)

    effects = []
    menu = SimpleNamespace(try_launch_menubar=lambda: effects.append("menubar"))
    web = SimpleNamespace(serve=lambda daemon: (effects.append("web") or
                                               "http://synthetic.invalid", None))
    monkeypatch.setitem(sys.modules, "fulcra_collect.menubar_launcher", menu)
    monkeypatch.setitem(sys.modules, "fulcra_collect.web", web)
    import fulcra_collect
    monkeypatch.setattr(fulcra_collect, "menubar_launcher", menu, raising=False)
    monkeypatch.setattr(fulcra_collect, "web", web, raising=False)
    from fulcra_collect import daemon as dm, supervisor
    from fulcra_collect.config import Config
    from fulcra_collect.registry import RegistryResult

    def lease(profile):
        assert profile == collect_home.resolve()
        effects.append("lease")
        return object()

    class FakeControl:
        def __init__(self, path, handler):
            assert path == collect_home / "control.sock"

        def serve_forever(self):
            effects.append("control")

        def shutdown(self):
            effects.append("control shutdown")

    class ImmediateThread:
        def __init__(self, *, target, daemon):
            assert daemon is True
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(dm, "acquire_daemon_lease", lease)
    monkeypatch.setattr(dm._db, "open", lambda: effects.append("migration"))
    monkeypatch.setattr(dm, "ControlServer", FakeControl)
    monkeypatch.setattr(dm.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(dm.Daemon, "_check_account_fingerprint",
                        lambda self: effects.append("auth preflight"))
    monkeypatch.setattr(supervisor, "ServiceSupervisor", lambda: SimpleNamespace(
        shutdown_all=lambda: effects.append("workers shutdown")))
    monkeypatch.setattr(dm.time, "sleep", lambda seconds: effects.append("tick wait"))
    daemon = dm.Daemon(registry=RegistryResult(), config=Config(), **options)

    class StopServe(Exception):
        pass

    def dispatch(*args, **kwargs):
        effects.append("dispatch")
        if effects.count("dispatch") == 2:
            raise StopServe()

    monkeypatch.setattr(daemon, "_dispatch_tick", dispatch)
    with pytest.raises(StopServe):
        daemon.serve(tick_seconds=0)

    assert effects == [
        "lease", "migration", "control", "auth preflight", "web",
        *expected_menu, "dispatch", "tick wait", "dispatch",
        "control shutdown", "workers shutdown",
    ]

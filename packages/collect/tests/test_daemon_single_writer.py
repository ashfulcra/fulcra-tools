"""Actual serve regressions: duplicate starts must not replace a live endpoint.

All production startup dependencies except the real control UDS are fenced.
Imports of Collect happen after the synthetic profile and fences are installed.
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import types

import pytest


class StopServe(Exception):
    pass


@pytest.fixture
def fenced_daemon(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(home))
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "synthetic-test-token")

    def forbidden(*args, **kwargs):
        raise AssertionError("external startup dependency reached")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    import keyring
    for name in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(keyring, name, forbidden)
    import fulcra_collect.credentials as credentials
    for name in ("get_user_secret", "get_user_secret_fresh", "get_secret", "set_secret"):
        monkeypatch.setattr(credentials, name, forbidden)
    monkeypatch.setattr(credentials, "_uses_macos_keychain", forbidden)
    # No web/auth/native modules need to load in these startup tests.
    effects = []
    servers = []
    fingerprints = {}
    monkeypatch.setitem(sys.modules, "fulcra_collect.web", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "fulcra_collect.menubar_launcher", types.SimpleNamespace(
        try_launch_menubar=lambda: effects.append("menubar")))
    import fulcra_collect
    monkeypatch.setattr(fulcra_collect, "menubar_launcher",
                        sys.modules["fulcra_collect.menubar_launcher"], raising=False)
    from fulcra_collect import daemon as dm
    from fulcra_collect import control, supervisor
    from fulcra_collect.config import Config
    from fulcra_collect.registry import RegistryResult
    monkeypatch.setattr(dm, "discover", forbidden)
    monkeypatch.setattr(dm.config_mod, "load", forbidden)
    monkeypatch.setattr(dm._db, "open", lambda: effects.append("migration"))
    def fingerprint(daemon):
        effects.append("fingerprint")
        fingerprints[id(daemon)].set()
    monkeypatch.setattr(dm.Daemon, "_check_account_fingerprint", fingerprint)

    class OwnedControl(control.ControlServer):
        def __init__(self, path, handler):
            super().__init__(path, handler)
            self.thread = None
            servers.append(self)

        def serve_forever(self):
            self.thread = threading.current_thread()
            super().serve_forever()

    monkeypatch.setattr(dm, "ControlServer", OwnedControl)

    def web(daemon):
        servers[-1].wait_ready(3)
        assert fingerprints[id(daemon)].wait(3)
        effects.append("web")
        return "http://synthetic.invalid", None

    monkeypatch.setattr(sys.modules["fulcra_collect.web"], "serve", web, raising=False)
    monkeypatch.setattr(supervisor, "ServiceSupervisor", lambda: types.SimpleNamespace(
        shutdown_all=lambda: None))
    threads = []
    daemons = []
    stops = []

    def make(label):
        daemon = dm.Daemon(registry=RegistryResult(), config=Config())
        daemons.append(daemon)
        fingerprints[id(daemon)] = threading.Event()
        daemon.handle_request = lambda request: {"owner": label}
        entered = threading.Event()
        stop = threading.Event()
        stops.append(stop)
        errors = []

        def tick(*args, **kwargs):
            effects.append("dispatch:" + label)
            entered.set()
            if not stop.wait(5):
                raise AssertionError("serve test barrier timed out")
            raise StopServe()

        daemon._dispatch_tick = tick

        def run():
            try:
                daemon.serve(tick_seconds=0)
            except StopServe:
                pass
            except BaseException as exc:
                errors.append(exc)
                entered.set()

        thread = threading.Thread(target=run)
        threads.append(thread)
        return daemon, thread, entered, errors

    yield types.SimpleNamespace(home=home, effects=effects, servers=servers,
                                make=make, dm=dm, control=control, stops=stops)
    for stop in stops:
        stop.set()
    for thread in threads:
        if thread.ident is not None:
            thread.join(6)
            assert not thread.is_alive(), "owned serve thread did not stop"
    for server in servers:
        server.shutdown()
        if server.thread:
            server.thread.join(3)
            assert not server.thread.is_alive(), "owned control thread did not stop"
    # Production intentionally has no lifetime release; isolated tests own cleanup.
    for daemon in daemons:
        if hasattr(daemon, "_daemon_lease"):
            os.close(daemon._daemon_lease.fd)


@pytest.mark.parametrize("long_path", [False, True], ids=["short", "long-symlink"])
def test_duplicate_daemon_preserves_socket_and_refuses_effects(fenced_daemon, monkeypatch, long_path):
    f = fenced_daemon
    # Ensure both actual short and symlink branches without the production temp dir.
    with tempfile.TemporaryDirectory(prefix="fc-lease-", dir="/tmp") as short:
        endpoint = Path(short) / "control.sock"
        requested = f.home / ("x" * 110) if long_path else endpoint
        monkeypatch.setattr(f.dm, "_control_socket_path", lambda: requested)
        monkeypatch.setattr(f.control, "_short_bind_path", lambda path: endpoint)
        first, t1, ready1, errors1 = f.make("first")
        second, t2, ready2, errors2 = f.make("second")
        t1.start()
        assert ready1.wait(4) and not errors1
        assert f.control.send_request(requested, {}, timeout=1) == {"owner": "first"}
        before = (requested.lstat().st_ino, endpoint.stat().st_ino, requested.resolve())
        t2.start()
        assert ready2.wait(4), "second serve neither refused nor dispatched"
        observed = {
            "socket_unchanged": before == (requested.lstat().st_ino, endpoint.stat().st_ino, requested.resolve()),
            "reply": f.control.send_request(requested, {}, timeout=1),
            "migrations": f.effects.count("migration"),
            "fingerprint": f.effects.count("fingerprint"),
            "web": f.effects.count("web"),
            "menubar": f.effects.count("menubar"),
            "second_dispatch": f.effects.count("dispatch:second"),
            "refusal": [type(e).__name__ for e in errors2],
        }
        assert observed == {
            "socket_unchanged": True, "reply": {"owner": "first"},
            "migrations": 1, "fingerprint": 1, "web": 1, "menubar": 1, "second_dispatch": 0,
            "refusal": ["DaemonAlreadyRunning"],
        }


def test_lease_survives_serve_unwind_and_same_object_reentry(fenced_daemon, monkeypatch):
    f = fenced_daemon
    first, _, _, _ = f.make("first")
    def failed_migration():
        f.effects.append("migration-failed")
        raise StopServe()
    monkeypatch.setattr(f.dm._db, "open", failed_migration)
    with pytest.raises(StopServe):
        first.serve()
    from fulcra_collect.daemon_lease import DaemonAlreadyRunning
    with pytest.raises(DaemonAlreadyRunning):
        first.serve()
    second, _, _, _ = f.make("second")
    with pytest.raises(DaemonAlreadyRunning):
        second.serve()
    assert f.effects == ["migration-failed"]
    assert not f.servers


def test_unavailable_lease_prevents_all_serve_effects(fenced_daemon):
    f = fenced_daemon
    lock = f.home / "daemon.lock"
    lock.write_bytes(b"preserve")
    lock.chmod(0o644)
    daemon, _, _, _ = f.make("first")
    from fulcra_collect.daemon_lease import DaemonLeaseUnavailable
    with pytest.raises(DaemonLeaseUnavailable):
        daemon.serve()
    assert not f.effects
    assert not f.servers
    assert lock.read_bytes() == b"preserve"
    assert lock.stat().st_mode & 0o777 == 0o644


def test_changed_effective_profile_refuses_before_effects(fenced_daemon, monkeypatch, tmp_path):
    f = fenced_daemon
    daemon, _, _, _ = f.make("first")
    other = tmp_path / "new-profile"
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(other))
    from fulcra_collect.daemon_lease import DaemonLeaseUnavailable
    with pytest.raises(DaemonLeaseUnavailable):
        daemon.serve()
    assert not f.effects
    assert not f.servers
    assert not other.exists()
    assert not (f.home / "daemon.lock").exists()


def test_lease_stays_held_after_shutdown_cleanup(fenced_daemon, monkeypatch):
    f = fenced_daemon
    with tempfile.TemporaryDirectory(prefix="fc-unwind-", dir="/tmp") as short:
        requested = Path(short) / "control.sock"
        monkeypatch.setattr(f.dm, "_control_socket_path", lambda: requested)
        first, thread, ready, errors = f.make("first")
        thread.start()
        assert ready.wait(4) and not errors
        f.stops[0].set()
        thread.join(3)
        assert not thread.is_alive()
        f.servers[0].thread.join(3)
        assert not f.servers[0].thread.is_alive()
        assert not requested.exists()
        before = f.effects[:]
        second, _, _, _ = f.make("second")
        from fulcra_collect.daemon_lease import DaemonAlreadyRunning
        with pytest.raises(DaemonAlreadyRunning):
            second.serve()
        assert f.effects == before


def test_profile_alias_uses_same_effective_lease(fenced_daemon, monkeypatch, tmp_path):
    f = fenced_daemon
    first, _, _, _ = f.make("first")
    monkeypatch.setattr(f.dm._db, "open", lambda: (_ for _ in ()).throw(StopServe()))
    with pytest.raises(StopServe):
        first.serve()
    alias = tmp_path / "alias"
    alias.symlink_to(f.home, target_is_directory=True)
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(alias))
    second, _, _, _ = f.make("second")
    from fulcra_collect.daemon_lease import DaemonAlreadyRunning
    with pytest.raises(DaemonAlreadyRunning):
        second.serve()
    assert not f.effects
    assert not f.servers

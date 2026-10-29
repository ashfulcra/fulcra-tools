"""Owned service exits must reach cached status without any credential read."""
from datetime import datetime, timedelta, timezone
from io import StringIO
import os
import socket
import subprocess

import httpx
import pytest


T0 = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
# Reference only: no client construction/credentials at import. The route test
# restores this method solely for its injected, in-process ASGI transport.
_HTTPX_SEND = httpx.Client.send


class Process:
    def __init__(self):
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True


@pytest.fixture
def service(tmp_path, monkeypatch):
    # Bind the private profile before mkdir, leases, DB or application imports.
    root = tmp_path / "private-collect"
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(root))

    def forbidden(*args, **kwargs):
        pytest.fail("service status attempted an external effect")

    for name in ("Popen", "run", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    import httpx
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    from fulcra_collect import config, credentials, db
    monkeypatch.setattr(db, "config_dir", lambda: root)
    for name in ("get_user_secret", "get_user_secret_fresh", "get_secret",
                 "get_plugin_credentials", "_keyring_get", "refresh_fulcra_access_token"):
        monkeypatch.setattr(credentials, name, forbidden)
    for name in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(credentials.keyring, name, forbidden)
    from fulcra_collect import macos_keychain
    monkeypatch.setattr(macos_keychain, "_read_password", forbidden)
    monkeypatch.setattr(macos_keychain, "get_password", forbidden)
    monkeypatch.setattr(macos_keychain, "_Security", forbidden)
    monkeypatch.setattr(macos_keychain, "_api", forbidden)
    from fulcra_common import BaseFulcraClient
    monkeypatch.setattr(BaseFulcraClient, "__init__", forbidden)
    from fulcra_collect import daemon as daemon_mod, worker
    from fulcra_collect.daemon import Daemon
    from fulcra_collect.plugin import Plugin
    from fulcra_collect.registry import RegistryResult
    from fulcra_collect.supervisor import ServiceSupervisor
    monkeypatch.setattr(worker, "_make_fulcra_definition_client", forbidden)
    monkeypatch.setattr(Daemon, "_build_version_snapshot", lambda self: {})
    monkeypatch.setattr(daemon_mod, "is_online", lambda: False)
    root.mkdir()
    db.close_all()
    cfg = config.Config(enabled={"relay"})
    config.save(cfg)
    plugin = Plugin(id="relay", name="Synthetic receiver", kind="service",
                    collect_mode="live_continuous", run=forbidden)
    daemon = Daemon(registry=RegistryResult(plugins={"relay": plugin}), config=cfg)
    supervisor = ServiceSupervisor()
    procs = []

    def spawn(plugin_id):
        proc = Process()
        procs.append(proc)
        return proc

    monkeypatch.setattr(daemon, "_spawn_service", spawn)
    yield daemon, supervisor, procs, plugin
    db.close_all()


def _tick(service, seconds=0):
    daemon, supervisor, *_ = service
    daemon._dispatch_tick(supervisor, now=T0 + timedelta(seconds=seconds))


def _status(service):
    return service[0]._status()["plugins"][0]


def test_actual_worker_error_exit_is_cached_once_through_normal_supervision(service, monkeypatch):
    from fulcra_collect import credentials, state, worker
    from fulcra_collect.registry import RegistryResult
    daemon, supervisor, procs, plugin = service

    def unavailable(plugin):
        raise credentials.KeychainAccessRequired("synthetic only")

    # The real worker's handled error returns process exit 1 before plugin.run.
    monkeypatch.setattr(credentials, "get_plugin_credentials", unavailable)
    out = StringIO()
    monkeypatch.setattr(worker.sys, "stdout", out)
    rc = worker.main(["relay"], registry=RegistryResult(plugins={"relay": plugin}))
    assert rc == 1
    assert '"outcome": "error"' in out.getvalue()
    _tick(service)
    procs[0].returncode = rc
    _tick(service, 1)
    row = _status(service)
    assert row["last_outcome"] == "error"
    assert row["consecutive_failures"] == 1
    assert row["last_run"] == (T0 + timedelta(seconds=1)).isoformat()
    assert "exit status 1" in row["last_error"]
    assert "Keychain" not in row["last_error"]  # exit status proves no cause
    assert supervisor._backoff_until["relay"] == T0 + timedelta(seconds=2)
    _tick(service, 1)
    _tick(service, 2)
    _tick(service, 3)
    assert len(procs) == 2
    assert state.load("relay").consecutive_failures == 1
    assert _status(service)["last_outcome"] == "error"


@pytest.mark.parametrize("change", ["disable", "settings", "epoch", "account", "profile"])
def test_exit_observer_rechecks_current_scope_before_writing(service, monkeypatch, change, tmp_path):
    from fulcra_collect import config, state
    daemon, supervisor, procs, _ = service
    _tick(service)
    procs[0].returncode = 1
    original = daemon._record_service_exit

    def race(observation):
        if change == "profile":
            monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path / "other-profile"))
        else:
            current = config.load()
            if change == "disable":
                current.disable("relay")
            elif change == "settings":
                # A direct TOML edit changes settings without rotating epoch.
                import tomlkit
                path = config._config_path()
                doc = tomlkit.parse(path.read_text())
                doc["plugin_settings"] = {"relay": {"port": 9999}}
                path.write_text(tomlkit.dumps(doc))
                return original(observation)
            else:
                current.rotate_plugin_epoch("relay")
            config.save(current)
            if change == "account":
                config._persist_account_transition(config._config_path(), True)
        original(observation)

    monkeypatch.setattr(daemon, "_record_service_exit", race)
    old_profile = os.environ["FULCRA_COLLECT_HOME"]
    _tick(service, 1)
    monkeypatch.setenv("FULCRA_COLLECT_HOME", old_profile)
    assert state.load("relay").consecutive_failures == 0


@pytest.mark.parametrize("failure", ["observer", "db", "commit_then_raise", "lease", "config"])
def test_uncertain_failure_stays_visible_without_repeated_writes(service, monkeypatch, caplog, failure):
    from fulcra_collect import config, config_leases, state
    daemon, supervisor, procs, _ = service
    _tick(service)
    procs[0].returncode = 1
    attempts = []
    original_record = state.record_service_failure

    def broken(*args, **kwargs):
        attempts.append(1)
        if failure == "commit_then_raise":
            original_record(*args, **kwargs)
        raise RuntimeError("secret-value-must-not-appear")

    if failure in {"db", "commit_then_raise"}:
        monkeypatch.setattr(state, "record_service_failure", broken)
    elif failure == "observer":
        monkeypatch.setattr(daemon, "_record_service_exit", broken)
    else:
        original = daemon._record_service_exit

        def raced(observation):
            with monkeypatch.context() as scoped:
                if failure == "lease":
                    scoped.setattr(config_leases, "task_mutation_scope", broken)
                else:
                    scoped.setattr(config, "load", broken)
                original(observation)

        monkeypatch.setattr(daemon, "_record_service_exit", raced)
    _tick(service, 1)
    assert supervisor._backoff_until["relay"] == T0 + timedelta(seconds=2)
    for _ in range(3):
        row = _status(service)
        assert row["last_outcome"] == "error"
        assert row["service_status"] == "unknown"
        assert "UNKNOWN" in row["last_error"]
        assert "secret-value" not in str(row)
    _tick(service, 1)
    _tick(service, 2)  # a fresh launch is not evidence of recovered status
    assert len(procs) == 2
    assert _status(service)["service_status"] == "unknown"
    assert attempts == [1]
    assert state.load("relay").consecutive_failures == (1 if failure == "commit_then_raise" else 0)
    assert "secret-value" not in caplog.text


def test_unknown_scope_or_db_status_masks_old_account_fields(service, monkeypatch):
    from fulcra_collect import config, state
    daemon, supervisor, procs, _ = service
    st = state.PluginState("relay", definition_id="old-account-definition", watermark="old-watermark")
    st.record_finish(outcome="done", when=T0)
    state.save(st)
    _tick(service)
    procs[0].returncode = 1
    monkeypatch.setattr(state, "record_service_failure", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    _tick(service, 1)
    for target, name in ((config, "load"), (state, "load")):
        with monkeypatch.context() as scoped:
            scoped.setattr(target, name, lambda *a: (_ for _ in ()).throw(RuntimeError("private")))
            row = _status(service)
            assert row["service_status"] == "unknown"
            assert row["definition_id"] is None
            assert row["last_run"] is None
            assert "old-account" not in str(row)


@pytest.mark.parametrize("unknown", [False, True])
def test_status_api_exposes_owned_service_failure_without_auth_reads(service, monkeypatch, unknown):
    from fulcra_collect import state
    daemon, supervisor, procs, _ = service
    _tick(service)
    procs[0].returncode = -9
    if unknown:
        monkeypatch.setattr(state, "record_service_failure", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    _tick(service, 1)
    response = _api_status(service, monkeypatch)
    assert response.status_code == 200
    row = response.json()["plugins"][0]
    assert row["last_outcome"] == "error"
    assert ("UNKNOWN" if unknown else "exit status -9") in row["last_error"]


def _api_status(service, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from fulcra_collect.routes.status import register
    from fulcra_collect.routes._deps import RouteContext
    # Register the real /api/status handler; injected local authorization does
    # not exercise unrelated app bootstrap or bearer credential machinery.
    app = FastAPI()
    def forbidden(*args, **kwargs):
        pytest.fail("status route requested a credential or provider")

    register(app, RouteContext(daemon=service[0], require_token=lambda: None,
                             require_plugin=forbidden, fulcra_token_or_401=forbidden,
                             fulcra_http_client=forbidden))
    # TestClient uses ASGI, not the HTTP send seam blocked by our tripwire.
    import httpx
    with monkeypatch.context() as scoped:
        scoped.setattr(httpx.Client, "send", _HTTPX_SEND)
        response = TestClient(app).get("/api/status")
    return response


def test_atomic_failure_update_preserves_state_and_concurrent_unrelated_change(service, monkeypatch):
    from fulcra_collect import db, state
    st = state.PluginState("relay", watermark="cursor", definition_id="definition", consecutive_failures=2,
                           override_definition_name="name", definition_validated_at="validation",
                           last_yield_at=T0, newest_item_at=T0.isoformat())
    state.save(st)
    original = db.record_service_failure

    def concurrent(conn, **kwargs):
        import sqlite3
        other = sqlite3.connect(db.default_path())
        try:
            other.execute("UPDATE plugin_state SET watermark = ? WHERE plugin_id = ?", ("concurrent", "relay"))
            other.commit()
        finally:
            other.close()
        original(conn, **kwargs)

    monkeypatch.setattr(db, "record_service_failure", concurrent)
    state.record_service_failure("relay", when=T0, error="static failure")
    saved = state.load("relay")
    assert saved.consecutive_failures == 3
    assert saved.watermark == "concurrent"
    assert saved.definition_id == "definition"
    assert saved.override_definition_name == "name"
    assert saved.definition_validated_at == "validation"
    assert saved.last_yield_at == T0
    assert saved.newest_item_at == T0.isoformat()


@pytest.mark.parametrize("mode", ["nonzero", "clean", "disable", "revision", "shutdown"])
def test_supervisor_observes_only_owned_unexpected_nonzero_exit(service, mode):
    _, sup, *_ = service
    process = Process()
    observations = []
    sup.tick(now=T0, enabled_ids={"relay"}, revisions={"relay": "original"},
             spawn=lambda _: process, on_exit=observations.append)
    process.returncode = 0 if mode == "clean" else 1
    if mode == "shutdown":
        sup.shutdown_all()
    sup.tick(now=T0, enabled_ids=set() if mode in {"disable", "shutdown"} else {"relay"},
             revisions={"relay": "new" if mode == "revision" else "original"},
             spawn=lambda _: Process(), on_exit=observations.append)
    if mode == "nonzero":
        assert len(observations) == 1
        observed = observations[0]
        assert observed.process is process
        assert observed.plugin_id == "relay"
        assert observed.revision == "original"
        assert observed.observed_at == T0
        assert observed.returncode == 1
        sup.tick(now=T0, enabled_ids={"relay"}, revisions={"relay": "original"},
                 spawn=lambda _: Process(), on_exit=observations.append)
        assert len(observations) == 1
    else:
        assert observations == []


def test_launch_oserror_uses_existing_path_without_exit_observer_double_count(service, monkeypatch):
    from fulcra_collect import state
    daemon, supervisor, *_ = service

    def fail(_):
        raise OSError("synthetic failure")

    monkeypatch.setattr(daemon, "_spawn_service", fail)
    _tick(service)
    _tick(service)
    assert state.load("relay").consecutive_failures == 1
    assert supervisor.unknown_exits == {}


def test_unknown_observation_does_not_stop_supervision_when_db_unavailable(service, monkeypatch):
    from fulcra_collect import db
    daemon, supervisor, procs, _ = service
    _tick(service)
    procs[0].returncode = 1

    def unavailable(*args, **kwargs):
        raise RuntimeError("private-db-text")

    monkeypatch.setattr(db, "open", unavailable)
    _tick(service, 1)
    _tick(service, 2)
    assert len(procs) == 2
    assert _status(service)["service_status"] == "unknown"


def test_db_outage_returns_pending_service_unknown_even_with_other_plugin_rows(service, monkeypatch):
    from fulcra_collect import db
    from fulcra_collect.plugin import Plugin
    daemon, supervisor, procs, _ = service
    daemon.registry.plugins["aaa"] = Plugin(id="aaa", name="Other source", kind="manual",
                                           collect_mode="historical", run=lambda _: None)
    _tick(service)
    procs[0].returncode = 1

    def unavailable(*args, **kwargs):
        raise RuntimeError("private-db-text")

    monkeypatch.setattr(db, "open", unavailable)
    _tick(service, 1)
    response = _api_status(service, monkeypatch)
    assert response.status_code == 200
    rows = response.json()["plugins"]
    assert [row["id"] for row in rows] == ["aaa", "relay"]
    assert rows[1]["service_status"] == "unknown"
    assert all(row["last_outcome"] == "error" for row in rows)
    assert all(row["definition_id"] is None for row in rows)
    assert "private-db-text" not in str(rows)


def test_status_preserves_normal_db_error_behavior_without_pending_observations(service, monkeypatch):
    from fulcra_collect import db

    def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic-db-error")

    monkeypatch.setattr(db, "open", unavailable)
    with pytest.raises(RuntimeError, match="synthetic-db-error"):
        service[0]._status()


def test_unknown_survives_a_new_confirmed_failure_in_the_same_revision(service, monkeypatch):
    from fulcra_collect import state
    daemon, supervisor, procs, _ = service
    _tick(service)
    procs[0].returncode = 1
    with monkeypatch.context() as scoped:
        scoped.setattr(state, "record_service_failure", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
        _tick(service, 1)
    _tick(service, 2)
    procs[1].returncode = 1
    _tick(service, 3)
    assert state.load("relay").consecutive_failures == 1
    assert _status(service)["service_status"] == "unknown"


@pytest.mark.parametrize("change", ["settings", "disable", "account"])
def test_positive_scope_retirement_suppresses_old_unknown_and_account_metadata(service, monkeypatch, change):
    from fulcra_collect import config, state
    daemon, supervisor, procs, _ = service
    state.save(state.PluginState("relay", definition_id="private-old-account"))
    _tick(service)
    procs[0].returncode = 1
    monkeypatch.setattr(state, "record_service_failure", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    _tick(service, 1)
    if change == "account":
        config._persist_account_transition(config._config_path(), True)
    else:
        current = config.load()
        if change == "disable":
            current.disable("relay")
        else:
            current.update_plugin_settings("relay", {"port": 9999})
        config.save(current)
    row = _status(service)
    assert "service_status" not in row
    assert row["last_outcome"] is None
    assert row["definition_id"] is None
    assert "private-old-account" not in str(row)


def test_new_revision_oserror_replaces_stale_unknown_using_only_current_static_status(service, monkeypatch):
    from fulcra_collect import config, state
    daemon, supervisor, procs, _ = service
    state.save(state.PluginState("relay", definition_id="old-account-definition", consecutive_failures=7))
    _tick(service)
    procs[0].returncode = 1
    with monkeypatch.context() as scoped:
        scoped.setattr(state, "record_service_failure", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
        _tick(service, 1)
    current = config.load()
    current.update_plugin_settings("relay", {"port": 1234})
    config.save(current)

    def fail(_):
        raise OSError("private-launch-error")

    monkeypatch.setattr(daemon, "_spawn_service", fail)
    _tick(service, 2)
    for _ in range(3):
        response = _api_status(service, monkeypatch)
        assert response.status_code == 200
        row = response.json()["plugins"][0]
        assert row["last_outcome"] == "error"
        assert "service_status" not in row
        assert "launch failed" in row["last_error"]
        assert row["last_run"] == (T0 + timedelta(seconds=2)).isoformat()
        assert row["consecutive_failures"] == 1  # locally known current revision
        assert row["definition_id"] is None
        assert "private-launch-error" not in str(row)
    assert state.load("relay").consecutive_failures == 8
    assert state.load("relay").definition_id == "old-account-definition"


@pytest.mark.parametrize("failure", ["observer", "db", "commit_then_raise", "lease", "config"])
def test_oserror_observation_unknown_is_visible_and_never_retried(service, monkeypatch, caplog, failure):
    from fulcra_collect import config, config_leases, state
    daemon, supervisor, *_ = service
    original = state.record_service_failure
    attempts = []

    def broken(*args, **kwargs):
        attempts.append(1)
        if failure == "commit_then_raise":
            original(*args, **kwargs)
        raise RuntimeError("private-observer-error")

    monkeypatch.setattr(daemon, "_spawn_service", lambda _: (_ for _ in ()).throw(OSError("private-oserror")))
    if failure == "observer":
        monkeypatch.setattr(daemon, "_record_service_launch_failure", broken)
    elif failure in {"db", "commit_then_raise"}:
        monkeypatch.setattr(state, "record_service_failure", broken)
    else:
        original_observer = daemon._record_service_launch_failure

        def raced(*args, **kwargs):
            with monkeypatch.context() as scoped:
                scoped.setattr(config_leases if failure == "lease" else config,
                               "task_mutation_scope" if failure == "lease" else "load", broken)
                return original_observer(*args, **kwargs)

        monkeypatch.setattr(daemon, "_record_service_launch_failure", raced)
    _tick(service)
    _tick(service)
    assert _status(service)["service_status"] == "unknown"
    assert "UNKNOWN" in _status(service)["last_error"]
    assert attempts == [1]
    assert "private-" not in caplog.text


@pytest.mark.parametrize("change", ["epoch", "account", "disable", "profile"])
def test_oserror_callback_does_not_recapture_new_scope(service, monkeypatch, tmp_path, change):
    from fulcra_collect import config, state
    daemon, supervisor, *_ = service
    original = daemon._record_service_launch_failure
    old_profile = os.environ["FULCRA_COLLECT_HOME"]

    def raced(plugin_id, exc, **kwargs):
        if change == "profile":
            monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path / "other-profile"))
        elif change == "account":
            config._persist_account_transition(config._config_path(), True)
        else:
            current = config.load()
            current.rotate_plugin_epoch("relay")
            if change == "disable":
                current.disable("relay")
            config.save(current)
        return original(plugin_id, exc, **kwargs)

    monkeypatch.setattr(daemon, "_spawn_service", lambda _: (_ for _ in ()).throw(OSError("synthetic")))
    monkeypatch.setattr(daemon, "_record_service_launch_failure", raced)
    _tick(service)
    monkeypatch.setenv("FULCRA_COLLECT_HOME", old_profile)
    assert state.load("relay").consecutive_failures == 0

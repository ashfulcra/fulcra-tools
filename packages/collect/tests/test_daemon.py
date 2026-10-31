"""The daemon request handler + status snapshot."""
from __future__ import annotations

import os
import socket
from datetime import datetime, timezone
from pathlib import Path

from fulcra_collect.config import Config
from fulcra_collect.daemon import Daemon
from fulcra_collect.plugin import Plugin
from fulcra_collect.registry import RegistryResult
from fulcra_collect import state
import fulcra_collect.daemon as daemon_module


def _registry() -> RegistryResult:
    r = RegistryResult()
    r.plugins["lastfm"] = Plugin(id="lastfm", name="Last.fm", kind="scheduled",
                                 collect_mode="live_polled",
                                 run=lambda c: None,
                                 default_interval=__import__("datetime").timedelta(hours=1))
    r.plugins["dayone"] = Plugin(id="dayone", name="Day One", kind="manual",
                                 collect_mode="historical",
                                 run=lambda c: None)
    r.errors["brokenplugin"] = "ImportError: bad"
    return r


def test_construction_never_reads_the_keychain(collect_home: Path, monkeypatch):
    """Daemon.__init__ must not read the OS keychain.

    On macOS a keychain read can block indefinitely on an ACL-confirmation
    dialog (e.g. after the daemon binary's identity changes, or under a
    launchd/remote session where the dialog can't be answered). When the
    account-fingerprint pre-flight ran that read in __init__, a single
    blocked keychain access wedged the daemon before it bound its control
    socket — the menubar then showed "daemon not reachable" with no
    recourse. The pre-flight now runs in serve() on a background thread
    AFTER the sockets are up; construction must stay keychain-free.
    """
    import fulcra_collect.credentials as creds

    def _boom(key):  # any keychain read during construction is the bug
        raise AssertionError(f"keychain read during Daemon construction: {key!r}")

    monkeypatch.setattr(creds, "get_user_secret", _boom)
    # Must not raise — i.e. construction touches no keychain item.
    Daemon(registry=_registry(), config=Config())


def test_service_launch_failure_becomes_state_and_activity(collect_home: Path):
    from fulcra_collect import config
    registry = _registry()
    registry.plugins["relay"] = Plugin(id="relay", name="Synthetic receiver", kind="service",
                                       collect_mode="live_continuous", run=lambda _: None)
    cfg = Config(enabled={"relay"})
    config.save(cfg)
    daemon = Daemon(registry=registry, config=cfg)
    when = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)

    daemon._record_service_launch_failure(
        "relay",
        OSError("Authorization: Bearer should-not-survive"),
        when=when,
        revision=daemon._service_revision(config.load(), "relay"),
    )

    saved = state.load("relay")
    assert saved.last_outcome == "error"
    assert saved.last_run == when
    assert "service worker launch failed" in saved.last_error
    assert "should-not-survive" not in saved.last_error
    entry = daemon.activity.recent(limit=1)[0]
    assert entry.plugin_id == "relay"
    assert entry.ok is False
    assert "service worker launch failed" in entry.summary
    assert "should-not-survive" not in entry.summary


def test_service_workers_start_in_their_own_process_group(
    collect_home: Path, monkeypatch,
):
    captured = {}
    daemon = Daemon(registry=_registry(), config=Config())

    def popen(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return object()

    monkeypatch.setattr("fulcra_collect.daemon.subprocess.Popen", popen)

    daemon._spawn_service("relay")

    assert captured["command"][-2:] == ["_worker", "relay"]
    assert captured["kwargs"]["start_new_session"] is (os.name == "posix")


def test_online_probe_uses_the_fulcra_https_endpoint_not_public_dns(
    monkeypatch,
):
    attempted = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def connect(address, *, timeout):
        attempted.append((address, timeout))
        return Connection()

    monkeypatch.setattr(
        "fulcra_common.DEFAULT_BASE_URL", "https://fulcra.example:8443/api",
    )
    monkeypatch.setattr(socket, "create_connection", connect)

    assert daemon_module.is_online(timeout=0.25) is True
    assert attempted == [(('fulcra.example', 8443), 0.25)]


def test_online_probe_falls_back_to_a_normal_https_host(monkeypatch):
    attempted = []

    def connect(address, *, timeout):
        attempted.append(address)
        if len(attempted) == 1:
            raise OSError("primary unavailable")
        return type("Connection", (), {
            "__enter__": lambda self: self,
            "__exit__": lambda self, *exc: False,
        })()

    monkeypatch.setattr(
        "fulcra_common.DEFAULT_BASE_URL", "https://fulcra.example/api",
    )
    monkeypatch.setattr(socket, "create_connection", connect)

    assert daemon_module.is_online(timeout=0.25) is True
    assert attempted[0] == ("fulcra.example", 443)
    assert all(port == 443 for _host, port in attempted)


def test_status_lists_every_plugin_with_enabled_flag(collect_home: Path):
    d = Daemon(registry=_registry(), config=Config(enabled={"lastfm"}))
    reply = d.handle_request({"cmd": "status"})
    assert reply["ok"] is True
    by_id = {p["id"]: p for p in reply["plugins"]}
    assert by_id["lastfm"]["enabled"] is True
    assert by_id["dayone"]["enabled"] is False
    assert by_id["lastfm"]["kind"] == "scheduled"


def test_status_includes_collect_mode(collect_home: Path):
    """SP3 task 2: status reply must propagate each plugin's collect_mode
    ('historical' vs. 'live_polled') so the menubar can group the popover
    and render a per-row chip in Preferences. Both the HTTP /api/status
    route and the UDS `status` command share this code path via
    Daemon._status()."""
    d = Daemon(registry=_registry(), config=Config())
    reply = d.handle_request({"cmd": "status"})
    by_id = {p["id"]: p for p in reply["plugins"]}
    assert by_id["lastfm"]["collect_mode"] == "live_polled"
    assert by_id["dayone"]["collect_mode"] == "historical"


def test_status_reports_registry_load_errors(collect_home: Path):
    d = Daemon(registry=_registry(), config=Config())
    reply = d.handle_request({"cmd": "status"})
    assert reply["load_errors"] == {"brokenplugin": "ImportError: bad"}


def test_status_includes_default_interval_s(collect_home: Path):
    """Status reply must expose each plugin's default_interval_s (seconds),
    or None for non-scheduled plugins, so the menubar can show the correct
    default rather than a hardcoded 3600."""
    d = Daemon(registry=_registry(), config=Config())
    reply = d.handle_request({"cmd": "status"})
    by_id = {p["id"]: p for p in reply["plugins"]}
    # lastfm is scheduled with default_interval=timedelta(hours=1) → 3600 s
    assert by_id["lastfm"]["default_interval_s"] == 3600
    # dayone is manual — no default_interval
    assert by_id["dayone"]["default_interval_s"] is None


def test_status_includes_description(collect_home: Path):
    """status() must include each plugin's description string so the menubar
    can render it in the Preferences Plugins tab."""
    r = RegistryResult()
    r.plugins["lastfm"] = Plugin(
        id="lastfm", name="Last.fm", kind="scheduled",
        collect_mode="live_polled",
        run=lambda c: None,
        description="Imports your Last.fm scrobble history.",
        default_interval=__import__("datetime").timedelta(hours=1),
    )
    r.plugins["dayone"] = Plugin(id="dayone", name="Day One", kind="manual",
                                 collect_mode="historical",
                                 run=lambda c: None)
    d = Daemon(registry=r, config=Config())
    reply = d.handle_request({"cmd": "status"})
    by_id = {p["id"]: p for p in reply["plugins"]}
    assert by_id["lastfm"]["description"] == "Imports your Last.fm scrobble history."
    assert by_id["dayone"]["description"] == ""  # default empty string


def test_unknown_command_is_an_error_reply(collect_home: Path):
    d = Daemon(registry=_registry(), config=Config())
    reply = d.handle_request({"cmd": "frobnicate"})
    assert reply["ok"] is False
    assert "frobnicate" in reply["error"]


def test_run_command_rejects_an_unknown_plugin(collect_home: Path):
    d = Daemon(registry=_registry(), config=Config())
    reply = d.handle_request({"cmd": "run", "plugin": "nope"})
    assert reply["ok"] is False


def test_run_command_triggers_a_known_plugin(collect_home: Path):
    d = Daemon(registry=_registry(), config=Config())
    triggered: list[str] = []
    def _fake_trigger(pid: str) -> bool:
        triggered.append(pid)
        return True
    d._trigger = _fake_trigger  # injected for the test
    reply = d.handle_request({"cmd": "run", "plugin": "dayone"})
    assert reply["ok"] is True
    assert reply["started"] is True
    assert triggered == ["dayone"]


def test_reload_command_rereads_config(collect_home: Path):
    from fulcra_collect import config as config_mod
    d = Daemon(registry=_registry(), config=Config())
    cfg = config_mod.load()
    cfg.enable("lastfm")
    config_mod.save(cfg)
    reply = d.handle_request({"cmd": "reload"})
    assert reply["ok"] is True
    assert "lastfm" in d.config.enabled


def test_trigger_skips_a_second_dispatch_while_a_run_is_in_flight(
        collect_home: Path, monkeypatch):
    """A scheduled plugin must not be dispatched twice concurrently — the
    in-flight guard makes the second `_trigger` a no-op until the first
    run finishes."""
    import threading

    from fulcra_collect import runner

    release = threading.Event()
    started = threading.Event()
    runs: list[str] = []

    def fake_run(plugin_id, command, *, now, on_spawn=None, timeout_s=None,
                 daemon=None):
        runs.append(plugin_id)
        started.set()
        release.wait(timeout=5)
        return "done"

    monkeypatch.setattr(runner, "run", fake_run)

    d = Daemon(registry=_registry(), config=Config())

    first = d._trigger("lastfm")
    assert started.wait(timeout=5)
    second = d._trigger("lastfm")  # while the first run is still blocked

    assert first is True
    assert second is False
    assert runs == ["lastfm"]

    release.set()
    # once the in-flight run drains, a fresh dispatch is allowed again
    deadline = __import__("time").time() + 5
    while "lastfm" in d._inflight and __import__("time").time() < deadline:
        __import__("time").sleep(0.01)
    assert d._trigger("lastfm") is True
    release.set()
    deadline = __import__("time").time() + 5
    while d._inflight and __import__("time").time() < deadline:
        __import__("time").sleep(0.01)


def test_run_command_reports_whether_a_run_was_started(
        collect_home: Path, monkeypatch):
    """The control-socket 'run' handler reports started vs already-running."""
    import threading

    from fulcra_collect import runner

    release = threading.Event()
    started = threading.Event()

    def fake_run(plugin_id, command, *, now, on_spawn=None, timeout_s=None,
                 daemon=None):
        started.set()
        release.wait(timeout=5)
        return "done"

    monkeypatch.setattr(runner, "run", fake_run)
    d = Daemon(registry=_registry(), config=Config())

    first = d.handle_request({"cmd": "run", "plugin": "lastfm"})
    assert started.wait(timeout=5)
    second = d.handle_request({"cmd": "run", "plugin": "lastfm"})

    assert first == {"ok": True, "started": True}
    assert second["ok"] is True
    assert second["started"] is False

    release.set()
    deadline = __import__("time").time() + 5
    while d._inflight and __import__("time").time() < deadline:
        __import__("time").sleep(0.01)


def test_status_includes_category(collect_home, monkeypatch):
    """The daemon's status reply must include each plugin's category
    so the web UI can group plugins by category in the picker."""
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.plugin import Plugin
    from fulcra_collect.registry import RegistryResult

    plugin = Plugin(id="lastfm", name="Last.fm", kind="scheduled",
                    collect_mode="live_polled",
                    run=lambda c: None,
                    default_interval=__import__("datetime").timedelta(hours=1),
                    category="audio")
    registry = RegistryResult(plugins={"lastfm": plugin})
    d = daemon_mod.Daemon(registry=registry, config=daemon_mod.Config())
    reply = d.handle_request({"cmd": "status"})
    by_id = {p["id"]: p for p in reply["plugins"]}
    assert by_id["lastfm"]["category"] == "audio"


def test_version_handler_returns_daemon_and_plugin_versions(collect_home: Path, monkeypatch):
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.plugin import Plugin
    from fulcra_collect.registry import RegistryResult

    def fake_run(ctx): pass

    plugin = Plugin(id="lastfm", name="Last.fm", kind="manual", collect_mode="historical", run=fake_run)
    registry = RegistryResult(plugins={"lastfm": plugin})

    def fake_version(dist_name):
        return {"fulcra-collect": "0.1.0", "fulcra-media-helpers": "0.4.2"}[dist_name]

    monkeypatch.setattr("fulcra_collect.daemon._distribution_for_plugin",
                        lambda pid: "fulcra-media-helpers")
    monkeypatch.setattr("importlib.metadata.version", fake_version)

    d = daemon_mod.Daemon(registry=registry, config=daemon_mod.Config())

    reply = d.handle_request({"cmd": "version"})

    assert reply["ok"] is True
    assert reply["daemon_version"] == "0.1.0"
    assert reply["plugins"] == {"lastfm": "0.4.2"}
    # daemon_pid is the PID of the answering process — used by the menubar
    # to show "Running (PID 12345)" in its daemon-controls header.
    import os
    assert reply["daemon_pid"] == os.getpid()


def test_credential_status_reports_set_and_missing(collect_home: Path, monkeypatch):
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.plugin import Credential, Plugin
    from fulcra_collect.registry import RegistryResult

    plugin = Plugin(
        id="lastfm",
        name="Last.fm",
        kind="manual",
        collect_mode="historical",
        run=lambda ctx: None,
        required_credentials=(
            Credential(key="session_key", label="Session key", help=""),
            Credential(key="api_key", label="API key", help=""),
        ),
    )
    registry = RegistryResult(plugins={"lastfm": plugin})

    fake_store = {("lastfm", "session_key"): True, ("lastfm", "api_key"): False}
    monkeypatch.setattr(
        "fulcra_collect.credentials.has_secret",
        lambda pid, key: fake_store[(pid, key)],
    )

    d = daemon_mod.Daemon(registry=registry, config=daemon_mod.Config())

    reply = d.handle_request({"cmd": "credential_status", "plugin": "lastfm"})

    assert reply == {
        "ok": True,
        "credentials": {"session_key": "set", "api_key": "missing"},
    }


def test_credential_status_unknown_plugin_returns_error(collect_home: Path):
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.registry import RegistryResult

    d = daemon_mod.Daemon(registry=RegistryResult(), config=daemon_mod.Config())

    reply = d.handle_request({"cmd": "credential_status", "plugin": "nope"})

    assert reply["ok"] is False
    assert "nope" in reply["error"]


def test_credential_status_empty_credentials_returns_empty_dict(collect_home: Path):
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.plugin import Plugin
    from fulcra_collect.registry import RegistryResult

    plugin = Plugin(id="noop", name="Noop", kind="manual", collect_mode="historical", run=lambda ctx: None)
    registry = RegistryResult(plugins={"noop": plugin})
    d = daemon_mod.Daemon(registry=registry, config=daemon_mod.Config())
    reply = d.handle_request({"cmd": "credential_status", "plugin": "noop"})
    assert reply == {"ok": True, "credentials": {}}


def test_set_credential_writes_to_keyring(collect_home: Path, monkeypatch):
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.plugin import Credential, Plugin
    from fulcra_collect.registry import RegistryResult

    plugin = Plugin(
        id="lastfm", name="Last.fm", kind="manual", collect_mode="historical", run=lambda c: None,
        required_credentials=(Credential(key="session_key", label="", help=""),),
    )
    registry = RegistryResult(plugins={"lastfm": plugin})

    calls = []
    monkeypatch.setattr(
        "fulcra_collect.credentials.set_secret",
        lambda pid, k, v: calls.append(("set", pid, k, v)),
    )

    d = daemon_mod.Daemon(registry=registry, config=daemon_mod.Config())
    reply = d.handle_request({
        "cmd": "set_credential", "plugin": "lastfm",
        "key": "session_key", "secret": "abc-secret",
    })

    assert reply == {"ok": True}
    assert calls == [("set", "lastfm", "session_key", "abc-secret")]
    from fulcra_collect.config import load
    assert load().plugin_epochs["lastfm"]


def test_delete_credential_calls_keyring(collect_home: Path, monkeypatch):
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.plugin import Credential, Plugin
    from fulcra_collect.registry import RegistryResult

    plugin = Plugin(
        id="lastfm", name="Last.fm", kind="manual", collect_mode="historical", run=lambda c: None,
        required_credentials=(Credential(key="session_key", label="", help=""),),
    )
    registry = RegistryResult(plugins={"lastfm": plugin})

    calls = []
    monkeypatch.setattr(
        "fulcra_collect.credentials.delete_secret",
        lambda pid, k: calls.append(("delete", pid, k)),
    )

    d = daemon_mod.Daemon(registry=registry, config=daemon_mod.Config())
    reply = d.handle_request({
        "cmd": "delete_credential", "plugin": "lastfm", "key": "session_key",
    })

    assert reply == {"ok": True}
    assert calls == [("delete", "lastfm", "session_key")]
    from fulcra_collect.config import load
    assert load().plugin_epochs["lastfm"]


def test_set_credential_rejects_unknown_plugin(collect_home: Path):
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.registry import RegistryResult

    d = daemon_mod.Daemon(registry=RegistryResult(), config=daemon_mod.Config())

    reply = d.handle_request({
        "cmd": "set_credential", "plugin": "nope", "key": "x", "secret": "y",
    })

    assert reply["ok"] is False
    assert "nope" in reply["error"]


def test_set_credential_rejects_unknown_key(collect_home: Path):
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.plugin import Credential, Plugin
    from fulcra_collect.registry import RegistryResult

    plugin = Plugin(
        id="lastfm", name="Last.fm", kind="manual", collect_mode="historical", run=lambda c: None,
        required_credentials=(Credential(key="session_key", label="", help=""),),
    )
    registry = RegistryResult(plugins={"lastfm": plugin})

    d = daemon_mod.Daemon(registry=registry, config=daemon_mod.Config())

    reply = d.handle_request({
        "cmd": "set_credential", "plugin": "lastfm",
        "key": "not_a_real_key", "secret": "x",
    })

    assert reply["ok"] is False
    assert "not_a_real_key" in reply["error"]


def test_delete_credential_rejects_unknown_plugin(collect_home: Path):
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.registry import RegistryResult

    d = daemon_mod.Daemon(registry=RegistryResult(), config=daemon_mod.Config())
    reply = d.handle_request({
        "cmd": "delete_credential", "plugin": "nope", "key": "x",
    })
    assert reply["ok"] is False
    assert "nope" in reply["error"]


def test_delete_credential_rejects_unknown_key(collect_home: Path):
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.plugin import Credential, Plugin
    from fulcra_collect.registry import RegistryResult

    plugin = Plugin(
        id="lastfm", name="Last.fm", kind="manual", collect_mode="historical", run=lambda c: None,
        required_credentials=(Credential(key="session_key", label="", help=""),),
    )
    registry = RegistryResult(plugins={"lastfm": plugin})
    d = daemon_mod.Daemon(registry=registry, config=daemon_mod.Config())
    reply = d.handle_request({
        "cmd": "delete_credential", "plugin": "lastfm", "key": "not_a_real_key",
    })
    assert reply["ok"] is False
    assert "not_a_real_key" in reply["error"]


# ---- keychain exception sanitization tests --------------------------------
# These verify that if keyring (or any future backend) raises an exception
# whose str() contains a secret-looking value, the daemon never forwards
# that raw message to the control-socket caller.


def test_set_credential_does_not_leak_keyring_exception_message(
        collect_home: Path, monkeypatch):
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.plugin import Credential, Plugin
    from fulcra_collect.registry import RegistryResult

    plugin = Plugin(
        id="lastfm", name="Last.fm", kind="manual", collect_mode="historical", run=lambda c: None,
        required_credentials=(Credential(key="session_key", label="", help=""),),
    )
    registry = RegistryResult(plugins={"lastfm": plugin})

    def boom(*a, **kw):
        raise RuntimeError("SENSITIVE_TOKEN_VALUE_DO_NOT_LEAK")

    monkeypatch.setattr("fulcra_collect.credentials.set_secret", boom)

    d = daemon_mod.Daemon(registry=registry, config=daemon_mod.Config())
    reply = d.handle_request({
        "cmd": "set_credential", "plugin": "lastfm",
        "key": "session_key", "secret": "abc",
    })

    assert reply["ok"] is False
    assert "SENSITIVE_TOKEN_VALUE_DO_NOT_LEAK" not in reply["error"]
    assert "keychain" in reply["error"].lower()


def test_delete_credential_does_not_leak_keyring_exception_message(
        collect_home: Path, monkeypatch):
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.plugin import Credential, Plugin
    from fulcra_collect.registry import RegistryResult

    plugin = Plugin(
        id="lastfm", name="Last.fm", kind="manual", collect_mode="historical", run=lambda c: None,
        required_credentials=(Credential(key="session_key", label="", help=""),),
    )
    registry = RegistryResult(plugins={"lastfm": plugin})

    def boom(*a, **kw):
        raise RuntimeError("SENSITIVE_TOKEN_VALUE_DO_NOT_LEAK")

    monkeypatch.setattr("fulcra_collect.credentials.delete_secret", boom)

    d = daemon_mod.Daemon(registry=registry, config=daemon_mod.Config())
    reply = d.handle_request({
        "cmd": "delete_credential", "plugin": "lastfm", "key": "session_key",
    })

    assert reply["ok"] is False
    assert "SENSITIVE_TOKEN_VALUE_DO_NOT_LEAK" not in reply["error"]
    assert "keychain" in reply["error"].lower()


def test_credential_status_does_not_leak_keyring_exception_message(
        collect_home: Path, monkeypatch):
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.plugin import Credential, Plugin
    from fulcra_collect.registry import RegistryResult

    plugin = Plugin(
        id="lastfm", name="Last.fm", kind="manual", collect_mode="historical", run=lambda c: None,
        required_credentials=(Credential(key="session_key", label="", help=""),),
    )
    registry = RegistryResult(plugins={"lastfm": plugin})

    def boom(*a, **kw):
        raise RuntimeError("SENSITIVE_TOKEN_VALUE_DO_NOT_LEAK")

    monkeypatch.setattr("fulcra_collect.credentials.has_secret", boom)

    d = daemon_mod.Daemon(registry=registry, config=daemon_mod.Config())
    reply = d.handle_request({
        "cmd": "credential_status", "plugin": "lastfm",
    })

    assert reply["ok"] is False
    assert "SENSITIVE_TOKEN_VALUE_DO_NOT_LEAK" not in reply["error"]
    assert "keychain" in reply["error"].lower()


# ---------------------------------------------------------------------------
# Phase G — quick_record_list + record_annotation commands
#
# The fake-httpx seam (FakeHttpxClient / FakeHttpxResponse / install_fake_httpx)
# is shared with test_web.py and lives in conftest.py.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Sprint B — Duration record + soft-delete tombstone
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase H — account-switch fingerprint pre-flight
# ---------------------------------------------------------------------------

def test_fingerprint_preflight_writes_initial_fingerprint(collect_home, monkeypatch):
    """First Daemon boot with a token writes the fingerprint file; no
    invalidation happens because there's no prior fingerprint to
    compare against."""
    monkeypatch.setattr("fulcra_collect.credentials.get_user_secret",
                        lambda key: "tok-first-boot" if key == "bearer-token" else None)
    d = Daemon(registry=_registry(), config=Config())
    # The pre-flight now runs in serve() on a background thread (not in
    # __init__, which must stay keychain-free); call it directly to exercise
    # the behaviour under test.
    d._check_account_fingerprint()
    fp_path = collect_home / "auth-fingerprint"
    assert fp_path.exists()
    # No invalidation activity entry — first boot is the baseline.
    daemon_entries = [e for e in d.activity.recent()
                      if e.plugin_id == "daemon"]
    assert daemon_entries == []


def test_fingerprint_preflight_does_nothing_when_no_token(collect_home, monkeypatch):
    """Daemon boots without a bearer-token (user hasn't signed in yet)
    leave the fingerprint file alone — we have nothing to remember."""
    monkeypatch.setattr("fulcra_collect.credentials.get_user_secret",
                        lambda key: None)
    Daemon(registry=_registry(), config=Config())
    fp_path = collect_home / "auth-fingerprint"
    assert not fp_path.exists()


def test_fingerprint_preflight_skips_on_unchanged_token(collect_home, monkeypatch):
    """Same bearer-token across boots → no invalidation, no activity
    entry. The fingerprint file stays the same."""
    monkeypatch.setattr("fulcra_collect.credentials.get_user_secret",
                        lambda key: "stable-tok" if key == "bearer-token" else None)
    # First boot writes the fingerprint.
    d1 = Daemon(registry=_registry(), config=Config())
    d1._check_account_fingerprint()
    fp_path = collect_home / "auth-fingerprint"
    first_fp = fp_path.read_text()
    # Second boot with the same token.
    d2 = Daemon(registry=_registry(), config=Config())
    d2._check_account_fingerprint()
    assert fp_path.read_text() == first_fp
    assert [e for e in d2.activity.recent() if e.plugin_id == "daemon"] == []


def test_account_cache_invalidation_retains_pending_payloads_for_old_account(collect_home, monkeypatch):
    from fulcra_collect import db, state as state_mod
    from fulcra_media import state as media_state
    # This cross-package cache path must not read or write the operator's
    # actual default media state while testing account invalidation.
    monkeypatch.setattr(media_state, "load", lambda: media_state.State())
    monkeypatch.setattr(media_state, "save", lambda *_: None)
    pending = {"account_id": "synthetic-old-account", "epoch": "old-epoch", "groups": [{"records": []}]}
    conn = db.open()
    db.set_record_outbox(conn, "lastfm", "v1_media_pending", pending)
    db.set_plugin_kv(conn, "purpleair", "v1_pending", {"account_id": "synthetic-old-account", "items": ["retained"]})
    state_mod.save(state_mod.PluginState(plugin_id="lastfm", definition_id="synthetic-legacy-definition"))
    daemon = Daemon(registry=_registry(), config=Config())
    daemon._invalidate_plugin_caches(reason="Synthetic account transition")
    assert state_mod.load("lastfm").definition_id is None
    assert db.get_record_outbox(conn, "lastfm", "v1_media_pending") == pending
    assert db.get_plugin_kv(conn, "purpleair", "v1_pending")["items"] == ["retained"]


def test_fingerprint_preflight_invalidates_on_token_change(collect_home, monkeypatch):
    """The headline behaviour: a different bearer-token between boots
    means we re-authed to a different Fulcra account, so the cached
    def_ids + tag_ids in every plugin state file are now orphans.
    Pre-flight clears them and surfaces a one-line activity entry so
    the user can see what happened."""
    from fulcra_collect import state as state_mod
    # Boot 1: write the fingerprint AND seed a per-plugin state file
    # with a cached def_id (mimicking a plugin having run on account A).
    monkeypatch.setattr("fulcra_collect.credentials.get_user_secret",
                        lambda key: "tok-account-A" if key == "bearer-token" else None)
    d1 = Daemon(registry=_registry(), config=Config())
    d1._check_account_fingerprint()
    st = state_mod.PluginState(plugin_id="lastfm", definition_id="def-from-A")
    state_mod.save(st)
    # Boot 2: different token → invalidation should happen.
    monkeypatch.setattr("fulcra_collect.credentials.get_user_secret",
                        lambda key: "tok-account-B" if key == "bearer-token" else None)
    d2 = Daemon(registry=_registry(), config=Config())
    d2._check_account_fingerprint()
    # The cached def_id was cleared.
    reloaded = state_mod.load("lastfm")
    assert reloaded.definition_id is None
    # Activity feed got a "account change" entry.
    daemon_entries = [e for e in d2.activity.recent()
                      if e.plugin_id == "daemon"]
    assert len(daemon_entries) == 1
    assert "account change" in daemon_entries[0].summary.lower()
    assert daemon_entries[0].ok is True
    # And the fingerprint file is updated to the new token.
    new_fp = (collect_home / "auth-fingerprint").read_text()
    import hashlib
    # Opaque tokens fingerprint via the "tok:<bytes>" identity (JWTs use
    # "sub:<claim>" — see Daemon._account_identity).
    assert new_fp == hashlib.sha256(b"tok:tok-account-B").hexdigest()[:16]


# ---------------------------------------------------------------------------
# Task #64 — quick-record favorites: list ordering + dispatch
# ---------------------------------------------------------------------------


def _fake_jwt(sub: str, nonce: str) -> str:
    """Minimal unsigned-JWT shape: header.payload.signature (base64url)."""
    import base64
    import json as _json

    def b64(obj) -> str:
        raw = _json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{b64({'alg': 'RS256', 'typ': 'JWT'})}.{b64({'sub': sub, 'jti': nonce})}.sig-{nonce}"


def _fake_jwt_payload(payload, nonce: str) -> str:
    """JWT-looking token with an arbitrary JSON payload."""
    import base64
    import json as _json

    def b64(obj) -> str:
        raw = _json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{b64({'alg': 'RS256', 'typ': 'JWT'})}.{b64(payload)}.sig-{nonce}"


def test_fingerprint_survives_token_rotation_same_account(collect_home, monkeypatch):
    """Access tokens rotate hourly (refresh-on-401 rewrites the keychain), so
    fingerprinting the raw token invalidated every plugin cache on effectively
    every daemon restart >1h apart — a full definitions re-resolution storm
    with no actual account change. The fingerprint must key on the JWT `sub`
    claim (the account), not the token bytes."""
    monkeypatch.setattr(
        "fulcra_collect.credentials.get_user_secret",
        lambda key: _fake_jwt("auth0|user-A", "boot1") if key == "bearer-token" else None)
    d1 = Daemon(registry=_registry(), config=Config())
    d1._check_account_fingerprint()
    fp_path = collect_home / "auth-fingerprint"
    first_fp = fp_path.read_text()

    # Same account, ROTATED token (different bytes, same sub).
    monkeypatch.setattr(
        "fulcra_collect.credentials.get_user_secret",
        lambda key: _fake_jwt("auth0|user-A", "boot2") if key == "bearer-token" else None)
    d2 = Daemon(registry=_registry(), config=Config())
    d2._check_account_fingerprint()

    assert fp_path.read_text() == first_fp
    assert [e for e in d2.activity.recent() if e.plugin_id == "daemon"] == []


def test_fingerprint_still_invalidates_on_real_account_switch(collect_home, monkeypatch):
    """Different JWT sub → genuine account switch → caches must clear."""
    monkeypatch.setattr(
        "fulcra_collect.credentials.get_user_secret",
        lambda key: _fake_jwt("auth0|user-A", "a1") if key == "bearer-token" else None)
    d1 = Daemon(registry=_registry(), config=Config())
    d1._check_account_fingerprint()

    monkeypatch.setattr(
        "fulcra_collect.credentials.get_user_secret",
        lambda key: _fake_jwt("auth0|user-B", "b1") if key == "bearer-token" else None)
    d2 = Daemon(registry=_registry(), config=Config())
    d2._check_account_fingerprint()

    daemon_entries = [e for e in d2.activity.recent() if e.plugin_id == "daemon"]
    assert len(daemon_entries) == 1


def test_fingerprint_falls_back_to_token_hash_for_non_jwt(collect_home, monkeypatch):
    """Opaque (non-JWT) tokens keep the old token-hash behavior: same token →
    stable; different token → invalidation. (Covers pasted tokens and the
    pre-existing tests' fixtures.)"""
    monkeypatch.setattr(
        "fulcra_collect.credentials.get_user_secret",
        lambda key: "not-a-jwt-token" if key == "bearer-token" else None)
    d1 = Daemon(registry=_registry(), config=Config())
    d1._check_account_fingerprint()
    fp_path = collect_home / "auth-fingerprint"
    first_fp = fp_path.read_text()

    d2 = Daemon(registry=_registry(), config=Config())
    d2._check_account_fingerprint()
    assert fp_path.read_text() == first_fp
    assert [e for e in d2.activity.recent() if e.plugin_id == "daemon"] == []


def test_fingerprint_falls_back_for_non_object_jwt_payload():
    """A JWT-looking token whose payload is valid JSON but not an object is
    malformed for our purposes; it must not crash the pre-flight."""
    token = _fake_jwt_payload(["not", "a", "dict"], "array")
    assert Daemon._account_identity(token) == f"tok:{token}"

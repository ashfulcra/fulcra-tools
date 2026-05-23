"""The daemon request handler + status snapshot."""
from __future__ import annotations

from pathlib import Path

from fulcra_collect.config import Config
from fulcra_collect.daemon import Daemon
from fulcra_collect.plugin import Plugin
from fulcra_collect.registry import RegistryResult


def _registry() -> RegistryResult:
    r = RegistryResult()
    r.plugins["lastfm"] = Plugin(id="lastfm", name="Last.fm", kind="scheduled",
                                 run=lambda c: None,
                                 default_interval=__import__("datetime").timedelta(hours=1))
    r.plugins["dayone"] = Plugin(id="dayone", name="Day One", kind="manual",
                                 run=lambda c: None)
    r.errors["brokenplugin"] = "ImportError: bad"
    return r


def test_status_lists_every_plugin_with_enabled_flag(collect_home: Path):
    d = Daemon(registry=_registry(), config=Config(enabled={"lastfm"}))
    reply = d.handle_request({"cmd": "status"})
    assert reply["ok"] is True
    by_id = {p["id"]: p for p in reply["plugins"]}
    assert by_id["lastfm"]["enabled"] is True
    assert by_id["dayone"]["enabled"] is False
    assert by_id["lastfm"]["kind"] == "scheduled"


def test_status_reports_registry_load_errors(collect_home: Path):
    d = Daemon(registry=_registry(), config=Config())
    reply = d.handle_request({"cmd": "status"})
    assert reply["load_errors"] == {"brokenplugin": "ImportError: bad"}


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

    def fake_run(plugin_id, command, *, now, on_spawn=None, timeout_s=None):
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

    def fake_run(plugin_id, command, *, now, on_spawn=None, timeout_s=None):
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


def test_version_handler_returns_daemon_and_plugin_versions(collect_home: Path, monkeypatch):
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.plugin import Plugin
    from fulcra_collect.registry import RegistryResult

    def fake_run(ctx): pass

    plugin = Plugin(id="lastfm", name="Last.fm", kind="manual", run=fake_run)
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


def test_credential_status_reports_set_and_missing(collect_home: Path, monkeypatch):
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.plugin import Credential, Plugin
    from fulcra_collect.registry import RegistryResult

    plugin = Plugin(
        id="lastfm",
        name="Last.fm",
        kind="manual",
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

    plugin = Plugin(id="noop", name="Noop", kind="manual", run=lambda ctx: None)
    registry = RegistryResult(plugins={"noop": plugin})
    d = daemon_mod.Daemon(registry=registry, config=daemon_mod.Config())
    reply = d.handle_request({"cmd": "credential_status", "plugin": "noop"})
    assert reply == {"ok": True, "credentials": {}}


def test_set_credential_writes_to_keyring(collect_home: Path, monkeypatch):
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.plugin import Credential, Plugin
    from fulcra_collect.registry import RegistryResult

    plugin = Plugin(
        id="lastfm", name="Last.fm", kind="manual", run=lambda c: None,
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


def test_delete_credential_calls_keyring(collect_home: Path, monkeypatch):
    from fulcra_collect import daemon as daemon_mod
    from fulcra_collect.plugin import Credential, Plugin
    from fulcra_collect.registry import RegistryResult

    plugin = Plugin(
        id="lastfm", name="Last.fm", kind="manual", run=lambda c: None,
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
        id="lastfm", name="Last.fm", kind="manual", run=lambda c: None,
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

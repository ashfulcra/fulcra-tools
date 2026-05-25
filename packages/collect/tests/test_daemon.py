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
        run=lambda c: None,
        description="Imports your Last.fm scrobble history.",
        default_interval=__import__("datetime").timedelta(hours=1),
    )
    r.plugins["dayone"] = Plugin(id="dayone", name="Day One", kind="manual",
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
                    run=lambda c: None,
                    default_interval=__import__("datetime").timedelta(hours=1),
                    category="music")
    registry = RegistryResult(plugins={"lastfm": plugin})
    d = daemon_mod.Daemon(registry=registry, config=daemon_mod.Config())
    reply = d.handle_request({"cmd": "status"})
    by_id = {p["id"]: p for p in reply["plugins"]}
    assert by_id["lastfm"]["category"] == "music"


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
        id="lastfm", name="Last.fm", kind="manual", run=lambda c: None,
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
        id="lastfm", name="Last.fm", kind="manual", run=lambda c: None,
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
        id="lastfm", name="Last.fm", kind="manual", run=lambda c: None,
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
        id="lastfm", name="Last.fm", kind="manual", run=lambda c: None,
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
# ---------------------------------------------------------------------------

class _FakeHttpxResponse:
    status_code = 200
    def raise_for_status(self): pass
    def json(self): return self._data

    def __init__(self, data):
        self._data = data


class _FakeHttpxClient:
    """httpx.Client stub that records calls and returns preset responses."""
    def __init__(self, *, get_data=None, post_status=200):
        self._get_data = get_data or []
        self._post_status = post_status
        self.requests: list[dict] = []

    def __enter__(self): return self
    def __exit__(self, *a): pass

    def get(self, url, **kw):
        self.requests.append({"method": "GET", "url": url, **kw})
        resp = _FakeHttpxResponse(self._get_data)
        return resp

    def post(self, url, **kw):
        self.requests.append({"method": "POST", "url": url, **kw})
        resp = _FakeHttpxResponse({"ok": True})
        resp.status_code = self._post_status
        return resp


def _make_fake_client_factory(client_obj):
    """Return a class whose constructor always returns client_obj."""
    class _Cls:
        def __new__(cls, **kw):
            return client_obj
    return _Cls


def test_quick_record_list_returns_empty_when_unauthenticated(collect_home, monkeypatch):
    """quick_record_list returns ok=False with empty list when no bearer token."""
    monkeypatch.setattr("fulcra_collect.credentials.get_user_secret", lambda key: None)
    d = Daemon(registry=_registry(), config=Config())
    reply = d.handle_request({"cmd": "quick_record_list"})
    assert reply["ok"] is False
    assert "authenticated" in reply["error"].lower()
    assert reply["definitions"] == []


def test_quick_record_list_happy_path(collect_home, monkeypatch):
    """quick_record_list filters to moments, excludes deleted, caps at 20."""
    import fulcra_collect.daemon as daemon_mod

    monkeypatch.setattr("fulcra_collect.credentials.get_user_secret", lambda key: "tok")

    defs = [
        {"id": f"m-{i}", "name": f"Moment {i}", "annotation_type": "moment",
         "deleted_at": None, "created_at": f"2026-05-{i+1:02d}T00:00:00Z"}
        for i in range(25)
    ] + [
        {"id": "dur-1", "name": "Duration", "annotation_type": "duration",
         "deleted_at": None, "created_at": "2026-05-01T00:00:00Z"},
        {"id": "deleted-mom", "name": "Gone", "annotation_type": "moment",
         "deleted_at": "2026-01-01T00:00:00Z", "created_at": "2026-04-01T00:00:00Z"},
    ]

    fake_client = _FakeHttpxClient(get_data=defs)
    monkeypatch.setattr(daemon_mod, "httpx",
                        type("httpx", (), {"Client": _make_fake_client_factory(fake_client)})())

    d = Daemon(registry=_registry(), config=Config())
    reply = d._quick_record_list()

    assert reply["ok"] is True
    # Should return at most 20 moments, excluding deleted and duration
    assert len(reply["definitions"]) == 20
    # All returned are moments
    assert all(d["annotation_type"] == "moment" for d in reply["definitions"])
    # All returned are non-deleted
    assert all(d["deleted_at"] is None for d in reply["definitions"])


def test_quick_record_list_caches_result(collect_home, monkeypatch):
    """Second call within 60s returns cached result without hitting the API."""
    import fulcra_collect.daemon as daemon_mod

    monkeypatch.setattr("fulcra_collect.credentials.get_user_secret", lambda key: "tok")

    fake_client = _FakeHttpxClient(get_data=[
        {"id": "m1", "name": "Coffee", "annotation_type": "moment",
         "deleted_at": None, "created_at": "2026-05-01T00:00:00Z"},
    ])
    monkeypatch.setattr(daemon_mod, "httpx",
                        type("httpx", (), {"Client": _make_fake_client_factory(fake_client)})())

    d = Daemon(registry=_registry(), config=Config())
    r1 = d._quick_record_list()
    # Clear the fake_client's request log so we can check if a second GET fires
    initial_count = len(fake_client.requests)
    r2 = d._quick_record_list()

    assert r1["ok"] is True and r2["ok"] is True
    assert r1["definitions"] == r2["definitions"]
    # No additional GET was issued (the second hit the cache)
    assert len(fake_client.requests) == initial_count


def test_quick_record_list_api_error_returns_graceful_response(collect_home, monkeypatch):
    """quick_record_list returns ok=False with empty list on API failure."""
    import fulcra_collect.daemon as daemon_mod

    monkeypatch.setattr("fulcra_collect.credentials.get_user_secret", lambda key: "tok")

    class _ErrorClient:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, *a, **kw): raise RuntimeError("network failure")

    monkeypatch.setattr(daemon_mod, "httpx",
                        type("httpx", (), {"Client": lambda timeout=None: _ErrorClient()})())

    d = Daemon(registry=_registry(), config=Config())
    reply = d._quick_record_list()
    assert reply["ok"] is False
    assert "Fulcra" in reply["error"]
    assert reply["definitions"] == []


def test_record_annotation_missing_definition_id(collect_home):
    """record_annotation rejects an empty definition_id."""
    d = Daemon(registry=_registry(), config=Config())
    reply = d.handle_request({"cmd": "record_annotation", "definition_id": ""})
    assert reply["ok"] is False
    assert "definition_id" in reply["error"]


def test_record_annotation_unauthenticated(collect_home, monkeypatch):
    """record_annotation returns ok=False when no Fulcra token."""
    monkeypatch.setattr("fulcra_collect.credentials.get_user_secret", lambda key: None)
    d = Daemon(registry=_registry(), config=Config())
    reply = d.handle_request({"cmd": "record_annotation", "definition_id": "def-abc"})
    assert reply["ok"] is False
    assert "authenticated" in reply["error"].lower()


def test_record_annotation_happy_path(collect_home, monkeypatch):
    """record_annotation calls Fulcra POST and surfaces ok=True + activity entry."""
    import fulcra_collect.daemon as daemon_mod

    monkeypatch.setattr("fulcra_collect.credentials.get_user_secret", lambda key: "tok")

    fake_client = _FakeHttpxClient()
    monkeypatch.setattr(daemon_mod, "httpx",
                        type("httpx", (), {"Client": _make_fake_client_factory(fake_client)})())

    d = Daemon(registry=_registry(), config=Config())
    # Pre-populate cache so _quick_record_list doesn't interfere
    reply = d._record_annotation("def-abcdef12", None)

    assert reply == {"ok": True}
    # One POST was issued
    post_reqs = [r for r in fake_client.requests if r["method"] == "POST"]
    assert len(post_reqs) == 1
    # Activity buffer has one entry
    entries = d.activity.recent(limit=1)
    assert entries[0].ok is True
    assert entries[0].plugin_id == "quick-record"
    assert "def-abcd" in entries[0].summary


def test_record_annotation_api_error_surfaces_activity_failure(collect_home, monkeypatch):
    """record_annotation records a failure entry in the activity buffer on API error."""
    import fulcra_collect.daemon as daemon_mod

    monkeypatch.setattr("fulcra_collect.credentials.get_user_secret", lambda key: "tok")

    class _ErrorClient:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, *a, **kw): raise RuntimeError("connection refused")

    monkeypatch.setattr(daemon_mod, "httpx",
                        type("httpx", (), {"Client": lambda timeout=None, follow_redirects=True: _ErrorClient()})())

    d = Daemon(registry=_registry(), config=Config())
    reply = d._record_annotation("def-xyz", None)

    assert reply["ok"] is False
    assert "Fulcra" in reply["error"]
    entries = d.activity.recent(limit=1)
    assert entries[0].ok is False
    assert entries[0].plugin_id == "quick-record"

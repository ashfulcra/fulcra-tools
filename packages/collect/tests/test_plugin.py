"""The plugin API types."""
from __future__ import annotations

import logging
from datetime import timedelta

import pytest

from fulcra_collect.plugin import Credential, Permission, Plugin, RunContext
from fulcra_collect.state import PluginState


def _noop(ctx) -> None:
    pass


def test_scheduled_plugin_requires_default_interval():
    with pytest.raises(ValueError, match="default_interval"):
        Plugin(id="x", name="X", kind="scheduled", run=_noop)


def test_non_scheduled_plugin_rejects_default_interval():
    with pytest.raises(ValueError, match="default_interval"):
        Plugin(id="x", name="X", kind="manual", run=_noop,
               default_interval=timedelta(hours=1))


def test_unknown_kind_rejected():
    with pytest.raises(ValueError, match="kind"):
        Plugin(id="x", name="X", kind="weekly", run=_noop)


def test_valid_plugins_of_each_kind():
    svc = Plugin(id="relay", name="Relay", kind="service", run=_noop)
    sch = Plugin(id="lastfm", name="Last.fm", kind="scheduled", run=_noop,
                 default_interval=timedelta(hours=1))
    man = Plugin(id="dayone", name="Day One", kind="manual", run=_noop)
    assert svc.kind == "service"
    assert sch.default_interval == timedelta(hours=1)
    assert man.kind == "manual"


def test_permission_and_credential_are_simple_records():
    p = Permission(id="full-disk-access", explanation="needed to read the DB")
    c = Credential(key="lastfm-api-key", label="Last.fm API key",
                   help="https://www.last.fm/api/account/create")
    assert p.id == "full-disk-access"
    assert c.key == "lastfm-api-key"


def test_requires_network_defaults_true_and_is_overridable():
    online = Plugin(id="x", name="X", kind="manual", run=_noop)
    offline_ok = Plugin(id="y", name="Y", kind="manual", run=_noop,
                        requires_network=False)
    assert online.requires_network is True
    assert offline_ok.requires_network is False


# ---------------------------------------------------------------------------
# RunContext.resolved_definition_id tests.
#
# The helper hides the resolver + state caching from plugin code. It is
# exercised via a fake fulcra_client and a fresh PluginState — the
# resolver itself is tested in fulcra-common.
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self):
        self.list_calls = 0
        self.create_calls = 0

    def list_definitions(self, *, name):
        self.list_calls += 1
        return []

    def create_definition(self, *, name, **spec):
        self.create_calls += 1
        return {"id": "def-fresh", "name": name, **spec}


def _make_ctx(state, client):
    return RunContext(
        plugin_id="lastfm",
        config={},
        credentials={},
        state=state,
        log=logging.getLogger("test"),
        _emit=lambda evt: None,
        _fulcra_client_factory=lambda: client,
    )


def test_resolved_definition_id_calls_resolver_when_state_empty():
    state = PluginState(plugin_id="lastfm")
    client = _FakeClient()
    ctx = _make_ctx(state, client)
    out = ctx.resolved_definition_id({"annotation_type": "moment"},
                                     canonical_name="lastfm-listens")
    assert out == "def-fresh"
    assert state.definition_id == "def-fresh"
    assert client.create_calls == 1


def test_resolved_definition_id_uses_cache_on_second_call():
    state = PluginState(plugin_id="lastfm", definition_id="cached-id")
    client = _FakeClient()
    ctx = _make_ctx(state, client)
    out = ctx.resolved_definition_id({"annotation_type": "moment"},
                                     canonical_name="lastfm-listens")
    assert out == "cached-id"
    assert client.list_calls == 0   # resolver was NOT called
    assert client.create_calls == 0


def test_canonical_definition_name_is_optional_on_plugin():
    # Plugins without a canonical name (e.g. dayone moments) must
    # still construct cleanly.
    p = Plugin(id="dayone", name="Day One", kind="manual", run=_noop)
    assert p.canonical_definition_name is None


def test_setting_dataclass_fields():
    from fulcra_collect.plugin import Setting
    s = Setting(key="feed_url", label="RSS feed URL", kind="url",
                help="Where to fetch the feed from.", default=None,
                required=True, placeholder="https://example.com/feed.xml")
    assert s.key == "feed_url"
    assert s.kind == "url"
    assert s.required is True


def test_setting_enum_kind_with_values():
    from fulcra_collect.plugin import Setting
    s = Setting(key="category", label="Category", kind="enum",
                enum_values=("watched", "listened", "read"), default="watched")
    assert s.enum_values == ("watched", "listened", "read")


def test_plugin_required_settings_default_empty():
    from fulcra_collect.plugin import Plugin
    p = Plugin(id="x", name="X", kind="manual", run=lambda c: None)
    assert p.required_settings == ()


def test_setup_step_dataclass():
    from fulcra_collect.plugin import SetupStep
    s = SetupStep(kind="intro", title="What this does", body_md="…")
    assert s.kind == "intro"


def test_setup_step_input_kind_with_settings_keys():
    from fulcra_collect.plugin import SetupStep
    s = SetupStep(kind="input", title="Paste your API key",
                  settings_keys=("api_key",))
    assert s.settings_keys == ("api_key",)


def test_plugin_setup_steps_default_empty():
    from fulcra_collect.plugin import Plugin
    p = Plugin(id="x", name="X", kind="manual", run=lambda c: None)
    assert p.setup_steps == ()


def test_health_result_basic():
    from fulcra_collect.plugin import HealthResult
    r = HealthResult(ok=True, summary="5 recent scrobbles",
                     preview=[{"title": "Song A"}, {"title": "Song B"}])
    assert r.ok is True
    assert len(r.preview) == 2


def test_health_result_default_empty_preview():
    from fulcra_collect.plugin import HealthResult
    r = HealthResult(ok=False, summary="Not signed in.")
    assert r.preview == []


def test_plugin_health_check_optional():
    from fulcra_collect.plugin import Plugin
    p = Plugin(id="x", name="X", kind="manual", run=lambda c: None)
    assert p.health_check is None


def test_canonical_definition_name_persists_when_set():
    p = Plugin(
        id="lastfm", name="Last.fm", kind="manual",
        run=_noop,
        canonical_definition_name="lastfm-listens",
    )
    assert p.canonical_definition_name == "lastfm-listens"


def test_resolved_definition_id_raises_when_factory_not_set():
    """Important 2: the factory guard must fire BEFORE the lazy import of
    fulcra_common.definitions. A missing fulcra_common should produce a
    clear RuntimeError, not a ModuleNotFoundError."""
    state = PluginState(plugin_id="lastfm")
    ctx = RunContext(
        plugin_id="lastfm", config={}, credentials={},
        state=state,
        log=logging.getLogger("test"),
        _emit=lambda evt: None,
        # _fulcra_client_factory intentionally omitted
    )
    with pytest.raises(RuntimeError, match="_fulcra_client_factory"):
        ctx.resolved_definition_id({"annotation_type": "moment"},
                                   canonical_name="lastfm-listens")


# ---------------------------------------------------------------------------
# RunContext.fulcra_token() — user-level credential store (B9)
# ---------------------------------------------------------------------------

def _make_bare_ctx() -> RunContext:
    """Minimal RunContext for fulcra_token() tests."""
    return RunContext(
        plugin_id="test",
        config={},
        credentials={},
        state=None,
        log=logging.getLogger("test"),
        _emit=lambda evt: None,
    )


def test_fulcra_token_reads_from_user_level_store(monkeypatch):
    """RunContext.fulcra_token() returns the token stored in
    credentials.get_user_secret("bearer-token") without invoking the
    fulcra CLI or reading any env var."""
    from fulcra_collect import credentials
    monkeypatch.setattr(credentials, "get_user_secret",
                        lambda key: "the-real-token" if key == "bearer-token" else None)
    ctx = _make_bare_ctx()
    assert ctx.fulcra_token() == "the-real-token"


def test_fulcra_token_returns_none_when_unset(monkeypatch):
    """When no user-level token is stored and the CLI is unavailable,
    fulcra_token() returns None rather than raising."""
    from fulcra_collect import credentials
    monkeypatch.setattr(credentials, "get_user_secret", lambda key: None)
    # Simulate CLI unavailable (BaseFulcraClient.get_token raises RuntimeError).
    import fulcra_common
    monkeypatch.setattr(fulcra_common.BaseFulcraClient, "get_token",
                        lambda self: (_ for _ in ()).throw(
                            RuntimeError("fulcra CLI not found")))
    ctx = _make_bare_ctx()
    assert ctx.fulcra_token() is None


def test_fulcra_token_prefers_user_level_over_cli(monkeypatch):
    """User-level store takes priority; the CLI subprocess is never called
    when a keychain token is already present."""
    from fulcra_collect import credentials
    monkeypatch.setattr(credentials, "get_user_secret",
                        lambda key: "keychain-token" if key == "bearer-token" else None)
    cli_called = []
    import fulcra_common
    monkeypatch.setattr(fulcra_common.BaseFulcraClient, "get_token",
                        lambda self: cli_called.append(True) or "cli-token")
    ctx = _make_bare_ctx()
    assert ctx.fulcra_token() == "keychain-token"
    assert cli_called == [], "CLI must not be invoked when keychain has a token"


def test_annotation_method_emits_annotation_event():
    """RunContext.annotation() emits a correctly-shaped annotation event."""
    emitted: list[dict] = []
    ctx = RunContext(
        plugin_id="lastfm",
        config={},
        credentials={},
        state=None,
        log=logging.getLogger("test"),
        _emit=emitted.append,
    )
    ctx.annotation("Listened: 3 new scrobbles", ok=True)
    assert emitted == [
        {"type": "annotation", "summary": "Listened: 3 new scrobbles", "ok": True}
    ]


def test_annotation_method_ok_defaults_to_true():
    """The `ok` kwarg defaults to True when not supplied."""
    emitted: list[dict] = []
    ctx = RunContext(
        plugin_id="lastfm",
        config={},
        credentials={},
        state=None,
        log=logging.getLogger("test"),
        _emit=emitted.append,
    )
    ctx.annotation("Song A — Artist B")
    assert emitted[0]["ok"] is True


def test_annotation_method_accepts_ok_false():
    """ok=False is forwarded so failed writes can also be surfaced."""
    emitted: list[dict] = []
    ctx = RunContext(
        plugin_id="x",
        config={},
        credentials={},
        state=None,
        log=logging.getLogger("test"),
        _emit=emitted.append,
    )
    ctx.annotation("Write failed", ok=False)
    assert emitted == [{"type": "annotation", "summary": "Write failed", "ok": False}]


def test_fulcra_token_falls_back_to_cli_when_no_keychain_token(monkeypatch):
    """When the user-level store is empty, fulcra_token() falls back to
    BaseFulcraClient.get_token() (env var + CLI subprocess path)."""
    from fulcra_collect import credentials
    monkeypatch.setattr(credentials, "get_user_secret", lambda key: None)
    import fulcra_common
    monkeypatch.setattr(fulcra_common.BaseFulcraClient, "get_token",
                        lambda self: "cli-fallback-token")
    ctx = _make_bare_ctx()
    assert ctx.fulcra_token() == "cli-fallback-token"

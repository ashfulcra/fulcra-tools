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

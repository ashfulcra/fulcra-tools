"""The attention-relay fulcra-collect plugin."""
from __future__ import annotations

import logging

from fulcra_attention.collect_plugin import ATTENTION_SPEC, PLUGIN
from fulcra_collect.plugin import RunContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeServer:
    def __init__(self):
        self.served = False

    def serve_forever(self):
        self.served = True


def _fake_make_server(**kw) -> _FakeServer:
    return _FakeServer()


def _make_ctx(*, factory=None) -> RunContext:
    return RunContext(
        plugin_id="attention-relay",
        config={},
        credentials={},
        state=None,
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=factory,
    )


# ---------------------------------------------------------------------------
# Metadata tests
# ---------------------------------------------------------------------------

def test_plugin_metadata_is_a_service():
    assert PLUGIN.id == "attention-relay"
    assert PLUGIN.kind == "service"
    assert PLUGIN.default_interval is None


def test_plugin_declares_canonical_definition_name():
    """R5: the plugin opts into the shared resolver via canonical_definition_name."""
    assert PLUGIN.canonical_definition_name == "Attention"


def test_plugin_declares_the_loopback_server_permission():
    perm_ids = {p.id for p in PLUGIN.required_permissions}
    assert "network-loopback-server" in perm_ids


def test_attention_spec_shape():
    """ATTENTION_SPEC must declare a duration annotation with a full
    measurement_spec so the resolver can match existing definitions."""
    assert ATTENTION_SPEC["annotation_type"] == "duration"
    ms = ATTENTION_SPEC["measurement_spec"]
    assert ms["measurement_type"] == "duration"
    assert ms["value_type"] == "duration"
    assert "unit" in ms  # unit may be None — presence matters for _spec_matches


# ---------------------------------------------------------------------------
# run() — definition already in attention state (no resolver call needed)
# ---------------------------------------------------------------------------

def test_run_starts_and_stops_the_relay_server(monkeypatch, tmp_path):
    """run(ctx) builds the relay server and calls serve_forever when the
    attention definition is already cached in the attention state file."""
    server = _FakeServer()
    monkeypatch.setattr("fulcra_attention.collect_plugin.make_server",
                        lambda **kw: server)
    monkeypatch.setattr("fulcra_attention.collect_plugin._load_relay_config",
                        lambda ctx: {"bearer_token": "t", "port": 8771})

    class FakeState:
        attention_definition_id = "def-1"

    monkeypatch.setattr("fulcra_attention.collect_plugin.load_state",
                        lambda: FakeState())

    ctx = _make_ctx()
    PLUGIN.run(ctx)
    assert server.served is True


# ---------------------------------------------------------------------------
# run() — resolver path (no pre-existing attention_definition_id)
# ---------------------------------------------------------------------------

def test_run_uses_resolver_when_definition_not_bootstrapped(monkeypatch, tmp_path):
    """R5 regression: when attention_definition_id is absent from the attention
    state file, run() must call ctx.resolved_definition_id (the shared resolver
    path) rather than raise RuntimeError.

    The resolver is mocked at the RunContext level: we supply a
    _fulcra_client_factory whose client returns a known id. After run()
    completes, the attention state file must persist that id so subsequent
    relay lookups (and the old bootstrap check) find it."""
    server = _FakeServer()
    monkeypatch.setattr("fulcra_attention.collect_plugin.make_server",
                        lambda **kw: server)
    monkeypatch.setattr("fulcra_attention.collect_plugin._load_relay_config",
                        lambda ctx: {"bearer_token": "t", "port": 8771})

    # Attention state starts empty (no bootstrap)
    from fulcra_attention.state import State
    saved_states: list[State] = []

    attention_state = State()  # attention_definition_id is None
    monkeypatch.setattr("fulcra_attention.collect_plugin.load_state",
                        lambda: attention_state)
    monkeypatch.setattr("fulcra_attention.collect_plugin._state_save",
                        lambda s: saved_states.append(s))

    # Resolver fake: list_definitions returns nothing → create_definition called
    class _FakeClient:
        def __init__(self):
            self.list_calls: list = []
            self.create_calls: list = []

        def list_definitions(self, *, name: str) -> list:
            self.list_calls.append(name)
            return []

        def create_definition(self, *, name: str, **spec) -> dict:
            self.create_calls.append({"name": name, **spec})
            return {"id": "def-resolver-new"}

    fake_client = _FakeClient()

    # Give ctx.state a PluginState-like object so resolved_definition_id can
    # cache the id there (it writes ctx.state.definition_id).
    class _FakePluginState:
        definition_id: str | None = None

    ctx = RunContext(
        plugin_id="attention-relay",
        config={},
        credentials={},
        state=_FakePluginState(),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: fake_client,
    )
    PLUGIN.run(ctx)

    # Resolver was used
    assert fake_client.list_calls == ["Attention"]
    assert fake_client.create_calls[0]["name"] == "Attention"
    assert fake_client.create_calls[0]["annotation_type"] == "duration"

    # The id was written back to the attention state
    assert attention_state.attention_definition_id == "def-resolver-new"
    # The attention state was persisted
    assert len(saved_states) == 1
    assert saved_states[0].attention_definition_id == "def-resolver-new"

    # The relay server still ran
    assert server.served is True


def test_run_uses_resolver_only_once_when_state_has_id(monkeypatch, tmp_path):
    """When attention_definition_id is already in the attention state,
    the resolver must NOT be called — no network trip, no side effects."""
    server = _FakeServer()
    monkeypatch.setattr("fulcra_attention.collect_plugin.make_server",
                        lambda **kw: server)
    monkeypatch.setattr("fulcra_attention.collect_plugin._load_relay_config",
                        lambda ctx: {"bearer_token": "t", "port": 8771})

    from fulcra_attention.state import State
    attention_state = State(attention_definition_id="def-already")
    monkeypatch.setattr("fulcra_attention.collect_plugin.load_state",
                        lambda: attention_state)

    resolver_calls: list = []

    def _fake_resolver(spec, *, canonical_name):
        resolver_calls.append(canonical_name)
        return "should-not-be-returned"

    # Intercept resolved_definition_id at the RunContext level
    monkeypatch.setattr(RunContext, "resolved_definition_id", _fake_resolver)

    ctx = _make_ctx()
    PLUGIN.run(ctx)

    assert resolver_calls == [], "resolver must not be called when def id is already cached"
    assert server.served is True

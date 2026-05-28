"""The attention fulcra-collect plugin (manual sanity-check kind)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fulcra_attention.collect_plugin import ATTENTION_SPEC, PLUGIN
from fulcra_collect.plugin import RunContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(*, factory=None, emit_sink: list | None = None) -> RunContext:
    """Build a RunContext whose progress() pushes into `emit_sink`."""
    sink = emit_sink if emit_sink is not None else []
    return RunContext(
        plugin_id="attention-relay",
        config={},
        credentials={},
        state=None,
        log=logging.getLogger("t"),
        _emit=lambda e: sink.append(e),
        _fulcra_client_factory=factory,
    )


# ---------------------------------------------------------------------------
# Metadata tests
# ---------------------------------------------------------------------------

def test_plugin_metadata_is_manual_kind():
    """Phase 2: the plugin no longer runs a server; it's a manual
    sanity-check now. The daemon's /api/extension/attention route is
    what receives extension events."""
    assert PLUGIN.id == "attention-relay"
    assert PLUGIN.kind == "manual"
    assert PLUGIN.default_interval is None


def test_plugin_declares_canonical_definition_name():
    """The plugin still opts into the shared resolver via canonical_definition_name."""
    assert PLUGIN.canonical_definition_name == "Attention"


def test_plugin_no_longer_declares_loopback_server_permission():
    """The daemon's own HTTP port is what listens; the plugin needs no
    extra permission of its own."""
    perm_ids = {p.id for p in PLUGIN.required_permissions}
    assert "network-loopback-server" not in perm_ids


def test_plugin_requires_extension_token_credential():
    """The user-level extension-token is the only credential this plugin
    declares — paired with the daemon-side keychain entry the route
    checks."""
    keys = {c.key for c in PLUGIN.required_credentials}
    assert keys == {"extension-token"}


def test_attention_spec_shape():
    """ATTENTION_SPEC must declare a duration annotation with a full
    measurement_spec so the resolver can match existing definitions."""
    assert ATTENTION_SPEC["annotation_type"] == "duration"
    ms = ATTENTION_SPEC["measurement_spec"]
    assert ms["measurement_type"] == "duration"
    assert ms["value_type"] == "duration"
    assert "unit" in ms


def test_setup_steps_have_pair_step_and_avoid_old_relay_port():
    """The wizard's pairing step replaces the two-paste flow that used
    to surface the daemon URL. The user no longer needs to see the
    endpoint at all — the extension's content script handles it. We
    still guard against the old 8771 relay port leaking into copy."""
    kinds = [s.kind for s in PLUGIN.setup_steps]
    assert "extension_pair" in kinds
    # Old per-step inputs are gone — there should no longer be a
    # standalone "input" step asking the user to invent a token.
    assert "input" not in kinds
    blob = "\n".join(s.body_md for s in PLUGIN.setup_steps)
    # Old 8771 port should not appear in any user-facing copy.
    assert "8771" not in blob


# ---------------------------------------------------------------------------
# run() — sanity checks
# ---------------------------------------------------------------------------

def test_run_reports_missing_extension_token(monkeypatch, tmp_path):
    """When the extension-token isn't in the user keychain, the first
    progress event reports ok=False so the UI can flag it."""
    # Isolate state to tmp
    monkeypatch.setattr(
        "fulcra_attention.collect_plugin.DEFAULT_PATH", tmp_path / "state.json",
    )
    from fulcra_attention.state import State
    attention_state = State(attention_definition_id="def-1")
    monkeypatch.setattr(
        "fulcra_attention.collect_plugin.load_state", lambda: attention_state,
    )

    # No extension-token in keychain
    monkeypatch.setattr(
        "fulcra_collect.credentials.has_user_secret", lambda key: False,
    )

    sink: list = []
    ctx = _make_ctx(emit_sink=sink)
    PLUGIN.run(ctx)

    # First progress event is the extension_token check, ok=False
    token_check = next(e for e in sink if e.get("check") == "extension_token")
    assert token_check["ok"] is False
    assert "missing" in token_check["detail"]


def test_run_reports_definition_bound_when_state_has_id(monkeypatch, tmp_path):
    """When the attention state already has a definition_id, the check
    reports ok=True with the id."""
    from fulcra_attention.state import State

    attention_state = State(attention_definition_id="def-existing")
    monkeypatch.setattr(
        "fulcra_attention.collect_plugin.load_state", lambda: attention_state,
    )
    monkeypatch.setattr(
        "fulcra_collect.credentials.has_user_secret", lambda key: True,
    )

    sink: list = []
    ctx = _make_ctx(emit_sink=sink)
    PLUGIN.run(ctx)

    def_check = next(e for e in sink if e.get("check") == "definition_bound")
    assert def_check["ok"] is True
    assert "def-existing" in def_check["detail"]


def test_run_resolves_definition_when_state_empty(monkeypatch, tmp_path):
    """When the attention state file has no definition id, run() uses
    the shared resolver to adopt-or-create one, and persists the result."""
    from fulcra_attention.state import State

    saved: list = []
    attention_state = State()
    monkeypatch.setattr(
        "fulcra_attention.collect_plugin.load_state", lambda: attention_state,
    )
    monkeypatch.setattr(
        "fulcra_attention.collect_plugin._state_save",
        lambda s: saved.append(s),
    )
    monkeypatch.setattr(
        "fulcra_collect.credentials.has_user_secret", lambda key: True,
    )

    class _FakeClient:
        def list_definitions(self, *, name): return []
        def create_definition(self, *, name, **spec):
            return {"id": "def-resolver-new"}

    sink: list = []
    ctx = _make_ctx(factory=lambda: _FakeClient(), emit_sink=sink)

    # Give ctx.state a PluginState-shaped object — resolved_definition_id
    # writes ctx.state.definition_id.
    class _PluginState:
        definition_id: str | None = None
    ctx.state = _PluginState()

    PLUGIN.run(ctx)

    assert attention_state.attention_definition_id == "def-resolver-new"
    assert len(saved) == 1
    def_check = next(e for e in sink if e.get("check") == "definition_bound")
    assert def_check["ok"] is True


def test_run_reports_recent_activity_present(monkeypatch):
    """A watermark within the last 24h counts as recent activity."""
    from fulcra_attention.state import State

    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    attention_state = State(
        attention_definition_id="def-1",
        watermarks={"fulcra-attention-chrome/0.1.0": recent},
    )
    monkeypatch.setattr(
        "fulcra_attention.collect_plugin.load_state", lambda: attention_state,
    )
    monkeypatch.setattr(
        "fulcra_collect.credentials.has_user_secret", lambda key: True,
    )

    sink: list = []
    PLUGIN.run(_make_ctx(emit_sink=sink))
    rec_check = next(e for e in sink if e.get("check") == "recent_activity")
    assert rec_check["ok"] is True
    assert "fulcra-attention-chrome/0.1.0" in rec_check["detail"]


def test_run_reports_recent_activity_stale(monkeypatch):
    """All watermarks older than 24h → recent_activity check ok=False."""
    from fulcra_attention.state import State

    long_ago = "2024-01-01T00:00:00Z"
    attention_state = State(
        attention_definition_id="def-1",
        watermarks={"old-client": long_ago},
    )
    monkeypatch.setattr(
        "fulcra_attention.collect_plugin.load_state", lambda: attention_state,
    )
    monkeypatch.setattr(
        "fulcra_collect.credentials.has_user_secret", lambda key: True,
    )

    sink: list = []
    PLUGIN.run(_make_ctx(emit_sink=sink))
    rec_check = next(e for e in sink if e.get("check") == "recent_activity")
    assert rec_check["ok"] is False

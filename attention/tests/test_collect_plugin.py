"""The attention fulcra-collect plugin — now an informational pointer.

The browser extension is relayless (device-flow OIDC + direct Fulcra API
ingest); it no longer posts to the daemon. So this plugin no longer pairs,
binds a definition, or runs a relay sanity check. It exists only so Collect
still surfaces an "Attention" entry that points the user at the browser
extension.
"""
from __future__ import annotations

import logging

from fulcra_attention.collect_plugin import PLUGIN
from fulcra_collect.plugin import RunContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(*, emit_sink: list | None = None) -> RunContext:
    """Build a RunContext whose progress() pushes into `emit_sink`."""
    sink = emit_sink if emit_sink is not None else []
    return RunContext(
        plugin_id="attention-relay",
        config={},
        credentials={},
        state=None,
        log=logging.getLogger("t"),
        _emit=lambda e: sink.append(e),
    )


# ---------------------------------------------------------------------------
# Metadata tests
# ---------------------------------------------------------------------------

def test_plugin_is_registered_with_attention_id():
    """Collect still surfaces an Attention entry under the stable id."""
    assert PLUGIN.id == "attention-relay"
    assert PLUGIN.name == "Attention (browser extension)"
    assert PLUGIN.kind == "manual"
    assert PLUGIN.default_interval is None


def test_plugin_declares_no_credentials():
    """The relayless extension owns its own auth — the daemon holds no
    extension-token, so the pointer plugin declares no credentials."""
    assert PLUGIN.required_credentials == ()


def test_plugin_declares_no_setup_steps():
    """No pairing, no definition picker — the plugin is purely informational."""
    assert PLUGIN.setup_steps == ()


def test_plugin_declares_no_definition_binding():
    """The plugin no longer owns the Attention definition; the extension
    resolves its own def directly against Fulcra."""
    assert PLUGIN.canonical_definition_name is None


def test_description_points_at_the_browser_extension():
    """The description must steer the user to install the extension and sign
    in via the browser — and must not reference the dead relay machinery."""
    desc = PLUGIN.description.lower()
    assert "browser extension" in desc
    assert "attention/chrome" in PLUGIN.description
    # No residue of the retired relay/pairing machinery.
    assert "extension-token" not in desc
    assert "pair" not in desc
    assert "8771" not in PLUGIN.description


# ---------------------------------------------------------------------------
# run() — informational only
# ---------------------------------------------------------------------------

def test_run_emits_a_single_informational_message():
    """run() does no collection — it emits one ok=True progress event that
    tells the user to install the extension and sign in via the browser."""
    sink: list = []
    PLUGIN.run(_make_ctx(emit_sink=sink))

    assert len(sink) == 1
    event = sink[0]
    assert event["ok"] is True
    detail = event["detail"]
    assert "browser" in detail.lower()
    assert "attention/chrome/dist" in detail


def test_run_does_not_touch_credentials_or_fulcra():
    """The pointer run() must not reach for a Fulcra client or keychain —
    the RunContext here has no client factory, so any such access would
    raise. A clean run proves it's purely informational."""
    ctx = _make_ctx()
    # _fulcra_client_factory is None by default; a relay sanity check would
    # have called ctx.resolved_definition_id and blown up here.
    PLUGIN.run(ctx)

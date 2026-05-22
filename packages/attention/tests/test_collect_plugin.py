"""The attention-relay fulcra-collect plugin."""
from __future__ import annotations

from fulcra_attention.collect_plugin import PLUGIN


def test_plugin_metadata_is_a_service():
    assert PLUGIN.id == "attention-relay"
    assert PLUGIN.kind == "service"
    assert PLUGIN.default_interval is None


def test_plugin_declares_the_loopback_server_permission():
    perm_ids = {p.id for p in PLUGIN.required_permissions}
    assert "network-loopback-server" in perm_ids


def test_run_starts_and_stops_the_relay_server(monkeypatch, tmp_path):
    """run(ctx) builds the relay server and calls serve_forever. Stub
    make_server so the test doesn't actually bind a socket forever."""
    served = {}

    class FakeServer:
        def serve_forever(self):
            served["ran"] = True

    monkeypatch.setattr("fulcra_attention.collect_plugin.make_server",
                        lambda **kw: FakeServer())
    monkeypatch.setattr("fulcra_attention.collect_plugin._load_relay_config",
                        lambda ctx: {"bearer_token": "t", "port": 8771})

    class FakeState:
        attention_definition_id = "def-1"

    monkeypatch.setattr("fulcra_attention.collect_plugin.load_state",
                        lambda: FakeState())

    import logging
    from fulcra_collect.plugin import RunContext
    ctx = RunContext(plugin_id="attention-relay", config={}, credentials={},
                     state=None, log=logging.getLogger("t"), _emit=lambda e: None)
    PLUGIN.run(ctx)
    assert served["ran"] is True

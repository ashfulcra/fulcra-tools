"""fulcra-collect plugin: the attention relay as a supervised service.

run(ctx) builds the loopback relay HTTP server and serves it forever.
The hub's supervisor keeps this alive in a worker subprocess.
"""
from __future__ import annotations

from fulcra_collect.plugin import Credential, Permission, Plugin, RunContext

from .fulcra import FulcraClient
from .relay import ReceiverContext, make_server
from .state import DEFAULT_PATH
from .state import load as _state_load
from .state import save as _state_save

# The Fulcra annotation definition shape for the Attention DurationAnnotation.
# Passed to ctx.resolved_definition_id as the expected_spec so the shared
# resolver can verify an adopted definition has the right structure, or create
# a new one when none exists. Mirrors the payload produced by
# wire.duration_definition_payload (the bootstrap CLI path) — annotation_type
# and measurement_spec are the two axes that _spec_matches compares.
ATTENTION_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


def load_state():
    return _state_load(DEFAULT_PATH)


def _load_relay_config(ctx: RunContext) -> dict:
    """The relay's bearer token + port. The token is a hub credential;
    the port falls back to 8771 (the value the Chrome extension expects)."""
    return {
        "bearer_token": ctx.credentials.get("bearer-token") or "",
        "port": int(ctx.config.get("port", 8771)),
    }


def run(ctx: RunContext) -> None:
    cfg = _load_relay_config(ctx)
    state = load_state()
    if not state.attention_definition_id:
        # The definition was not pre-created by `fulcra-attention bootstrap`.
        # Use the shared resolver to adopt an existing "Attention" definition
        # on this account, or create one if none exists. This gives the same
        # multi-machine dedup guarantee that bootstrap provides, without
        # requiring bootstrap to have been run on every machine.
        def_id = ctx.resolved_definition_id(
            ATTENTION_SPEC,
            canonical_name="Attention",
        )
        state.attention_definition_id = def_id
        _state_save(state)
    client = FulcraClient()
    receiver = ReceiverContext(client=client, state=state,
                               bearer_token=cfg["bearer_token"])
    server = make_server(host="127.0.0.1", port=cfg["port"], context=receiver)
    ctx.log.info("attention relay listening on 127.0.0.1:%s", cfg["port"])
    server.serve_forever()


PLUGIN = Plugin(
    id="attention-relay",
    name="Attention relay",
    kind="service",
    run=run,
    description=(
        "Captures activity signals from this Mac (which apps you're using, when "
        "you're idle) and writes them to Fulcra. Needs a Fulcra bearer token."
    ),
    category="activity",
    canonical_definition_name="Attention",
    required_permissions=(
        Permission(id="network-loopback-server",
                   explanation="Runs a local server on 127.0.0.1:8771 that the "
                               "Fulcra Attention browser extension posts to."),
    ),
    required_credentials=(
        Credential(key="bearer-token", label="Relay bearer token",
                   help="The token the browser extension sends; from "
                        "~/.config/fulcra-attention/relay.json"),
    ),
)

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
        raise RuntimeError("attention not bootstrapped — run `fulcra-attention bootstrap`")
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

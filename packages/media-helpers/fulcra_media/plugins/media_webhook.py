"""Plex/Jellyfin webhook receiver — service plugin (long-running HTTP server)."""
from __future__ import annotations

from fulcra_collect.plugin import Credential, Permission, Plugin, RunContext, Setting, SetupStep

from .. import webhook_receiver
from ..fulcra import FulcraClient
from ..state import DEFAULT_PATH as STATE_PATH
from ..state import load as _state_load
from ..state import save as _state_save
from ._common import DURATION_SPEC, ensure_media_def


# Loopback addresses that are safe to bind without a bearer token.
# Matches the CLI's check (cli.py: `host != "127.0.0.1" and host != "localhost"`).
# Note: the CLI does not currently include "::1" in the guard; we match it exactly.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}

# Same structure as NETFLIX_WATCHED_SPEC and all other Watched plugins — they
# all share the same definition.
MEDIA_WEBHOOK_WATCHED_SPEC: dict = DURATION_SPEC


def _run_media_webhook(ctx: RunContext) -> None:
    """Long-running Plex/Jellyfin webhook receiver.

    Binds an HTTP server on host:port (default 127.0.0.1:8765) and serves
    forever.  Refuses to start on a non-loopback host without a bearer token,
    mirroring the `fulcra-media webhook` CLI's safety check.

    Resolves the "Watched" definition at startup (before the receive loop
    begins) so the service works standalone on a fresh machine that has never
    run `fulcra-attention bootstrap` or another Watched-producing plugin.
    """
    host: str = ctx.config.get("host", "127.0.0.1")
    port: int = int(ctx.config.get("port", 8765))
    bearer_token: str | None = ctx.credentials.get("bearer-token") or None

    # Non-loopback guard — mirrors cli.py's `webhook_serve` exactly:
    # refuse to bind a non-loopback address unless a bearer token is set.
    if host not in _LOOPBACK_HOSTS and not bearer_token:
        raise RuntimeError(
            f"media-webhook: host {host!r} is non-loopback; refusing to start "
            "without a bearer token. Set the 'bearer-token' credential "
            "(`fulcra-collect set-credential media-webhook bearer-token`) "
            "or bind on 127.0.0.1."
        )

    # Ensure the "Watched" annotation definition is known before entering the
    # receive loop.  On a fresh machine where the user only enables media-webhook
    # (no fulcra-attention bootstrap, no other Watched-producing plugin) the
    # media state has no watched_definition_id, so the service couldn't start.
    # The shared resolver adopts Machine 1's existing "Watched" definition rather
    # than creating a duplicate — the same multi-machine guarantee every other
    # Watched plugin gets.  After a supervisor restart the cached state makes
    # this call fast (no network round-trip needed).
    media_state = _state_load(STATE_PATH)
    ensure_media_def(ctx, media_state, attr="watched_definition_id",
                     spec=MEDIA_WEBHOOK_WATCHED_SPEC,
                     canonical_name="Watched",
                     state_save=_state_save)

    client = FulcraClient()
    server = webhook_receiver.make_server(
        host=host,
        port=port,
        state=media_state,
        client=client,
        bearer_token=bearer_token,
        log_stream=None,
    )
    ctx.log.info("media webhook receiver listening on %s:%s", host, port)
    server.serve_forever()


PLUGIN = Plugin(
    id="media-webhook",
    name="Plex/Jellyfin webhook receiver",
    kind="service",
    run=_run_media_webhook,
    description=(
        "Captures what you watch on Plex or Jellyfin by running a tiny "
        "local HTTP server that your media server POSTs playback events "
        "to. Runs continuously as a service — one annotation per session. "
        "Plex Pass is required for Plex webhooks; Jellyfin works on any tier."
    ),
    category="video",
    canonical_definition_name="Watched",
    required_permissions=(
        Permission(
            id="network-loopback-server",
            explanation=(
                "Runs a local HTTP server (default 127.0.0.1:8765) that "
                "Plex/Jellyfin POST playback webhooks to."
            ),
        ),
    ),
    required_credentials=(
        Credential(
            key="bearer-token",
            label="Webhook bearer token",
            help=(
                "Required when Plex/Jellyfin runs on a different machine "
                "than the daemon (host = 0.0.0.0). Plex doesn't send "
                "Authorization headers, so the receiver also accepts the "
                "token via `?token=...` on the webhook URL. Leave empty "
                "for the loopback-only setup (host = 127.0.0.1)."
            ),
        ),
    ),
    required_settings=(
        Setting(
            key="host",
            label="Bind address",
            kind="text",
            default="127.0.0.1",
            help=(
                "127.0.0.1 = same machine only (Plex/Jellyfin on this Mac). "
                "0.0.0.0 = accept connections from other machines on your "
                "network (requires the bearer token below)."
            ),
        ),
        # Wizard-only navigation hint. _run_media_webhook ignores this; it
        # exists purely so the conditional setup_steps below can branch on
        # the user's topology choice. required=False because once setup is
        # complete the daemon doesn't need it; we also default to "same"
        # so the wizard preselects the most common option.
        Setting(
            key="setup_topology",
            label="Where does Plex/Jellyfin run?",
            kind="enum",
            enum_values=("same", "lan"),
            enum_labels=(
                "On this same Mac",
                "On a different machine on my network",
            ),
            default="same",
            required=False,
            help=(
                "Choose 'same' if Plex/Jellyfin is on this Mac (loopback "
                "is enough). Choose 'lan' if your media server is on "
                "another box and needs to reach this Mac over the LAN — "
                "we'll walk you through binding to 0.0.0.0 and setting a "
                "bearer token."
            ),
        ),
    ),
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What this plugin does",
            body_md=(
                "media-webhook is a tiny HTTP server. Configure Plex or "
                "Jellyfin to POST playback events to it; we write a "
                "'Watched' annotation per session. Plex Pass is required "
                "to use Plex webhooks; Jellyfin webhooks need no paid tier."
            ),
        ),
        SetupStep(
            kind="permission_request",
            title="Allow a local webhook server",
            body_md=(
                "We'll bind a local HTTP server on port 8765 (default "
                "`127.0.0.1`, or `0.0.0.0` if you're configuring this "
                "from a Plex/Jellyfin server on another machine). The "
                "next step lets you pick which."
            ),
        ),
        SetupStep(
            kind="input",
            title="Where does Plex/Jellyfin run?",
            body_md=(
                "Pick **On this same Mac** if Plex or Jellyfin is "
                "installed locally. Pick **On a different machine on my "
                "network** if your media server runs on another box (a "
                "NAS, a separate desktop, a home server) and will POST "
                "events to this Mac over your LAN."
            ),
            settings_keys=("setup_topology",),
        ),
        SetupStep(
            kind="input",
            title="Bind address and bearer token",
            body_md=(
                "For LAN mode we bind to `0.0.0.0` so other machines on "
                "your network can reach the receiver. A **bearer token** "
                "is required — it's the only thing standing between "
                "anyone on your LAN and your Fulcra account. Paste your "
                "own random string (32+ characters recommended) or let "
                "the field stay blank and generate one with a password "
                "manager. **Save this token** — you'll paste it into the "
                "webhook URL in the next step."
            ),
            settings_keys=("host", "bearer-token"),
            condition={"setup_topology": ("lan",)},
        ),
        SetupStep(
            kind="external_action",
            title="Wire up your media server",
            body_md=(
                "**Plex:** open the **Plex Web app while signed in as the "
                "server's admin account** (this is the account that owns "
                "the server, not just any account with access). Go to "
                "**Settings -> the SERVER name (NOT 'Your Account') -> "
                "Webhooks -> Add Webhook**, enter "
                "`http://127.0.0.1:8765/webhook`, and click **Save**. "
                "Webhooks are a server-side setting — they live under your "
                "server's settings page, not your account's. **Plex Pass is "
                "required for this feature.**\n\n"
                "**Jellyfin:** open **Dashboard -> Plugins -> Webhook -> "
                "Add Generic Destination**, enter the same URL, and save."
            ),
            condition={"setup_topology": ("same",)},
        ),
        SetupStep(
            kind="external_action",
            title="Wire up your media server (cross-machine)",
            body_md=(
                "**You're using cross-machine mode**, so we need two "
                "extra pieces:\n\n"
                "1. Find this Mac's LAN IP — **System Settings -> "
                "Wi-Fi/Network -> Details -> IP Address**. It's probably "
                "`192.168.X.X` or `10.X.X.X`.\n"
                "2. In Plex/Jellyfin, set the webhook URL to:\n\n"
                "   `http://<this-mac-LAN-IP>:8765/webhook?token=<the-bearer-token-you-set-above>`\n\n"
                "   Example: `http://192.168.1.42:8765/webhook?token=abc123...`\n\n"
                "**Plex:** sign into the Plex Web app as your server's "
                "**admin account** (the one that owns the server), then "
                "**Settings -> the SERVER name (NOT 'Your Account') -> "
                "Webhooks -> Add Webhook**. Webhooks are a server-side "
                "feature — they live under your server's settings page, "
                "not your account's. **Plex Pass is required.**\n\n"
                "**Jellyfin:** Dashboard -> Plugins -> Webhook -> Add "
                "Generic Destination.\n\n"
                "The `?token=...` is how Plex authenticates to your "
                "daemon — Plex doesn't natively send Authorization "
                "headers. **Anyone on your LAN who knows this token can "
                "post events to your Fulcra account**, so keep it secret; "
                "rotate it via **Configure** if it leaks."
            ),
            condition={"setup_topology": ("lan",)},
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write your watches?",
            body_md=(
                "We can write to your existing 'Watched' annotation or "
                "create a new one."
            ),
            annotation_type="duration",
        ),
        SetupStep(
            kind="done",
            title="You're set",
            body_md=(
                "media-webhook will run as a service — restarting the "
                "daemon restarts it. Trigger a playback in Plex/Jellyfin "
                "to see it record."
            ),
        ),
    ),
)

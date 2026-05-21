"""Jellyfin webhook setup walkthrough."""

from __future__ import annotations

import click


JELLYFIN_STEPS = """\
Jellyfin webhook setup

  Jellyfin's first-party `jellyfin-plugin-webhook` POSTs JSON to any
  endpoint on user-defined events. This helper runs a small HTTP
  server that translates `PlaybackStop` events into Fulcra Watched
  annotations.

  Setup:

  1. Run bootstrap first (if you haven't):
       fulcra-media bootstrap

  2. Install the webhook plugin in Jellyfin:
     Dashboard → Plugins → Catalog → Notifications → "Webhook"
     Install and restart Jellyfin.

  3. Pick where the receiver will run.
     - Same machine as Jellyfin / browser: defaults are fine. The
       webhook URL is http://127.0.0.1:8765/webhook.
     - Different machine: bind on 0.0.0.0 and mint a bearer token:
         openssl rand -hex 32

  4. Start the receiver (it runs until Ctrl-C):
       fulcra-media webhook --host 0.0.0.0 --port 8765 --bearer-token <hex>
     For pure local-only setups:
       fulcra-media webhook

  5. In Jellyfin Dashboard → Plugins → Webhook → Add Generic:
       Webhook Url: http://<this-machine-ip>:8765/webhook
       Notification Type: Playback Stop
       Request Headers: Authorization: Bearer <hex>     (if you set a token)
       Content Type: application/json

       Template: use the plugin's default Generic template, OR ensure
       the JSON body includes at minimum:
         {
           "Event": "{{NotificationType}}",
           "Item": {{Item}},
           "User": {{User}},
           "Server": {{Server}},
           "Session": {{Session}},
           "PlaybackPositionTicks": {{PlaybackPositionTicks}},
           "Date": "{{Date}}"
         }
       (The receiver tolerates extra fields, so you can keep the
       plugin's defaults.)

  6. Save. Watch something to ≥75% completion. The receiver will
     ingest one Fulcra Watched event per playback that crossed that
     threshold (early bails return 204 with no ingest).

  7. Verify:
       curl http://127.0.0.1:8765/health

  TLS:
  - The server is HTTP-only. For internet-facing exposure, front it
    with caddy/nginx terminating TLS. The bearer token flows through
    unchanged.

  Notes:
  - Jellyfin's plugin supports custom headers; prefer the
    Authorization header over a query string for token transport.
  - The receiver dispatches by Content-Type (application/json →
    Jellyfin path), so this won't collide with a Plex hook on the same
    endpoint if you happen to run both.
"""


@click.command("walkthrough")
def walkthrough() -> None:
    """Interactive walkthrough for setting up the Jellyfin webhook receiver."""
    click.echo(JELLYFIN_STEPS)

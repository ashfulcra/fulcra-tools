"""Plex webhook setup walkthrough."""

from __future__ import annotations

import click


PLEX_STEPS = """\
Plex webhook setup

  Plex's webhook fires once per play event. This helper runs a small
  HTTP server locally; you point Plex at it, and every scrobble shows
  up in Fulcra as a Watched event.

  Webhook eligibility:
  - Plex Pass subscribers get native webhooks for free.
  - Non-Pass users: install Tautulli (https://tautulli.com), enable its
    "Webhook" notification agent, and point it at the same URL we set
    up below. Tautulli ships the same five events that Plex Pass does.

  Setup:

  1. Run bootstrap first (if you haven't):
       fulcra-media bootstrap

  2. Pick where the server will run.
     - Same machine as your Plex client / browser: leave defaults
       (--host 127.0.0.1). The webhook URL is http://127.0.0.1:8765/webhook.
       Plex Media Server runs on a different machine? You'll need to bind
       on a network-reachable IP (--host 0.0.0.0) and also a bearer token.

  3. If exposing on a network, mint a bearer token:
       openssl rand -hex 32

  4. Start the receiver (it runs until Ctrl-C):
       fulcra-media webhook --host 0.0.0.0 --port 8765 --bearer-token <hex>
     For pure local-only setups:
       fulcra-media webhook

  5. In Plex Web (or your Plex account):
     Settings → Account → Webhooks → Add Webhook
       URL: http://<this-machine-ip>:8765/webhook?token=<hex>
     Plex's webhook config doesn't allow custom headers, so we accept
     the token as a query string fallback. (Header form works too if
     you wire it via Tautulli's webhook agent, which supports headers.)

  6. Watch something. Plex sends `media.play`, `media.pause`,
     `media.resume`, `media.stop`, and `media.scrobble`. Only
     `media.scrobble` (fires at 90% playback) actually creates a
     Fulcra event — the rest return 204 No Content.

  7. Verify with the health endpoint:
       curl http://127.0.0.1:8765/health
     Should return {"ok": true, ...} with received + posted counters.

  TLS:
  - The server doesn't do HTTPS. For internet-facing exposure use a
    reverse proxy (caddy/nginx) terminating TLS and forwarding to this
    HTTP endpoint. The bearer token still flows through unchanged.

  Service compatibility:
  - Works with stock Plex Media Server and with Tautulli's webhook agent.
  - Plex sends the payload as multipart/form-data with a JSON `payload`
    field; Tautulli emits the same shape, configured via its template.
"""


@click.command("walkthrough")
def walkthrough() -> None:
    """Interactive walkthrough for setting up the Plex webhook receiver."""
    click.echo(PLEX_STEPS)

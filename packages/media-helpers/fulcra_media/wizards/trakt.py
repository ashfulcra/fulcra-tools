"""Trakt setup walkthrough."""

from __future__ import annotations

import click


TRAKT_STEPS = """\
Trakt API setup
  Reference: https://trakt.docs.apiary.io/

  Trakt aggregates watch history across many streaming services. Once
  connected, it backfills history from supported services (Apple TV+,
  Netflix, Disney+, Hulu, Prime Video, Max, Paramount+) automatically.

  1. If you don't have an account, sign up at https://trakt.tv.
  2. Trakt VIP enables the 'Streaming Scrobbler' which syncs streaming-
     service watches in real time. Highly recommended for ongoing capture.
  3. Get your free Trakt API client credentials at https://trakt.tv/oauth/applications/new
     - Name: anything (e.g. "fulcra-media-helpers")
     - Redirect URI: urn:ietf:wg:oauth:2.0:oob
  4. Save your client_id and client_secret to:
       ~/.config/fulcra-media/trakt.json
     as JSON: {"client_id": "...", "client_secret": "..."}

  5. Run the device-flow login to grant your personal account access:
       fulcra-media auth trakt    (coming soon - for now use the
       Python script in the project's scratch dir or the README)

  6. Once authenticated, import:
       fulcra-media import trakt

  Known quirk: when you connect a streaming service to Trakt, Trakt may
  backfill that service's history with the connection-day timestamp
  rather than the real watch time. The importer detects clusters
  (>=5 items sharing watched_at) and flags them timestamp_confidence: low
  so consumers can filter or weight accordingly.
"""


@click.command("walkthrough")
def walkthrough() -> None:
    """Interactive walkthrough for setting up Trakt."""
    click.echo(TRAKT_STEPS)

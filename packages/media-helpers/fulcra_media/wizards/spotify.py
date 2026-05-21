"""Spotify Extended Streaming History setup walkthrough."""

from __future__ import annotations

import click


SPOTIFY_STEPS = """\
Spotify Extended Streaming History setup
  Reference: https://www.spotify.com/account/privacy/

  Spotify offers two data exports - request the *Extended* version, NOT
  the standard 'Account data' export. The standard one only covers the
  last 12 months; Extended covers your entire account lifetime.

  1. Open https://www.spotify.com/account/privacy in a browser.
  2. Scroll to the section that mentions 'extended streaming history'.
  3. Click 'Request' next to 'Extended streaming history'.
  4. Confirm via the email Spotify sends.
  5. Wait - Spotify states up to 30 days; usually 1-5 days in practice.
  6. Download the ZIP from the email link when ready.
  7. Import:
       fulcra-media import spotify-extended /path/to/my_spotify_data.zip
     Or, if uploaded to your Fulcra Library:
       fulcra-media import spotify-extended fulcra:/takeouts/spotify-2026.zip

  The importer reads Streaming_History_Audio_*.json files inside the zip,
  filters streams with ms_played < 30000 (skipped/too-short), filters
  rows flagged as skipped, and emits Listened events for music tracks
  (master_metadata_track_name) and podcast episodes (episode_name).
  timestamp_confidence: high - these are real stream events with real
  ms_played durations.

  Note: the Web API /me/player/recently-played endpoint is capped at
  the most recent 50 items per request with no cursor to reach further
  back - so the GDPR Extended export is the only way to capture full
  history. For ongoing capture, Last.fm scrobbling is recommended (a
  future importer).
"""


@click.command("walkthrough")
def walkthrough() -> None:
    click.echo(SPOTIFY_STEPS)

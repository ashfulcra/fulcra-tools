"""Spotify IFTTT->GDrive legacy importer walkthrough."""

from __future__ import annotations

import click


SPOTIFY_IFTTT_STEPS = """\
Spotify IFTTT -> Google Drive (legacy applet)

  Years ago, IFTTT offered a 'New track played by you on Spotify'
  trigger that could append rows to a Google Sheets spreadsheet via
  Google Drive. If you set one (or two!) of these up well before
  Spotify made Extended Streaming History a thing, you have years of
  historical play data in your Drive that pre-dates the Extended
  export's window.

  Common applets emit a 5-column row per play, no header:
    timestamp_str, track_name, artist, spotify_track_id, spotify_url

  The timestamp is rendered in your IFTTT account's timezone
  ('November 4, 2022 at 03:53PM') — NOT UTC. Pass --tz when you import
  if your IFTTT account is set to a non-UTC zone.

  1. Open https://drive.google.com and find the Spotify folder (or
     wherever your IFTTT applets wrote to — often 'IFTTT/Spotify/').
  2. Right-click the folder and choose 'Download' to get a zip.
  3. Import:
       fulcra-media import spotify-ifttt /path/to/spotify_ifttt.zip
       # or with a non-UTC IFTTT timezone:
       fulcra-media import spotify-ifttt /path/to/spotify_ifttt.zip \\
           --tz America/New_York

  If you ran two applets in parallel (e.g. 'Recent tracks' and
  'Spotify Tracks V2'), the importer dedupes exact matches on
  (track_id, timestamp). Replays of the same track at different times
  are preserved as separate events.

  timestamp_confidence: medium - IFTTT polled Spotify's
  /me/player/recently-played, which returns Spotify's actual played_at
  timestamp. The 'medium' rating reflects two realities: (a) polling
  cadence can miss plays if the user listened to more than ~50 tracks
  between polls, and (b) IFTTT's wall-clock recording introduces a
  small but bounded skew vs. the canonical Spotify timestamp.

  For ongoing capture (post-export), prefer the Spotify Extended
  GDPR export ('import spotify-extended') or Last.fm scrobbling.
  This importer is for backfilling the pre-Extended era.
"""


@click.command("walkthrough")
def walkthrough() -> None:
    click.echo(SPOTIFY_IFTTT_STEPS)

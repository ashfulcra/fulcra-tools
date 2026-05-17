"""Pipedream-to-CSV ongoing-capture walkthrough."""

from __future__ import annotations

import click


PIPEDREAM_STEPS = """\
Capturing media via Pipedream (ongoing, 1-min cadence)

  Pipedream (pipedream.com) is a code-first automation platform that
  pairs well with this tool when IFTTT's 5-15 min polling isn't tight
  enough. Workflows can run on a 1-minute schedule and write rows to
  Google Sheets, Dropbox CSV, S3, or any HTTP endpoint.

  Why pick Pipedream over IFTTT:
    - 1-minute schedules instead of 5-15 minutes (catches more plays).
    - First-class OAuth connections for Spotify, Last.fm, YouTube,
      Plex, Trakt, Letterboxd, GitHub, etc.
    - You write the row format in JS/Python — no string-template
      escaping headaches.
    - Free tier: 10k invocations/month, generous for personal use.

  Recommended workflow shape (Spotify example):

    Trigger:  Schedule -> every 1 minute
    Step 1:   Spotify -> Get Recently Played Tracks (limit=50)
    Step 2:   Code step: deduplicate against last-seen played_at,
              build rows with shape:
                 timestamp,track,artist,track_id,duration_ms
    Step 3:   Google Sheets -> Append Row(s) (or Dropbox upload CSV)

  Dedup state: store the most-recent played_at in Pipedream's
  data store between runs. On each tick, fetch /me/player/recently-played,
  drop rows older than the stored cursor, append the rest, save the new
  cursor. This avoids the per-poll re-appending problem IFTTT has.

  Importing the CSV (any service that produces a CSV with a timestamp +
  title column works):

    fulcra-media import generic-csv \\
      --service <tag> --category <listened|watched> \\
      --ts-col timestamp --title-col track --subtitle-col artist \\
      --id-col track_id --duration-col duration_ms \\
      --tz UTC \\
      /path/to/spotify_capture.csv

  The importer hashes (timestamp, title, tag, id) per row so re-importing
  the same CSV is safe — duplicates are detected and skipped.

  Other useful Pipedream sources:
    - Last.fm 'user.getRecentTracks' for historical music scrobbles
    - YouTube 'liked videos' polling (no IFTTT trigger drift)
    - Trakt 'sync/history' for cleaner timestamps than Trakt's web export
    - Plex webhooks landing in Pipedream HTTP triggers, written to CSV

  See pipedream.com/apps for the full integration list.
"""


@click.command("walkthrough")
def walkthrough() -> None:
    click.echo(PIPEDREAM_STEPS)

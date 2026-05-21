"""Apple Data & Privacy takeout setup walkthrough."""

from __future__ import annotations

import click


APPLE_TAKEOUT_STEPS = """\
Apple Data & Privacy takeout setup
  Reference: https://privacy.apple.com

  Apple includes Apple TV viewing history in their full data export.

  1. Open https://privacy.apple.com in a browser, sign in.
  2. Click 'Get a copy of your data'.
  3. Select 'Apple Media Services information' (you can skip the rest
     to make the export smaller).
  4. Submit the request. Apple states delivery may take up to 7 days
     (typically 1-3 days in practice).
  5. When the email arrives, download the .zip.
  6. The file you need is inside:
       Apple Media Services information/Apple TV/Playback Activity.csv
  7. Import either the CSV directly or the unzipped export tree:
       fulcra-media import apple-takeout /path/to/apple_data_export/
       fulcra-media import apple-takeout /path/to/Playback Activity.csv
     Or via Fulcra Library:
       fulcra-media import apple-takeout fulcra:/takeouts/apple-2026.zip

  The CSV schema: Event Type, Content Type, Title, Episode Title,
  Season Number, Episode Number, Start Time, End Time, Play Duration,
  Device Type, Device Model, Country. We filter to Event Type = PLAY
  (drop PAUSE/RESUME sub-events). Real UTC start/end times,
  timestamp_confidence: high.
"""


@click.command("walkthrough")
def walkthrough() -> None:
    click.echo(APPLE_TAKEOUT_STEPS)

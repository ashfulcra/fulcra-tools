"""IFTTT-to-CSV ongoing-capture walkthrough (service-agnostic)."""

from __future__ import annotations

import click


IFTTT_STEPS = """\
Capturing media via IFTTT -> Google Drive (ongoing)

  IFTTT (ifttt.com) is the easiest no-code way to keep a rolling CSV
  of plays/listens/watches that this importer can ingest periodically.

  Caveats up front:
    - IFTTT polls source services every 5-15 minutes. Heavy listeners
      may lose plays between polls (Spotify's recently-played window
      is the most-recent 50 tracks).
    - The free tier limits the number of active Applets.
    - Spreadsheet writes append to a Google Sheets file in your Drive.
      Each file caps at ~2,000 rows; IFTTT rolls a new file
      ('Recent tracks (1).xlsx', '(2).xlsx', ...) automatically.

  Pattern that works well:

    1. Sign in at https://ifttt.com.
    2. Create a new Applet:
         IF: <service trigger>
           e.g. 'Spotify - New track played by you'
                'YouTube - New liked video'
                'Reddit - New post upvoted by you'
                'Instapaper - New archived item'
       THEN: 'Google Drive - Add row to spreadsheet'
    3. Configure the spreadsheet:
         Folder: IFTTT/<service>/
         File: <service> tracks
         Formatted row: include AT LEAST a timestamp and a title.
           Spotify ex: {{TrackName}} ||| {{ArtistName}} ||| {{TrackUrl}}
           Recommended: prepend {{OccurredAt}} so we have a clean ts.
    4. Activate the Applet. Let it accumulate data.

  When you're ready to import:

    1. Open Google Drive, find IFTTT/<service>/, right-click the folder
       and choose 'Download' to get a zip.
    2. Extract the spreadsheet(s) and convert each to CSV (File ->
       Download -> Comma-separated values, or use `ssconvert` /
       `xlsx2csv` locally).
    3. Import:
         fulcra-media import generic-csv \\
           --service <tag> --category listened \\
           --ts-col OccurredAt --title-col TrackName \\
           --subtitle-col ArtistName \\
           --tz America/New_York \\
           ./<service>_export.csv

  Existing dedicated importers cover specific IFTTT outputs:
    - Legacy Spotify -> IFTTT -> Google Drive (xlsx, multi-file):
        fulcra-media import spotify-ifttt <zip>
        (handles the dual-applet overlap natively)

  For tighter polling and arbitrary services, consider Pipedream:
    fulcra-media wizard pipedream
"""


@click.command("walkthrough")
def walkthrough() -> None:
    click.echo(IFTTT_STEPS)

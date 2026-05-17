"""YouTube watch-history setup walkthrough."""

from __future__ import annotations

import click


YOUTUBE_STEPS = """\
YouTube watch-history setup (Google Takeout)

  Google's YouTube Data API explicitly disallows reading your watch-history
  playlist, so the only legitimate pathway is Google Takeout.

  Takeout supports SCHEDULED exports (every 2 months, up to a year), so
  once configured this works for ongoing capture too — not just one-shot.

  Setup:
  1. Visit https://takeout.google.com
  2. Click 'Deselect all'.
  3. Find 'YouTube and YouTube Music', tick it.
  4. Click 'All YouTube data included' → deselect everything except 'history'.
  5. Click 'Multiple formats' → set History to 'JSON'.
  6. Click 'Next step'.
  7. Choose delivery: 'Send download link via email' (one-shot) OR
     'Add to Drive' with 'Export type: Frequency: Every 2 months for 1 year'
     (recurring).
  8. Choose 'File type: .zip', 'File size: 2 GB' (probably fine for history).
  9. 'Create export'. Wait for Google to email you (usually <30 min).

  Importing the JSON:
  10. Download the zip, unzip it, find:
        Takeout/YouTube and YouTube Music/history/watch-history.json
  11. Run:
        fulcra-media import youtube path/to/watch-history.json

  Caveats:
  - YouTube watch history must be enabled at https://myactivity.google.com
    (it's now opt-out for some accounts created since 2023).
  - Takeout data does NOT include watch duration or how far you got into a
    video — just title + timestamp. The importer emits a 1-second sentinel
    duration.
  - Removed/private videos appear with their title but no URL. They still
    get imported (with title) so the count matches.
  - Searches, likes, and subscriptions are separate entries with different
    `header` / prefix values; only 'Watched <X>' entries become events.
"""


@click.command("walkthrough")
def walkthrough() -> None:
    click.echo(YOUTUBE_STEPS)

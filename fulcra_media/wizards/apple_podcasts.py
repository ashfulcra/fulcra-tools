"""Apple Podcasts setup walkthrough."""

from __future__ import annotations

import click


APPLE_PODCASTS_STEPS = """\
Apple Podcasts setup (macOS only)

  Apple Podcasts syncs play state across devices via iCloud, with the
  on-disk database at:
    ~/Library/Group Containers/243LU875E5.groups.com.apple.podcasts/Documents/MTLibrary.sqlite

  The importer reads this database directly - no API, no export request.

  1. Make sure the Mac Podcasts app is installed and signed into your
     Apple ID with 'Sync Library' enabled (so iPhone/CarPlay listens
     also show up).
  2. Run:
       fulcra-media import apple-podcasts

  The importer captures only *completed* episodes - ZPLAYSTATE=3 (played)
  with playhead/duration > 0.9 and ZPLAYSTATEMANUALLYSET=0. Manual
  marked-as-played is filtered out.

  Known fragility:
  - Auto-delete-after-played removes the row entirely -> history is lost
  - Replays between importer runs collapse into one event (the DB stores
    only ZLASTDATEPLAYED, not a per-play log) - run frequently (hourly
    launchd) to minimize collapse
  - Episodes from unfollowed shows get pruned over time
"""


@click.command("walkthrough")
def walkthrough() -> None:
    """Interactive walkthrough for Apple Podcasts setup."""
    click.echo(APPLE_PODCASTS_STEPS)

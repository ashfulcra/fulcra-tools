"""Apple TV app on-device setup walkthrough."""

from __future__ import annotations

import click


APPLE_TV_STEPS = """\
Apple TV app setup (macOS only)

  The Apple TV app keeps a local Watch Now cache with Up Next progress
  and a Recently Watched shelf. The Fulcra Collect apple-tv plugin reads
  that cache read-only - no Apple sign-in, data export, Full Disk Access,
  or network call is required for the local scan.

  Recommended setup:

  1. Open the Apple TV app once and visit the Home tab so macOS creates
     and refreshes the Watch Now cache.
  2. In the Fulcra Collect dashboard, enable the "Apple TV app
     (on-device)" plugin.
  3. Run the plugin health check. It should count parseable watch events
     or explain that the cache is missing.
  4. Pick your Watched annotation definition in the setup flow.

  Once enabled, the plugin syncs every 6 hours. In-progress items carry
  exact activity times; Recently Watched items use approximate
  low-confidence timestamps and automatically defer to more precise
  sources such as Trakt when both exist.
"""


@click.command("walkthrough")
def walkthrough() -> None:
    """Interactive walkthrough for Apple TV app setup."""
    click.echo(APPLE_TV_STEPS)

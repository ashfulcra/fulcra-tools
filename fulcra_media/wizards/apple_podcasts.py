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

  Going beyond the current snapshot:

  1. Hourly capture via launchd (recommended). Save this to
     ~/Library/LaunchAgents/com.fulcradynamics.media-podcasts.plist
     then `launchctl load <plist>`:

       <?xml version="1.0" encoding="UTF-8"?>
       <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
       <plist version="1.0">
       <dict>
         <key>Label</key><string>com.fulcradynamics.media-podcasts</string>
         <key>ProgramArguments</key>
         <array>
           <string>/path/to/.venv/bin/fulcra-media</string>
           <string>import</string>
           <string>apple-podcasts</string>
         </array>
         <key>StartInterval</key><integer>3600</integer>
         <key>RunAtLoad</key><true/>
       </dict>
       </plist>

  2. Recover history from Time Machine backups (one-shot):

       fulcra-media import apple-podcasts-timemachine

     This walks every Time Machine backup with a MTLibrary.sqlite and
     posts annotations for every historical ZLASTDATEPLAYED found.
     Idempotency means re-runs are safe.

  ZPLAYCOUNT is also captured as external_ids.play_count so consumers
  can see how many total plays an episode has had (the snapshot only
  knows the most recent play's timestamp).
"""


@click.command("walkthrough")
def walkthrough() -> None:
    """Interactive walkthrough for Apple Podcasts setup."""
    click.echo(APPLE_PODCASTS_STEPS)

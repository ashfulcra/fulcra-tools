"""Last.fm setup walkthrough."""

from __future__ import annotations

import click


LASTFM_STEPS = """\
Last.fm setup

  Last.fm is the canonical 'recently played' aggregator. If you've ever
  enabled Last.fm scrobbling in Spotify, Apple Music, Tidal, YouTube
  Music (via Web Scrobbler), Amazon Music, SoundCloud, or Pandora —
  your full play history is sitting there waiting.

  Why Last.fm over the underlying service's own API:
  - One credential covers many services (you set it up once)
  - Auth is just username + free API key — no OAuth dance
  - Scrobbles never expire from your history (vs. Spotify's 50-track
    'recently played' window)

  Setup:
  1. Visit https://www.last.fm/api/account/create
  2. Fill in:
       Application name: fulcra-media-helpers
       Application description: personal media import
  3. Copy the API key it shows (32 hex chars).
  4. Save credentials:
       mkdir -p ~/.config/fulcra-media
       cat > ~/.config/fulcra-media/lastfm.json <<'EOF'
       {"username": "<your-lastfm-username>", "api_key": "<the-key>"}
       EOF
       chmod 600 ~/.config/fulcra-media/lastfm.json

  5. Run:
       fulcra-media import lastfm

  Caveats:
  - Reads PUBLIC scrobbles only. If your Last.fm profile is private,
    flip 'Allow others to see what music I'm listening to' under your
    privacy settings, or wait for a future OAuth path.
  - First run pulls full history; can be tens of thousands of scrobbles.
    Use --max-pages N to cap, or --since YYYY-MM-DD to start at a date.
  - Subsequent runs are incremental — watermark stored in state.json.
    The poller fetches `from=<watermark - 1 hour>` to catch any late
    server-side reordering of recent scrobbles. Source-id dedup
    handles the overlap.

  Side benefit for ongoing capture across many services:
  - Tidal has built-in Last.fm scrobbling in its settings.
  - Amazon Music / SoundCloud / Pandora / YouTube Music: install the
    'Web Scrobbler' browser extension; one OAuth login to Last.fm
    covers all of them simultaneously.
"""


@click.command("walkthrough")
def walkthrough() -> None:
    click.echo(LASTFM_STEPS)

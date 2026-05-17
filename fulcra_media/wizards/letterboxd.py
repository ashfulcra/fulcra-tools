"""Letterboxd setup walkthrough."""

from __future__ import annotations

import click


LETTERBOXD_STEPS = """\
Letterboxd setup

  Letterboxd's official API (api-docs.letterboxd.com) is closed beta —
  applications are reviewed manually and explicitly NOT granted for
  "private or personal projects." So we don't go through the API.

  Every public profile, though, exposes a stable RSS feed of the user's
  diary entries at https://letterboxd.com/<username>/rss/. The feed
  includes title, watch date, member rating, rewatch flag, and the
  film's title + year — everything we need to build a Watched event with
  a movie content_fingerprint usable for cross-source dedup.

  Setup:
  1. Pick the Letterboxd username whose diary you want to import. This
     is the only "credential" — no key, no OAuth.
  2. Make sure that profile's diary is public (the default — only matters
     if you flipped to private).
  3. Run:
       fulcra-media import letterboxd --username <your-letterboxd-handle>

  Caveats:
  - The RSS feed exposes recent entries only (typically the last ~50).
    For full historical backfill, use Letterboxd's "Export your data"
    CSV from Settings → Import & Export, then import via the
    `generic-csv --service letterboxd --category watched` path.
  - Rewatch entries are flagged in external_ids["rewatch"]="Yes".
    Member ratings (0.5–5.0) are surfaced in external_ids["member_rating"].
  - Subsequent runs are incremental — watermark stored in state.json,
    keyed off the feed URL (so polling multiple Letterboxd users works
    without clobbering each other's watermarks).

  Cross-source dedup note:
  - Films you scrobbled to Trakt and then logged on Letterboxd will share
    a content_fingerprint of the form `movie:<slug>:y<year>`. The Trakt
    importer's twin-cache machinery handles the cross-source overlap.

  Other RSS-importer use cases:
  - The same generic-rss machinery works for Goodreads' "read" shelf
    feed, blog feeds (treated as 'watched' reads), podcast feeds, etc.
    Run `fulcra-media import generic-rss <FEED_URL> --service <name>
    --category watched|listened` for arbitrary feeds.
"""


@click.command("walkthrough")
def walkthrough() -> None:
    click.echo(LETTERBOXD_STEPS)

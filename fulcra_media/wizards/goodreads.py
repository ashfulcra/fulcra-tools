"""Goodreads setup walkthrough."""

from __future__ import annotations

import click


GOODREADS_STEPS = """\
Goodreads setup

  Goodreads stopped issuing API keys on December 8, 2020. Existing keys
  still work in a degraded fashion, but no new developer access is
  granted. Fortunately, every Goodreads shelf still publishes a public
  RSS feed of its contents — that's the pathway we use.

  Setup:
  1. Find your numeric Goodreads user id. Visit your profile page; the
     URL looks like:
       https://www.goodreads.com/user/show/<USER_ID>-<slug>
     The number between /show/ and the first hyphen is your user_id.
     (Example: in `.../user/show/12345-ash`, the user_id is `12345`.)

  2. Verify your 'read' shelf RSS feed is accessible. In a browser, open:
       https://www.goodreads.com/review/list_rss/<USER_ID>?shelf=read
     You should see RSS XML. If the page is blank or asks for login,
     your profile is locked — go to Settings → Profile → "Who can view
     my profile?" and set it to "anyone" (or at least make the 'read'
     shelf public via My Books → Edit shelves).

  3. Run:
       fulcra-media import goodreads --user-id <USER_ID>

  Caveats:
  - Goodreads RSS exposes a recent slice of entries (typically the most
    recent ~100 reviews). For full historical backfill, use Goodreads'
    "Export Library" CSV from Settings → Import & Export — request it,
    wait a few minutes, then download and convert via:
       fulcra-media import generic-csv <export.csv> \\
         --service goodreads --category read \\
         --ts-col "Date Read" --title-col Title --subtitle-col Author
    (The CLI's generic-csv only handles watched/listened today — for
    book backfill, hold off until a 'read' option lands or do it manually.)

  - Some private/non-public shelves require a `key=<API_KEY>` query
    parameter. New keys aren't being issued, so if you don't already
    have one, your only option is to flip the shelf to public.

  - Timestamp pickiness: the RSS feed's <pubDate> is when the review
    was *added*, not when the book was finished. The importer prefers
    <user_read_at> (the user-supplied finished-reading date) when
    present — confidence=high. Falls back to <pubDate> with
    confidence=medium otherwise.

  - Watermark is keyed off the user_id so polling multiple Goodreads
    accounts works without clobbering each other's state.

  Alternatives — Goodreads' API is dead, so for a more sustainable
  long-term integration, consider migrating to one of:
    - StoryGraph     (real REST API)
    - Hardcover      (open GraphQL API)
    - Bookwyrm       (federated, ActivityPub-based)
"""


@click.command("walkthrough")
def walkthrough() -> None:
    click.echo(GOODREADS_STEPS)

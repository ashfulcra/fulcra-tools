"""Goodreads read shelf — scheduled RSS plugin."""
from __future__ import annotations

import re
from datetime import timedelta

from fulcra_collect.plugin import Plugin, RunContext, Setting, SetupStep

from ..feed_plugin_health import goodreads_health_check
from ..fulcra import FulcraClient
from ..importers import goodreads as gr_importer
from ..state import DEFAULT_PATH as STATE_PATH
from ..state import load as _state_load
from ..state import save as _state_save
from ._common import (
    DURATION_SPEC,
    ensure_media_def,
    newest_event_iso,
    rss_import_and_advance,
    rss_since,
)


# Same structure as NETFLIX_WATCHED_SPEC and LASTFM_LISTENED_SPEC — all
# typed-media plugins share the same duration shape; only the canonical name
# differs.
GOODREADS_READ_SPEC: dict = DURATION_SPEC


def _extract_goodreads_user_id(raw: str) -> str:
    """Extract the numeric Goodreads user ID from whatever the user pasted.

    Accepts any of:
      - `12345678`              — bare numeric ID
      - `12345678-singularity`  — numeric ID with a Goodreads name slug
      - `https://www.goodreads.com/user/show/12345678-singularity` — full URL
      - `goodreads.com/user/show/12345678` — URL without scheme
      - mixed whitespace, trailing query strings, etc.

    Returns the bare numeric ID string. Raises RuntimeError if no numeric
    ID can be found, so the user gets a clear "we couldn't parse that"
    message instead of a silent 404 from the RSS fetch.

    User feedback 2026-05-26: wizard required users to extract the numeric
    ID from their profile URL by hand. Now they paste anything Goodreads-y
    and we figure it out.
    """
    s = (raw or "").strip()
    # Try the URL shape first: /user/show/<digits>[-name]
    m = re.search(r"/user/show/(\d+)", s)
    if m:
        return m.group(1)
    # Then a bare ID at the start, optionally followed by -name
    m = re.match(r"^(\d+)(?:-\S*)?$", s)
    if m:
        return m.group(1)
    raise RuntimeError(
        f"goodreads: couldn't find a numeric user ID in {raw!r}. "
        "Expected something like '12345678' or "
        "'https://www.goodreads.com/user/show/12345678-name'."
    )


def _run_goodreads(ctx: RunContext) -> None:
    raw_user_id = ctx.config.get("user_id")
    if not raw_user_id:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'user_id' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    user_id = _extract_goodreads_user_id(raw_user_id)
    max_entries: int | None = ctx.config.get("max_entries")

    # Ensure the "Read" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # read_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Read" definition
    # rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    ensure_media_def(ctx, media_state, attr="read_definition_id",
                     spec=GOODREADS_READ_SPEC, canonical_name="Read",
                     state_save=_state_save)

    since = rss_since(ctx)
    all_events = list(gr_importer.fetch_diary(user_id))
    rss_import_and_advance(
        ctx, all_events, tag="goodreads", since=since,
        max_entries=max_entries,
        fulcra_client_cls=FulcraClient,
        state_load=_state_load,
        newest_iso=newest_event_iso,
    )


PLUGIN = Plugin(
    id="goodreads",
    name="Goodreads read shelf",
    kind="scheduled",
    collect_mode="live_polled",
    run=_run_goodreads,
    health_check=goodreads_health_check,
    description=(
        "Polls your Goodreads 'read' shelf RSS feed every 12 hours. "
        "Anything you mark as read on Goodreads becomes a 'Read' "
        "annotation in Fulcra. Read-only — we never write to Goodreads."
    ),
    default_interval=timedelta(hours=12),
    category="books",
    canonical_definition_name="Read",
    required_credentials=(),
    required_settings=(
        Setting(
            key="user_id",
            label="Your Goodreads profile",
            kind="text",
            help=(
                "Either your profile URL (paste it from the browser — "
                "e.g. `https://www.goodreads.com/user/show/12345678-your-name`) "
                "or just the numeric ID. We'll extract the right part."
            ),
        ),
    ),
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What this plugin does",
            body_md=(
                "Goodreads publishes your 'read' shelf as an RSS feed. "
                "We poll it every 12 hours and record each book you "
                "mark as read as a 'Read' annotation in Fulcra."
            ),
        ),
        SetupStep(
            kind="external_action",
            title="Find your Goodreads user ID",
            body_md=(
                "Sign in to Goodreads and click your profile picture. "
                "Your profile URL looks like "
                "`goodreads.com/user/show/12345678-your-name`. Copy the "
                "numeric portion (`12345678` in this example) — that's "
                "your user ID. Your profile must be public for the RSS "
                "feed to be reachable."
            ),
            external_link="https://www.goodreads.com",
        ),
        SetupStep(
            kind="input",
            title="Enter your Goodreads user ID",
            body_md=(
                "Paste the numeric user ID you copied from your "
                "Goodreads profile URL."
            ),
            settings_keys=("user_id",),
        ),
        SetupStep(
            kind="test_connection",
            title="Verify your Goodreads shelf",
            body_md=(
                "We'll fetch your 'read' shelf RSS feed and show you "
                "the most recent books so you can confirm we're looking "
                "at the right profile."
            ),
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write your Goodreads reads?",
            body_md=(
                "We can write to your existing 'Read' annotation or "
                "create a new one."
            ),
            annotation_type="duration",
        ),
        SetupStep(
            kind="done",
            title="You're set",
            body_md="Goodreads will sync every 12 hours.",
        ),
    ),
)

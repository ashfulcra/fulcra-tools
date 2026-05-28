"""Letterboxd film diary — scheduled RSS plugin."""
from __future__ import annotations

import re
from datetime import timedelta

from fulcra_collect.plugin import Plugin, RunContext, Setting, SetupStep

from ..feed_plugin_health import letterboxd_health_check
from ..fulcra import FulcraClient
from ..importers import letterboxd as lb_importer
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


# Same structure as NETFLIX_WATCHED_SPEC — all Watched plugins share the same
# definition.
LETTERBOXD_WATCHED_SPEC: dict = DURATION_SPEC


def _extract_letterboxd_username(raw: str) -> str:
    """Pull a Letterboxd username out of whatever the user pasted.

    Accepts URL forms (`https://letterboxd.com/foo`, `letterboxd.com/foo/`,
    `letterboxd.com/foo/films/diary/`), bare usernames (`foo`), and the
    `@foo` shorthand. Strips trailing slashes and path segments so the RSS
    fetcher gets just the username.

    User feedback 2026-05-26: Goodreads had the same pain — wizard asked
    for a numeric ID and got a URL pasted. Parsing it permissively here
    saves the user the same dance.
    """
    s = (raw or "").strip()
    # URL shape: optional scheme, letterboxd.com, /username, optional path
    m = re.search(r"letterboxd\.com/([A-Za-z0-9_-]+)", s)
    if m:
        return m.group(1)
    # Bare username — strip leading @ if present
    bare = s.lstrip("@").rstrip("/")
    if bare and "/" not in bare and " " not in bare:
        return bare
    raise RuntimeError(
        f"letterboxd: couldn't find a username in {raw!r}. "
        "Expected something like 'username' or "
        "'https://letterboxd.com/username'."
    )


def _run_letterboxd(ctx: RunContext) -> None:
    raw_username = ctx.config.get("username")
    if not raw_username:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'username' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    username = _extract_letterboxd_username(raw_username)
    max_entries: int | None = ctx.config.get("max_entries")

    # Ensure the "Watched" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # watched_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Watched" definition
    # rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    ensure_media_def(ctx, media_state, attr="watched_definition_id",
                     spec=LETTERBOXD_WATCHED_SPEC,
                     canonical_name="Watched",
                     state_save=_state_save)

    since = rss_since(ctx)
    all_events = list(lb_importer.fetch_diary(username))
    rss_import_and_advance(
        ctx, all_events, tag="letterboxd", since=since,
        max_entries=max_entries,
        fulcra_client_cls=FulcraClient,
        state_load=_state_load,
        newest_iso=newest_event_iso,
    )


PLUGIN = Plugin(
    id="letterboxd",
    name="Letterboxd film diary",
    kind="scheduled",
    collect_mode="live_polled",
    run=_run_letterboxd,
    health_check=letterboxd_health_check,
    description=(
        "Polls your public Letterboxd diary RSS feed every 12 hours. "
        "Each diary entry becomes a 'Watched' annotation in Fulcra. "
        "Only needs your Letterboxd username (no API key)."
    ),
    default_interval=timedelta(hours=12),
    category="video",
    canonical_definition_name="Watched",
    required_credentials=(),
    required_settings=(
        Setting(
            key="username",
            label="Your Letterboxd profile",
            kind="text",
            help=(
                "Either your profile URL (paste from the browser — "
                "e.g. `https://letterboxd.com/your-name`) or just the "
                "username. We'll extract the right part."
            ),
        ),
    ),
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What this plugin does",
            body_md=(
                "Letterboxd publishes your public film diary as an RSS "
                "feed. We poll it every 12 hours and record each entry "
                "as a 'Watched' annotation. No API key needed — just "
                "your username."
            ),
        ),
        SetupStep(
            kind="input",
            title="Enter your Letterboxd username",
            body_md=(
                "Your **username** is the slug after `letterboxd.com/` "
                "in your profile URL — for example `letterboxd.com/"
                "yourname` means your username is `yourname`. Your "
                "diary must be public for the RSS feed to work."
            ),
            settings_keys=("username",),
        ),
        SetupStep(
            kind="test_connection",
            title="Verify your Letterboxd diary",
            body_md=(
                "We'll fetch your public diary RSS feed and show you "
                "the most recent films so you can confirm we're looking "
                "at the right profile."
            ),
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write your Letterboxd diary?",
            body_md=(
                "We can write to your existing 'Watched' annotation or "
                "create a new one."
            ),
            annotation_type="duration",
        ),
        SetupStep(
            kind="done",
            title="You're set",
            body_md="Letterboxd will sync every 12 hours.",
        ),
    ),
)

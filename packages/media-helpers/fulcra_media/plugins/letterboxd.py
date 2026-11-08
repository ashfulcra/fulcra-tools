"""Letterboxd film diary — scheduled RSS plugin."""
from __future__ import annotations

import re
from datetime import timedelta

from fulcra_collect.plugin import Plugin, RunContext, Setting, SetupStep

from ..feed_plugin_health import letterboxd_health_check
from ..collect_v1 import media_destinations, media_pending
from ..daemon_poll import daemon_poll
from ..fulcra import FulcraClient
from ..importers import letterboxd as lb_importer
from ..state import load as _state_load
from ._common import (
    DURATION_SPEC,
    newest_event_iso,
    rss_import_and_advance,
    rss_since,
)


# Legacy spec alias retained for imports; v1 setup uses media_destinations.
LETTERBOXD_WATCHED_SPEC: dict = DURATION_SPEC


def _extract_letterboxd_username(raw: str) -> str:
    """Pull a Letterboxd username out of whatever the user pasted.

    Accepts URL forms (`https://letterboxd.com/foo`, `letterboxd.com/foo/`,
    `letterboxd.com/foo/films/diary/`), bare usernames (`foo`), and the
    `@foo` shorthand. Strips trailing slashes and path segments so the RSS
    fetcher gets just the username.

    user feedback 2026-05-26: Goodreads had the same pain — wizard asked
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


@daemon_poll
def _run_letterboxd(ctx: RunContext) -> None:
    raw_username = ctx.config.get("username")
    if not raw_username:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'username' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    username = _extract_letterboxd_username(raw_username)

    ctx._daemon_poll.select("watched", "letterboxd", ["letterboxd", username, lb_importer.feed_url_for(username)])
    since = rss_since(ctx)
    all_events = list(lb_importer.fetch_diary(username, **ctx._daemon_poll.strict))
    rss_import_and_advance(
        ctx, all_events, tag="letterboxd", since=since,
        max_entries=ctx._daemon_poll.max_entries,
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
    record_data_types=media_destinations("watched"),
    record_pending=media_pending,
    health_check=letterboxd_health_check,
    description=(
        "Polls your public Letterboxd diary RSS feed every 12 hours. "
        "Each diary entry becomes a 'Watched' data type in Fulcra. "
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
                "as a 'Watched' data type. No API key needed — just "
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
                "We can write to your existing 'Watched' data type or "
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

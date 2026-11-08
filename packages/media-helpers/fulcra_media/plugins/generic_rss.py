"""Generic RSS/Atom feed — scheduled plugin."""
from __future__ import annotations

from datetime import timedelta

from fulcra_collect.plugin import Plugin, RunContext, Setting, SetupStep

from ..feed_plugin_health import generic_rss_health_check
from ..collect_v1 import media_destinations, media_pending
from ..daemon_poll import daemon_poll
from ..fulcra import FulcraClient
from ..importers import generic_rss as rss_importer
from ..state import load as _state_load
from ._common import (
    DURATION_SPEC,
    newest_event_iso,
    rss_import_and_advance,
    rss_since,
)


# Legacy spec alias retained for imports; v1 setup uses media_destinations.
_GENERIC_DURATION_SPEC: dict = DURATION_SPEC


@daemon_poll
def _run_generic_rss(ctx: RunContext) -> None:
    feed_url = ctx.config.get("feed_url")
    if not feed_url:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'feed_url' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    service = ctx.config.get("service")
    if not service:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'service' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    category = ctx.config.get("category")
    if not category:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'category' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )

    ctx._daemon_poll.select(category, service, ["generic-rss", feed_url, service, category])
    since = rss_since(ctx)
    all_events = list(rss_importer.normalize_feed(feed_url, service=service, category=category, **ctx._daemon_poll.strict))
    rss_import_and_advance(
        ctx, all_events, tag=service, since=since,
        max_entries=ctx._daemon_poll.max_entries,
        fulcra_client_cls=FulcraClient,
        state_load=_state_load,
        newest_iso=newest_event_iso,
    )


PLUGIN = Plugin(
    id="generic-rss",
    name="Generic RSS/Atom feed",
    kind="scheduled",
    collect_mode="live_polled",
    run=_run_generic_rss,
    record_data_types=media_destinations(),
    record_pending=media_pending,
    health_check=generic_rss_health_check,
    description=(
        "Watches any RSS or Atom feed and records each new entry as a "
        "Fulcra event. You set the feed URL, the service tag, and "
        "the category (watched / listened / read). Runs every 6 hours."
    ),
    default_interval=timedelta(hours=6),
    category="other",
    # The v1 destination slot follows the configured media category.
    required_credentials=(),
    required_settings=(
        Setting(
            key="feed_url",
            label="Feed URL",
            kind="url",
            help="RSS or Atom feed URL we'll poll every 6 hours.",
            placeholder="https://example.com/feed.xml",
        ),
        Setting(
            key="service",
            label="Service tag",
            kind="text",
            help=(
                "Short identifier we'll attach to each event "
                "(e.g. 'pinboard', 'feedly'). Used for dedup and display."
            ),
        ),
        Setting(
            key="category",
            label="Category",
            kind="enum",
            enum_values=("watched", "listened", "read"),
            help=(
                "Which data type to write to — 'watched' for "
                "video, 'listened' for audio, 'read' for text/books."
            ),
        ),
    ),
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What this plugin does",
            body_md=(
                "Point this at any RSS or Atom feed — e.g. a personal "
                "bookmarking export, a podcast feed, or any service that "
                "publishes activity over RSS. Every 6 hours we'll fetch "
                "new entries and record them as Fulcra events."
            ),
        ),
        SetupStep(
            kind="input",
            title="Configure the feed",
            body_md=(
                "Enter the **feed URL**, a short **service** tag we'll "
                "use to label events, and pick a **category** — "
                "'watched' for video, 'listened' for audio, 'read' for "
                "books/text. The category determines which canonical "
                "event we write to."
            ),
            settings_keys=("feed_url", "service", "category"),
        ),
        SetupStep(
            kind="test_connection",
            title="Verify the feed",
            body_md=(
                "We'll fetch the feed and show you the most recent "
                "entries so you can confirm it's reachable and shaped "
                "the way you expect."
            ),
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write these entries?",
            body_md=(
                "We can write to your existing Watched/Listened/Read "
                "data type (whichever matches your category) or create "
                "a new one."
            ),
            annotation_type="duration",
        ),
        SetupStep(
            kind="done",
            title="You're set",
            body_md="The feed will be polled every 6 hours.",
        ),
    ),
)

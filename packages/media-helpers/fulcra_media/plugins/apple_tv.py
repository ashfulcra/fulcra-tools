"""Apple TV app (on-device) — scheduled plugin reading the local UTS cache."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fulcra_collect.plugin import Plugin, RunContext, SetupStep

from ..apple_tv_health import apple_tv_health_check
from ..fulcra import FulcraClient
from ..importers import apple_tv as apple_tv_importer
from ..state import DEFAULT_PATH as STATE_PATH
from ..state import load as _state_load
from ..state import save as _state_save
from ._common import DURATION_SPEC, ensure_media_def, import_events


# Same structure as NETFLIX_WATCHED_SPEC and the other "Watched" plugins.
APPLE_TV_WATCHED_SPEC: dict = DURATION_SPEC


def _run_apple_tv(ctx: RunContext) -> None:
    """Scan the TV app's UTS cache and import watch events.

    The whole cache is scanned on every run; deterministic ids are
    idempotent by design (Continue events dedup on activity-day, Recently
    Watched events dedup on (show, season, episode) forever), so no
    watermark/rewind bookkeeping is needed — source-id dedup in the ingest
    layer discards re-imports, exactly like apple-music-takeout's flow.

    A SnapshotError (the cache is I/O-stalled) is surfaced as a
    RuntimeError with a clear message so the scheduler can retry later.
    """
    raw_dir = ctx.config.get("cache_dir")
    cache_dir = Path(raw_dir) if raw_dir else apple_tv_importer.DEFAULT_CACHE_DIR

    # Ensure the "Watched" annotation definition is known before importing.
    # On a fresh install the media state file may have no
    # watched_definition_id; the shared resolver adopts an existing
    # "Watched" definition rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    ensure_media_def(ctx, media_state, attr="watched_definition_id",
                     spec=APPLE_TV_WATCHED_SPEC, canonical_name="Watched",
                     state_save=_state_save)

    try:
        events = apple_tv_importer.parse_cache(cache_dir)
    except apple_tv_importer.SnapshotError as exc:
        raise RuntimeError(
            f"apple-tv: cache snapshot failed — macOS App Group protection is "
            f"blocking access to the TV service's container. Approve the "
            f"\"access data from other apps\" prompt for the daemon's python "
            f"(one-time, per-app), then this heals. Details: {exc}"
        ) from exc

    import_events(
        ctx, events, "apple-tv",
        fulcra_client_cls=FulcraClient,
        state_load=_state_load,
        # Apple TV+ watches also arrive via Trakt at high confidence; the
        # low-conf history-shelf backfill here should defer to those. Operator
        # config `twin_policy` still overrides.
        default_twin_policy="auto-discard",
    )


PLUGIN = Plugin(
    id="apple-tv",
    name="Apple TV app (on-device)",
    kind="scheduled",
    collect_mode="live_polled",
    run=_run_apple_tv,
    description=(
        "Captures what you watch in the Apple TV app by reading the app's "
        "local Watch Now cache — no sign-in, no export, and no Full Disk "
        "Access needed. The cache auto-refreshes every few hours even when "
        "the app is closed. In-progress items give exact activity times; "
        "the Recently Watched shelf backfills history with approximate "
        "times. Runs every 6 hours."
    ),
    default_interval=timedelta(hours=6),
    requires_network=False,
    category="video",
    canonical_definition_name="Watched",
    required_permissions=(),
    required_credentials=(),
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What this plugin does",
            body_md=(
                "The TV app keeps a local cache of its Watch Now screen, "
                "including your **Up Next** progress and **Recently "
                "Watched** shelf. We read that cache every 6 hours — the "
                "app doesn't need to be open, and unlike most Apple app "
                "data it doesn't require Full Disk Access.\n\n"
                "Items you're mid-way through carry an exact last-activity "
                "time. Recently Watched history has no watch times, so "
                "those events are recorded with approximate (low-"
                "confidence) timestamps — and automatically defer to a "
                "more precise source (like Trakt) when you use one."
            ),
        ),
        SetupStep(
            kind="test_connection",
            title="Verify your TV app cache",
            body_md=(
                "We'll open the TV app's cache read-only and count the "
                "watch events we can see. No data is sent to Fulcra yet — "
                "this just confirms the cache exists and is readable. If "
                "it isn't found, open the TV app once (Home tab) and "
                "re-check."
            ),
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write your watches?",
            body_md=(
                "We can write to your existing 'Watched' annotation or "
                "create a new one."
            ),
            annotation_type="duration",
        ),
        SetupStep(
            kind="done",
            title="You're set",
            body_md="Apple TV will sync every 6 hours.",
        ),
    ),
    health_check=apple_tv_health_check,
)

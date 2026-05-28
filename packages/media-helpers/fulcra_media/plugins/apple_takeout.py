"""Apple TV playback (takeout) — manual file/folder/zip plugin."""
from __future__ import annotations

from fulcra_collect.plugin import Plugin, RunContext, Setting, SetupStep

from .. import library
from ..fulcra import FulcraClient
from ..importers import apple_takeout as apple_takeout_importer
from ..since_filter import parse_window
from ..state import DEFAULT_PATH as STATE_PATH
from ..state import load as _state_load
from ..state import save as _state_save
from ..takeout_health import apple_takeout_health_check
from ._common import DURATION_SPEC, ensure_media_def, import_events, resolve_path


# Same structure as NETFLIX_WATCHED_SPEC — all Watched plugins share the same
# definition.
APPLE_TAKEOUT_WATCHED_SPEC: dict = DURATION_SPEC


def _run_apple_takeout(ctx: RunContext) -> None:
    # Ensure the "Watched" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # watched_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Watched" definition
    # rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    ensure_media_def(ctx, media_state, attr="watched_definition_id",
                     spec=APPLE_TAKEOUT_WATCHED_SPEC,
                     canonical_name="Watched",
                     state_save=_state_save)

    # `since` defaults to "1y" — a real Apple takeout can be a decade
    # deep; importing the whole thing on first run would dump tens of
    # thousands of orphan-shaped events into the user's account before
    # they understood what was happening.
    #
    # `until` defaults to "" (no upper bound). When set, it's the user's
    # opt-in fix for the Apple-takeout-vs-realtime-source dedup problem:
    # Apple TV+ watches also flow in via Trakt, Apple Music listens also
    # scrobble to Last.fm, etc. The user pins `until` to the date their
    # realtime source started and this importer fills only the historical
    # gap. Cross-source dedup (issue #55) makes this unnecessary; until
    # then the cutoff is the practical workaround.
    since_str = ctx.config.get("since") or "1y"
    until_str = ctx.config.get("until") or ""
    try:
        since_cutoff = parse_window(since_str)
    except ValueError as exc:
        ctx.progress(check="since", ok=False, detail=str(exc))
        return
    try:
        until_cutoff = parse_window(until_str)
    except ValueError as exc:
        ctx.progress(check="until", ok=False, detail=str(exc))
        return

    resolved = resolve_path(ctx, library)
    # The importer's parse_any handles file / dir / zip / nested-zip
    # itself; we just give it the path the user configured.
    events = list(apple_takeout_importer.parse_any(
        resolved, since=since_cutoff, until=until_cutoff,
    ))
    import_events(
        ctx, events, "apple-tv",
        fulcra_client_cls=FulcraClient,
        state_load=_state_load,
    )


PLUGIN = Plugin(
    id="apple-takeout",
    name="Apple TV playback (takeout)",
    kind="manual",
    collect_mode="historical",
    run=_run_apple_takeout,
    health_check=apple_takeout_health_check,
    description=(
        "Imports your Apple TV watch history from an Apple Data & "
        "Privacy takeout. We handle both `Video Play Activity.csv` (the "
        "rich per-watch event log) and `Playback Activity.csv` (the "
        "sparse summary) automatically — point us at the file, the "
        "folder, or the takeout zip and we'll find what's there."
    ),
    default_interval=None,
    category="video",
    canonical_definition_name="Watched",
    required_credentials=(),
    required_settings=(
        Setting(
            key="path",
            label="Apple takeout path",
            kind="path",
            help=(
                "Local path to a takeout CSV, the folder you got from "
                "privacy.apple.com, or the original `.zip` download "
                "(we'll find the right file inside)."
            ),
        ),
        Setting(
            key="since",
            label="How far back to import",
            kind="text",
            default="1y",
            help=(
                "Filter events to AFTER this cutoff. Accepts 'all', a "
                "relative window like '30d', '90d', '1y', or an absolute "
                "date 'YYYY-MM-DD'. Default 1y."
            ),
        ),
        Setting(
            key="until",
            label="Don't import after",
            kind="text",
            default="",
            help=(
                "Filter events to BEFORE this cutoff. Useful when another "
                "source (e.g. Trakt for video) already covers recent "
                "activity — set 'until' to the date that source started "
                "to fill only the historical gap. Accepts the same "
                "formats as 'since'. Empty = no upper bound."
            ),
        ),
    ),
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What this plugin does",
            body_md=(
                "Apple's Data & Privacy export contains your full Apple "
                "TV+ watch history. We handle both the rich per-watch "
                "log (`Video Play Activity.csv`) and the sparse summary "
                "(`Playback Activity.csv`) automatically — give us the "
                "file or folder you got from privacy.apple.com and "
                "we'll record each watch as a 'Watched' annotation."
            ),
        ),
        SetupStep(
            kind="external_action",
            title="Request your Apple takeout",
            body_md=(
                "Sign in to https://privacy.apple.com and click **Request "
                "a copy of your data**. Select **Apple Media Services "
                "information** (the bundle that contains TV playback). "
                "Apple emails you a download link within a few days. "
                "You can hand us the zip as-is, or unzip it and pick the "
                "folder."
            ),
            external_link="https://privacy.apple.com",
        ),
        SetupStep(
            kind="intro",
            title="Heads up: avoiding duplicates with other sources",
            body_md=(
                "Apple's takeouts overlap with realtime sources. If you "
                "already collect Apple TV+ watches via Trakt — or any "
                "other source that's already feeding Fulcra — then "
                "without an upper bound this importer will write the "
                "same watch a second time. Set the **\"Don't import "
                "after\"** field in the next steps to the date the "
                "other source started (or to today, if you only want "
                "this takeout to fill a one-time backfill) and the "
                "duplicates go away.\n\n"
                "Cross-source dedup is on the roadmap (#55) but isn't "
                "shipped yet."
            ),
        ),
        SetupStep(
            kind="file_upload",
            title="Pick the takeout file or folder",
            body_md=(
                "Pick the file or folder you got from privacy.apple.com "
                "(or the unzipped one). We'll search inside for the "
                "Apple TV playback data."
            ),
            settings_keys=("path",),
        ),
        SetupStep(
            kind="test_connection",
            title="Preview your Apple takeout",
            body_md=(
                "We'll search inside the file/folder for the playback "
                "CSV and show you the first few rows so you can confirm "
                "it's the right export."
            ),
        ),
        SetupStep(
            kind="input",
            title="How far back?",
            body_md=(
                "Real Apple takeouts can span a decade. The default "
                "imports the last year — adjust if you want more or less."
            ),
            settings_keys=("since",),
        ),
        SetupStep(
            kind="input",
            title="Don't import after (optional)",
            body_md=(
                "If another source already covers your recent Apple TV+ "
                "watches, pin this to the date that source started so "
                "this takeout fills only the gap. Same format as "
                "'How far back?'. Leave empty for no upper bound."
            ),
            settings_keys=("until",),
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write your Apple TV watches?",
            body_md=(
                "We can write to your existing 'Watched' annotation or "
                "create a new one."
            ),
            annotation_type="duration",
        ),
        SetupStep(
            kind="done",
            title="You're set",
            body_md=(
                "Apple takeout is configured. Click **Run now** from the "
                "dashboard to import."
            ),
        ),
    ),
)

"""Apple Music listens (takeout) — manual file/folder/zip plugin."""
from __future__ import annotations

from fulcra_collect.plugin import Plugin, RunContext, Setting, SetupStep

from .. import library
from ..fulcra import FulcraClient
from ..importers import apple_music_takeout as apple_music_takeout_importer
from ..since_filter import parse_window
from ..state import DEFAULT_PATH as STATE_PATH
from ..state import load as _state_load
from ..state import save as _state_save
from ..takeout_health import apple_music_takeout_health_check
from ._common import DURATION_SPEC, ensure_media_def, import_events, resolve_path


# Same structure as LASTFM_LISTENED_SPEC and the other "Listened" plugins.
APPLE_MUSIC_TAKEOUT_LISTENED_SPEC: dict = DURATION_SPEC


def _run_apple_music_takeout(ctx: RunContext) -> None:
    # Ensure the "Listened" annotation definition is known before importing.
    # All audio plugins (Last.fm, Deezer, Spotify Extended, Apple Music)
    # share the listened_definition_id field — whichever plugin first runs
    # on this machine populates it; this one will find it already set.
    media_state = _state_load(STATE_PATH)
    ensure_media_def(ctx, media_state, attr="listened_definition_id",
                     spec=APPLE_MUSIC_TAKEOUT_LISTENED_SPEC,
                     canonical_name="Listened",
                     state_save=_state_save)

    # `since` defaults to "1y". Apple Music Play Activity can be hundreds
    # of thousands of rows for long-term subscribers; gate behind a
    # cutoff by default and let the user widen it explicitly.
    #
    # `until` defaults to "" (no upper bound). Same dedup workaround as
    # apple-takeout: if the user also has Last.fm scrobbling Apple Music
    # plays in real time, they pin `until` to the Last.fm start date so
    # this takeout fills only the backfill window. See #55 for the
    # cross-source dedup that supersedes this knob.
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
    events = list(apple_music_takeout_importer.parse_any(
        resolved, since=since_cutoff, until=until_cutoff,
    ))
    import_events(
        ctx, events, "apple-music",
        fulcra_client_cls=FulcraClient,
        state_load=_state_load,
    )


PLUGIN = Plugin(
    id="apple-music-takeout",
    name="Apple Music listens (takeout)",
    kind="manual",
    collect_mode="historical",
    run=_run_apple_music_takeout,
    health_check=apple_music_takeout_health_check,
    description=(
        "Imports your Apple Music play history from an Apple Data & "
        "Privacy takeout. We parse `Apple Music Play Activity.csv` "
        "(the rich per-listen event log) and write Listened annotations "
        "to Fulcra. Point us at the file, the folder, or the takeout "
        "zip."
    ),
    default_interval=None,
    category="audio",
    canonical_definition_name="Listened",
    required_credentials=(),
    required_settings=(
        Setting(
            key="path",
            label="Apple takeout path",
            kind="path",
            help=(
                "Local path to `Apple Music Play Activity.csv`, the "
                "folder that contains it, or the takeout zip (we'll "
                "find the file inside)."
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
                "source (e.g. Last.fm) already covers recent listens — "
                "set 'until' to the date that source started so this "
                "takeout fills only the historical gap. Accepts the same "
                "formats as 'since'. Empty = no upper bound."
            ),
        ),
    ),
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What this plugin does",
            body_md=(
                "Apple's Data & Privacy export includes an `Apple Music "
                "Play Activity.csv` listing every track you've listened "
                "to via Apple Music — across every device tied to your "
                "Apple Account. Give us the file or folder and we'll "
                "record each listen as a 'Listened' annotation."
            ),
        ),
        SetupStep(
            kind="external_action",
            title="Request your Apple takeout",
            body_md=(
                "Sign in to https://privacy.apple.com and click **Request "
                "a copy of your data**. Select **Apple Media Services "
                "information** (the bundle that contains Apple Music "
                "activity). Apple emails you a download link within a "
                "few days. You can hand us the zip as-is, or unzip it "
                "and pick the folder."
            ),
            external_link="https://privacy.apple.com",
        ),
        SetupStep(
            kind="intro",
            title="Heads up: avoiding duplicates with other sources",
            body_md=(
                "Apple's takeouts overlap with realtime sources. If "
                "Last.fm is already scrobbling your Apple Music listens "
                "— or any other source is already feeding Fulcra — then "
                "without an upper bound this importer will write the "
                "same listen a second time. Set the **\"Don't import "
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
                "(or the unzipped one). We'll find `Apple Music Play "
                "Activity.csv` inside."
            ),
            settings_keys=("path",),
        ),
        SetupStep(
            kind="test_connection",
            title="Preview your Apple Music takeout",
            body_md=(
                "We'll find `Apple Music Play Activity.csv` inside the "
                "file/folder you picked and show you the first few "
                "listens so you can confirm it's the right export."
            ),
        ),
        SetupStep(
            kind="input",
            title="How far back?",
            body_md=(
                "Apple Music play history can be hundreds of thousands "
                "of rows for long-term subscribers. The default imports "
                "the last year — adjust if you want more or less."
            ),
            settings_keys=("since",),
        ),
        SetupStep(
            kind="input",
            title="Don't import after (optional)",
            body_md=(
                "If another source (Last.fm, etc.) already covers your "
                "recent Apple Music listens, pin this to the date that "
                "source started so this takeout fills only the gap. "
                "Same format as 'How far back?'. Leave empty for no "
                "upper bound."
            ),
            settings_keys=("until",),
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write your Apple Music listens?",
            body_md=(
                "We can write to your existing 'Listened' annotation or "
                "create a new one."
            ),
            annotation_type="duration",
        ),
        SetupStep(
            kind="done",
            title="You're set",
            body_md=(
                "Apple Music takeout is configured. Click **Run now** "
                "from the dashboard to import."
            ),
        ),
    ),
)

"""YouTube watch history — manual file-based plugin."""
from __future__ import annotations

from fulcra_collect.plugin import Plugin, RunContext, Setting, SetupStep

from .. import library
from ..fulcra import FulcraClient
from ..importers import youtube as youtube_importer
from ..state import DEFAULT_PATH as STATE_PATH
from ..state import load as _state_load
from ..state import save as _state_save
from ..takeout_health import youtube_health_check
from ._common import DURATION_SPEC, ensure_media_def, run_file_import


# Same structure as NETFLIX_WATCHED_SPEC — all Watched plugins share the same
# definition.
YOUTUBE_WATCHED_SPEC: dict = DURATION_SPEC


def _run_youtube(ctx: RunContext) -> None:
    # Ensure the "Watched" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # watched_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Watched" definition
    # rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    ensure_media_def(ctx, media_state, attr="watched_definition_id",
                     spec=YOUTUBE_WATCHED_SPEC, canonical_name="Watched",
                     state_save=_state_save)

    run_file_import(
        ctx,
        parse=youtube_importer.parse_takeout_json,
        tag="youtube",
        library_mod=library,
        fulcra_client_cls=FulcraClient,
        state_load=_state_load,
    )


PLUGIN = Plugin(
    id="youtube",
    name="YouTube watch history",
    kind="manual",
    collect_mode="historical",
    run=_run_youtube,
    description=(
        "Imports a YouTube `watch-history.json` from Google Takeout. "
        "Manual — request a Takeout containing YouTube, then upload the "
        "JSON file here. Each watch becomes a 'Watched' annotation."
    ),
    default_interval=None,
    category="video",
    required_settings=(
        Setting(
            key="path",
            label="watch-history.json path",
            kind="path",
            help=(
                "Local path to the `watch-history.json` file from your "
                "Google Takeout (inside the YouTube and YouTube Music folder)."
            ),
        ),
    ),
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What this plugin does",
            body_md=(
                "Google Takeout can hand you a JSON of every YouTube "
                "video you've watched. Upload that file here and we'll "
                "import each watch as a 'Watched' annotation."
            ),
        ),
        SetupStep(
            kind="external_action",
            title="Request a Google Takeout",
            body_md=(
                "Go to https://takeout.google.com, click **Deselect "
                "all**, then check just **YouTube and YouTube Music**. "
                "Under that, click **All YouTube data included** and "
                "leave only **history** ticked. Choose **JSON** as the "
                "format. Submit the export; Google emails you a download "
                "link, typically within a few hours. Unzip and find "
                "`watch-history.json` inside the YouTube folder."
            ),
            external_link="https://takeout.google.com",
        ),
        SetupStep(
            kind="file_upload",
            title="Upload your watch-history.json",
            body_md=(
                "Pick the `watch-history.json` you extracted from the "
                "Takeout zip."
            ),
            settings_keys=("path",),
        ),
        SetupStep(
            kind="test_connection",
            title="Preview your YouTube takeout",
            body_md=(
                "We'll open the JSON and show you the first few watches "
                "so you can confirm it's the right file."
            ),
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write your YouTube watches?",
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
                "YouTube is configured. Click **Run now** from the "
                "dashboard to import the JSON. Re-upload a fresh Takeout "
                "whenever you want to pull in newer watches."
            ),
        ),
    ),
    canonical_definition_name="Watched",
    required_credentials=(),
    health_check=youtube_health_check,
)

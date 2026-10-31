"""YouTube watch history — manual file-based plugin."""
from __future__ import annotations

from fulcra_collect.plugin import Plugin, RunContext, Setting, SetupStep

from .. import library
from ..collect_v1 import media_destinations, media_pending, media_run
from ..fulcra import FulcraClient
from ..importers import youtube as youtube_importer
from ..state import load as _state_load
from ..takeout_health import youtube_health_check
from ._common import DURATION_SPEC, run_file_import


# Legacy spec alias retained for imports; v1 setup uses media_destinations.
YOUTUBE_WATCHED_SPEC: dict = DURATION_SPEC


@media_run
def _run_youtube(ctx: RunContext) -> None:

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
    record_data_types=media_destinations("watched"),
    record_pending=media_pending,
    description=(
        "Imports a YouTube `watch-history.json` from Google Takeout. "
        "Manual — request a Takeout containing YouTube, then upload the "
        "JSON file here. Each watch becomes a 'Watched' data type."
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
                "import each watch as a 'Watched' data type."
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
                "We can write to your existing 'Watched' data type or "
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

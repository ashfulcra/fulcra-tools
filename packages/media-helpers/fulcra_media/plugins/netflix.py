"""Netflix viewing history — manual file-based plugin."""
from __future__ import annotations

from fulcra_collect.plugin import Plugin, RunContext, Setting, SetupStep

from .. import library
from ..collect_v1 import media_destinations, media_pending, media_run
from ..fulcra import FulcraClient
from ..importers import netflix as netflix_importer
from ..state import load as _state_load
from ..takeout_health import netflix_health_check
from ._common import DURATION_SPEC, run_file_import


# Legacy spec alias retained for imports; v1 setup uses media_destinations.
NETFLIX_WATCHED_SPEC: dict = DURATION_SPEC


@media_run
def _run_netflix(ctx: RunContext) -> None:

    run_file_import(
        ctx,
        parse=netflix_importer.parse_auto,
        tag="netflix",
        library_mod=library,
        fulcra_client_cls=FulcraClient,
        state_load=_state_load,
        # Netflix titles can also land via Trakt at high confidence; a low-conf
        # takeout row should defer to those. Operator config `twin_policy`
        # still overrides.
        default_twin_policy="auto-discard",
    )


PLUGIN = Plugin(
    id="netflix",
    name="Netflix viewing history",
    kind="manual",
    collect_mode="historical",
    run=_run_netflix,
    record_data_types=media_destinations("watched"),
    record_pending=media_pending,
    description=(
        "Imports a Netflix viewing-history CSV. Manual — download "
        "ViewingActivity.csv from netflix.com/Activity, then point this "
        "plugin at the file. Each watch becomes a 'Watched' data type."
    ),
    default_interval=None,
    category="video",
    canonical_definition_name="Watched",
    required_credentials=(),
    required_settings=(
        Setting(
            key="path",
            label="Netflix CSV path",
            kind="path",
            help="Local path to the ViewingActivity.csv downloaded from Netflix.",
        ),
    ),
    health_check=netflix_health_check,
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What this plugin does",
            body_md=(
                "Netflix exports your full viewing history as a CSV. "
                "Upload it here and we'll record each watch as a "
                "'Watched' data type in Fulcra."
            ),
        ),
        SetupStep(
            kind="external_action",
            title="Download your Netflix CSV",
            body_md=(
                "Sign in to https://www.netflix.com/Activity and click "
                "**Download all** at the bottom of the page. Netflix will "
                "give you a `ViewingActivity.csv` file. Save it somewhere "
                "you can find — you'll upload it on the next step."
            ),
            external_link="https://www.netflix.com/Activity",
        ),
        SetupStep(
            kind="file_upload",
            title="Upload your ViewingActivity.csv",
            body_md=(
                "Pick the `ViewingActivity.csv` you just downloaded. We'll "
                "store its path; the import runs whenever you click **Run "
                "now** on the dashboard."
            ),
            settings_keys=("path",),
        ),
        SetupStep(
            kind="test_connection",
            title="Preview your Netflix export",
            body_md=(
                "We'll open the CSV and show you the first few rows so "
                "you can confirm it's the right file before we import "
                "anything."
            ),
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write your Netflix watches?",
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
                "Netflix is set up. Click **Run now** from the dashboard "
                "to import the CSV. Re-upload a fresh CSV whenever you "
                "want to pull in new watches."
            ),
        ),
    ),
)

"""Netflix viewing history — manual file-based plugin."""
from __future__ import annotations

from fulcra_collect.plugin import Plugin, RunContext, Setting, SetupStep

from .. import library
from ..fulcra import FulcraClient
from ..importers import netflix as netflix_importer
from ..state import DEFAULT_PATH as STATE_PATH
from ..state import load as _state_load
from ..state import save as _state_save
from ..takeout_health import netflix_health_check
from ._common import DURATION_SPEC, ensure_media_def, run_file_import


# Mirrors wire.duration_definition_payload defaults and LASTFM_LISTENED_SPEC —
# same structure, different canonical name.
NETFLIX_WATCHED_SPEC: dict = DURATION_SPEC


def _run_netflix(ctx: RunContext) -> None:
    # Ensure the "Watched" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # watched_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Watched" definition
    # rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    ensure_media_def(ctx, media_state, attr="watched_definition_id",
                     spec=NETFLIX_WATCHED_SPEC, canonical_name="Watched",
                     state_save=_state_save)

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
    description=(
        "Imports a Netflix viewing-history CSV. Manual — download "
        "ViewingActivity.csv from netflix.com/Activity, then point this "
        "plugin at the file. Each watch becomes a 'Watched' annotation."
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
                "'Watched' annotation in Fulcra."
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
                "We can write to your existing 'Watched' annotation or "
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

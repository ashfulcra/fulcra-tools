"""Spotify Extended Streaming History — manual file-based plugin."""
from __future__ import annotations

from fulcra_collect.plugin import Plugin, RunContext, Setting, SetupStep

from .. import library
from ..fulcra import FulcraClient
from ..importers import spotify as spotify_importer
from ..state import DEFAULT_PATH as STATE_PATH
from ..state import load as _state_load
from ..state import save as _state_save
from ..takeout_health import spotify_extended_health_check
from ._common import DURATION_SPEC, ensure_media_def, run_file_import


# Identical structure to LASTFM_LISTENED_SPEC — both plugins produce "Listened"
# Duration annotations against the same shared definition.
SPOTIFY_EXTENDED_LISTENED_SPEC: dict = DURATION_SPEC


def _run_spotify_extended(ctx: RunContext) -> None:
    # Ensure the "Listened" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # listened_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Listened" definition
    # rather than creating a duplicate.  lastfm and spotify-extended share the
    # same State.listened_definition_id field — whichever plugin runs first
    # on a new machine will populate it; the other will find it already set.
    media_state = _state_load(STATE_PATH)
    ensure_media_def(ctx, media_state, attr="listened_definition_id",
                     spec=SPOTIFY_EXTENDED_LISTENED_SPEC,
                     canonical_name="Listened",
                     state_save=_state_save)

    run_file_import(
        ctx,
        parse=spotify_importer.parse_extended_zip,
        tag="spotify",
        library_mod=library,
        fulcra_client_cls=FulcraClient,
        state_load=_state_load,
    )


PLUGIN = Plugin(
    id="spotify-extended",
    name="Spotify Extended Streaming History",
    kind="manual",
    collect_mode="historical",
    run=_run_spotify_extended,
    description=(
        "Imports the Spotify Extended Streaming History GDPR export — a "
        "zip of `Streaming_History_Audio_*.json` files covering your full "
        "lifetime of Spotify listens. Manual; request the export from "
        "Spotify Account Privacy, wait ~30 days, then upload the zip."
    ),
    default_interval=None,
    category="audio",
    canonical_definition_name="Listened",
    required_credentials=(),
    required_settings=(
        Setting(
            key="path",
            label="Spotify export zip path",
            kind="path",
            help=(
                "Local path to the `my_spotify_data.zip` (or similar) "
                "you downloaded from Spotify."
            ),
        ),
    ),
    health_check=spotify_extended_health_check,
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What this plugin does",
            body_md=(
                "Spotify's Extended Streaming History is the deepest "
                "archive of your listens — it covers your entire account "
                "lifetime, not just the last year. Once you've requested "
                "the export and Spotify has emailed you the download, "
                "upload the zip here."
            ),
        ),
        SetupStep(
            kind="external_action",
            title="Request your Spotify export",
            body_md=(
                "Open https://www.spotify.com/account/privacy/ and scroll "
                "to **Download your data**. Tick **Extended streaming "
                "history** (the third option — the lifetime archive) and "
                "click **Request data**. Spotify will email you within ~30 "
                "days with a download link. Save the zip somewhere you "
                "can find."
            ),
            external_link="https://www.spotify.com/account/privacy/",
        ),
        SetupStep(
            kind="file_upload",
            title="Upload your Spotify export zip",
            body_md=(
                "Pick the zip Spotify sent you. We'll read every "
                "`Streaming_History_Audio_*.json` file inside it."
            ),
            settings_keys=("path",),
        ),
        SetupStep(
            kind="test_connection",
            title="Preview your Spotify export",
            body_md=(
                "We'll peek inside the zip and show you the first few "
                "listens so you can confirm it's the right file."
            ),
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write your Spotify listens?",
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
                "Spotify Extended History is configured. Click **Run "
                "now** from the dashboard to import the zip."
            ),
        ),
    ),
)

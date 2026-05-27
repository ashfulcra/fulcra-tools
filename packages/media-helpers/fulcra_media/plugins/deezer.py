"""Deezer listening history — scheduled plugin."""
from __future__ import annotations

from datetime import timedelta

from fulcra_collect.plugin import Credential, Plugin, RunContext, SetupStep

from ..deezer_health import deezer_health_check
from ..fulcra import FulcraClient
from ..importers import deezer as deezer_importer
from ..state import DEFAULT_PATH as STATE_PATH
from ..state import load as _state_load
from ..state import save as _state_save
from ._common import (
    DURATION_SPEC,
    ensure_media_def,
    newest_event_iso,
    run_scheduled_import,
)


# Same structure as LASTFM_LISTENED_SPEC and SPOTIFY_EXTENDED_LISTENED_SPEC —
# all three plugins produce "Listened" Duration annotations against the same
# shared definition. Kept as a distinct constant so the resolver call below is
# self-documenting and so spec-shape tests are local to this plugin.
DEEZER_LISTENED_SPEC: dict = DURATION_SPEC


def _run_deezer(ctx: RunContext) -> None:
    """Fetch the authenticated user's Deezer play history and import it.

    Credentials: the Deezer OAuth access token must be stored in
    fulcra-collect's credential store — set it with:
        fulcra-collect set-credential deezer access-token <token>
    If the token is missing, raises RuntimeError with a clear instruction.
    """
    access_token = ctx.credentials.get("access-token")
    if not access_token:
        raise RuntimeError(
            "deezer: credential 'access-token' is not set — "
            "run `fulcra-collect set-credential deezer access-token`"
        )
    creds = {"access_token": access_token}

    # Ensure the "Listened" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # listened_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Listened" definition
    # rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    ensure_media_def(ctx, media_state, attr="listened_definition_id",
                     spec=DEEZER_LISTENED_SPEC, canonical_name="Listened",
                     state_save=_state_save)

    run_scheduled_import(
        ctx,
        fetch=lambda since: deezer_importer.fetch_history(
            creds, since=since, max_pages=None
        ),
        normalize=deezer_importer.normalize_history,
        tag="deezer",
        fulcra_client_cls=FulcraClient,
        state_load=_state_load,
        newest_iso=newest_event_iso,
    )


PLUGIN = Plugin(
    id="deezer",
    name="Deezer listening history",
    kind="scheduled",
    run=_run_deezer,
    description=(
        "Polls your Deezer listening history every 2 hours and records "
        "each track as a 'Listened' annotation. Requires a Deezer OAuth "
        "access token (free Deezer dev account)."
    ),
    default_interval=timedelta(hours=2),
    category="audio",
    canonical_definition_name="Listened",
    required_credentials=(
        Credential(
            key="access-token",
            label="Deezer OAuth access token",
            help=(
                "Mint an access token at https://developers.deezer.com/api/oauth "
                "or run `fulcra-media wizard deezer` for the guided flow."
            ),
        ),
    ),
    health_check=deezer_health_check,
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What this plugin does",
            body_md=(
                "Deezer exposes your listening history through its "
                "Web API. We poll it every 2 hours and record each "
                "track you finish as a 'Listened' annotation in Fulcra."
            ),
        ),
        SetupStep(
            kind="external_action",
            title="Get a Deezer access token",
            body_md=(
                "Visit https://developers.deezer.com/api/oauth and "
                "register a new application (any name and redirect URI "
                "will do — Deezer's OAuth playground accepts "
                "`http://localhost`). Complete the OAuth flow with the "
                "`listening_history` permission to mint an access "
                "token. If this is too fiddly via the web UI, run "
                "`fulcra-media wizard deezer` from your terminal for a "
                "guided flow."
            ),
            external_link="https://developers.deezer.com/api/oauth",
        ),
        SetupStep(
            kind="input",
            title="Paste your Deezer access token",
            body_md=(
                "Paste the access token Deezer minted for you. We'll "
                "store it in your macOS keychain."
            ),
            settings_keys=("access-token",),
        ),
        SetupStep(
            kind="test_connection",
            title="Verify your Deezer connection",
            body_md="Asking Deezer for your 5 most recent listens…",
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write your Deezer listens?",
            body_md=(
                "We can write to your existing 'Listened' annotation "
                "or create a new one."
            ),
            annotation_type="duration",
        ),
        SetupStep(
            kind="done",
            title="You're set",
            body_md="Deezer will sync every 2 hours.",
        ),
    ),
)

"""Last.fm scrobbles — scheduled plugin."""
from __future__ import annotations

from datetime import timedelta

from fulcra_collect.plugin import Credential, Plugin, RunContext, Setting, SetupStep

from ..fulcra import FulcraClient
from ..importers.lastfm import fetch_recent_tracks, normalize_history
from ..lastfm_health import lastfm_health_check
from ..state import DEFAULT_PATH as STATE_PATH
from ..state import load as _state_load
from ..state import save as _state_save
from ._common import (
    DURATION_SPEC,
    ensure_media_def,
    newest_event_iso,
    run_scheduled_import,
)


# The Fulcra annotation definition shape for the "Listened" DurationAnnotation.
# Passed to ctx.resolved_definition_id as the expected_spec so the shared
# resolver can verify an adopted definition has the right structure, or create
# a new one when none exists. Mirrors the payload produced by
# wire.duration_definition_payload (the bootstrap CLI path) — annotation_type
# and measurement_spec are the two axes that _spec_matches compares.
LASTFM_LISTENED_SPEC: dict = DURATION_SPEC


def _run_lastfm(ctx: RunContext) -> None:
    api_key = ctx.credentials.get("api-key")
    if not api_key:
        raise RuntimeError("lastfm: credential 'api-key' is not set — "
                           "run `fulcra-collect set-credential lastfm api-key`")
    # The importer expects username + api_key in one dict. Username is a
    # Setting (configured via the wizard or `fulcra-collect set-setting`),
    # api-key is a Credential (encrypted store). Until 2026-05-25 this
    # function only forwarded api_key, so every run crashed with
    # KeyError: 'username' inside fetch_recent_tracks the moment a real
    # sync was attempted.
    username = ctx.config.get("username")
    if not username:
        raise RuntimeError("lastfm: setting 'username' is not set — "
                           "run `fulcra-collect set-setting lastfm username <your-lastfm-handle>` "
                           "or re-run the Last.fm setup wizard from the dashboard.")
    creds = {"api_key": api_key, "username": username}

    # Ensure the "Listened" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # listened_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Listened" definition
    # rather than creating a duplicate — giving the same multi-machine dedup
    # guarantee that bootstrap provides without requiring bootstrap to have
    # been run on every machine.
    media_state = _state_load(STATE_PATH)
    ensure_media_def(ctx, media_state, attr="listened_definition_id",
                     spec=LASTFM_LISTENED_SPEC, canonical_name="Listened",
                     state_save=_state_save)

    # Delegate to the shared helper; `since` (watermark - 1h) is computed
    # there so it stays consistent with the Deezer plugin.
    run_scheduled_import(
        ctx,
        fetch=lambda since: fetch_recent_tracks(creds, since=since, max_pages=None),
        normalize=normalize_history,
        tag="lastfm",
        fulcra_client_cls=FulcraClient,
        state_load=_state_load,
        newest_iso=newest_event_iso,
    )


PLUGIN = Plugin(
    id="lastfm",
    name="Last.fm scrobbles",
    kind="scheduled",
    run=_run_lastfm,
    description=(
        "Captures your music listening history via Last.fm — the universal "
        "scrobble aggregator that catches Spotify, Apple Music, Tidal, and "
        "more. Runs every hour. You'll need a free Last.fm API key and your "
        "Last.fm username."
    ),
    default_interval=timedelta(hours=1),
    category="audio",
    canonical_definition_name="Listened",
    required_credentials=(
        Credential(key="api-key", label="Last.fm API key",
                   help="Create one at https://www.last.fm/api/account/create"),
    ),
    required_settings=(
        Setting(
            key="username",
            label="Last.fm username",
            kind="text",
            help="Your Last.fm account name — e.g. the one in the URL last.fm/user/<this>.",
        ),
    ),
    health_check=lastfm_health_check,
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What Last.fm does",
            body_md=(
                "Last.fm is the universal music sidecar — even when you "
                "listen via Spotify, Apple Music, or another player, "
                "Last.fm scrobbles capture it. We import your scrobble "
                "history every hour."
            ),
        ),
        SetupStep(
            kind="external_action",
            title="Create a Last.fm API account",
            body_md=(
                "Visit https://www.last.fm/api/account/create and fill in "
                "the form. **Application name:** `Fulcra Collect` (or "
                "anything you like). **Application description:** optional. "
                "**Callback URL:** leave blank. **Application homepage:** "
                "leave blank. Click **Submit**. Last.fm will show you an "
                "**API Key** and a **Shared Secret** — you only need the "
                "API Key for the next step."
            ),
            external_link="https://www.last.fm/api/account/create",
        ),
        SetupStep(
            kind="input",
            title="Paste your Last.fm API key and username",
            body_md=(
                "**API Key** is the first value Last.fm showed you on the "
                "API account page. **username** is the slug after "
                "`last.fm/user/` in your profile URL — typically your "
                "account name."
            ),
            settings_keys=("api-key", "username"),
        ),
        # Verify the entered creds before letting the user advance — without
        # this, bad key/username silently writes to the settings store and
        # surfaces as a runtime crash an hour later. The wizard's generic
        # test_connection renderer calls /api/plugin/lastfm/health_check,
        # which runs lastfm_health_check above and gates Next on ok=True.
        SetupStep(
            kind="test_connection",
            title="Verify your Last.fm connection",
            body_md="Asking Last.fm for your 5 most recent scrobbles…",
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write your scrobbles?",
            body_md=(
                "We can write to your existing 'Listened' annotation or "
                "create a new one."
            ),
            annotation_type="duration",
        ),
        SetupStep(
            kind="done",
            title="You're set",
            body_md="Last.fm will sync every hour.",
        ),
    ),
)

"""Last.fm scrobbles — scheduled plugin."""
from __future__ import annotations

from datetime import timedelta

from fulcra_collect.plugin import Credential, Plugin, RunContext, Setting, SetupStep

from ..collect_v1 import media_destinations, media_pending
from ..daemon_poll import daemon_poll
from ..fulcra import FulcraClient
from ..importers.lastfm import fetch_recent_tracks, normalize_history
from ..lastfm_health import lastfm_health_check
from ..state import load as _state_load
from ._common import (
    DURATION_SPEC,
    newest_event_iso,
    run_scheduled_import,
)


# Legacy spec alias retained for imports; v1 setup uses media_destinations.
LASTFM_LISTENED_SPEC: dict = DURATION_SPEC


@daemon_poll
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
    ctx._daemon_poll.select("listened", "lastfm", ["lastfm", username], secret_key="api-key")
    creds = {"api_key": api_key, "username": username}

    # Delegate to the shared helper; `since` (watermark - 24h) is computed
    # there so it stays consistent with the Deezer plugin.
    run_scheduled_import(
        ctx,
        fetch=lambda since: fetch_recent_tracks(creds, since=since, max_pages=ctx._daemon_poll.max_pages, **ctx._daemon_poll.strict),
        normalize=lambda raw: normalize_history(raw, **ctx._daemon_poll.strict),
        tag="lastfm",
        fulcra_client_cls=FulcraClient,
        state_load=_state_load,
        newest_iso=newest_event_iso,
    )


PLUGIN = Plugin(
    id="lastfm",
    name="Last.fm scrobbles",
    kind="scheduled",
    collect_mode="live_polled",
    run=_run_lastfm,
    record_data_types=media_destinations("listened"),
    record_pending=media_pending,
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
                "We can write to your existing 'Listened' data type or "
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

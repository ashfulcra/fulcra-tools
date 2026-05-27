"""fulcra-collect plugins exported by fulcra-media-helpers.

Exposes scheduled plugins (Last.fm, Deezer, Trakt, Generic RSS, Letterboxd,
Goodreads) and five manual file-based plugins: Netflix, Spotify Extended,
YouTube, Spotify IFTTT, and Apple TV takeout.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fulcra_collect.plugin import Credential, Permission, Plugin, RunContext, Setting, SetupStep
from fulcra_csv import ClusterPolicy, ColumnMap, apply_cluster_policy, apply_twin_decisions, find_low_conf_twins

from . import library
from . import twin_cache
from . import webhook_receiver
from .trakt_oauth import trakt_authorize_url, trakt_oauth_handler
from .trakt_health import trakt_health_check
from .fulcra import FulcraClient
from .importers import apple_podcasts as ap
from .importers import apple_music_takeout as apple_music_takeout_importer
from .importers import apple_takeout as apple_takeout_importer
from .importers import deezer as deezer_importer
from .importers import generic_rss as rss_importer
from .importers import goodreads as gr_importer
from .importers import letterboxd as lb_importer
from .importers import netflix as netflix_importer
from .importers import spotify as spotify_importer
from .importers import spotify_ifttt as spotify_ifttt_importer
from .importers import trakt as trakt_importer
from .importers import youtube as youtube_importer
from .importers.generic_csv import _FP_AUTO, parse_media_csv
from .importers.lastfm import fetch_recent_tracks, normalize_history
from .lastfm_health import lastfm_health_check
from .apple_podcasts_health import apple_podcasts_health_check
from .deezer_health import deezer_health_check
from .feed_plugin_health import (
    generic_rss_health_check,
    goodreads_health_check,
    letterboxd_health_check,
)
from .takeout_health import (
    apple_music_takeout_health_check,
    apple_takeout_health_check,
    netflix_health_check,
    spotify_extended_health_check,
    youtube_health_check,
)
from .since_filter import parse_window
from .state import DEFAULT_PATH as STATE_PATH
from .state import load as _state_load
from .state import save as _state_save


def _ensure_media_def(ctx: RunContext, media_state, *,
                       attr: str, spec: dict, canonical_name: str) -> str:
    """Get/refresh a canonical definition id stored on the shared media
    state file. Wraps `ctx.ensure_definition` to also write the new id
    back to per-package state when it changes.

    Replaces the older `if not media_state.<attr>: resolve; save` pattern
    that trusted the per-package cache blindly across daemon re-auths
    to a different Fulcra account — the same orphan-ingest hazard task
    #12 fixed for the attention plugin. See [[task #13]] for the
    generalisation and [[task #16]] for the tag_ids parity.

    When the def is re-resolved (cached value was stale on the current
    account), also invalidate the tag_ids dict — those tag UUIDs were
    populated alongside the now-orphan def and almost certainly belong
    to the prior account too. ensure_tag will repopulate fresh UUIDs
    from the current account on next access. Costs O(N) round-trips
    once, only after an account switch.
    """
    cached = getattr(media_state, attr, None)
    def_id = ctx.ensure_definition(
        cached=cached, expected_spec=spec, canonical_name=canonical_name,
    )
    if cached != def_id:
        setattr(media_state, attr, def_id)
        # Stale-def detected → tag cache is also suspect on an account
        # switch. Clear only when `cached` was truthy (i.e. we had a
        # cache and it was wrong) — first-run resolves shouldn't pay
        # the tag-rebuild cost.
        if cached and hasattr(media_state, "tag_ids"):
            media_state.tag_ids = {}
        _state_save(media_state)
    return def_id


def newest_event_iso(events: list) -> str | None:
    """The newest start_time across `events`, as an ISO string — the new
    watermark. None when there are no events."""
    if not events:
        return None
    return max(e.start_time for e in events).isoformat()


# ---------------------------------------------------------------------------
# Last.fm scheduled plugin
# ---------------------------------------------------------------------------

# The Fulcra annotation definition shape for the "Listened" DurationAnnotation.
# Passed to ctx.resolved_definition_id as the expected_spec so the shared
# resolver can verify an adopted definition has the right structure, or create
# a new one when none exists. Mirrors the payload produced by
# wire.duration_definition_payload (the bootstrap CLI path) — annotation_type
# and measurement_spec are the two axes that _spec_matches compares.
LASTFM_LISTENED_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


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
    _ensure_media_def(ctx, media_state, attr="listened_definition_id",
                       spec=LASTFM_LISTENED_SPEC, canonical_name="Listened")

    # Delegate to the shared helper; `since` (watermark - 1h) is computed
    # there so it stays consistent with the Deezer plugin.
    _run_scheduled_import(
        ctx,
        fetch=lambda since: fetch_recent_tracks(creds, since=since, max_pages=None),
        normalize=normalize_history,
        tag="lastfm",
    )


LASTFM_PLUGIN = Plugin(
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


# ---------------------------------------------------------------------------
# Shared helper: compute `since` from a watermark (1-hour rewind)
# ---------------------------------------------------------------------------

def _since_from_watermark(ctx: RunContext) -> datetime | None:
    """Return watermark - 1h as a tz-aware datetime, or None for full backfill.

    The 1-hour rewind hedges against late server-side reordering (same policy
    as Last.fm). Source-id dedup in the ingest layer discards any resulting
    duplicates.
    """
    if not ctx.state.watermark:
        return None
    return datetime.fromisoformat(
        ctx.state.watermark.replace("Z", "+00:00")
    ) - timedelta(hours=1)


def _run_scheduled_import(
    ctx: RunContext,
    *,
    fetch,
    normalize,
    tag: str,
) -> None:
    """Shared tail for simple fetch-normalize-import-advance scheduled plugins.

    Calls ``fetch(since)`` → ``normalize(raw)`` → ensure_tag + run_import →
    advances ctx.state.watermark to the newest processed event whenever the
    import completes — both posted and skipped-existing count as progress, so
    the all-duplicate steady state created by the 1-hour rewind window does
    not freeze the watermark.

    This eliminates copy-pasted watermark logic between Last.fm-shaped plugins.
    """
    since = _since_from_watermark(ctx)
    raw = list(fetch(since))
    events = list(normalize(raw))
    ctx.progress(stage="fetched", count=len(events))

    media_state = _state_load(STATE_PATH)
    client = FulcraClient()
    client.ensure_tag(tag, media_state)
    result = client.run_import(events, media_state)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)
    if result.posted > 0:
        ctx.annotation(
            f"{tag.capitalize()}: {result.posted} new annotation"
            + ("s" if result.posted != 1 else ""),
            ok=True,
        )

    # Advance even when posted == 0: every event in `events` was either posted
    # OR skipped-as-already-in-Fulcra — both count as successfully processed.
    # Gating on posted > 0 froze the watermark indefinitely in the all-duplicate
    # steady state created by the 1-hour rewind window above.
    new_wm = newest_event_iso(events)
    if new_wm:
        ctx.state.watermark = new_wm


# ---------------------------------------------------------------------------
# Deezer listening history scheduled plugin
# ---------------------------------------------------------------------------

# The Fulcra annotation definition shape for the "Listened" DurationAnnotation
# used by the deezer plugin.  Identical structure to LASTFM_LISTENED_SPEC and
# SPOTIFY_EXTENDED_LISTENED_SPEC — all three plugins produce "Listened" Duration
# annotations against the same shared definition.  Kept as a distinct constant
# so the resolver call below is self-documenting and so spec-shape tests are
# local to this plugin block.
DEEZER_LISTENED_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


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
    # rather than creating a duplicate — giving the same multi-machine dedup
    # guarantee that bootstrap provides without requiring bootstrap to have
    # been run on every machine.
    media_state = _state_load(STATE_PATH)
    _ensure_media_def(ctx, media_state, attr="listened_definition_id",
                       spec=DEEZER_LISTENED_SPEC, canonical_name="Listened")

    _run_scheduled_import(
        ctx,
        fetch=lambda since: deezer_importer.fetch_history(
            creds, since=since, max_pages=None
        ),
        normalize=deezer_importer.normalize_history,
        tag="deezer",
    )


DEEZER_PLUGIN = Plugin(
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


# ---------------------------------------------------------------------------
# Trakt watch history scheduled plugin
# ---------------------------------------------------------------------------

# The Fulcra annotation definition shape for the "Watched" DurationAnnotation
# used by the trakt plugin.  Same structure as NETFLIX_WATCHED_SPEC —
# all Watched plugins share the same definition.
TRAKT_WATCHED_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


def _run_trakt(ctx: RunContext) -> None:
    """Fetch Trakt watch history and import it, applying cluster and twin-dedup policy.

    Authentication: credentials are resolved in the following order:
      1. Keychain (set via the web-UI onboarding wizard OAuth flow — the new path).
         Reads ctx.credentials["access_token"] and ctx.credentials["client_id"].
      2. File-based TraktAuth (~/.config/fulcra-media/trakt.json) — the legacy
         path used by the old CLI wizard. Preserved for users who set up via
         `fulcra-media wizard trakt` before the web UI existed.

    If neither source provides credentials, raises RuntimeError with a clear
    instruction pointing to the web-UI wizard.

    Interactive cluster/twin-dedup policies are NOT supported in headless mode.
    Configure them via ctx.config:
      clusters:           "drop" | "sentinel:<YYYY>" | "keep"  (default: "keep")
      twin_policy:        "auto-discard" | "keep"               (default: "keep")
      cluster_threshold:  int                                    (default: 5)

    Setting either policy to "ask" raises RuntimeError so the failure is
    obvious rather than silently skipping dedup.
    """
    clusters_spec: str = ctx.config.get("clusters", "keep")
    twin_policy: str = ctx.config.get("twin_policy", "keep")
    cluster_threshold: int = int(ctx.config.get("cluster_threshold", 5))

    if clusters_spec == "ask":
        raise RuntimeError(
            "trakt: 'ask' cluster policy is interactive — "
            "set clusters to drop, keep, or sentinel:YYYY in config"
        )
    if twin_policy == "ask":
        raise RuntimeError(
            "trakt: 'ask' twin_policy is interactive — "
            "set twin_policy to auto-discard or keep in config"
        )

    # Fetch — prefer keychain credentials (set via web-UI OAuth wizard);
    # fall back to the legacy file-based TraktAuth for users who authenticated
    # via the old `fulcra-media wizard trakt` CLI path.
    access_token = ctx.credentials.get("access_token")
    client_id = ctx.credentials.get("client_id")
    if access_token and client_id:
        # New path: credentials came from the web-UI OAuth flow (keychain).
        api_headers = {
            "Authorization": f"Bearer {access_token}",
            "trakt-api-version": "2",
            "trakt-api-key": client_id,
            "Content-Type": "application/json",
        }
        items = list(trakt_importer.fetch_history_with_headers(api_headers))
    else:
        # Legacy path: try the file-based creds from the old CLI wizard.
        try:
            items = list(trakt_importer.fetch_history())
        except FileNotFoundError as exc:
            raise RuntimeError(
                "trakt: not authenticated — sign in via Fulcra Collect's "
                "web UI wizard or run `fulcra-media wizard trakt` first"
            ) from exc

    events = list(trakt_importer.normalize_history(items, cluster_threshold=cluster_threshold))
    ctx.progress(stage="fetched", count=len(events))

    # --- cluster policy ---------------------------------------------------
    # Build a ClusterPolicy from the config string and apply it to events.
    # "keep" is the do-nothing pass-through; parsing matches the CLI's
    # _resolve_cluster_policy non-interactive branches exactly.
    if clusters_spec == "keep":
        cluster_policy = ClusterPolicy(
            action="keep", cluster_size_threshold=cluster_threshold
        )
    elif clusters_spec == "drop":
        cluster_policy = ClusterPolicy(
            action="drop", cluster_size_threshold=cluster_threshold
        )
    elif clusters_spec.startswith("sentinel:"):
        try:
            year = int(clusters_spec.split(":", 1)[1])
        except ValueError as exc:
            raise RuntimeError(
                f"trakt: invalid clusters config {clusters_spec!r} — "
                "expected 'sentinel:YYYY'"
            ) from exc
        cluster_policy = ClusterPolicy(
            action="sentinel", sentinel_year=year,
            cluster_size_threshold=cluster_threshold,
        )
    else:
        raise RuntimeError(
            f"trakt: unknown clusters value {clusters_spec!r} — "
            "must be drop, keep, or sentinel:YYYY"
        )

    events = apply_cluster_policy(events, cluster_policy)

    # --- twin dedup -------------------------------------------------------
    # Mirror the non-interactive branches of cli._maybe_apply_twin_dedup.
    # "keep" → no-op.  "auto-discard" → drop any low-conf event whose
    # content_fingerprint matches a high-conf entry in the twin cache.
    if twin_policy != "keep":
        cached = twin_cache.load_for_twin_lookup()
        pairs = find_low_conf_twins(events, extra_pool=cached)
        if pairs and twin_policy == "auto-discard":
            to_drop = {twin_cache._source_id_of(low) for low, _high in pairs}
            events = apply_twin_decisions(events, to_drop)

    # --- definition resolver + import + watermark advance ----------------
    # Ensure the "Watched" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # watched_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Watched" definition
    # rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    _ensure_media_def(ctx, media_state, attr="watched_definition_id",
                       spec=TRAKT_WATCHED_SPEC, canonical_name="Watched")

    client = FulcraClient()
    client.ensure_tag("trakt", media_state)
    result = client.run_import(events, media_state)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)
    if result.posted > 0:
        ctx.annotation(
            f"Trakt: {result.posted} new annotation"
            + ("s" if result.posted != 1 else ""),
            ok=True,
        )

    # Advance even when posted == 0 — see _run_scheduled_import for the full
    # rationale. Skipped-existing means the event is already in Fulcra; both
    # outcomes are progress the watermark must reflect.
    new_wm = newest_event_iso(events)
    if new_wm:
        ctx.state.watermark = new_wm


TRAKT_PLUGIN = Plugin(
    id="trakt",
    name="Trakt watch history",
    kind="scheduled",
    run=_run_trakt,
    description=(
        "Records your TV and movie watch history from Trakt.tv — which "
        "covers Netflix, Apple TV+, Plex, and most other video services "
        "via Trakt's scrobbler plugins. We sync new watches every 6 hours. "
        "You'll create a free Trakt OAuth app and sign in once."
    ),
    default_interval=timedelta(hours=6),
    category="video",
    canonical_definition_name="Watched",
    required_credentials=(
        Credential(
            key="client_id",
            label="Trakt Client ID",
            help="From your Trakt OAuth application's settings page.",
        ),
        Credential(
            key="client_secret",
            label="Trakt Client Secret",
            help="From your Trakt OAuth application's settings page.",
        ),
        Credential(
            key="access_token",
            label="Trakt Access Token",
            help="Set automatically when you sign in to Trakt.",
        ),
        Credential(
            key="refresh_token",
            label="Trakt Refresh Token",
            help="Set automatically when you sign in to Trakt.",
        ),
    ),
    oauth_handler=trakt_oauth_handler,
    oauth_authorize_url=trakt_authorize_url,
    health_check=trakt_health_check,
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What Trakt does",
            body_md=(
                "Trakt tracks your TV and movie watch history. "
                "Once connected, every time you finish a show or movie, "
                "it'll be recorded as a Watched annotation in your "
                "Fulcra account."
            ),
        ),
        SetupStep(
            kind="external_action",
            title="Create a Trakt OAuth app",
            body_md=(
                "Go to https://trakt.tv/oauth/applications and click "
                "**New Application**. Fill the form in like this:\n\n"
                "- **Name:** `Fulcra Collect`\n"
                "- **Icon:** leave blank — we don't need it.\n"
                "- **Description:** leave blank (or write anything you "
                "like; it's only shown when the app asks for permissions).\n"
                "- **JavaScript (CORS) origins:** leave blank.\n\n"
                "**Redirect URI — read this carefully.**\n\n"
                "Trakt pre-fills this field with "
                "`urn:ietf:wg:oauth:2.0:oob`. **Delete that default** "
                "and replace it with exactly:\n\n"
                "`http://127.0.0.1:9292/api/oauth/trakt/callback`\n\n"
                "If you leave the default in place, the sign-in step will "
                "fail with 'Invalid redirect URI'. (Note: the port shown "
                "in the next step may differ if you've changed Preferences "
                "— update this URI to match if so.)\n\n"
                "**Permissions — uncheck both checkboxes.**\n\n"
                "Trakt pre-checks `/checkin` and `/scrobble`. "
                "**Uncheck both** before saving. Fulcra Collect only reads "
                "your watch history; it does not need write access. "
                "(`/users/me/history` is gated by the OAuth grant itself, "
                "not these scopes.)\n\n"
                "Click **Save App** and copy the **Client ID** and "
                "**Client Secret** to the next step."
            ),
            external_link="https://trakt.tv/oauth/applications",
        ),
        SetupStep(
            kind="input",
            title="Paste your Trakt OAuth credentials",
            body_md=(
                "Trakt will have shown you the **Client ID** and **Client "
                "Secret** after you saved the app. Paste each into the "
                "matching field below. The wizard will store them in your "
                "macOS keychain."
            ),
            settings_keys=("client_id", "client_secret"),
        ),
        SetupStep(
            kind="oauth",
            title="Sign in to Trakt",
            body_md=(
                "Click below to authorize Fulcra Collect to read "
                "your Trakt history."
            ),
        ),
        SetupStep(
            kind="test_connection",
            title="Verify connection",
            body_md="Fetching your most recent watches from Trakt…",
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write your Trakt watches?",
            body_md=(
                "We can write to your existing 'Watched' annotation "
                "or create a new one."
            ),
            annotation_type="duration",
        ),
        SetupStep(
            kind="done",
            title="You're set",
            body_md=(
                "Trakt will sync every 6 hours. "
                "You can change this in Preferences."
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# Shared helpers for file-based manual plugins
# ---------------------------------------------------------------------------

def _resolve_path(ctx: RunContext) -> Path:
    """Read `path` from ctx.config, raising RuntimeError if absent."""
    raw = ctx.config.get("path")
    if not raw:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'path' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    return library.resolve(raw)


def _import_events(ctx: RunContext, events: list, tag: str) -> None:
    """Run the standard ensure_tag + run_import pipeline and report progress."""
    ctx.progress(stage="parsed", count=len(events))
    media_state = _state_load(STATE_PATH)
    client = FulcraClient()
    client.ensure_tag(tag, media_state)
    result = client.run_import(events, media_state)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)
    if result.posted > 0:
        ctx.annotation(
            f"{tag.capitalize()}: {result.posted} new annotation"
            + ("s" if result.posted != 1 else ""),
            ok=True,
        )


def _run_file_import(ctx: RunContext, *, parse, tag: str) -> None:
    """Resolve path → parse → import.  Used by the three simple file plugins."""
    resolved = _resolve_path(ctx)
    events = list(parse(resolved))
    _import_events(ctx, events, tag)


# ---------------------------------------------------------------------------
# Netflix manual plugin
# ---------------------------------------------------------------------------

# The Fulcra annotation definition shape for the "Watched" DurationAnnotation
# used by the netflix plugin.  Mirrors wire.duration_definition_payload
# defaults and LASTFM_LISTENED_SPEC — same structure, different canonical name.
NETFLIX_WATCHED_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


def _run_netflix(ctx: RunContext) -> None:
    # Ensure the "Watched" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # watched_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Watched" definition
    # rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    _ensure_media_def(ctx, media_state, attr="watched_definition_id",
                       spec=NETFLIX_WATCHED_SPEC, canonical_name="Watched")

    _run_file_import(
        ctx,
        parse=netflix_importer.parse_auto,
        tag="netflix",
    )


NETFLIX_PLUGIN = Plugin(
    id="netflix",
    name="Netflix viewing history",
    kind="manual",
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


# ---------------------------------------------------------------------------
# Spotify Extended Streaming History manual plugin
# ---------------------------------------------------------------------------

# The Fulcra annotation definition shape for the "Listened" DurationAnnotation
# used by the spotify-extended plugin.  Identical structure to
# LASTFM_LISTENED_SPEC — both plugins produce "Listened" Duration annotations
# against the same shared definition.  Kept as a distinct constant so the
# resolver call below is self-documenting and so spec-shape tests are local to
# each plugin block.
SPOTIFY_EXTENDED_LISTENED_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


def _run_spotify_extended(ctx: RunContext) -> None:
    # Ensure the "Listened" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # listened_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Listened" definition
    # rather than creating a duplicate.  lastfm and spotify-extended share the
    # same State.listened_definition_id field — whichever plugin runs first
    # on a new machine will populate it; the other will find it already set.
    media_state = _state_load(STATE_PATH)
    _ensure_media_def(ctx, media_state, attr="listened_definition_id",
                       spec=SPOTIFY_EXTENDED_LISTENED_SPEC,
                       canonical_name="Listened")

    _run_file_import(
        ctx,
        parse=spotify_importer.parse_extended_zip,
        tag="spotify",
    )


SPOTIFY_EXTENDED_PLUGIN = Plugin(
    id="spotify-extended",
    name="Spotify Extended Streaming History",
    kind="manual",
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


# ---------------------------------------------------------------------------
# YouTube watch history manual plugin
# ---------------------------------------------------------------------------

# The Fulcra annotation definition shape for the "Watched" DurationAnnotation
# used by the youtube plugin.  Same structure as NETFLIX_WATCHED_SPEC —
# all Watched plugins share the same definition.
YOUTUBE_WATCHED_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


def _run_youtube(ctx: RunContext) -> None:
    # Ensure the "Watched" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # watched_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Watched" definition
    # rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    _ensure_media_def(ctx, media_state, attr="watched_definition_id",
                       spec=YOUTUBE_WATCHED_SPEC, canonical_name="Watched")

    _run_file_import(
        ctx,
        parse=youtube_importer.parse_takeout_json,
        tag="youtube",
    )


YOUTUBE_PLUGIN = Plugin(
    id="youtube",
    name="YouTube watch history",
    kind="manual",
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


# ---------------------------------------------------------------------------
# Spotify IFTTT/GDrive backfill manual plugin
# ---------------------------------------------------------------------------

# The Fulcra annotation definition shape for the "Listened" DurationAnnotation
# used by the spotify-ifttt plugin.  Identical structure to
# LASTFM_LISTENED_SPEC and SPOTIFY_EXTENDED_LISTENED_SPEC — all "Listened"
# plugins produce Duration annotations against the same shared definition.
# Kept as a distinct constant so the resolver call below is self-documenting
# and so spec-shape tests are local to this plugin block.
SPOTIFY_IFTTT_LISTENED_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


def _run_spotify_ifttt(ctx: RunContext) -> None:
    # Ensure the "Listened" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # listened_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Listened" definition
    # rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    _ensure_media_def(ctx, media_state, attr="listened_definition_id",
                       spec=SPOTIFY_IFTTT_LISTENED_SPEC,
                       canonical_name="Listened")

    resolved = _resolve_path(ctx)
    tz = ZoneInfo(ctx.config.get("tz", "UTC"))
    events = list(spotify_ifttt_importer.parse_ifttt_zip(resolved, tz=tz))
    _import_events(ctx, events, "spotify")


# NOTE (2026-05-24): SPOTIFY_IFTTT_PLUGIN is intentionally NOT registered as a
# fulcra_collect.plugins entry-point. It was a one-time backfill tool and does
# not belong in the default menubar plugin list. The code remains here for
# manual backfill use via `uv run --package fulcra-media-helpers ...`.
SPOTIFY_IFTTT_PLUGIN = Plugin(
    id="spotify-ifttt",
    name="Spotify IFTTT/GDrive backfill",
    kind="manual",
    run=_run_spotify_ifttt,
    default_interval=None,
    canonical_definition_name="Listened",
    required_credentials=(),
)


# ---------------------------------------------------------------------------
# Apple TV playback (takeout) manual plugin
# ---------------------------------------------------------------------------

# The Fulcra annotation definition shape for the "Watched" DurationAnnotation
# used by the apple-takeout plugin.  Same structure as NETFLIX_WATCHED_SPEC —
# all Watched plugins share the same definition.
APPLE_TAKEOUT_WATCHED_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


def _run_apple_takeout(ctx: RunContext) -> None:
    # Ensure the "Watched" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # watched_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Watched" definition
    # rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    _ensure_media_def(ctx, media_state, attr="watched_definition_id",
                       spec=APPLE_TAKEOUT_WATCHED_SPEC,
                       canonical_name="Watched")

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

    resolved = _resolve_path(ctx)
    # The importer's parse_any handles file / dir / zip / nested-zip
    # itself; we just give it the path the user configured.
    events = list(apple_takeout_importer.parse_any(
        resolved, since=since_cutoff, until=until_cutoff,
    ))
    _import_events(ctx, events, "apple-tv")


APPLE_TAKEOUT_PLUGIN = Plugin(
    id="apple-takeout",
    name="Apple TV playback (takeout)",
    kind="manual",
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


# ---------------------------------------------------------------------------
# Apple Music listens (takeout) manual plugin
# ---------------------------------------------------------------------------

# The Fulcra annotation definition shape for the "Listened" DurationAnnotation
# used by the apple-music-takeout plugin. Same structure as
# LASTFM_LISTENED_SPEC and the other "Listened" plugins.
APPLE_MUSIC_TAKEOUT_LISTENED_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


def _run_apple_music_takeout(ctx: RunContext) -> None:
    # Ensure the "Listened" annotation definition is known before importing.
    # All audio plugins (Last.fm, Deezer, Spotify Extended, Apple Music)
    # share the listened_definition_id field — whichever plugin first runs
    # on this machine populates it; this one will find it already set.
    media_state = _state_load(STATE_PATH)
    _ensure_media_def(ctx, media_state, attr="listened_definition_id",
                       spec=APPLE_MUSIC_TAKEOUT_LISTENED_SPEC,
                       canonical_name="Listened")

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

    resolved = _resolve_path(ctx)
    events = list(apple_music_takeout_importer.parse_any(
        resolved, since=since_cutoff, until=until_cutoff,
    ))
    _import_events(ctx, events, "apple-music")


APPLE_MUSIC_TAKEOUT_PLUGIN = Plugin(
    id="apple-music-takeout",
    name="Apple Music listens (takeout)",
    kind="manual",
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


# ---------------------------------------------------------------------------
# Shared helper for RSS scheduled plugins
# ---------------------------------------------------------------------------

def _rss_since(ctx: RunContext) -> datetime | None:
    """Parse ctx.state.watermark into a tz-aware datetime, or None for full backfill.

    RSS feeds are append-only and ordered, so a plain >= comparison is
    sufficient — no rewind needed (unlike Last.fm's 1-hour rewind).
    """
    if not ctx.state.watermark:
        return None
    return datetime.fromisoformat(
        ctx.state.watermark.replace("Z", "+00:00")
    )


def _rss_import_and_advance(
    ctx: RunContext,
    events: list,
    *,
    tag: str,
    since: datetime | None,
    max_entries: int | None,
) -> None:
    """Filter events by watermark, optionally cap, import, and advance watermark.

    This is the shared tail common to all three RSS plugins:
      1. Filter to events at/after `since` (skip when since is None — full backfill).
      2. Sort ascending by start_time, then apply max_entries cap.
      3. ensure_tag + run_import.
      4. Advance ctx.state.watermark to the newest processed event.
    """
    if since is not None:
        events = [e for e in events if e.start_time >= since]
    # Sort oldest-first so the `max_entries` cap deterministically keeps the
    # oldest contiguous block. Without this, a newest-first feed would lose
    # its older middle history forever: the cap would keep the newest N, the
    # watermark would jump past everything older, and the next run would
    # filter that older history out via `since`.
    events.sort(key=lambda e: e.start_time)
    if max_entries is not None:
        events = events[:max_entries]

    ctx.progress(stage="fetched", count=len(events))
    media_state = _state_load(STATE_PATH)
    client = FulcraClient()
    client.ensure_tag(tag, media_state)
    result = client.run_import(events, media_state)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)
    if result.posted > 0:
        ctx.annotation(
            f"{tag.capitalize()}: {result.posted} new annotation"
            + ("s" if result.posted != 1 else ""),
            ok=True,
        )

    # Advance even when posted == 0 — see _run_scheduled_import for rationale.
    new_wm = newest_event_iso(events)
    if new_wm:
        ctx.state.watermark = new_wm


# ---------------------------------------------------------------------------
# Generic RSS/Atom scheduled plugin
# ---------------------------------------------------------------------------

# Maps the runtime config category to the canonical Fulcra definition name.
# canonical_definition_name is intentionally absent from GENERIC_RSS_PLUGIN
# because it depends on runtime config, not on the Plugin definition itself.
_CATEGORY_TO_CANONICAL: dict[str, str] = {
    "watched": "Watched",
    "listened": "Listened",
    "read": "Read",
}

# Shared duration-annotation spec shape used by all three category branches.
# All typed-media definitions share the same structure; category is expressed
# only via the canonical_name argument passed to the resolver.
_GENERIC_DURATION_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


def _run_generic_rss(ctx: RunContext) -> None:
    feed_url = ctx.config.get("feed_url")
    if not feed_url:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'feed_url' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    service = ctx.config.get("service")
    if not service:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'service' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    category = ctx.config.get("category")
    if not category:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'category' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    max_entries: int | None = ctx.config.get("max_entries")

    # Ensure the correct annotation definition is known before importing.
    # The category (watched/listened/read) is set per-instance via plugin config,
    # so we look it up at run-time and call the resolver with the matching
    # canonical name.  On a fresh install (machine 2) the target field in
    # media state may be absent; the resolver adopts the existing definition
    # rather than creating a duplicate.
    canonical = _CATEGORY_TO_CANONICAL[category]
    target_field = f"{category}_definition_id"
    media_state = _state_load(STATE_PATH)
    _ensure_media_def(ctx, media_state, attr=target_field,
                       spec=_GENERIC_DURATION_SPEC, canonical_name=canonical)

    since = _rss_since(ctx)
    all_events = list(rss_importer.normalize_feed(feed_url, service=service, category=category))
    _rss_import_and_advance(ctx, all_events, tag=service, since=since,
                            max_entries=max_entries)


GENERIC_RSS_PLUGIN = Plugin(
    id="generic-rss",
    name="Generic RSS/Atom feed",
    kind="scheduled",
    run=_run_generic_rss,
    health_check=generic_rss_health_check,
    description=(
        "Watches any RSS or Atom feed and records each new entry as a "
        "Fulcra annotation. You set the feed URL, the service tag, and "
        "the category (watched / listened / read). Runs every 6 hours."
    ),
    default_interval=timedelta(hours=6),
    category="other",
    # canonical_definition_name is intentionally absent: the canonical identity
    # depends on the runtime config value of "category", not on the Plugin
    # definition itself.  See _CATEGORY_TO_CANONICAL and _run_generic_rss.
    required_credentials=(),
    required_settings=(
        Setting(
            key="feed_url",
            label="Feed URL",
            kind="url",
            help="RSS or Atom feed URL we'll poll every 6 hours.",
            placeholder="https://example.com/feed.xml",
        ),
        Setting(
            key="service",
            label="Service tag",
            kind="text",
            help=(
                "Short identifier we'll attach to each event "
                "(e.g. 'pinboard', 'feedly'). Used for dedup and display."
            ),
        ),
        Setting(
            key="category",
            label="Category",
            kind="enum",
            enum_values=("watched", "listened", "read"),
            help=(
                "Which canonical annotation to write to — 'watched' for "
                "video, 'listened' for audio, 'read' for text/books."
            ),
        ),
    ),
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What this plugin does",
            body_md=(
                "Point this at any RSS or Atom feed — e.g. a personal "
                "bookmarking export, a podcast feed, or any service that "
                "publishes activity over RSS. Every 6 hours we'll fetch "
                "new entries and record them as Fulcra annotations."
            ),
        ),
        SetupStep(
            kind="input",
            title="Configure the feed",
            body_md=(
                "Enter the **feed URL**, a short **service** tag we'll "
                "use to label events, and pick a **category** — "
                "'watched' for video, 'listened' for audio, 'read' for "
                "books/text. The category determines which canonical "
                "annotation we write to."
            ),
            settings_keys=("feed_url", "service", "category"),
        ),
        SetupStep(
            kind="test_connection",
            title="Verify the feed",
            body_md=(
                "We'll fetch the feed and show you the most recent "
                "entries so you can confirm it's reachable and shaped "
                "the way you expect."
            ),
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write these entries?",
            body_md=(
                "We can write to your existing Watched/Listened/Read "
                "annotation (whichever matches your category) or create "
                "a new one."
            ),
            annotation_type="duration",
        ),
        SetupStep(
            kind="done",
            title="You're set",
            body_md="The feed will be polled every 6 hours.",
        ),
    ),
)


# ---------------------------------------------------------------------------
# Letterboxd film diary scheduled plugin
# ---------------------------------------------------------------------------

# The Fulcra annotation definition shape for the "Watched" DurationAnnotation
# used by the letterboxd plugin.  Same structure as NETFLIX_WATCHED_SPEC —
# all Watched plugins share the same definition.
LETTERBOXD_WATCHED_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


def _extract_letterboxd_username(raw: str) -> str:
    """Pull a Letterboxd username out of whatever the user pasted.

    Accepts URL forms (`https://letterboxd.com/foo`, `letterboxd.com/foo/`,
    `letterboxd.com/foo/films/diary/`), bare usernames (`foo`), and the
    `@foo` shorthand. Strips trailing slashes and path segments so the RSS
    fetcher gets just the username.

    User feedback 2026-05-26: Goodreads had the same pain — wizard asked
    for a numeric ID and got a URL pasted. Parsing it permissively here
    saves the user the same dance.
    """
    import re
    s = (raw or "").strip()
    # URL shape: optional scheme, letterboxd.com, /username, optional path
    m = re.search(r"letterboxd\.com/([A-Za-z0-9_-]+)", s)
    if m:
        return m.group(1)
    # Bare username — strip leading @ if present
    bare = s.lstrip("@").rstrip("/")
    if bare and "/" not in bare and " " not in bare:
        return bare
    raise RuntimeError(
        f"letterboxd: couldn't find a username in {raw!r}. "
        "Expected something like 'username' or "
        "'https://letterboxd.com/username'."
    )


def _run_letterboxd(ctx: RunContext) -> None:
    raw_username = ctx.config.get("username")
    if not raw_username:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'username' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    username = _extract_letterboxd_username(raw_username)
    max_entries: int | None = ctx.config.get("max_entries")

    # Ensure the "Watched" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # watched_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Watched" definition
    # rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    _ensure_media_def(ctx, media_state, attr="watched_definition_id",
                       spec=LETTERBOXD_WATCHED_SPEC,
                       canonical_name="Watched")

    since = _rss_since(ctx)
    all_events = list(lb_importer.fetch_diary(username))
    _rss_import_and_advance(ctx, all_events, tag="letterboxd", since=since,
                            max_entries=max_entries)


LETTERBOXD_PLUGIN = Plugin(
    id="letterboxd",
    name="Letterboxd film diary",
    kind="scheduled",
    run=_run_letterboxd,
    health_check=letterboxd_health_check,
    description=(
        "Polls your public Letterboxd diary RSS feed every 12 hours. "
        "Each diary entry becomes a 'Watched' annotation in Fulcra. "
        "Only needs your Letterboxd username (no API key)."
    ),
    default_interval=timedelta(hours=12),
    category="video",
    canonical_definition_name="Watched",
    required_credentials=(),
    required_settings=(
        Setting(
            key="username",
            label="Your Letterboxd profile",
            kind="text",
            help=(
                "Either your profile URL (paste from the browser — "
                "e.g. `https://letterboxd.com/your-name`) or just the "
                "username. We'll extract the right part."
            ),
        ),
    ),
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What this plugin does",
            body_md=(
                "Letterboxd publishes your public film diary as an RSS "
                "feed. We poll it every 12 hours and record each entry "
                "as a 'Watched' annotation. No API key needed — just "
                "your username."
            ),
        ),
        SetupStep(
            kind="input",
            title="Enter your Letterboxd username",
            body_md=(
                "Your **username** is the slug after `letterboxd.com/` "
                "in your profile URL — for example `letterboxd.com/"
                "yourname` means your username is `yourname`. Your "
                "diary must be public for the RSS feed to work."
            ),
            settings_keys=("username",),
        ),
        SetupStep(
            kind="test_connection",
            title="Verify your Letterboxd diary",
            body_md=(
                "We'll fetch your public diary RSS feed and show you "
                "the most recent films so you can confirm we're looking "
                "at the right profile."
            ),
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write your Letterboxd diary?",
            body_md=(
                "We can write to your existing 'Watched' annotation or "
                "create a new one."
            ),
            annotation_type="duration",
        ),
        SetupStep(
            kind="done",
            title="You're set",
            body_md="Letterboxd will sync every 12 hours.",
        ),
    ),
)


# ---------------------------------------------------------------------------
# Goodreads read shelf scheduled plugin
# ---------------------------------------------------------------------------

# The Fulcra annotation definition shape for the "Read" DurationAnnotation
# used by the goodreads plugin.  Same structure as NETFLIX_WATCHED_SPEC and
# LASTFM_LISTENED_SPEC — all typed-media plugins share the same duration shape;
# only the canonical name differs.  Kept as a distinct constant so the resolver
# call below is self-documenting and spec-shape tests are local to this block.
GOODREADS_READ_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


def _extract_goodreads_user_id(raw: str) -> str:
    """Extract the numeric Goodreads user ID from whatever the user pasted.

    Accepts any of:
      - `12345678`              — bare numeric ID
      - `12345678-singularity`  — numeric ID with a Goodreads name slug
      - `https://www.goodreads.com/user/show/12345678-singularity` — full URL
      - `goodreads.com/user/show/12345678` — URL without scheme
      - mixed whitespace, trailing query strings, etc.

    Returns the bare numeric ID string. Raises RuntimeError if no numeric
    ID can be found, so the user gets a clear "we couldn't parse that"
    message instead of a silent 404 from the RSS fetch.

    User feedback 2026-05-26: wizard required users to extract the numeric
    ID from their profile URL by hand. Now they paste anything Goodreads-y
    and we figure it out.
    """
    import re
    s = (raw or "").strip()
    # Try the URL shape first: /user/show/<digits>[-name]
    m = re.search(r"/user/show/(\d+)", s)
    if m:
        return m.group(1)
    # Then a bare ID at the start, optionally followed by -name
    m = re.match(r"^(\d+)(?:-\S*)?$", s)
    if m:
        return m.group(1)
    raise RuntimeError(
        f"goodreads: couldn't find a numeric user ID in {raw!r}. "
        "Expected something like '12345678' or "
        "'https://www.goodreads.com/user/show/12345678-name'."
    )


def _run_goodreads(ctx: RunContext) -> None:
    raw_user_id = ctx.config.get("user_id")
    if not raw_user_id:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'user_id' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    user_id = _extract_goodreads_user_id(raw_user_id)
    max_entries: int | None = ctx.config.get("max_entries")

    # Ensure the "Read" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # read_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Read" definition
    # rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    _ensure_media_def(ctx, media_state, attr="read_definition_id",
                       spec=GOODREADS_READ_SPEC, canonical_name="Read")

    since = _rss_since(ctx)
    all_events = list(gr_importer.fetch_diary(user_id))
    _rss_import_and_advance(ctx, all_events, tag="goodreads", since=since,
                            max_entries=max_entries)


GOODREADS_PLUGIN = Plugin(
    id="goodreads",
    name="Goodreads read shelf",
    kind="scheduled",
    run=_run_goodreads,
    health_check=goodreads_health_check,
    description=(
        "Polls your Goodreads 'read' shelf RSS feed every 12 hours. "
        "Anything you mark as read on Goodreads becomes a 'Read' "
        "annotation in Fulcra. Read-only — we never write to Goodreads."
    ),
    default_interval=timedelta(hours=12),
    category="books",
    canonical_definition_name="Read",
    required_credentials=(),
    required_settings=(
        Setting(
            key="user_id",
            label="Your Goodreads profile",
            kind="text",
            help=(
                "Either your profile URL (paste it from the browser — "
                "e.g. `https://www.goodreads.com/user/show/12345678-your-name`) "
                "or just the numeric ID. We'll extract the right part."
            ),
        ),
    ),
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What this plugin does",
            body_md=(
                "Goodreads publishes your 'read' shelf as an RSS feed. "
                "We poll it every 12 hours and record each book you "
                "mark as read as a 'Read' annotation in Fulcra."
            ),
        ),
        SetupStep(
            kind="external_action",
            title="Find your Goodreads user ID",
            body_md=(
                "Sign in to Goodreads and click your profile picture. "
                "Your profile URL looks like "
                "`goodreads.com/user/show/12345678-your-name`. Copy the "
                "numeric portion (`12345678` in this example) — that's "
                "your user ID. Your profile must be public for the RSS "
                "feed to be reachable."
            ),
            external_link="https://www.goodreads.com",
        ),
        SetupStep(
            kind="input",
            title="Enter your Goodreads user ID",
            body_md=(
                "Paste the numeric user ID you copied from your "
                "Goodreads profile URL."
            ),
            settings_keys=("user_id",),
        ),
        SetupStep(
            kind="test_connection",
            title="Verify your Goodreads shelf",
            body_md=(
                "We'll fetch your 'read' shelf RSS feed and show you "
                "the most recent books so you can confirm we're looking "
                "at the right profile."
            ),
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write your Goodreads reads?",
            body_md=(
                "We can write to your existing 'Read' annotation or "
                "create a new one."
            ),
            annotation_type="duration",
        ),
        SetupStep(
            kind="done",
            title="You're set",
            body_md="Goodreads will sync every 12 hours.",
        ),
    ),
)


# ---------------------------------------------------------------------------
# Apple Podcasts — shared helpers
# ---------------------------------------------------------------------------

_FULL_DISK_ACCESS_PERMISSION = Permission(
    id="full-disk-access",
    explanation=(
        "Reads the on-device Apple Podcasts database (~/Library/...), which macOS "
        "guards behind Full Disk Access."
    ),
)


def apple_podcasts_permission_check(ctx) -> dict:
    """Verify Full Disk Access by attempting to open the Podcasts SQLite DB.

    Returns {"granted": bool, "hint": str | None}. The wizard's
    permission_request step calls this so it can show a real
    "verified / not verified" status instead of guessing — Full Disk
    Access never produces an in-app prompt, so this round-trip is the
    only way to know whether the user actually added the binary in
    System Settings.
    """
    import sqlite3
    import glob
    from pathlib import Path
    pattern = str(
        Path.home() / "Library/Group Containers/*.podcasts*/Documents/MTLibrary.sqlite"
    )
    candidates = glob.glob(pattern)
    if not candidates:
        return {
            "granted": False,
            "hint": (
                "No Podcasts database found — Apple Podcasts may not be "
                "installed or has never run."
            ),
        }
    db_path = candidates[0]
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return {"granted": True, "hint": None}
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "permission" in msg or "unable to open" in msg or "authorization denied" in msg:
            return {
                "granted": False,
                "hint": (
                    "Full Disk Access not granted. Add the terminal running "
                    "fulcra-collect to System Settings -> Privacy & Security "
                    "-> Full Disk Access."
                ),
            }
        return {"granted": False, "hint": f"sqlite error: {exc}"}
    except Exception as exc:
        return {"granted": False, "hint": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Apple Podcasts (on-device) scheduled plugin
# ---------------------------------------------------------------------------

# The Fulcra annotation definition shape for the "Listened" DurationAnnotation
# used by the apple-podcasts plugin.  Identical structure to
# LASTFM_LISTENED_SPEC and SPOTIFY_EXTENDED_LISTENED_SPEC — all "Listened"
# plugins produce Duration annotations against the same shared definition.
# Kept as a distinct constant so the resolver call below is self-documenting
# and so spec-shape tests are local to this plugin block.
APPLE_PODCASTS_LISTENED_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


def _run_apple_podcasts(ctx: RunContext) -> None:
    """Read the local Apple Podcasts SQLite DB and import played episodes.

    Reads ctx.config["db_path"] if set, otherwise uses the default DB location
    (~/Library/Group Containers/…/MTLibrary.sqlite).  The whole DB is parsed on
    every run; source-id dedup in the ingest layer handles re-imports.  The
    watermark is advanced after a successful run so progress is visible.

    A SnapshotError (the DB is I/O-stalled, e.g. mid-iCloud-sync) is surfaced
    as a RuntimeError with a clear message so the scheduler can retry later.
    """
    raw_path = ctx.config.get("db_path")
    db_path = Path(raw_path) if raw_path else ap.DEFAULT_DB_PATH

    try:
        events = list(ap.parse_db(db_path))
    except ap.SnapshotError as exc:
        raise RuntimeError(
            f"apple-podcasts: DB snapshot failed — the database is likely "
            f"I/O-stalled (iCloud sync in progress). Try again later. "
            f"Details: {exc}"
        ) from exc

    ctx.progress(stage="parsed", count=len(events))

    # Ensure the "Listened" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # listened_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Listened" definition
    # rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    _ensure_media_def(ctx, media_state, attr="listened_definition_id",
                       spec=APPLE_PODCASTS_LISTENED_SPEC,
                       canonical_name="Listened")

    client = FulcraClient()
    client.ensure_tag("apple-podcasts", media_state)
    result = client.run_import(events, media_state)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)
    if result.posted > 0:
        ctx.annotation(
            f"Apple-podcasts: {result.posted} new annotation"
            + ("s" if result.posted != 1 else ""),
            ok=True,
        )

    # Advance even when posted == 0 — see _run_scheduled_import for rationale.
    new_wm = newest_event_iso(events)
    if new_wm:
        ctx.state.watermark = new_wm


APPLE_PODCASTS_PLUGIN = Plugin(
    id="apple-podcasts",
    name="Apple Podcasts (on-device)",
    kind="scheduled",
    run=_run_apple_podcasts,
    description=(
        "Captures podcast episodes you finish on your Mac by reading the "
        "local Apple Podcasts database directly — the app doesn't need to "
        "be open. Runs every 6 hours. Needs Full Disk Access so macOS lets "
        "the daemon read the Podcasts SQLite file."
    ),
    default_interval=timedelta(hours=6),
    requires_network=False,
    category="audio",
    canonical_definition_name="Listened",
    # The wizard's permission_request step currently gates on FDA
    # (nextBlocked = !permissionResult.ok), so FDA is effectively required
    # for onboarding even though the underlying importer would work without
    # it if the daemon process already inherits read access (e.g. its parent
    # has FDA). See task #74 for the planned soft-gate follow-up.
    required_permissions=(_FULL_DISK_ACCESS_PERMISSION,),
    required_credentials=(),
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What this plugin does",
            body_md=(
                "We read your local Apple Podcasts database every 6 hours — "
                "the Podcasts app doesn't need to be open. iCloud sync stays "
                "fresh only while macOS can run the Podcasts extension in the "
                "background; if you quit the app for days, the DB may fall "
                "behind."
            ),
        ),
        SetupStep(
            kind="permission_request",
            title="Grant Full Disk Access",
            body_md=(
                "Full Disk Access is the fallback path when sandboxing "
                "blocks the direct read of the Podcasts database. Click "
                "Next to test whether access works already. If the test "
                "fails, grant Full Disk Access in **System Settings -> "
                "Privacy & Security -> Full Disk Access** by clicking "
                "**+** and adding the terminal you're running the daemon "
                "from (or the bundled fulcra-collect.app once it exists)."
            ),
        ),
        # Verify the DB is readable before letting the user advance. The
        # wizard's generic test_connection renderer calls
        # /api/plugin/apple-podcasts/health_check, which runs
        # apple_podcasts_health_check (DB open + COUNT of played episodes)
        # and gates Next on ok=True. Without this, a missing-FDA error
        # only surfaces at the first scheduled run hours later.
        SetupStep(
            kind="test_connection",
            title="Verify your Podcasts library",
            body_md=(
                "We'll open your Podcasts database read-only and count "
                "played episodes. No data is sent to Fulcra yet — this "
                "just confirms the file is readable."
            ),
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write your podcast listens?",
            body_md=(
                "We can write to your existing 'Listened' annotation or "
                "create a new one."
            ),
            annotation_type="duration",
        ),
        SetupStep(
            kind="done",
            title="You're set",
            body_md="Apple Podcasts will sync every 6 hours.",
        ),
    ),
    permission_check=apple_podcasts_permission_check,
    health_check=apple_podcasts_health_check,
)


# ---------------------------------------------------------------------------
# Apple Podcasts (Time Machine recovery) manual plugin
# ---------------------------------------------------------------------------

# The Fulcra annotation definition shape for the "Listened" DurationAnnotation
# used by the apple-podcasts-timemachine plugin.  Identical structure to
# APPLE_PODCASTS_LISTENED_SPEC — both plugins produce "Listened" Duration
# annotations from Apple Podcasts data against the same shared definition.
# Kept as a distinct constant so the resolver call below is self-documenting
# and so spec-shape tests are local to this plugin block.
APPLE_PODCASTS_TIMEMACHINE_LISTENED_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


def _run_apple_podcasts_timemachine(ctx: RunContext) -> None:
    """Walk all Time Machine snapshots and import Apple Podcasts history from each.

    This is a one-shot recovery operation — it imports every played episode
    found across every Time Machine backup.  No watermark is advanced because
    the run is manual and non-incremental; source-id dedup in the ingest layer
    prevents duplicate annotations.

    Raises RuntimeError if no Time Machine snapshots are found (volume not
    mounted).  A SnapshotError on an individual snapshot is logged and skipped
    so a single bad backup does not abort the whole recovery walk.
    """
    # Ensure the "Listened" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # listened_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Listened" definition
    # rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    _ensure_media_def(ctx, media_state, attr="listened_definition_id",
                       spec=APPLE_PODCASTS_TIMEMACHINE_LISTENED_SPEC,
                       canonical_name="Listened")

    snapshots = ap.find_timemachine_snapshots()
    if not snapshots:
        raise RuntimeError(
            "apple-podcasts-timemachine: no Time Machine snapshots found — "
            "a Time Machine volume must be mounted"
        )

    all_events: list = []
    for snap in snapshots:
        try:
            all_events.extend(ap.parse_db(snap))
        except ap.SnapshotError as exc:
            ctx.log.warning(
                "apple-podcasts-timemachine: skipping snapshot %s — %s", snap, exc
            )

    ctx.progress(stage="parsed", count=len(all_events))
    client = FulcraClient()
    client.ensure_tag("apple-podcasts", media_state)
    result = client.run_import(all_events, media_state)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)
    if result.posted > 0:
        ctx.annotation(
            f"Apple-podcasts: {result.posted} new annotation"
            + ("s" if result.posted != 1 else ""),
            ok=True,
        )
    # No watermark advance — this is a manual, one-shot recovery run.


APPLE_PODCASTS_TIMEMACHINE_PLUGIN = Plugin(
    id="apple-podcasts-timemachine",
    name="Apple Podcasts (Time Machine recovery)",
    kind="manual",
    run=_run_apple_podcasts_timemachine,
    description=(
        "One-shot recovery: walks every Time Machine backup on the "
        "mounted backup volume, reads the Apple Podcasts SQLite "
        "database from each snapshot, and imports every played episode "
        "found. Run this once if you've lost historical listens but "
        "have an older Time Machine backup that still has them."
    ),
    default_interval=None,
    requires_network=False,
    category="audio",
    canonical_definition_name="Listened",
    required_permissions=(_FULL_DISK_ACCESS_PERMISSION,),
    required_credentials=(),
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What this plugin does",
            body_md=(
                "If you've lost historical Apple Podcasts listens (e.g. "
                "the Podcasts app reset its database), this recovery "
                "tool walks every Time Machine backup on your mounted "
                "backup drive and pulls played episodes from each "
                "snapshot. It's a one-shot manual run — source-id dedup "
                "handles any overlap with the live database."
            ),
        ),
        SetupStep(
            kind="permission_request",
            title="Mount your Time Machine drive and grant access (if needed)",
            body_md=(
                "Make sure your Time Machine backup drive is mounted "
                "before running. macOS may also require Full Disk "
                "Access for the daemon to read backup snapshots — open "
                "**System Settings -> Privacy & Security -> Full Disk "
                "Access** and add the terminal running the daemon if "
                "the import fails."
            ),
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write recovered listens?",
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
                "Time Machine recovery is configured. Click **Run "
                "now** from the dashboard to walk every snapshot — this "
                "can take a few minutes."
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# Generic media CSV manual plugin
# ---------------------------------------------------------------------------

def _run_generic_csv(ctx: RunContext) -> None:
    """Import an arbitrary CSV (IFTTT, Pipedream, manual export) as Watched/Listened/Read.

    All parameters are read from ctx.config.  Required keys: path, service,
    category.  Optional keys mirror the CLI flags for import generic-csv with
    the same defaults.

    Column-map keys (all optional, CLI defaults):
      ts_col        — timestamp column name (default: "timestamp")
      title_col     — title column name (default: "title")
      subtitle_col  — subtitle/artist column name (default: "artist")
      id_col        — per-content id column name (default: "id")
      duration_col  — duration-in-seconds column name (default: None)
      end_col       — explicit end_time column name (default: None)

    Other optional keys:
      tz            — IANA timezone name for naive timestamps (default: "UTC")
      confidence    — timestamp_confidence value (default: "medium")
      fingerprint   — content fingerprint kind: "auto", "none", or an explicit
                      kind string such as "music", "movie" (default: "auto")
    """
    from datetime import timezone as _timezone

    # --- Required parameters ------------------------------------------------
    path_raw = ctx.config.get("path")
    if not path_raw:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'path' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    service = ctx.config.get("service")
    if not service:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'service' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )
    category = ctx.config.get("category")
    if not category:
        raise RuntimeError(
            f"{ctx.plugin_id}: 'category' is not configured — "
            f"set it in [plugin_settings.{ctx.plugin_id}] in config.toml"
        )

    # --- Optional column-map parameters (CLI defaults) ----------------------
    ts_col: str = ctx.config.get("ts_col", "timestamp")
    title_col: str = ctx.config.get("title_col", "title")
    subtitle_col: str = ctx.config.get("subtitle_col", "artist")
    id_col: str = ctx.config.get("id_col", "id")
    duration_col: str | None = ctx.config.get("duration_col", None)
    end_col: str | None = ctx.config.get("end_col", None)

    # --- Other optional parameters ------------------------------------------
    tz_name: str = ctx.config.get("tz", "UTC")
    confidence: str = ctx.config.get("confidence", "medium")
    fingerprint: str = ctx.config.get("fingerprint", "auto")

    # --- Build ColumnMap (mirror CLI's subtitle_col or None / id_col or None) -
    cm = ColumnMap(
        timestamp=ts_col,
        title=title_col,
        subtitle=subtitle_col or None,
        source_id=id_col or None,
        duration_seconds=duration_col,
        end_time=end_col,
    )

    # --- Resolve timezone (CLI shortcut: "UTC" → timezone.utc, else ZoneInfo) -
    if tz_name == "UTC":
        tz = _timezone.utc
    else:
        tz = ZoneInfo(tz_name)

    # --- Map fingerprint string → fingerprint_kind argument -----------------
    # Mirrors the CLI's two-step mapping exactly:
    #   fp_kind = None if fingerprint == "none" else (None if fingerprint == "auto" else fingerprint)
    #   fp_arg  = _FP_AUTO if fingerprint == "auto" else fp_kind
    fp_kind = None if fingerprint == "none" else (None if fingerprint == "auto" else fingerprint)
    fp_arg = _FP_AUTO if fingerprint == "auto" else fp_kind

    # --- Ensure the annotation definition is known before importing ----------
    # The category (watched/listened/read) is set per-instance via plugin
    # config, so we look it up at run-time and call the resolver with the
    # matching canonical name.  On a fresh install (machine 2) the target
    # field in media state may be absent; the resolver adopts the existing
    # definition rather than creating a duplicate.
    canonical = _CATEGORY_TO_CANONICAL[category]
    target_field = f"{category}_definition_id"
    media_state = _state_load(STATE_PATH)
    _ensure_media_def(ctx, media_state, attr=target_field,
                       spec=_GENERIC_DURATION_SPEC, canonical_name=canonical)

    # --- Resolve path, parse, and import ------------------------------------
    resolved = library.resolve(path_raw)
    events = list(parse_media_csv(
        resolved,
        service=service,
        category=category,
        column_map=cm,
        tz=tz,
        confidence=confidence,
        fingerprint_kind=fp_arg,
    ))
    _import_events(ctx, events, service)


GENERIC_CSV_PLUGIN = Plugin(
    id="generic-csv",
    name="Generic media CSV",
    kind="manual",
    run=_run_generic_csv,
    description=(
        "Imports any CSV of media events — IFTTT exports, Pipedream "
        "dumps, hand-crafted spreadsheets. You configure which columns "
        "hold the timestamp, title, and subtitle, plus a service tag "
        "and category (watched / listened / read). Manual."
    ),
    default_interval=None,
    category="other",
    # canonical_definition_name is intentionally absent: the canonical identity
    # depends on the runtime config value of "category", not on the Plugin
    # definition itself.  See _CATEGORY_TO_CANONICAL and _run_generic_csv.
    required_credentials=(),
    required_settings=(
        Setting(
            key="path",
            label="CSV file path",
            kind="path",
            help="Local path to the CSV file you want to import.",
        ),
        Setting(
            key="service",
            label="Service tag",
            kind="text",
            help=(
                "Short identifier we'll attach to each event "
                "(e.g. 'ifttt', 'manual', 'sheets')."
            ),
        ),
        Setting(
            key="category",
            label="Category",
            kind="enum",
            enum_values=("watched", "listened", "read"),
            help=(
                "Which canonical annotation to write to — 'watched' "
                "for video, 'listened' for audio, 'read' for text/books."
            ),
        ),
    ),
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What this plugin does",
            body_md=(
                "Got a CSV of media events from somewhere we don't "
                "natively support? Upload it here. You'll tell us which "
                "columns hold the timestamp, title, and subtitle "
                "(advanced options live in `config.toml` — defaults "
                "match common IFTTT/Pipedream exports). Each row "
                "becomes a Fulcra annotation."
            ),
        ),
        SetupStep(
            kind="file_upload",
            title="Upload your CSV",
            body_md=(
                "Pick the CSV file. Defaults assume columns named "
                "`timestamp`, `title`, `artist`, and `id` — tweak "
                "`ts_col`, `title_col`, etc. in `config.toml` later if "
                "yours differs."
            ),
            settings_keys=("path",),
        ),
        SetupStep(
            kind="input",
            title="Tag and categorise",
            body_md=(
                "Pick a **service** tag (a short label that identifies "
                "where this CSV came from) and a **category** — "
                "'watched' for video, 'listened' for audio, 'read' for "
                "books or articles."
            ),
            settings_keys=("service", "category"),
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write these events?",
            body_md=(
                "We can write to your existing Watched/Listened/Read "
                "annotation (whichever matches your category) or "
                "create a new one."
            ),
            annotation_type="duration",
        ),
        SetupStep(
            kind="done",
            title="You're set",
            body_md=(
                "Generic CSV is configured. Click **Run now** from the "
                "dashboard to import the file. Re-upload a fresh CSV "
                "any time you want to import new rows."
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# Plex/Jellyfin webhook receiver service plugin
# ---------------------------------------------------------------------------

# Loopback addresses that are safe to bind without a bearer token.
# Matches the CLI's check (cli.py: `host != "127.0.0.1" and host != "localhost"`).
# Note: the CLI does not currently include "::1" in the guard; we match it exactly.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}

# The Fulcra annotation definition shape for the "Watched" DurationAnnotation
# used by the media-webhook plugin.  Same structure as NETFLIX_WATCHED_SPEC and
# all other Watched plugins — they all share the same definition.  Kept as a
# distinct constant so the resolver call below is self-documenting and so
# spec-shape tests are local to this plugin block.
MEDIA_WEBHOOK_WATCHED_SPEC: dict = {
    "annotation_type": "duration",
    "measurement_spec": {
        "measurement_type": "duration",
        "value_type": "duration",
        "unit": None,
    },
}


def _run_media_webhook(ctx: RunContext) -> None:
    """Long-running Plex/Jellyfin webhook receiver.

    Binds an HTTP server on host:port (default 127.0.0.1:8765) and serves
    forever.  Refuses to start on a non-loopback host without a bearer token,
    mirroring the `fulcra-media webhook` CLI's safety check.

    Resolves the "Watched" definition at startup (before the receive loop
    begins) so the service works standalone on a fresh machine that has never
    run `fulcra-attention bootstrap` or another Watched-producing plugin.
    """
    host: str = ctx.config.get("host", "127.0.0.1")
    port: int = int(ctx.config.get("port", 8765))
    bearer_token: str | None = ctx.credentials.get("bearer-token") or None

    # Non-loopback guard — mirrors cli.py's `webhook_serve` exactly:
    # refuse to bind a non-loopback address unless a bearer token is set.
    if host not in _LOOPBACK_HOSTS and not bearer_token:
        raise RuntimeError(
            f"media-webhook: host {host!r} is non-loopback; refusing to start "
            "without a bearer token. Set the 'bearer-token' credential "
            "(`fulcra-collect set-credential media-webhook bearer-token`) "
            "or bind on 127.0.0.1."
        )

    # Ensure the "Watched" annotation definition is known before entering the
    # receive loop.  On a fresh machine where the user only enables media-webhook
    # (no fulcra-attention bootstrap, no other Watched-producing plugin) the
    # media state has no watched_definition_id, so the service couldn't start.
    # The shared resolver adopts Machine 1's existing "Watched" definition rather
    # than creating a duplicate — the same multi-machine guarantee every other
    # Watched plugin gets.  After a supervisor restart the cached state makes
    # this call fast (no network round-trip needed).
    media_state = _state_load(STATE_PATH)
    _ensure_media_def(ctx, media_state, attr="watched_definition_id",
                       spec=MEDIA_WEBHOOK_WATCHED_SPEC,
                       canonical_name="Watched")

    client = FulcraClient()
    server = webhook_receiver.make_server(
        host=host,
        port=port,
        state=media_state,
        client=client,
        bearer_token=bearer_token,
        log_stream=None,
    )
    ctx.log.info("media webhook receiver listening on %s:%s", host, port)
    server.serve_forever()


MEDIA_WEBHOOK_PLUGIN = Plugin(
    id="media-webhook",
    name="Plex/Jellyfin webhook receiver",
    kind="service",
    run=_run_media_webhook,
    description=(
        "Captures what you watch on Plex or Jellyfin by running a tiny "
        "local HTTP server that your media server POSTs playback events "
        "to. Runs continuously as a service — one annotation per session. "
        "Plex Pass is required for Plex webhooks; Jellyfin works on any tier."
    ),
    category="video",
    canonical_definition_name="Watched",
    required_permissions=(
        Permission(
            id="network-loopback-server",
            explanation=(
                "Runs a local HTTP server (default 127.0.0.1:8765) that "
                "Plex/Jellyfin POST playback webhooks to."
            ),
        ),
    ),
    required_credentials=(
        Credential(
            key="bearer-token",
            label="Webhook bearer token",
            help=(
                "Required when Plex/Jellyfin runs on a different machine "
                "than the daemon (host = 0.0.0.0). Plex doesn't send "
                "Authorization headers, so the receiver also accepts the "
                "token via `?token=...` on the webhook URL. Leave empty "
                "for the loopback-only setup (host = 127.0.0.1)."
            ),
        ),
    ),
    required_settings=(
        Setting(
            key="host",
            label="Bind address",
            kind="text",
            default="127.0.0.1",
            help=(
                "127.0.0.1 = same machine only (Plex/Jellyfin on this Mac). "
                "0.0.0.0 = accept connections from other machines on your "
                "network (requires the bearer token below)."
            ),
        ),
        # Wizard-only navigation hint. _run_media_webhook ignores this; it
        # exists purely so the conditional setup_steps below can branch on
        # the user's topology choice. required=False because once setup is
        # complete the daemon doesn't need it; we also default to "same"
        # so the wizard preselects the most common option.
        Setting(
            key="setup_topology",
            label="Where does Plex/Jellyfin run?",
            kind="enum",
            enum_values=("same", "lan"),
            enum_labels=(
                "On this same Mac",
                "On a different machine on my network",
            ),
            default="same",
            required=False,
            help=(
                "Choose 'same' if Plex/Jellyfin is on this Mac (loopback "
                "is enough). Choose 'lan' if your media server is on "
                "another box and needs to reach this Mac over the LAN — "
                "we'll walk you through binding to 0.0.0.0 and setting a "
                "bearer token."
            ),
        ),
    ),
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What this plugin does",
            body_md=(
                "media-webhook is a tiny HTTP server. Configure Plex or "
                "Jellyfin to POST playback events to it; we write a "
                "'Watched' annotation per session. Plex Pass is required "
                "to use Plex webhooks; Jellyfin webhooks need no paid tier."
            ),
        ),
        SetupStep(
            kind="permission_request",
            title="Allow a local webhook server",
            body_md=(
                "We'll bind a local HTTP server on port 8765 (default "
                "`127.0.0.1`, or `0.0.0.0` if you're configuring this "
                "from a Plex/Jellyfin server on another machine). The "
                "next step lets you pick which."
            ),
        ),
        SetupStep(
            kind="input",
            title="Where does Plex/Jellyfin run?",
            body_md=(
                "Pick **On this same Mac** if Plex or Jellyfin is "
                "installed locally. Pick **On a different machine on my "
                "network** if your media server runs on another box (a "
                "NAS, a separate desktop, a home server) and will POST "
                "events to this Mac over your LAN."
            ),
            settings_keys=("setup_topology",),
        ),
        SetupStep(
            kind="input",
            title="Bind address and bearer token",
            body_md=(
                "For LAN mode we bind to `0.0.0.0` so other machines on "
                "your network can reach the receiver. A **bearer token** "
                "is required — it's the only thing standing between "
                "anyone on your LAN and your Fulcra account. Paste your "
                "own random string (32+ characters recommended) or let "
                "the field stay blank and generate one with a password "
                "manager. **Save this token** — you'll paste it into the "
                "webhook URL in the next step."
            ),
            settings_keys=("host", "bearer-token"),
            condition={"setup_topology": ("lan",)},
        ),
        SetupStep(
            kind="external_action",
            title="Wire up your media server",
            body_md=(
                "**Plex:** open the **Plex Web app while signed in as the "
                "server's admin account** (this is the account that owns "
                "the server, not just any account with access). Go to "
                "**Settings -> the SERVER name (NOT 'Your Account') -> "
                "Webhooks -> Add Webhook**, enter "
                "`http://127.0.0.1:8765/webhook`, and click **Save**. "
                "Webhooks are a server-side setting — they live under your "
                "server's settings page, not your account's. **Plex Pass is "
                "required for this feature.**\n\n"
                "**Jellyfin:** open **Dashboard -> Plugins -> Webhook -> "
                "Add Generic Destination**, enter the same URL, and save."
            ),
            condition={"setup_topology": ("same",)},
        ),
        SetupStep(
            kind="external_action",
            title="Wire up your media server (cross-machine)",
            body_md=(
                "**You're using cross-machine mode**, so we need two "
                "extra pieces:\n\n"
                "1. Find this Mac's LAN IP — **System Settings -> "
                "Wi-Fi/Network -> Details -> IP Address**. It's probably "
                "`192.168.X.X` or `10.X.X.X`.\n"
                "2. In Plex/Jellyfin, set the webhook URL to:\n\n"
                "   `http://<this-mac-LAN-IP>:8765/webhook?token=<the-bearer-token-you-set-above>`\n\n"
                "   Example: `http://192.168.1.42:8765/webhook?token=abc123...`\n\n"
                "**Plex:** sign into the Plex Web app as your server's "
                "**admin account** (the one that owns the server), then "
                "**Settings -> the SERVER name (NOT 'Your Account') -> "
                "Webhooks -> Add Webhook**. Webhooks are a server-side "
                "feature — they live under your server's settings page, "
                "not your account's. **Plex Pass is required.**\n\n"
                "**Jellyfin:** Dashboard -> Plugins -> Webhook -> Add "
                "Generic Destination.\n\n"
                "The `?token=...` is how Plex authenticates to your "
                "daemon — Plex doesn't natively send Authorization "
                "headers. **Anyone on your LAN who knows this token can "
                "post events to your Fulcra account**, so keep it secret; "
                "rotate it via **Configure** if it leaks."
            ),
            condition={"setup_topology": ("lan",)},
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write your watches?",
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
                "media-webhook will run as a service — restarting the "
                "daemon restarts it. Trigger a playback in Plex/Jellyfin "
                "to see it record."
            ),
        ),
    ),
)

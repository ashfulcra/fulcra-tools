"""Trakt watch history — scheduled plugin with cluster/twin-dedup policy."""
from __future__ import annotations

from datetime import timedelta

from fulcra_collect.plugin import Credential, Plugin, RunContext, SetupStep
from fulcra_csv import ClusterPolicy, apply_cluster_policy, apply_twin_decisions, find_low_conf_twins

from .. import twin_cache
from ..fulcra import FulcraClient
from ..importers import trakt as trakt_importer
from ..state import DEFAULT_PATH as STATE_PATH
from ..state import load as _state_load
from ..state import save as _state_save
from ..trakt_health import trakt_health_check
from ..trakt_oauth import trakt_authorize_url, trakt_oauth_handler
from ._common import DURATION_SPEC, ensure_media_def, newest_event_iso


# Same structure as NETFLIX_WATCHED_SPEC — all Watched plugins share the same
# definition.
TRAKT_WATCHED_SPEC: dict = DURATION_SPEC


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
    ensure_media_def(ctx, media_state, attr="watched_definition_id",
                     spec=TRAKT_WATCHED_SPEC, canonical_name="Watched",
                     state_save=_state_save)

    client = FulcraClient()
    client.ensure_tag("trakt", media_state)
    result = client.run_import(events, media_state, claim=ctx.claim_dedup_keys)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)
    if result.posted > 0:
        ctx.annotation(
            f"Trakt: {result.posted} new annotation"
            + ("s" if result.posted != 1 else ""),
            ok=True,
        )

    # Advance even when posted == 0 — see _common.run_scheduled_import for
    # the full rationale. Skipped-existing means the event is already in
    # Fulcra; both outcomes are progress the watermark must reflect.
    new_wm = newest_event_iso(events)
    if new_wm:
        ctx.state.watermark = new_wm


PLUGIN = Plugin(
    id="trakt",
    name="Trakt watch history",
    kind="scheduled",
    collect_mode="live_polled",
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

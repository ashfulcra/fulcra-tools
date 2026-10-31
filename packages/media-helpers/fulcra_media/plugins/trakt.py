"""Trakt watch history — scheduled plugin with cluster/twin-dedup policy."""
from __future__ import annotations

from datetime import timedelta

import httpx

from fulcra_collect.plugin import Credential, Plugin, RunContext, SetupStep
from fulcra_csv import ClusterPolicy, apply_cluster_policy

from .. import twin_cache
from ..collect_v1 import media_destinations, media_pending, media_run
from ..fulcra import FulcraClient
from ..importers import trakt as trakt_importer
from ..state import DEFAULT_PATH as STATE_PATH
from ..state import load as _state_load
from ..trakt_health import trakt_health_check
from ..trakt_oauth import TRAKT_TOKEN_ENDPOINT, trakt_authorize_url, trakt_oauth_handler
from ._common import DURATION_SPEC, newest_event_iso, consult_twin_cache, report_import


# Legacy spec alias retained for imports; v1 setup uses media_destinations.
TRAKT_WATCHED_SPEC: dict = DURATION_SPEC


def apply_twin_decisions(events: list, discard_source_ids: set[str]) -> list:
    """Cross-name-safe replacement for ``fulcra_csv.apply_twin_decisions``.

    fulcra_csv's version filters on ``e.source_id`` only, but trakt's events
    are ``NormalizedEvent``s whose dedup id is ``deterministic_id`` — so the
    upstream filter raised ``AttributeError`` on the auto-discard path. This
    uses ``twin_cache._source_id_of``, the same accessor that builds
    ``discard_source_ids``, so the read and the filter agree regardless of which
    id attribute the event carries (GenericEvent: source_id, NormalizedEvent:
    deterministic_id). Kept as a module-level symbol so the existing
    ``fulcra_media.plugins.trakt.apply_twin_decisions`` monkeypatch still works.
    """
    return [
        e for e in events
        if twin_cache._source_id_of(e) not in discard_source_ids
    ]


_REAUTH_MESSAGE = (
    "trakt: access token expired and refresh failed — re-connect Trakt "
    "in the Fulcra Collect web UI wizard"
)


def _trakt_headers(access_token: str, client_id: str) -> dict[str, str]:
    """Build Trakt API headers for the keychain-credential path. Mirrors
    the header shape of ``TraktAuth.headers`` (the legacy file path)."""
    return {
        "Authorization": f"Bearer {access_token}",
        "trakt-api-version": "2",
        "trakt-api-key": client_id,
        "Content-Type": "application/json",
    }


def _refresh_keychain_tokens(ctx, *, binding, remaining):
    def grant(values):
        payload = {
            "refresh_token": values["refresh_token"], "client_id": values["client_id"],
            "redirect_uri": values.get("redirect_uri") or "urn:ietf:wg:oauth:2.0:oob",
            "grant_type": "refresh_token",
        }
        if values.get("client_secret"):
            payload["client_secret"] = values["client_secret"]
        response = httpx.post(TRAKT_TOKEN_ENDPOINT, json=payload, timeout=remaining())
        if response.status_code >= 400:
            raise RuntimeError(_REAUTH_MESSAGE)
        return response.json()
    return binding.refresh(grant, _before_claim=remaining)["access_token"]


def _fetch_keychain_history(ctx, *, binding, identity, bounds, initial_probe):
    # One allowance belongs to this outer acquisition, not either probe helper.
    try:
        identity.observe(bounds, window=initial_probe)
        headers = _trakt_headers(binding.values["access_token"], binding.values["client_id"])
        items = list(trakt_importer.fetch_history_with_headers(
            headers, strict_scan=True, _bounds=bounds,
            _before_request=binding.check_before_source))
    except httpx.HTTPStatusError as exc:
        request = exc.request
        url = request.url
        if (exc.response.status_code != 401 or request.method != "GET"
                or url.scheme != "https" or url.host != "api.trakt.tv" or url.port not in (None, 443)
                or url.path not in ("/users/settings", "/sync/history")
                or url.userinfo or (url.path == "/users/settings" and url.query)):
            raise
        _refresh_keychain_tokens(ctx, binding=binding, remaining=bounds.timeout)
        # Receipt and exact attempt-bound successor are already durable. A 401
        # here, on restarted history, or on terminal proof cannot consume again.
        identity.observe(bounds)
        headers = _trakt_headers(binding.values["access_token"], binding.values["client_id"])
        items = list(trakt_importer.fetch_history_with_headers(
            headers, strict_scan=True, _bounds=bounds,
            _before_request=binding.check_before_source))
    identity.observe(bounds, terminal=True)
    return items


@media_run
def _run_trakt(ctx: RunContext) -> None:
    """Fetch Trakt watch history and import it, applying cluster and twin-dedup policy.

    The worker binds a strict Keychain-only source owner. Explicit CLI file
    collection uses its own fixed-file owner; missing/UNKNOWN daemon fields
    never select that file.

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

    owner = ctx._trakt_source_owner
    if owner is None or not hasattr(ctx, "_register_current_work_guard"):
        raise RuntimeError("Trakt strict credential backend unavailable")
    from fulcra_collect.trakt_identity import CurrentIdentity, ProbeWindow
    bounds = trakt_importer._ScanBounds()
    initial_probe = ProbeWindow(bounds)
    binding = owner.capture(ctx._trakt_authority, ctx._trakt_local)
    initial_probe.check()
    identity = CurrentIdentity(binding)
    ctx._register_current_work_guard(identity.check_local_after_authority)
    items = _fetch_keychain_history(ctx, binding=binding, identity=identity,
                                    bounds=bounds, initial_probe=initial_probe)

    events = list(trakt_importer.normalize_history(items, cluster_threshold=cluster_threshold))
    identity.check_before_current()
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

    identity.check_before_current()
    events = apply_cluster_policy(events, cluster_policy)

    identity.check_before_current()
    events = consult_twin_cache(ctx, events)

    identity.check_before_current()
    media_state = _state_load(STATE_PATH)

    client = FulcraClient(collect_context=ctx)
    identity.check_before_current()
    result = client.run_import(events, media_state, claim=ctx.claim_dedup_keys,
                               unclaim=ctx.unclaim_dedup_keys)
    identity.check_before_current()
    report_import(ctx, result, "Trakt")

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
    record_data_types=media_destinations("watched"),
    record_pending=media_pending,
    description=(
        "Records your TV and movie watch history from Trakt.tv — which "
        "covers Netflix, Apple TV+, Plex, and most other video services "
        "via Trakt's scrobbler plugins. We sync new watches every 6 hours. "
        "You'll use your own Trakt OAuth app and sign in once."
    ),
    default_interval=timedelta(hours=6),
    category="video",
    canonical_definition_name="Watched",
    required_credentials=(
        Credential(
            key="client_id",
            label="Trakt Client ID",
            help="From your Trakt app in the developer portal or legacy API apps page.",
        ),
        Credential(
            key="client_secret",
            label="Trakt Client Secret (legacy)",
            help="Kept for existing token refresh. New PKCE sign-in uses only the Client ID.",
            required=False,
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
        Credential(
            key="redirect_uri",
            label="Trakt OAuth callback",
            help="Saved during sign-in so token refresh uses the same callback.",
            required=False,
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
                "it'll be recorded as a Watched event in your "
                "Fulcra account."
            ),
        ),
        SetupStep(
            kind="external_action",
            title="Create a Trakt OAuth app",
            body_md=(
                "Open [Trakt My Apps](https://developer.trakt.tv/apps). "
                "The current developer portal uses a verified GitHub account "
                "and provides a **Client ID** for PKCE sign-in. "
                "The [legacy API apps page](https://trakt.tv/oauth/applications) "
                "may still be available for an existing app. Keep that app "
                "and its credentials if it already works.\n\n"
                "[Connected Apps](https://app.trakt.tv/settings/apps/connected) "
                "manages account authorizations; it is separate from creating "
                "an API app.\n\n"
                "- **Name:** `Fulcra Collect`\n"
                "- **App type:** **Development** for your personal Collect "
                "setup. This permits local HTTP callbacks; only you and "
                "people you add can sign in.\n"
                "- **Icon:** if shown, leave blank.\n"
                "- **Description:** leave blank (or write anything you "
                "like; it's only shown when the app asks for permissions).\n"
                "- **JavaScript (CORS) origins:** leave blank.\n\n"
                "**Redirect URI — read this carefully.**\n\n"
                "For a Development app or an existing legacy app, use "
                "exactly:\n\n"
                "`http://127.0.0.1:9292/api/oauth/trakt/callback`\n\n"
                "Replace any `urn:ietf:wg:oauth:2.0:oob` default. "
                "The redirect must match exactly. (Note: the port shown "
                "in the next step may differ if you've changed Preferences "
                "— update this URI to match if so.)\n\n"
                "**Production apps require HTTPS redirects.** Collect uses "
                "a local HTTP callback, so choose **Development** in the new "
                "portal. If Trakt rejects the URI, check the app type and "
                "the exact callback shown by this wizard.\n\n"
                "**Permissions:** if `/checkin` and `/scrobble` checkboxes are "
                "shown, **uncheck both** before saving. Fulcra Collect only reads "
                "your watch history; it does not need write access. "
                "(`/users/me/history` is gated by the OAuth grant itself, "
                "not these scopes.)\n\n"
                "Save the app and copy the **Client ID** to the next step. "
                "A Client Secret is not needed for PKCE sign-in."
            ),
            external_link="https://developer.trakt.tv/apps",
        ),
        SetupStep(
            kind="input",
            title="Paste your Trakt OAuth credentials",
            body_md=(
                "Paste your app's **Client ID**, from either the current "
                "developer portal or an existing legacy app. The wizard "
                "stores it in your macOS keychain. PKCE sign-in does not "
                "need a Client Secret; any previously saved secret is kept "
                "for existing token refresh."
            ),
            settings_keys=("client_id",),
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
                "We can write to your existing 'Watched' data type "
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

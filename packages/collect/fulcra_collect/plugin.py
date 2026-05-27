"""The fulcra-collect plugin API.

A plugin is a `Plugin` object discovered via the `fulcra_collect.plugins`
entry-point group. It declares metadata and a `run(ctx)` callable. The
hub builds the `RunContext` and supplies config, credentials, and state —
a plugin never reaches for those itself.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

PluginKind = Literal["service", "scheduled", "manual"]
_KINDS = ("service", "scheduled", "manual")

CollectMode = Literal["historical", "live_polled", "live_continuous"]
_COLLECT_MODES = ("historical", "live_polled", "live_continuous")
"""User-facing 'historical vs live' framing for SP3. NOT derivable from
``PluginKind`` — the Attention extension's kind="manual" but functionally
collect_mode="live_continuous" because the data flow is push-based via
the browser extension. Per-plugin explicit declarations surface this at
the metadata level so the menubar popover, Preferences chip, and any
future web-UI consumer can all read the same source of truth. See
docs/plans/2026-05-27-sp3-historical-live-framing-execution.md."""


@dataclass(frozen=True)
class Permission:
    """An OS permission a plugin needs. `explanation` is shown to the user
    by the sub-project-2 onboarding flow."""
    id: str
    explanation: str


@dataclass(frozen=True)
class Credential:
    """A secret a plugin needs. Stored in the OS keychain by the hub."""
    key: str
    label: str
    help: str


@dataclass(frozen=True)
class Setting:
    """A non-secret config field a plugin needs.

    Parallel to Credential. Stored in config.toml rather than the keychain.
    kind meanings:
      text        — single-line free text
      long_text   — multi-line text area
      path        — local filesystem path (file or directory)
      url         — URL string
      port        — TCP port number
      enum        — one of enum_values; rendered as a dropdown
      toggle      — boolean on/off switch
      interval    — duration / polling interval (seconds or ISO8601)
      secret      — short-lived secret kept in config.toml (true secrets like
                    API keys go in Credential + the OS keychain instead)
    """
    key: str
    label: str
    kind: Literal[
        "text", "long_text", "path", "url", "port",
        "enum", "toggle", "interval", "secret",
    ]
    help: str = ""
    enum_values: tuple[str, ...] | None = None
    # Optional human-readable labels for each enum_values entry — one per
    # value, positionally. When omitted, the wizard renders the raw enum
    # value as the label (used to be the only option, which left users
    # staring at `live_app` / `export_file` in production UI). Length must
    # match enum_values; the dataclass doesn't enforce it but the contract
    # serializer round-trips both and the renderer falls back to the value
    # when labels are missing or short.
    enum_labels: tuple[str, ...] | None = None
    default: object = None
    required: bool = True
    placeholder: str = ""


@dataclass(frozen=True)
class SetupStep:
    """One step in a plugin's onboarding wizard.

    The web UI iterates over Plugin.setup_steps and renders each step using
    a generic renderer keyed on `kind` — no plugin-specific UI code required.

    kind meanings:
      intro              — introductory text page; no user input
      external_action    — user must do something outside the app (e.g. visit a
                           URL); show external_link as a button
      input              — collect values for the keys in settings_keys
      oauth              — OAuth flow; the UI drives the redirect/callback cycle
      file_upload        — ask the user to choose a file (path Setting)
      permission_request — prompt the user to grant an OS permission
      browser_extension  — prompt the user to install a browser extension;
                           show extension_url as an install link
      extension_pair     — one-click pairing handshake with the installed
                           Fulcra Attention browser extension. The wizard
                           generates a fresh extension-token via the daemon,
                           postMessages it to the page, and waits for an
                           ack from the extension's content script. Falls
                           back to a copy-paste manual paste if no ack
                           arrives within 3 seconds.
      test_connection    — invoke Plugin.health_check and show the result
      definition_picker  — let the user choose (or confirm) a Fulcra annotation
                           definition for this plugin
      done               — final confirmation / success screen
    """
    kind: Literal[
        "intro", "external_action", "input", "oauth", "file_upload",
        "permission_request", "browser_extension", "extension_pair",
        "test_connection", "definition_picker", "done",
    ]
    title: str
    body_md: str = ""
    settings_keys: tuple[str, ...] = ()
    external_link: str = ""
    extension_url: str = ""
    annotation_type: str = ""
    """Hint for the definition_picker step: which annotation_type to filter
    by (e.g. "duration", "moment"). Emitted in the plugin contract so the
    wizard can pass ?annotation_type=... to /api/definitions. Empty string
    means no filter (wizard defaults to "duration")."""
    condition: dict[str, tuple[str, ...]] | None = None
    """Optional display condition: maps setting keys to tuples of acceptable
    values. When set, the wizard shows this step only when *all* keys match —
    i.e. inputValues[key] is in the acceptable-values tuple for every key in
    the dict. Steps that do not satisfy their condition are auto-skipped in
    both the Next and Back directions. If a key hasn't been filled yet
    (inputValues[key] is undefined), the condition is NOT satisfied — the
    step is skipped. Unconditional steps (condition=None) are always shown."""


@dataclass
class HealthResult:
    """Result of a plugin's health_check call.

    ok:      True when the plugin is operational (credentials valid, service
             reachable, etc.).
    summary: Short human-readable description of the health state shown in the
             dashboard pill (e.g. "5 recent scrobbles", "Not signed in.").
    preview: Optional list of recent items from the source, surfaced in the
             onboarding wizard's test_connection step so the user can see that
             data is actually flowing.
    """
    ok: bool
    summary: str
    preview: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class Plugin:
    """A hub plugin: metadata plus a `run(ctx)` callable.

    kind:
      "service"   — run(ctx) blocks (a long-lived server); supervised.
      "scheduled" — run(ctx) does one import pass; fired on default_interval.
      "manual"    — run(ctx) does one import pass; fired only on request.

    collect_mode (required, no default):
      "historical"      — one-shot import; the plugin does not update
                          afterwards (takeouts, user-provided files).
      "live_polled"     — captures new events on a polling schedule.
      "live_continuous" — captures events as they happen (webhook
                          receivers, browser-extension pushes).
      NOT derivable from ``kind`` — see the ``CollectMode`` docstring.

    requires_network: when True (the default), the daemon skips this
    plugin's scheduled dispatch while the machine is offline — deferring
    it rather than running it into a guaranteed failure.

    description: one-line human-readable description shown in the Preferences
    UI. Covers what the plugin imports and what credentials/files/permissions
    it needs. Defaults to empty string for backwards compatibility with plugins
    that pre-date this field.
    """
    id: str
    name: str
    kind: PluginKind
    collect_mode: CollectMode
    """Per-plugin tag for the user-facing 'historical vs live' framing the
    web UI's collect_modes onboarding screen introduced. Three values:

      "historical"      — one-shot import; the plugin doesn't update
                          afterwards (takeouts, user-provided files).
      "live_polled"     — captures new events on a polling schedule.
      "live_continuous" — captures events as they happen (webhook
                          receivers, browser-extension pushes).

    NOT derivable from `kind` — the Attention extension's kind="manual"
    but functionally collect_mode="live_continuous" because the data
    flow is push-based via the extension. Forcing per-plugin explicit
    values surfaces this distinction at the metadata level. See SP3
    in the 2026-05-27 menubar drift audit for the full mapping table.
    """
    run: Callable[["RunContext"], None]
    description: str = ""
    default_interval: timedelta | None = None
    requires_network: bool = True
    required_permissions: tuple[Permission, ...] = ()
    required_credentials: tuple[Credential, ...] = ()
    required_settings: tuple[Setting, ...] = ()
    setup_steps: tuple[SetupStep, ...] = ()
    health_check: Callable[["RunContext"], "HealthResult"] | None = None
    permission_check: Callable[["RunContext"], dict] | None = None
    """Optional callable that verifies an OS-level permission is actually
    granted (e.g. Full Disk Access). Returns
    {"granted": bool, "hint": str | None}. The wizard's permission_request
    step uses this so it can show "verified" instead of the misleading
    "macOS will prompt when you click Next" (which is false for FDA — there
    is no dialog; the user must add the binary in System Settings).
    """
    oauth_handler: Callable[..., dict[str, str]] | None = None
    """Optional OAuth handler called by the daemon's callback route.

    Signature: (*, plugin_id, code, code_verifier, redirect_uri) -> dict[str, str]

    Returns a dict of tokens (e.g. {"access_token": "...", "refresh_token": "..."})
    that the callback handler stores in the plugin's credential keychain namespace.
    Set this on any Plugin that authenticates via browser-based OAuth — the web UI
    wizard will render an "oauth" SetupStep and call /api/oauth/{plugin_id}/start
    to begin the flow.
    """
    oauth_authorize_url: Callable[..., str] | None = None
    """Optional callable that builds the provider's full authorize URL.

    Signature: (client_id: str, redirect_uri: str, state: str,
                code_challenge: str) -> str

    When present, the oauth_start route calls this after start_flow() and
    includes the result as "authorize_url" in the response body. The wizard
    opens that URL in a new tab instead of asking the user to construct it
    manually. Implement this on any plugin that uses browser OAuth so the
    wizard can drive the entire flow without knowing the provider's
    endpoint or client_id.
    """
    category: Literal["audio", "video", "books", "journal", "activity", "other"] = "other"
    canonical_definition_name: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"unknown kind {self.kind!r}; expected one of {_KINDS}")
        if self.collect_mode not in _COLLECT_MODES:
            raise ValueError(
                f"unknown collect_mode {self.collect_mode!r}; "
                f"expected one of {_COLLECT_MODES}"
            )
        if self.kind == "scheduled" and self.default_interval is None:
            raise ValueError("scheduled plugin requires a default_interval")
        if self.kind != "scheduled" and self.default_interval is not None:
            raise ValueError("default_interval is only valid for a scheduled plugin")


@dataclass
class RunContext:
    """Passed into `Plugin.run`. The hub builds it in the worker process."""
    plugin_id: str
    config: dict
    credentials: dict[str, str]
    state: "object"        # a PluginState (fulcra_collect.state) — duck-typed here
    log: logging.Logger
    _emit: Callable[[dict], None] = field(repr=False)
    _fulcra_client_factory: Callable[[], object] | None = field(default=None, repr=False)

    def progress(self, **fields: object) -> None:
        """Report structured progress back to the hub core."""
        self._emit({"type": "progress", **fields})

    def annotation(self, summary: str, *, ok: bool = True) -> None:
        """Report that an annotation was written to Fulcra. Surfaces in
        the web UI's recent-activity feed as a real receipt of the app
        working. Call this AFTER a successful (or attempted) annotation
        POST.
        """
        self._emit({"type": "annotation", "summary": summary, "ok": ok})

    def fulcra_token(self) -> str | None:
        """Return the Fulcra access token.

        Resolution order:
        1. The user-level keychain entry written by the web UI's onboarding
           wizard (credentials.get_user_secret("bearer-token")).  This is
           the preferred path once the user has signed in via the daemon.
        2. The FULCRA_ACCESS_TOKEN environment variable (useful for CI and
           CLI invocations where a keychain is not available).
        3. The `fulcra auth print-access-token` CLI subprocess — the
           original path that reads from the fulcra CLI's credential store.

        Returns None if no token is found via any path (first-launch state
        before the user has signed in via the web UI's onboarding wizard).
        """
        from . import credentials
        token = credentials.get_user_secret("bearer-token")
        if token:
            return token
        # Fall back to the fulcra-common path (env var + CLI subprocess).
        # BaseFulcraClient.get_token() raises RuntimeError when the CLI is
        # missing or fails; catch that so callers can treat it the same as
        # "no token configured yet."
        try:
            from fulcra_common import BaseFulcraClient
            return BaseFulcraClient().get_token()
        except (RuntimeError, OSError):
            return None

    def resolved_definition_id(
        self,
        expected_spec: dict,
        *,
        canonical_name: str,
        force_new: bool = False,
    ) -> str:
        """Return the cached Fulcra definition id for this plugin, or call
        the resolver, cache the result in state, and return the freshly
        resolved id.

        `expected_spec` is the shape the plugin expects — at minimum an
        `annotation_type` key; Duration annotations also supply a
        `measurement_spec` dict.  `canonical_name` is the stable, human-
        readable name used to find (or create) the definition in Fulcra
        across machines.

        The fulcra client is built by `_fulcra_client_factory` — the
        worker supplies this factory so `plugin.py` never has to know
        how to construct an HTTP client or handle auth directly. Plugins
        that do not call this method can leave the factory unset.

        Stale-cache guard: when `state.definition_id` is set, validate
        that the def still exists on the *current* Fulcra account before
        returning it. After a daemon re-auth to a different account, the
        cached id points at a def that doesn't exist here — ingest
        accepts events keyed to it silently and they end up orphaned in
        the timeline. On a stale hit we clear the cache and fall through
        to the resolver. Implementation requires the factory to provide
        a client whose `definition_exists(def_id)` is implemented (the
        worker's `_FulcraDefinitionAdapter`); a fake client without
        that method skips validation, which is fine for tests that
        don't exercise the stale path.
        """
        cached = getattr(self.state, "definition_id", None)
        if cached and not force_new:
            client = (self._fulcra_client_factory()
                      if self._fulcra_client_factory is not None else None)
            still_present = True
            if client is not None and hasattr(client, "definition_exists"):
                try:
                    still_present = client.definition_exists(cached)
                except Exception:
                    # Network/adapter failure — be conservative and trust
                    # the cache. The next run will retry the check.
                    still_present = True
            if still_present:
                return cached
            # Stale: clear and re-resolve. Caller's state mutation is
            # picked up by the runner at run end (state.definition_id
            # is part of the result envelope).
            self.state.definition_id = None
        if self._fulcra_client_factory is None:
            raise RuntimeError(
                "RunContext has no _fulcra_client_factory — the runner must "
                "supply one when the plugin uses resolved_definition_id."
            )
        from fulcra_common.definitions import resolve_definition_id
        client = self._fulcra_client_factory()
        # If the user supplied a custom name via the definition picker's
        # "Create new" input, use it verbatim — find-or-create by exact
        # match, no machine-id suffix. This overrides both the plugin's
        # canonical_name and the suffix-on-force_new behavior, because
        # the user explicitly typed what they want to see in Fulcra.
        override = getattr(self.state, "override_definition_name", None)
        if override:
            new_id = resolve_definition_id(
                canonical_name=override,
                expected_spec=expected_spec,
                fulcra_client=client,
                force_new=False,
            )
            # One-shot: the override has done its job. Clear it so a
            # later re-resolve (e.g. account switch) falls back to the
            # plugin's canonical_name rather than silently re-using a
            # name the user picked once weeks ago.
            self.state.override_definition_name = None
        else:
            new_id = resolve_definition_id(
                canonical_name=canonical_name,
                expected_spec=expected_spec,
                fulcra_client=client,
                force_new=force_new,
            )
        self.state.definition_id = new_id
        return new_id

    def ensure_definition(
        self,
        *,
        cached: str | None,
        expected_spec: dict,
        canonical_name: str,
    ) -> str:
        """Return a guaranteed-fresh definition id for callers that
        maintain their own per-package cache (alongside the per-plugin
        ``state.definition_id``).

        Pattern: media-helpers has a ``state.json`` with fields like
        ``listened_definition_id`` shared across Last.fm + Deezer +
        Spotify because they all write to the same canonical "Listened"
        def. Without this helper, each plugin's run does
        ``if not media_state.listened_definition_id: resolve`` — which
        trusts the per-package cache blindly across account switches.

        With this helper, callers pass their cached value in. If it's
        still live on the current account, we return it as-is (cheap
        round trip, cached). If it's stale or unset, we re-resolve and
        the caller writes the fresh id back to both per-package state
        and (via ``resolved_definition_id``) per-plugin state.

        On stale-cache re-resolution, also emits an annotation event so
        the dashboard activity feed surfaces a one-line note — mirrors
        the attention extension route's recovery surface and keeps the
        user informed when their data suddenly attaches to a different
        def after a daemon re-auth.
        """
        if cached:
            client = (self._fulcra_client_factory()
                      if self._fulcra_client_factory is not None else None)
            if client is not None and hasattr(client, "definition_exists"):
                try:
                    if client.definition_exists(cached):
                        # Keep the per-plugin cache in sync — important
                        # so future ``resolved_definition_id`` calls hit
                        # without another round trip.
                        if getattr(self.state, "definition_id", None) != cached:
                            self.state.definition_id = cached
                        return cached
                except Exception:
                    if getattr(self.state, "definition_id", None) != cached:
                        self.state.definition_id = cached
                    return cached
            else:
                # No validator available; trust the cache.
                if getattr(self.state, "definition_id", None) != cached:
                    self.state.definition_id = cached
                return cached
        # Stale or unset: re-resolve. resolved_definition_id will write
        # the new id to per-plugin state; the caller is responsible for
        # writing it to per-package state too.
        had_stale_cache = bool(cached)
        self.state.definition_id = None
        new_id = self.resolved_definition_id(
            expected_spec, canonical_name=canonical_name,
        )
        if had_stale_cache and new_id != cached:
            # Surface the auto-recovery in the dashboard. ok=True
            # because the recovery itself succeeded — the user just
            # benefits from knowing why their data is now attached to
            # a different def.
            self.annotation(
                f"Definition \"{canonical_name}\" re-resolved: "
                f"previous {cached[:8]}… not present on this Fulcra "
                f"account; now {new_id[:8]}…",
                ok=True,
            )
        return new_id

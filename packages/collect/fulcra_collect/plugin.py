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
      test_connection    — invoke Plugin.health_check and show the result
      definition_picker  — let the user choose (or confirm) a Fulcra annotation
                           definition for this plugin
      done               — final confirmation / success screen
    """
    kind: Literal[
        "intro", "external_action", "input", "oauth", "file_upload",
        "permission_request", "browser_extension", "test_connection",
        "definition_picker", "done",
    ]
    title: str
    body_md: str = ""
    settings_keys: tuple[str, ...] = ()
    external_link: str = ""
    extension_url: str = ""


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
    run: Callable[["RunContext"], None]
    description: str = ""
    default_interval: timedelta | None = None
    requires_network: bool = True
    required_permissions: tuple[Permission, ...] = ()
    required_credentials: tuple[Credential, ...] = ()
    required_settings: tuple[Setting, ...] = ()
    setup_steps: tuple[SetupStep, ...] = ()
    health_check: Callable[["RunContext"], "HealthResult"] | None = None
    oauth_handler: Callable[..., dict[str, str]] | None = None
    """Optional OAuth handler called by the daemon's callback route.

    Signature: (*, plugin_id, code, code_verifier, redirect_uri) -> dict[str, str]

    Returns a dict of tokens (e.g. {"access_token": "...", "refresh_token": "..."})
    that the callback handler stores in the plugin's credential keychain namespace.
    Set this on any Plugin that authenticates via browser-based OAuth — the web UI
    wizard will render an "oauth" SetupStep and call /api/oauth/{plugin_id}/start
    to begin the flow.
    """
    category: Literal["music", "video", "books", "journal", "activity", "other"] = "other"
    canonical_definition_name: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"unknown kind {self.kind!r}; expected one of {_KINDS}")
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
        """
        cached = getattr(self.state, "definition_id", None)
        if cached and not force_new:
            return cached
        if self._fulcra_client_factory is None:
            raise RuntimeError(
                "RunContext has no _fulcra_client_factory — the runner must "
                "supply one when the plugin uses resolved_definition_id."
            )
        from fulcra_common.definitions import resolve_definition_id
        client = self._fulcra_client_factory()
        new_id = resolve_definition_id(
            canonical_name=canonical_name,
            expected_spec=expected_spec,
            fulcra_client=client,
            force_new=force_new,
        )
        self.state.definition_id = new_id
        return new_id

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

    def fulcra_token(self) -> str:
        """The Fulcra access token, via the existing fulcra-api auth path."""
        from fulcra_common import BaseFulcraClient
        return BaseFulcraClient().get_token()

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

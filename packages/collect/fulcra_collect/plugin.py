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
from datetime import datetime, timedelta, timezone
from typing import Literal

# How long a successful definition_exists validation is trusted before the
# cached definition id must be re-validated against the live catalog.
#
# WHY a plain TTL and not a data-updates gate: the fleet directive asked
# whether GET /data/v1/updates could signal "definitions changed" so the
# gate could be event-driven. Investigated live (2026-07-06): it CANNOT.
# data_updates reports per-data-type RECORD processing counts; definition
# soft-deletes — the exact hazard the validation exists for — produce NO
# signal at all (an 11-definition delete burst shows data_types == {} over
# its own window), and definition creates surface only as the first real
# record written under the new def, not as the create itself. A zero-
# activity window therefore proves nothing about definition liveness, so
# consulting data_updates here would be a wasted round trip dressed up as
# a signal. Time-based it is: honest, cheap, bounded.
DEFINITION_VALIDATION_TTL = timedelta(minutes=15)

# Absolute ceiling regardless of any future TTL tuning: never trust a
# cached definition id for more than 24h without revalidating it.
DEFINITION_VALIDATION_HARD_CAP = timedelta(hours=24)

PluginKind = Literal["service", "scheduled", "manual"]
_KINDS = ("service", "scheduled", "manual")

CollectMode = Literal["historical", "live_polled", "live_continuous"]
_COLLECT_MODES = ("historical", "live_polled", "live_continuous")
"""User-facing 'historical vs live' framing for SP3. NOT derivable from
``PluginKind`` — the Attention extension's kind="manual" but functionally
collect_mode="live_continuous" because the data flow is push-based via
the browser extension. Per-plugin explicit declarations surface this at
the metadata level so the menubar popover, Preferences chip, and any
future web-UI consumer can all read the same source of truth."""


@dataclass(frozen=True)
class Permission:
    """An OS permission a plugin needs. `explanation` is shown to the user
    by the sub-project-2 onboarding flow."""
    id: str
    explanation: str


@dataclass(frozen=True)
class Credential:
    """A secret a plugin needs. Stored in the OS keychain by the hub.

    `user_level` selects WHICH keychain service the secret lives in:
      - False (default): plugin-scoped, service "fulcra-collect:<plugin_id>",
        accessed via credentials.set_secret / get_secret / has_secret. This
        is the normal case — per-plugin API keys etc.
      - True: account-scoped, service "fulcra-collect:user", accessed via
        credentials.set_user_secret / get_user_secret / has_user_secret. Use
        this for secrets shared across the account rather than owned by one
        plugin. The attention-relay `extension-token` is the canonical
        example: it's written by the extension-pair route and read by the
        ingest route + the plugin's own preflight, all via the *user* store.

    The credential-status endpoint and any code that probes presence MUST
    consult the same scope the plugin actually reads, or it reports a working
    secret as "missing" (the attention-relay false-"missing" bug)."""
    key: str
    label: str
    help: str
    user_level: bool = False


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
      test_connection    — invoke Plugin.health_check and show the result
      definition_picker  — let the user choose (or confirm) a Fulcra annotation
                           definition for this plugin
      done               — final confirmation / success screen
    """
    kind: Literal[
        "intro", "external_action", "input", "oauth", "file_upload",
        "permission_request",
        "test_connection", "definition_picker", "done",
    ]
    title: str
    body_md: str = ""
    settings_keys: tuple[str, ...] = ()
    external_link: str = ""
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


#: Refresh the stored Fulcra token this many seconds BEFORE its exp claim, so
#: a token that expires mid-run doesn't 401 halfway through a plugin's uploads.
_JWT_EXPIRY_SKEW_S = 120


def _jwt_expired(token: str, *, skew_s: int = _JWT_EXPIRY_SKEW_S) -> bool:
    """True iff `token` is a JWT whose ``exp`` is past (or within `skew_s`).

    Fulcra bearer tokens are JWTs, so expiry is checkable locally instead of
    burning an upload on a guaranteed 401. Anything unparseable (not a JWT,
    bad base64, no exp) returns False — treat it as opaque and behave exactly
    as before this check existed.
    """
    import base64
    import json as _json
    import time

    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if exp is None:
            return False
        return time.time() >= float(exp) - skew_s
    except Exception:  # noqa: BLE001 — unparseable == opaque, not expired
        return False


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
    _claim_dedup_keys: Callable[[set[str]], bool] | None = field(default=None, repr=False)
    """Daemon-backed per-event write-dedup claim. The worker supplies a
    callable that atomically claims a set of dedup keys in the daemon's
    ``state.db`` (the same store the attention route uses). Plugins that
    write to Fulcra (the media import path) pass ``ctx.claim_dedup_keys``
    down into ``run_import`` so concurrent runs / same-run cross-source
    twins can't double-write. ``None`` when no daemon db is available
    (standalone CLI), in which case the import path falls back to the
    readback-skip only."""
    _unclaim_dedup_keys: Callable[[set[str]], None] | None = field(default=None, repr=False)
    """Inverse of ``_claim_dedup_keys``: releases a set of dedup keys. The
    media import path passes ``ctx.unclaim_dedup_keys`` into ``run_import``
    so a batch POST that FAILS can release the claims it just took, letting
    the next run retry those durable-timeline events instead of skipping
    them forever. ``None`` when no daemon db is available."""
    _set_credential: Callable[[str, str], None] | None = field(default=None, repr=False)
    """Keychain write-back seam. ``ctx.credentials`` is a read-only snapshot
    taken when the worker built this context — a plugin that rotates an OAuth
    token mid-run (e.g. Trakt's single-use refresh tokens) needs a way to
    persist the new secrets or the next run is locked out forever. The worker
    supplies ``lambda k, v: credentials.set_secret(plugin_id, k, v)`` here;
    media-helpers plugins call the bound ``ctx.set_credential`` method instead
    of importing fulcra_collect.credentials directly (same inversion as
    ``_claim_dedup_keys``). ``None`` in contexts that never write secrets
    (health checks, permission checks, standalone CLI)."""
    _plugin_kv_get: Callable[[str, object], object] | None = field(
        default=None, repr=False,
    )
    _plugin_kv_set: Callable[[str, object], None] | None = field(
        default=None, repr=False,
    )
    _plugin_kv_update: Callable[
        [str, Callable[[object], object], object], object
    ] | None = field(default=None, repr=False)
    _plugin_kv_delete: Callable[[str], bool] | None = field(
        default=None, repr=False,
    )
    """Persistent JSON/KV state seams.  The worker binds these callables to
    this context's ``plugin_id``; plugin code can never select another
    plugin's namespace.  They are intentionally absent on lightweight
    health/permission contexts, where attempting durable state access fails
    loudly rather than pretending a write persisted."""

    def claim_dedup_keys(self, keys: set[str]) -> bool:
        """Atomically claim a set of dedup keys for one event, returning
        ``True`` iff the event is new (none of its keys were already
        claimed) and should be written to Fulcra, else ``False`` (skip).

        Backed by the daemon's ``state.db`` via the injected
        ``_claim_dedup_keys`` callable. When no claim backend was supplied
        (standalone CLI, or a RunContext built for health/permission checks
        that never write), this returns ``True`` — "always new" — so the
        caller's behaviour is identical to the pre-claim readback-only path.
        It is wired as a bound method (not the raw callable) so callers can
        pass ``ctx.claim_dedup_keys`` uniformly regardless of backend."""
        if self._claim_dedup_keys is None:
            return True
        return self._claim_dedup_keys(set(keys))

    def set_credential(self, key: str, value: str) -> None:
        """Persist a credential to this plugin's keychain namespace.

        Used by plugins that rotate secrets mid-run — the canonical case is
        Trakt's OAuth refresh, where the refresh token is single-use and the
        rotated pair MUST be persisted immediately or the user is forced to
        re-run the whole sign-in wizard. Note this does NOT update the
        ``credentials`` dict snapshot on this context; callers should keep
        using the value they just wrote.

        Backed by the injected ``_set_credential`` callable. When no backend
        was supplied (health/permission-check contexts, standalone CLI) this
        is a logged no-op — the plugin still works for the rest of the run
        with the in-memory token; only persistence is skipped. It is wired
        as a bound method (not the raw callable) so callers can use
        ``ctx.set_credential`` uniformly regardless of backend — same
        pattern as ``claim_dedup_keys``."""
        if self._set_credential is None:
            self.log.debug(
                "set_credential(%r): no credential write backend on this "
                "RunContext — skipping persistence", key,
            )
            return
        self._set_credential(key, value)

    def unclaim_dedup_keys(self, keys: set[str]) -> None:
        """Release a set of previously-claimed dedup keys (the inverse of
        ``claim_dedup_keys``). Used by the media import path to roll back a
        claim when the subsequent Fulcra POST failed, so the event is retried
        rather than lost. A no-op when no unclaim backend was supplied — same
        contexts where ``claim_dedup_keys`` is a no-op True."""
        if self._unclaim_dedup_keys is None:
            return
        self._unclaim_dedup_keys(set(keys))

    def kv_get(self, key: str, default: object = None) -> object:
        """Read a JSON value from this plugin's durable namespace.

        Keys are limited to 256 UTF-8 bytes and values to 64 KiB of compact
        JSON.  Only JSON-native types are accepted.  See ``kv_update`` for an
        atomic read-modify-write operation.
        """
        if self._plugin_kv_get is None:
            raise RuntimeError("plugin KV backend unavailable on this RunContext")
        return self._plugin_kv_get(key, default)

    def kv_set(self, key: str, value: object) -> None:
        """Atomically create or replace one durable JSON value."""
        if self._plugin_kv_set is None:
            raise RuntimeError("plugin KV backend unavailable on this RunContext")
        self._plugin_kv_set(key, value)

    def kv_update(
        self,
        key: str,
        update: Callable[[object], object],
        *,
        default: object = None,
    ) -> object:
        """Atomically transform one durable JSON value and return the result.

        Concurrent worker processes are serialised before the read, preventing
        lost updates.  ``update`` runs while the database write lock is held,
        so it must be quick and side-effect free.  Exceptions and invalid or
        oversized results leave the previous value unchanged.
        """
        if self._plugin_kv_update is None:
            raise RuntimeError("plugin KV backend unavailable on this RunContext")
        return self._plugin_kv_update(key, update, default)

    def kv_delete(self, key: str) -> bool:
        """Atomically delete one durable key; return whether it existed."""
        if self._plugin_kv_delete is None:
            raise RuntimeError("plugin KV backend unavailable on this RunContext")
        return self._plugin_kv_delete(key)

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
        if token and not _jwt_expired(token):
            return token
        if token:
            # Stored token is expired (or unreadably close to it). The routes
            # side has refreshed on 401 since SP5; workers never did — so an
            # expired stored token made EVERY plugin upload 401, the per-rule
            # fail-soft swallowed it, and runs reported "no new data" while
            # dropping every match (live incident 2026-07-16: 21 matched
            # emails silently dropped). Refresh through the same helper the
            # routes use; it re-stores the fresh token as a side effect.
            fresh = credentials.refresh_fulcra_access_token()
            if fresh:
                return fresh
            self.log.warning(
                "fulcra_token: stored token expired and refresh failed — "
                "falling back to env/CLI")
        # Fall back to the fulcra-common path (env var + CLI subprocess).
        # BaseFulcraClient.get_token() raises RuntimeError when the CLI is
        # missing or fails; catch that so callers can treat it the same as
        # "no token configured yet."
        try:
            from fulcra_common import BaseFulcraClient
            return BaseFulcraClient().get_token()
        except (RuntimeError, OSError) as exc:
            # Deliberate swallow (callers treat this as "not signed in yet"),
            # but LOG it: the 2026-06-10 launchd CLI-resolution failure was
            # invisible precisely because this catch turned "CLI not found"
            # into a silent None that surfaced only as "not authenticated".
            self.log.warning("fulcra_token fallback failed: %s", exc)
            return None

    def _definition_validation_fresh(self, cached: str) -> bool:
        """True iff the validated-at watermark covers exactly `cached` and
        is younger than min(TTL, hard cap) — i.e. the cached definition id
        was confirmed live on this account recently enough to skip the
        full-catalog ``definition_exists`` fetch this run.

        Every failure mode fails OPEN into a real validation (returns
        False): watermark unset, watermark for a different id, malformed
        or naive timestamp, or a timestamp in the future (clock skew).
        The gate can only ever SKIP work that would have confirmed a live
        def; it can never extend trust past the hard cap."""
        if getattr(self.state, "definition_id", None) != cached:
            return False
        stamp = getattr(self.state, "definition_validated_at", None)
        if not stamp:
            return False
        try:
            validated_at = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            return False
        if validated_at.tzinfo is None:
            return False
        age = datetime.now(timezone.utc) - validated_at
        ttl = min(DEFINITION_VALIDATION_TTL, DEFINITION_VALIDATION_HARD_CAP)
        return timedelta(0) <= age < ttl

    def _mark_definition_validated(self) -> None:
        """Advance the validated-at watermark to now (UTC). Called ONLY
        after a validation that actually ran and confirmed the def live,
        or after a fresh resolve (the resolver just found/created the def
        on the live account — that IS a validation). Never called on the
        trust-the-cache-on-network-error path, so a flake can't extend
        the watermark. Best-effort on duck-typed states that reject new
        attributes: the gate simply stays off."""
        try:
            self.state.definition_validated_at = (
                datetime.now(timezone.utc).isoformat()
            )
        except Exception:  # noqa: BLE001 — frozen/slotted fake states
            self.log.debug(
                "state object rejects definition_validated_at; "
                "validation gate disabled for this run",
            )

    def resolved_definition_id(
        self,
        expected_spec: dict,
        *,
        canonical_name: str,
        force_new: bool = False,
        create_extra: dict | None = None,
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
        returning it — at most once per ``DEFINITION_VALIDATION_TTL``
        (15 min; 24-h hard cap): a fresh ``definition_validated_at``
        watermark skips the full-catalog ``definition_exists`` fetch that
        every run used to pay. The TTL is time-based on purpose —
        ``GET /data/v1/updates`` was investigated (live, 2026-07-06) and
        does NOT surface definition create/soft-delete, so there is no
        cheaper change signal to key on. After a daemon re-auth to a different account, the
        cached id points at a def that doesn't exist here — ingest
        accepts events keyed to it silently and they end up orphaned in
        the timeline. On a stale hit we clear the cache and fall through
        to the resolver. Implementation requires the factory to provide
        a client whose `definition_exists(def_id)` is implemented (the
        worker's `_FulcraDefinitionAdapter`); a fake client without
        that method skips validation, which is fine for tests that
        don't exercise the stale path.

        `create_extra` (optional, default None) is forwarded verbatim to
        ``fulcra_common.definitions.resolve_definition_id`` and is merged
        into the create POST body ONLY when a new def is created — never
        when an existing def is adopted. Plugins that want a richer
        first-time create (e.g. Attention's canonical description +
        resolved tag ids) pass it here; every other plugin leaves it None
        and gets the unchanged sparse-create behaviour.
        """
        cached = getattr(self.state, "definition_id", None)
        if cached and not force_new:
            # Validation gate: skip the full-catalog definition_exists
            # fetch while the validated-at watermark is fresh (15-min TTL,
            # 24-h hard cap). Every plugin run used to pay this fetch.
            if self._definition_validation_fresh(cached):
                return cached
            client = (self._fulcra_client_factory()
                      if self._fulcra_client_factory is not None else None)
            still_present = True
            validated = False
            if client is not None and hasattr(client, "definition_exists"):
                try:
                    still_present = client.definition_exists(cached)
                    validated = still_present
                except Exception:
                    # Network/adapter failure — be conservative and trust
                    # the cache. The next run will retry the check. The
                    # watermark is NOT advanced: the check didn't run.
                    still_present = True
            if still_present:
                if validated:
                    self._mark_definition_validated()
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
                create_extra=create_extra,
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
                create_extra=create_extra,
            )
        self.state.definition_id = new_id
        # A fresh resolve IS a validation — the resolver just found or
        # created this def on the live account. Start the gate window now.
        self._mark_definition_validated()
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
            # Validation gate (same as resolved_definition_id): while the
            # watermark is fresh AND covers exactly this id, skip the
            # full-catalog definition_exists fetch. The watermark only
            # covers `cached` once a prior successful validation synced
            # state.definition_id to it, so a per-package cache that
            # disagrees with per-plugin state always gets a real check.
            if self._definition_validation_fresh(cached):
                return cached
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
                        self._mark_definition_validated()
                        return cached
                except Exception:
                    # Trust the cache on a flaky check; the watermark is
                    # NOT advanced (the check didn't run to completion).
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

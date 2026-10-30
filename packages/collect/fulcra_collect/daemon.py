"""The hub daemon: holds the registry + config, answers control-socket
requests, and runs the scheduler + supervisor loop.

The request handler and status snapshot are pure enough to unit-test;
`serve` runs the full loop: control socket, service supervision, and
scheduled dispatch.
"""
from __future__ import annotations

import importlib.metadata as _im
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone

from . import activity as _activity
from . import config as config_mod
from . import db as _db
from . import runner, state
from .config import Config
from .control import ControlServer
from .daemon_lease import DaemonLeaseUnavailable, acquire_daemon_lease
from .registry import RegistryResult, discover
from .scheduler import due_plugins


@dataclass(frozen=True)
class ServiceFailureStatus:
    """Volatile, scope-proved status only; never account-specific row data."""

    revision: str
    when: datetime
    error: str
    failures: int
    unknown: bool = False


def _control_socket_path():
    return config_mod.config_dir() / "control.sock"


def _distribution_for_plugin(plugin_id: str) -> str | None:
    """Find the distribution that registered this plugin's entry point.

    Returns the distribution name, or None if the plugin isn't found
    (or its entry point fails to load — the menubar's version display
    should never crash the hub the same way the plugin registry won't).
    """
    for ep in _im.entry_points(group="fulcra_collect.plugins"):
        try:
            obj = ep.load()
            # Two entry-point shapes: a Plugin object directly, or a callable
            # returning one. Match by id either way.
            candidate = obj() if callable(obj) and not hasattr(obj, "id") else obj
        except Exception:
            # A bad plugin must not crash the hub — same policy as
            # registry.load_plugins.
            continue
        if getattr(candidate, "id", None) == plugin_id:
            return ep.dist.name if ep.dist else None
    return None


def is_online(*, timeout: float = 2.0) -> bool:
    """Best-effort connectivity probe on ports normal networks permit.

    Used to defer (not fail) network-requiring scheduled plugins while the
    machine is offline. Probe the configured Fulcra endpoint first, then an
    ordinary HTTPS host. Direct TCP to public DNS resolvers produced false
    "offline" results on managed networks that correctly block port 53 while
    allowing HTTPS.
    """
    import socket
    from urllib.parse import urlsplit

    from fulcra_common import DEFAULT_BASE_URL

    parts = urlsplit(DEFAULT_BASE_URL)
    targets: list[tuple[str, int]] = []
    if parts.hostname:
        default_port = 443 if parts.scheme == "https" else 80
        targets.append((parts.hostname, parts.port or default_port))
    fallback = ("example.com", 443)
    if fallback not in targets:
        targets.append(fallback)
    for host, port in targets:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


class Daemon:
    def __init__(self, registry: RegistryResult | None = None,
                 config: Config | None = None, *, launch_menubar: bool = True) -> None:
        self._launch_menubar = launch_menubar
        self.registry = registry if registry is not None else discover()
        self.config = config if config is not None else config_mod.load()
        self._service_profile = config_mod.profile_path()
        self._service_supervisor = None
        self._service_failure_status: dict[str, ServiceFailureStatus] = {}
        self._service_masked: set[str] = set()
        self._worker_credential_status: dict[str, tuple[object, dict | None]] = {}
        # In-flight guard: a scheduled plugin must run at most once at a
        # time. `_inflight` holds the ids currently running; `_inflight_procs`
        # maps those ids to their live worker Popen so shutdown can stop
        # them. Both are guarded by `_inflight_lock`.
        self._inflight: set[str] = set()
        self._inflight_procs: dict[str, subprocess.Popen] = {}
        self._inflight_lock = threading.Lock()
        # Versions are cheap to compute but only at startup; the
        # menubar's About pane calls `version` every time the tab opens.
        self._version_snapshot = self._build_version_snapshot()
        # In-memory ring buffer of recent annotation writes — powers the web
        # UI's dashboard "Recently" feed. Lost on daemon restart (v1); sqlite
        # persistence is v1.5.
        self.activity = _activity.make_singleton()
        # Indirection so tests can inject a fake clock without monkeypatching
        # time.monotonic globally (which breaks pytest's own timing). The
        # production callable is set in __init__ and replaced in tests.
        import time as _time_mod
        self._monotonic = _time_mod.monotonic

        # NOTE: the account-switch pre-flight (_check_account_fingerprint)
        # is deliberately NOT run here. It reads the bearer token from the
        # OS keychain, and on macOS that read can block indefinitely on a
        # keychain-ACL confirmation dialog (e.g. after the daemon binary's
        # signing identity changes, or under a launchd/remote session where
        # the dialog can't be answered). Doing it in __init__ — before the
        # daemon binds its control socket — meant a single blocked keychain
        # read bricked the whole daemon: no control socket, no web UI, the
        # menubar showing "daemon not reachable" with no recourse but a
        # restart. The pre-flight is a cache-warming nicety, never a
        # correctness requirement (lazy per-call recovery is the real
        # safety net), so serve() runs it on a background thread AFTER the
        # sockets are up. Construction must stay keychain-free.

    # ---- account-switch pre-flight -------------------------------------

    @staticmethod
    def _account_identity(token: str) -> str:
        """The stable identity a bearer token belongs to.

        Fulcra bearer tokens are Auth0 JWTs; the `sub` claim identifies the
        ACCOUNT and survives token rotation. Decode it locally (no network,
        no signature verification — we need a stable identifier, not trust:
        an attacker who can plant a forged token in the keychain already
        owns the account's data path). Fall back to the raw token bytes for
        opaque/non-JWT tokens (pasted API tokens), preserving the original
        rotate-means-invalidate behavior for those.
        """
        import base64
        import json as _json
        parts = token.split(".")
        if len(parts) == 3:
            try:
                payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
                payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
                if isinstance(payload, dict):
                    sub = payload.get("sub")
                    if isinstance(sub, str) and sub:
                        return f"sub:{sub}"
            except (ValueError, UnicodeDecodeError):
                pass  # malformed JWT-lookalike — fall through to raw bytes
        return f"tok:{token}"

    def _check_account_fingerprint(self) -> None:
        """Detect a Fulcra-account switch since the previous boot and
        invalidate per-plugin caches that hold per-account UUIDs.

        Fingerprint is a SHA-256 prefix of the token's stable ACCOUNT
        identity (JWT `sub` claim) — NOT the token bytes. Access tokens
        rotate hourly (refresh-on-401 rewrites the keychain), so the old
        token-bytes fingerprint invalidated every plugin cache on
        effectively every daemon restart >1h apart: a full definitions
        re-resolution storm (one whole-catalog fetch per plugin) with no
        actual account change. Opaque non-JWT tokens fall back to the
        token-bytes behavior via _account_identity.

        One-time migration note: fingerprints written by the old scheme
        won't match the new one, so the first boot after this change
        triggers a single spurious invalidation — same cost as one old
        false positive, then never again.
        """
        import hashlib
        from . import credentials as _creds
        token = _creds.get_user_secret("bearer-token") or ""
        if not token:
            # Not signed in yet — nothing to invalidate, nothing to
            # remember. The fingerprint file gets written the first time
            # we boot WITH a token.
            return
        identity = self._account_identity(token)
        fingerprint = hashlib.sha256(identity.encode()).hexdigest()[:16]
        fingerprint_path = config_mod.config_dir() / "auth-fingerprint"
        previous = (fingerprint_path.read_text().strip()
                    if fingerprint_path.exists() else None)
        if previous == fingerprint:
            return
        if previous is not None:
            self._invalidate_plugin_caches(
                reason=f"Fulcra account fingerprint changed "
                       f"({previous}→{fingerprint})",
            )
        fingerprint_path.parent.mkdir(parents=True, exist_ok=True)
        fingerprint_path.write_text(fingerprint)
        try:
            import os
            os.chmod(fingerprint_path, 0o600)
        except OSError:
            pass

    def _invalidate_plugin_caches(self, *, reason: str) -> None:
        """Clear per-plugin and per-package def_ids + tag_ids on the
        host. Plugins re-resolve fresh defs on their next run.

        Best-effort: each clear is wrapped, a failure in one plugin
        doesn't block invalidation of others. Day One isn't touched
        because it re-queries by name on every run — no cache to clear.
        """
        import importlib
        logger = logging.getLogger("fulcra_collect.daemon")
        logger.warning("Invalidating plugin caches: %s", reason)
        cleared: list[str] = []
        # Per-plugin state rows in state.db — clear definition_id on any
        # row that has one cached. Phase 1 of refactor #1 moved per-plugin
        # state out of state/<id>.json into the unified SQLite db; the
        # surface here stays the same (load → mutate → save) but the
        # enumeration is a SELECT instead of a directory glob.
        try:
            conn = _db.open()
            for plugin_id in _db.all_plugin_ids(conn):
                try:
                    st = state.load(plugin_id)
                    if getattr(st, "definition_id", None) is not None:
                        st.definition_id = None
                        state.save(st)
                        cleared.append(f"plugin_state[{plugin_id}]")
                except Exception:
                    logger.exception("invalidate: failed for %s", plugin_id)
        except Exception:
            logger.exception("invalidate: failed to enumerate plugin_state")
        # Per-package state files (media). Deferred import tolerates the
        # module being absent.
        for label, module_name, attrs in (
            ("media", "fulcra_media.state",
             ("listened_definition_id", "watched_definition_id",
              "read_definition_id", "activity_definition_id", "tag_ids")),
        ):
            try:
                mod = importlib.import_module(module_name)
            except ImportError:
                continue
            try:
                s = mod.load()
                touched = False
                for attr in attrs:
                    if not hasattr(s, attr):
                        continue
                    cur = getattr(s, attr)
                    if isinstance(cur, dict):
                        if cur:
                            setattr(s, attr, {})
                            touched = True
                    elif cur is not None:
                        setattr(s, attr, None)
                        touched = True
                if touched:
                    mod.save(s)
                    cleared.append(f"{label} state.json")
            except Exception:
                logger.exception("invalidate: failed for %s state", label)
        # Dashboard surface so the user can see what happened.
        self.activity.add(
            plugin_id="daemon",
            summary=(
                "Account change detected. Invalidated cached def IDs + tag "
                f"UUIDs across {len(cleared)} state file"
                f"{'s' if len(cleared) != 1 else ''}. "
                "Plugins will re-resolve on next run."
            ),
            ok=True,
        )

    # ---- control-socket request handling -------------------------------

    def handle_request(self, request: dict) -> dict:
        cmd = request.get("cmd")
        if cmd == "status":
            return self._status()
        if cmd == "run":
            return self._run(request.get("plugin", ""))
        if cmd == "reload":
            self.config = config_mod.load()
            return {"ok": True}
        if cmd == "version":
            return {"ok": True, **self._version_snapshot}
        if cmd == "credential_status":
            return self._credential_status(request.get("plugin", ""))
        if cmd == "authorize_credential":
            return self._authorize_credential(request.get("plugin", ""), request.get("key", ""))
        if cmd == "set_credential":
            return self._set_credential(
                request.get("plugin", ""), request.get("key", ""),
                request.get("secret", ""),
            )
        if cmd == "delete_credential":
            return self._delete_credential(
                request.get("plugin", ""), request.get("key", ""),
            )
        if cmd == "quick_record_v2":
            from .quick_record_protocol import handle_quick_record
            return handle_quick_record(request)
        if cmd in {"quick_record_list", "record_annotation", "delete_annotation",
                   "get_quick_record_favorites", "set_quick_record_favorites"}:
            # Obsolete unscoped clients cannot write records, sentinels or pins.
            # Refuse before auth, source/state reads or metadata effects.
            return {"ok": False, "code": "refresh_required",
                    "error": "Quick record requires the current Collect app. Refresh or update it."}
        if cmd == "delete_definition":
            return self._delete_definition(request.get("def_id", ""))
        if cmd == "restore_definition":
            return self._restore_definition(request.get("def_id", ""))
        if cmd == "update_definition":
            return self._update_definition(
                request.get("def_id", ""),
                request.get("fields") or {},
            )
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    def _status(self) -> dict:
        plugins = []
        pending_exits = (dict(self._service_supervisor.unknown_exits)
                         if self._service_supervisor is not None else {})
        cached_failures = dict(self._service_failure_status)
        has_pending = bool(pending_exits) or any(
            observation.unknown for observation in cached_failures.values())
        for pid, plugin in sorted(self.registry.plugins.items()):
            pending = pending_exits.get(pid)
            cached = cached_failures.get(pid)
            unknown = False
            if pending is not None or pid in self._service_masked or (cached is not None and cached.unknown):
                st, unknown = self._service_failure_state(pid, pending)
            else:
                try:
                    st = state.load(pid)
                except Exception:  # noqa: BLE001 — keep the pending UNKNOWN row visible
                    if not has_pending:
                        raise
                    st = state.PluginState(pid)
                    unknown = True
                    st.last_outcome = "error"
                    st.last_error = "Cached status UNKNOWN; local state could not be read."
            credential_error = self._current_worker_credential_error(pid)
            plugins.append({
                "id": pid,
                "name": plugin.name,
                "kind": plugin.kind,
                # SP3: surface per-plugin collect_mode so the menubar can
                # split the popover/preferences into a "historical (one-shot
                # imports)" group vs. a "live (continuously polled)" group
                # and render a per-row chip in Preferences. Static field
                # from the Plugin dataclass — no state lookup needed.
                "collect_mode": plugin.collect_mode,
                "description": plugin.description,
                "category": plugin.category,
                "enabled": pid in self.config.enabled,
                "last_run": st.last_run.isoformat() if st.last_run else None,
                "last_outcome": st.last_outcome,
                "last_error": st.last_error,
                "consecutive_failures": st.consecutive_failures,
                "credential_error": credential_error,
                "credential_error_status": "observed" if credential_error is not None else "unknown",
                **({"service_status": "unknown"} if unknown else {}),
                # Surface the currently-bound Fulcra annotation definition id
                # so the wizard's "View on Fulcra timeline" deep-link can
                # filter to events of this plugin's track. Null until the
                # plugin's first run resolves a def (or the user picks one
                # via the definition_picker step).
                "definition_id": st.definition_id,
                "default_interval_s": (
                    int(plugin.default_interval.total_seconds())
                    if plugin.default_interval else None
                ),
            })
        return {"ok": True, "plugins": plugins,
                "load_errors": dict(self.registry.errors)}

    def _build_version_snapshot(self) -> dict:
        """Build the version dict cached at construction for the
        control-socket 'version' handler. Plugins whose distribution
        can't be resolved are silently omitted; an unresolvable daemon
        version falls back to 'unknown'.

        Also includes ``daemon_pid`` — captured here at construction time
        so the menubar can show "Running (PID 12345)" in its daemon
        controls section without having to call /api/version every tick.
        Since ``handle_request({"cmd": "version"})`` is answered by THIS
        process, os.getpid() is authoritative."""
        import os
        plugins: dict[str, str] = {}
        for pid in self.registry.plugins:
            dist = _distribution_for_plugin(pid)
            if dist is None:
                continue
            try:
                plugins[pid] = _im.version(dist)
            except _im.PackageNotFoundError:
                continue
        try:
            daemon_version = _im.version("fulcra-collect")
        except _im.PackageNotFoundError:
            daemon_version = "unknown"
        return {
            "daemon_version": daemon_version,
            "plugins": plugins,
            "daemon_pid": os.getpid(),
        }

    def _credential_status(self, plugin_id: str) -> dict:
        plugin = self.registry.plugins.get(plugin_id)
        if plugin is None:
            return {"ok": False, "error": f"unknown plugin {plugin_id!r}"}
        from . import credentials  # deferred so daemon stays importable
                                   # without a live keychain; tests
                                   # monkeypatch has_secret on this module
        out: dict[str, str] = {}
        for cred in plugin.required_credentials:
            try:
                present = (credentials.has_user_secret(cred.key)
                           if getattr(cred, "user_level", False)
                           else credentials.has_secret(plugin_id, cred.key))
                out[cred.key] = "set" if present else "missing"
            except credentials.KeychainAccessRequired:
                out[cred.key] = "access_required"
            except Exception:
                out[cred.key] = "unavailable"
        if "access_required" in out.values():
            return {"ok": False, "credentials": out, "code": "keychain_access_required",
                    "error": "Open Configure and choose Allow Keychain access."}
        if "unavailable" in out.values():
            return {"ok": False, "credentials": out, "code": "keychain_unavailable",
                    "error": "Keychain read is unavailable. A saved credential may still exist."}
        return {"ok": True, "credentials": out}

    def _authorize_credential(self, plugin_id: str, key: str) -> dict:
        err = self._check_credential_key(plugin_id, key)
        if err is not None:
            return err
        from . import credentials
        from .config_leases import task_mutation_scope
        try:
            with config_mod.pinned_profile(self._service_profile), task_mutation_scope(timeout_s=.2):
                if config_mod.load().account_transition:
                    raise credentials.CredentialStateChanged('Account transition in progress.')
                present = (credentials.authorize_user_secret(key)
                           if self._credential_is_user_level(plugin_id, key)
                           else credentials.authorize_secret(plugin_id, key))
                return {"ok": bool(present), "status": "set" if present else "missing"}
        except (credentials.KeychainAccessRequired, TimeoutError):
            return {"ok": False, "code": "keychain_access_required",
                    "error": "Finish the Keychain access request, then check again. "
                             "Always Allow remembers access for this app."}
        except Exception:
            return {"ok": False, "code": "keychain_unavailable",
                    "error": "Keychain access could not be verified. Your saved credential was kept."}

    def _check_credential_key(self, plugin_id: str, key: str) -> dict | None:
        """Return an error reply if (plugin_id, key) doesn't name a
        declared required_credential, else None."""
        plugin = self.registry.plugins.get(plugin_id)
        if plugin is None:
            return {"ok": False, "error": f"unknown plugin {plugin_id!r}"}
        if not any(c.key == key for c in plugin.required_credentials):
            return {"ok": False,
                    "error": f"plugin {plugin_id!r} does not declare credential {key!r}"}
        return None

    def _credential_is_user_level(self, plugin_id: str, key: str) -> bool:
        """Return whether the plugin declares credential ``key`` as
        user_level (account-scoped, stored under "fulcra-collect:user")
        rather than plugin-scoped.

        This is the SAME per-credential lookup ``_credential_status`` uses
        (find the plugin's declared Credential for ``key``, read its
        ``user_level`` flag) — factored out so the status / set / delete
        paths all route to the same keychain scope and can't drift apart.
        Defaults to False (plugin-scoped, the common case) when the plugin
        or the credential isn't found; callers gate on
        ``_check_credential_key`` first, so a missing credential here means
        "treat as the default plugin scope" rather than an error."""
        plugin = self.registry.plugins.get(plugin_id)
        if plugin is None:
            return False
        for cred in plugin.required_credentials:
            if cred.key == key:
                return bool(getattr(cred, "user_level", False))
        return False

    def _set_credential(self, plugin_id: str, key: str, secret: str) -> dict:
        # Route by scope, mirroring _credential_status: a credential the
        # plugin declares user_level=True is written to the account-scoped
        # store ("fulcra-collect:user") via set_user_secret; everything else
        # (the common case) goes to the plugin-scoped store via set_secret.
        # Using the SAME _credential_is_user_level lookup the status/delete
        # paths use keeps all three scopes consistent — a user_level
        # credential set here is found by has_user_secret in _credential_status
        # and removed by delete_user_secret in _delete_credential.
        err = self._check_credential_key(plugin_id, key)
        if err is not None:
            return err
        from . import credentials  # deferred so daemon stays importable without
                                   # a live keychain; tests monkeypatch on this module
        try:
            if plugin_id == "trakt":
                from .trakt_source import Owner, KeychainStore
                return Owner(KeychainStore()).edit(key, secret)
            from .config import invalidate_plugin_work
            invalidate_plugin_work(plugin_id)
            if self._credential_is_user_level(plugin_id, key):
                credentials.set_user_secret(key, secret)
            else:
                credentials.set_secret(plugin_id, key, secret)
            return {"ok": True}
        except Exception:
            import logging
            logging.getLogger("fulcra_collect.daemon").exception(
                "set_credential failed for %s/%s", plugin_id, key,
            )
            return {"ok": False, "error": "keychain write failed"}

    def _delete_credential(self, plugin_id: str, key: str) -> dict:
        # Route by scope, mirroring _set_credential / _credential_status: a
        # user_level credential is removed from the account-scoped store via
        # delete_user_secret; everything else from the plugin-scoped store via
        # delete_secret. Both deletes are idempotent (absence is success).
        err = self._check_credential_key(plugin_id, key)
        if err is not None:
            return err
        from . import credentials  # deferred so daemon stays importable without
                                   # a live keychain; tests monkeypatch on this module
        try:
            if plugin_id == "trakt":
                from .trakt_source import Owner, KeychainStore
                return Owner(KeychainStore()).edit(key, None)
            from .config import invalidate_plugin_work
            invalidate_plugin_work(plugin_id)
            if self._credential_is_user_level(plugin_id, key):
                credentials.delete_user_secret(key)
            else:
                credentials.delete_secret(plugin_id, key)
            return {"ok": True}
        except Exception:
            import logging
            logging.getLogger("fulcra_collect.daemon").exception(
                "delete_credential failed for %s/%s", plugin_id, key,
            )
            return {"ok": False, "error": "keychain delete failed"}

    def _delete_definition(self, def_id: str) -> dict:
        """Soft-delete legacy definition metadata and clear matching legacy state.

        Preserve unscoped favorites, account-scoped pins and v1 destinations.

        Single business-logic site shared by the HTTP route
        (``routes/definitions.py:delete_definition_route`` — now a thin
        wrapper that translates the structured error returns below back
        into ``HTTPException``) and the UDS command branch in
        :meth:`handle_request` (which the menubar's
        ``DaemonClient.delete_definition`` reaches via the local socket).

        Behaviour preserved from the pre-SP2 HTTP-only implementation:

        - DELETE to ``/user/v1alpha1/annotation/{def_id}``; treat 204 as
          success and 404 as already-deleted (still surfaced as an error
          so the caller knows the request is a no-op).
        - On success, walk every registered plugin's state and clear
          ``definition_id`` on any that pointed at the deleted def. Without
          this the next run of those plugins would either silently fail or
          re-create a side-def on Fulcra.
        - Leave legacy favorites untouched until account/mapping proof
          authorizes migration. Scoped pins may retain a hidden choice;
          fresh protocol2 catalogs omit the deleted definition.
        - Failure modes that the HTTP route used to raise ``HTTPException``
          for (404, 401, 5xx, network, timeout) are translated to
          ``{"ok": False, "error": "..."}`` returns with the SAME
          human-readable messages. The HTTP route translates back to
          ``HTTPException`` to preserve its API contract.

        Args:
            def_id: the annotation definition UUID to soft-delete.

        Returns:
            ``{"ok": True, "cleared_plugins": [plugin_ids]}`` on success.
            ``{"ok": False, "error": message}`` on any failure mode.
        """
        from . import credentials as _creds
        from . import state as _state_mod
        # Late import — tests monkeypatch ``fulcra_collect.web.httpx``
        # to substitute a fake HTTP client. Reaching httpx via the
        # ``web`` module attribute (rather than the top-level ``httpx``
        # imported at this module's load time) is what makes the same
        # patching site work for both the HTTP route AND this method.
        from . import web as _web

        _log = logging.getLogger("fulcra_collect.daemon")
        if not def_id:
            return {"ok": False, "code": "bad_request", "error": "def_id is required"}

        fulcra_token = _creds.get_user_secret("bearer-token")
        if not fulcra_token:
            return {"ok": False, "code": "unauthorized", "error": "not signed in to Fulcra"}

        # Use the same refresh-aware wrapper the FastAPI routes use, so a
        # soft-delete from the menubar's Annotations tab or the popover
        # "…" menu (both arrive via the local UDS) also benefits from the
        # automatic refresh-on-401 introduced in SP5 task 1. The wrapper
        # class lives at module scope in ``web.py``; reaching it via
        # ``_web._RetryingClient`` means tests that monkeypatch
        # ``fulcra_collect.web.httpx`` continue to intercept the inner
        # ``httpx.Client(...)`` call without modification.
        #
        # Distinct User-Agent kept (``fulcra-collect/daemon`` vs the
        # route's ``fulcra-collect/web-ui``) so upstream log triage can
        # tell the two surfaces apart.
        try:
            with _web._RetryingClient(
                fulcra_token, user_agent="fulcra-collect/daemon",
            ) as client:
                r = client.delete(f"/user/v1alpha1/annotation/{def_id}")
                if r.status_code == 404:
                    return {
                        "ok": False,
                        "code": "not_found",
                        "error": "Definition not found — it may have already been deleted.",
                    }
                if r.status_code != 204:
                    r.raise_for_status()
        except _web.httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            _log.warning("delete_definition(%s): Fulcra returned %s", def_id, status)
            if status in (401, 403):
                return {
                    "ok": False,
                    "code": "unauthorized",
                    "error": (
                        "Fulcra rejected the request — your sign-in may have expired. "
                        "Re-run sign-in from the wizard or paste a fresh token."
                    ),
                }
            if 500 <= status < 600:
                return {
                    "ok": False,
                    "code": "upstream_error",
                    "error": f"Fulcra returned {status}. Try again in a moment.",
                }
            return {
                "ok": False,
                "code": "upstream_error",
                "error": f"Fulcra returned an unexpected {status}.",
            }
        except (_web.httpx.ConnectError, _web.httpx.ConnectTimeout) as exc:
            _log.warning("delete_definition(%s): connect failed: %r", def_id, exc)
            return {
                "ok": False,
                "code": "upstream_error",
                "error": "Couldn't reach Fulcra. Check your internet, then try again.",
            }
        except _web.httpx.TimeoutException as exc:
            _log.warning("delete_definition(%s): timed out: %r", def_id, exc)
            return {
                "ok": False,
                "code": "timeout",
                "error": "Fulcra took too long to respond. Try again in a moment.",
            }
        except Exception as exc:
            _log.exception("delete_definition(%s): unexpected failure", def_id)
            return {
                "ok": False,
                "code": "upstream_error",
                "error": (
                    f"Fulcra request failed unexpectedly ({type(exc).__name__}). "
                    "Check the daemon log for details."
                ),
            }

        # Clear the cached definition_id on any plugin that was bound to
        # the deleted def. Without this the next run would try to write
        # to a tombstoned def and either silently fail or re-create a new
        # def on the side (depending on the plugin's error path).
        cleared: list[str] = []
        for p in self.registry.plugins.values():
            try:
                st = _state_mod.load(p.id)
            except Exception:
                # Per-plugin state corruption shouldn't abort the delete.
                continue
            if getattr(st, "definition_id", None) == def_id:
                st.definition_id = None
                _state_mod.save(st)
                cleared.append(p.id)
        if cleared:
            _log.info(
                "delete_definition(%s): cleared cached definition_id on %d plugin(s): %s",
                def_id, len(cleared), ", ".join(cleared),
            )
        return {"ok": True, "cleared_plugins": cleared}

    def _restore_definition(self, def_id: str) -> dict:
        """Restore (un-soft-delete) an annotation definition via Fulcra's
        ``cancel_deletion`` endpoint — the undo for :meth:`_delete_definition`.

        Mirrors ``_delete_definition``'s structure (same auth check, same
        refresh-aware HTTP wrapper, same structured error returns keyed by
        ``code``) so the HTTP route's error translation is shared. Shared
        by the HTTP route (``routes/definitions.py:restore_definition_route``)
        and the UDS command branch in :meth:`handle_request`.

        Deliberately does NOT re-establish local state that the delete
        cleaned up:

        - plugin ``definition_id`` bindings cleared by the delete are NOT
          re-bound (we can't know which plugins *should* point at the def
          again — a plugin may have re-resolved a fresh def since).
        Favorites were preserved by the delete and remain unchanged.

        The success payload carries ``rebound: False`` plus a human-readable
        ``note`` so callers/UI surface the caveat. The quick-record cache IS
        busted so the restored def shows up in the next list call.

        Args:
            def_id: the annotation definition UUID to restore.

        Returns:
            ``{"ok": True, "rebound": False, "note": ...}`` on success.
            ``{"ok": False, "code": ..., "error": ...}`` on any failure mode.
        """
        from . import credentials as _creds
        # Late import — tests monkeypatch ``fulcra_collect.web.httpx``; see
        # the equivalent comment in _delete_definition.
        from . import web as _web

        _log = logging.getLogger("fulcra_collect.daemon")
        if not def_id:
            return {"ok": False, "code": "bad_request", "error": "def_id is required"}

        fulcra_token = _creds.get_user_secret("bearer-token")
        if not fulcra_token:
            return {"ok": False, "code": "unauthorized", "error": "not signed in to Fulcra"}

        _log.info("restore_definition(%s): requesting cancel_deletion", def_id)
        try:
            with _web._RetryingClient(
                fulcra_token, user_agent="fulcra-collect/daemon",
            ) as client:
                r = client.post(f"/user/v1alpha1/annotation/{def_id}/cancel_deletion")
                if r.status_code == 404:
                    _log.warning("restore_definition(%s): Fulcra returned 404", def_id)
                    return {
                        "ok": False,
                        "code": "not_found",
                        "error": "Definition not found — check the id, or it may have been permanently removed.",
                    }
                # Any 2xx counts as restored (the endpoint returns the
                # annotation body on 200; tolerate a bodyless 204 too).
                r.raise_for_status()
        except _web.httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            _log.warning("restore_definition(%s): Fulcra returned %s", def_id, status)
            if status in (401, 403):
                return {
                    "ok": False,
                    "code": "unauthorized",
                    "error": (
                        "Fulcra rejected the request — your sign-in may have expired. "
                        "Re-run sign-in from the wizard or paste a fresh token."
                    ),
                }
            if 500 <= status < 600:
                return {
                    "ok": False,
                    "code": "upstream_error",
                    "error": f"Fulcra returned {status}. Try again in a moment.",
                }
            return {
                "ok": False,
                "code": "upstream_error",
                "error": f"Fulcra returned an unexpected {status}.",
            }
        except (_web.httpx.ConnectError, _web.httpx.ConnectTimeout) as exc:
            _log.warning("restore_definition(%s): connect failed: %r", def_id, exc)
            return {
                "ok": False,
                "code": "upstream_error",
                "error": "Couldn't reach Fulcra. Check your internet, then try again.",
            }
        except _web.httpx.TimeoutException as exc:
            _log.warning("restore_definition(%s): timed out: %r", def_id, exc)
            return {
                "ok": False,
                "code": "timeout",
                "error": "Fulcra took too long to respond. Try again in a moment.",
            }
        except Exception as exc:
            _log.exception("restore_definition(%s): unexpected failure", def_id)
            return {
                "ok": False,
                "code": "upstream_error",
                "error": (
                    f"Fulcra request failed unexpectedly ({type(exc).__name__}). "
                    "Check the daemon log for details."
                ),
            }

        # Bust the in-memory quick-record cache so the next list call sees
        # the restored def instead of a stale tombstone-filtered snapshot.
        self._quick_record_cache = None
        _log.info(
            "restore_definition(%s): restored on Fulcra; plugin bindings not "
            "re-established, favorites preserved",
            def_id,
        )
        return {
            "ok": True,
            "rebound": False,
            "note": (
                "plugin bindings are not restored — re-bind in plugin settings; "
                "favorites were kept"
            ),
        }

    def _update_definition(self, def_id: str, fields: dict) -> dict:
        """Rename/update an annotation definition IN PLACE via Fulcra's
        ``PUT /user/v1alpha1/annotation/{id}`` — P2 item 7 from the collect
        review. Before this, the only "rename" path was bind_definition's
        force_new+new_name flow, which creates a brand-new definition and
        orphans every event recorded under the old one. That flow stays
        (it's correct for genuinely-new defs); this is the in-place edit.

        Mirrors ``_delete_definition`` / ``_restore_definition``: same auth
        check, same refresh-aware ``_RetryingClient``, same structured
        ``{ok, code, error}`` returns translated by the HTTP route via
        ``_DAEMON_CODE_TO_HTTP``. Shared by the HTTP route
        (``routes/definitions.py:update_definition_route``) and the UDS
        command branch in :meth:`handle_request` (menubar parity).

        The Fulcra PUT is a FULL-REPLACE discriminated union (every member
        requires name+description+tags; the measured types also require
        measurement_spec, scale additionally spec). So this GETs the
        current record, merges only the validated changed fields via
        ``fulcra_common.merge_definition_update``, and PUTs the complete
        body — a partial body would null-out measurement_spec/spec and
        corrupt scale/numeric definitions. Only name/description/tags may
        change; anything else is rejected as ``bad_request`` before any
        network traffic (``fulcra_common.validate_definition_update``).

        On success the in-memory quick-record cache is busted so the next
        ``_quick_record_list`` shows the new name instead of a stale one.
        Plugin bindings are untouched — the definition id is unchanged, so
        every binding remains valid (that's the whole point).

        Args:
            def_id: the annotation definition UUID to update.
            fields: dict with any of ``name`` / ``description`` / ``tags``.

        Returns:
            ``{"ok": True, "definition": {id, name, description, tags,
            annotation_type}}`` on success (merged, as sent to Fulcra).
            ``{"ok": False, "code": ..., "error": ...}`` on any failure.
        """
        from fulcra_common import (
            merge_definition_update,
            validate_definition_update,
        )

        from . import credentials as _creds
        # Late import — tests monkeypatch ``fulcra_collect.web.httpx``; see
        # the equivalent comment in _delete_definition.
        from . import web as _web

        _log = logging.getLogger("fulcra_collect.daemon")
        if not def_id:
            return {"ok": False, "code": "bad_request", "error": "def_id is required"}
        try:
            effective = validate_definition_update(
                fields if isinstance(fields, dict) else {},
            )
        except ValueError as exc:
            _log.warning("update_definition(%s): rejected: %s", def_id, exc)
            return {"ok": False, "code": "bad_request", "error": str(exc)}

        fulcra_token = _creds.get_user_secret("bearer-token")
        if not fulcra_token:
            return {"ok": False, "code": "unauthorized", "error": "not signed in to Fulcra"}

        _log.info(
            "update_definition(%s): updating field(s) %s",
            def_id, ", ".join(sorted(effective)),
        )
        try:
            with _web._RetryingClient(
                fulcra_token, user_agent="fulcra-collect/daemon",
            ) as client:
                # Leg 1 — GET the current record. The PUT union is a full
                # replace, so the unchanged fields must come from here.
                r = client.get(f"/user/v1alpha1/annotation/{def_id}")
                if r.status_code == 404:
                    _log.warning("update_definition(%s): Fulcra returned 404 on GET", def_id)
                    return {
                        "ok": False,
                        "code": "not_found",
                        "error": "Definition not found — check the id, or it may have been deleted.",
                    }
                r.raise_for_status()
                body = merge_definition_update(r.json(), effective)
                # Leg 2 — PUT the complete merged body back.
                r2 = client.put(
                    f"/user/v1alpha1/annotation/{def_id}", json=body,
                )
                if r2.status_code == 404:
                    _log.warning("update_definition(%s): Fulcra returned 404 on PUT", def_id)
                    return {
                        "ok": False,
                        "code": "not_found",
                        "error": "Definition not found — it may have been deleted just now.",
                    }
                # The OpenAPI spec declares a 307 success for this PUT and
                # the client follows redirects — treat anything below 400
                # as success, raise on the rest.
                if r2.status_code >= 400:
                    r2.raise_for_status()
        except _web.httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            _log.warning("update_definition(%s): Fulcra returned %s", def_id, status)
            if status in (401, 403):
                return {
                    "ok": False,
                    "code": "unauthorized",
                    "error": (
                        "Fulcra rejected the request — your sign-in may have expired. "
                        "Re-run sign-in from the wizard or paste a fresh token."
                    ),
                }
            if 500 <= status < 600:
                return {
                    "ok": False,
                    "code": "upstream_error",
                    "error": f"Fulcra returned {status}. Try again in a moment.",
                }
            return {
                "ok": False,
                "code": "upstream_error",
                "error": f"Fulcra returned an unexpected {status}.",
            }
        except (_web.httpx.ConnectError, _web.httpx.ConnectTimeout) as exc:
            _log.warning("update_definition(%s): connect failed: %r", def_id, exc)
            return {
                "ok": False,
                "code": "upstream_error",
                "error": "Couldn't reach Fulcra. Check your internet, then try again.",
            }
        except _web.httpx.TimeoutException as exc:
            _log.warning("update_definition(%s): timed out: %r", def_id, exc)
            return {
                "ok": False,
                "code": "timeout",
                "error": "Fulcra took too long to respond. Try again in a moment.",
            }
        except Exception as exc:
            _log.exception("update_definition(%s): unexpected failure", def_id)
            return {
                "ok": False,
                "code": "upstream_error",
                "error": (
                    f"Fulcra request failed unexpectedly ({type(exc).__name__}). "
                    "Check the daemon log for details."
                ),
            }

        # Bust the in-memory quick-record cache so the next list call shows
        # the new name/tags instead of a stale snapshot. Plugin bindings
        # deliberately untouched — the id didn't change.
        self._quick_record_cache = None
        _log.info(
            "update_definition(%s): updated on Fulcra; quick-record cache busted",
            def_id,
        )
        return {
            "ok": True,
            "definition": {
                "id": def_id,
                "annotation_type": body.get("annotation_type"),
                "name": body.get("name"),
                "description": body.get("description"),
                "tags": body.get("tags"),
            },
        }

    def _run(self, plugin_id: str) -> dict:
        if plugin_id not in self.registry.plugins:
            return {"ok": False, "error": f"unknown plugin {plugin_id!r}"}
        started = self._trigger(plugin_id)
        if started:
            return {"ok": True, "started": True}
        return {"ok": True, "started": False, "note": "already running"}

    def _trigger(self, plugin_id: str) -> bool:
        """Fire one run of a plugin in a background thread — non-blocking,
        so a long run never stalls the tick loop or the control socket.

        Returns True if a run was started, False if one was already
        in-flight for this plugin (the in-flight guard prevents concurrent
        duplicate runs of the same scheduled plugin). Overridden in tests."""
        with self._inflight_lock:
            if plugin_id in self._inflight:
                return False
            self._inflight.add(plugin_id)

        def _work() -> None:
            try:
                runner.run(
                    plugin_id, runner.worker_command(plugin_id),
                    now=datetime.now(timezone.utc),
                    on_spawn=lambda proc: self._register_proc(plugin_id, proc),
                    daemon=self,
                )
            finally:
                with self._inflight_lock:
                    self._inflight.discard(plugin_id)
                    self._inflight_procs.pop(plugin_id, None)

        threading.Thread(target=_work, daemon=True).start()
        return True

    def _register_proc(self, plugin_id: str, proc: subprocess.Popen) -> None:
        """Track a worker Popen so `serve`'s shutdown can terminate it."""
        with self._inflight_lock:
            self._inflight_procs[plugin_id] = proc

    def _worker_authority(self, current: Config, plugin_id: str):
        from .owned_auth import attempts
        if current.account_transition or plugin_id not in self.registry.plugins:
            return None
        return (self._service_revision(current, plugin_id),
                plugin_id in current.enabled, attempts().read_committed())

    def _capture_worker_scope(self, plugin_id: str):
        from .config_leases import task_mutation_scope
        try:
            with config_mod.pinned_profile(self._service_profile), task_mutation_scope(timeout_s=.2):
                return self._worker_authority(config_mod.load(), plugin_id)
        except Exception:
            return None

    @contextmanager
    def _worker_result_scope(self, plugin_id: str, captured, detail):
        """Fence terminal state and volatile recovery against the original job authority."""
        from .config_leases import task_mutation_scope
        with ExitStack() as stack:
            try:
                stack.enter_context(config_mod.pinned_profile(self._service_profile))
                stack.enter_context(task_mutation_scope(timeout_s=.2))
                current = self._worker_authority(config_mod.load(), plugin_id)
                valid = captured is not None and captured == current
            except Exception:
                valid = False
            if not valid:
                yield False
                return
            yield True
            self._worker_credential_status[plugin_id] = (captured, self._validated_credential_error(plugin_id, detail))

    def _validated_credential_error(self, plugin_id: str, detail):
        if not isinstance(detail, dict) or set(detail) != {'stage', 'scope', 'key', 'availability'}:
            return None
        if not all(isinstance(value, str) and len(value) <= 256 for value in detail.values()):
            return None
        if detail['availability'] not in {'access_required', 'pending', 'changed', 'unavailable', 'missing'}:
            return None
        if detail['stage'] == 'collect_account' and detail['scope'] == 'user' and detail['key'] == 'bearer-token':
            return {**detail, 'recovery': 'settings'}
        if detail['stage'] == 'credential_hydration':
            for cred in self.registry.plugins[plugin_id].required_credentials:
                scope = 'user' if getattr(cred, 'user_level', False) else 'plugin'
                if detail['key'] == cred.key and detail['scope'] == scope:
                    return {**detail, 'recovery': 'configure'}
        return None

    def _current_worker_credential_error(self, plugin_id: str):
        cached = self._worker_credential_status.get(plugin_id)
        if cached is not None and cached[0] == self._capture_worker_scope(plugin_id):
            return cached[1]
        return None

    def _spawn_service(self, plugin_id: str):
        """Spawn a service plugin's worker subprocess (kept alive by the
        ServiceSupervisor)."""
        return subprocess.Popen(
            runner.worker_command(plugin_id),
            start_new_session=(os.name == "posix"),
        )

    def _service_revision(self, current: Config, plugin_id: str) -> str:
        return config_mod.settings_fingerprint({
            "profile": str(self._service_profile),
            "epoch": current.plugin_epochs.get(plugin_id, ""),
            "settings": current.plugin_settings.get(plugin_id, {}),
        })

    def _service_failure_state(self, plugin_id: str, pending):
        """Never release an old unscoped row after uncertainty/account change.

        Only a locally confirmed current failure can replace its stale mask.
        A launch alone proves nothing. Counts here include only confirmed
        observations of this revision in this daemon, not old account rows.
        """
        from .config_leases import task_mutation_scope

        try:
            with config_mod.pinned_profile(self._service_profile), task_mutation_scope(timeout_s=1):
                current = config_mod.load()
                cached = self._service_failure_status.get(plugin_id)
                pending_current = (pending is not None and self._service_revision_matches(
                    current, plugin_id, pending.revision))
                cached_current = (cached is not None and self._service_revision_matches(
                    current, plugin_id, cached.revision))
                if cached_current and not cached.unknown and not pending_current:
                    return state.PluginState(plugin_id, last_run=cached.when,
                        last_outcome="error", last_error=cached.error,
                        consecutive_failures=cached.failures), False
                if not pending_current and not cached_current:
                    return state.PluginState(plugin_id), False
        except Exception:  # noqa: BLE001 — static UNKNOWN, never parser/lease text
            pass
        st = state.PluginState(plugin_id, last_outcome="error", last_error=(
            "Service failure status UNKNOWN; the worker failed but its cached status could not be confirmed."))
        return st, True

    def _service_revision_matches(self, current: Config, plugin_id: str, revision) -> bool:
        return (not current.account_transition and plugin_id in current.enabled
                and plugin_id in self.registry.plugins
                and self.registry.plugins[plugin_id].kind == "service"
                and revision == self._service_revision(current, plugin_id))

    def _remember_service_failure(self, plugin_id, revision, when, error, *, prior):
        same_scope = prior is not None and prior.revision == revision
        self._service_failure_status[plugin_id] = ServiceFailureStatus(
            revision, when, error, prior.failures + 1 if same_scope else 1,
            unknown=bool(same_scope and prior.unknown))

    def _record_service_exit(self, observation) -> None:
        """Record only a current owned process failure, without reading auth.

        Hold the existing local configuration/account fences through the atomic
        status-only write. A stale observation is discarded; an unknown check
        or write propagates to the supervisor's static UNKNOWN cache.
        """
        from .config_leases import task_mutation_scope

        # A different profile cannot authorize touching either profile's state.
        if config_mod.profile_path() != self._service_profile:
            return
        with config_mod.pinned_profile(self._service_profile), task_mutation_scope(timeout_s=1):
            current = config_mod.load()
            if not self._service_revision_matches(current, observation.plugin_id, observation.revision):
                return
            error = f"Service worker exited with exit status {observation.returncode}; cause unavailable."
            state.record_service_failure(
                observation.plugin_id, when=observation.observed_at,
                error=error,
            )
            self._remember_service_failure(observation.plugin_id, observation.revision,
                observation.observed_at, error,
                prior=self._service_failure_status.get(observation.plugin_id))

    def _observe_service_launch_failure(self, plugin_id, exc, *, when, revision):
        """Pre-arm UNKNOWN so even an unexpected observer fault stays visible."""
        prior = self._service_failure_status.get(plugin_id)
        self._service_failure_status[plugin_id] = ServiceFailureStatus(
            revision, when, "", prior.failures if prior and prior.revision == revision else 0,
            unknown=True)
        try:
            if self._record_service_launch_failure(plugin_id, exc, when=when, revision=revision):
                self._remember_service_failure(plugin_id, revision, when,
                    "service worker launch failed (OS error); cause unavailable.", prior=prior)
            elif prior is not None:
                self._service_failure_status[plugin_id] = prior
            else:
                self._service_failure_status.pop(plugin_id, None)
        except Exception:
            self._service_masked.add(plugin_id)
            raise

    def _record_service_launch_failure(
        self, plugin_id: str, exc: OSError, *, when: datetime, revision: str,
    ) -> bool:
        """Persist only a spawn failure proved to belong to its original scope."""
        from .config_leases import task_mutation_scope

        if config_mod.profile_path() != self._service_profile:
            return False
        error = "service worker launch failed (OS error); cause unavailable."
        with config_mod.pinned_profile(self._service_profile), task_mutation_scope(timeout_s=1):
            current = config_mod.load()
            if not self._service_revision_matches(current, plugin_id, revision):
                return False
            state.record_service_failure(plugin_id, when=when, error=error)

        first_line = error.splitlines()[0]
        if len(first_line) > 200:
            first_line = first_line[:200].rstrip() + "…"
        self.activity.add(
            plugin_id=plugin_id,
            summary=first_line,
            ok=False,
            timestamp=when,
        )
        logging.getLogger("fulcra_collect").error(
            "service worker launch failed for plugin=%s: %s",
            plugin_id,
            error,
        )
        return True

    def _runtime_configuration(self):
        """Read authoritative consent each tick, including direct TOML edits.

        Unknown configuration or an in-progress account transition pauses new
        source dispatch and retires services. No raw config/parser text is
        logged because it can include user paths or credentials.
        """
        try:
            with config_mod.pinned_profile(self._service_profile):
                current = config_mod.load()
            services = set() if current.account_transition else {
                pid for pid in current.enabled if pid in self.registry.plugins
                and self.registry.plugins[pid].kind == "service"}
            revisions = {pid: self._service_revision(current, pid) for pid in services}
        except Exception as exc:  # noqa: BLE001 — fail closed at the dispatch boundary
            logging.getLogger("fulcra_collect").error(
                "configuration unavailable; source dispatch paused (%s)", type(exc).__name__)
            return False, set(), {}
        self.config = current
        return not current.account_transition, services, revisions

    def _dispatch_tick(self, supervisor, *, now):
        self._service_supervisor = supervisor
        self._service_masked.update(getattr(supervisor, "unknown_exits", {}))
        ready, service_ids, revisions = self._runtime_configuration()
        supervisor.tick(
            now=now, enabled_ids=service_ids, revisions=revisions,
            spawn=self._spawn_service,
            on_spawn_error=lambda plugin_id, exc: self._observe_service_launch_failure(
                plugin_id, exc, when=now, revision=revisions[plugin_id]),
            on_exit=self._record_service_exit,
        )
        self._service_masked.update(getattr(supervisor, "unknown_exits", {}))
        if not ready:
            return
        try:
            states = {pid: state.load(pid) for pid in self.registry.plugins}
        except Exception:  # noqa: BLE001 — DB outage must not stop service supervision
            logging.getLogger("fulcra_collect").error(
                "cached state unavailable; scheduled dispatch paused")
            return
        online = is_online()
        for pid in due_plugins(self.registry.plugins, self.config, states, now, online=online):
            self._trigger(pid)

    # ---- the run loop --------------------------------------------------

    def serve(self, *, tick_seconds: float = 30.0) -> None:
        """Run the daemon: serve the control socket, keep service plugins
        alive, and fire any scheduled plugin that is due. Blocks until the
        process is signalled.

        The tick uses a short relative sleep, so a system sleep suspends
        it and it resumes on wake — a machine asleep for hours catches up
        within one tick of waking, each overdue plugin firing once. While
        the machine is offline, network-requiring scheduled plugins are
        skipped (deferred), not run into a failure. Scheduled runs are
        dispatched on background threads so a long run never blocks the
        tick loop or the control socket."""
        # Own the captured effective profile before migration, socket unlinking,
        # credential work or any startup/dispatch effects. Retain this raw-fd
        # lease through all unwind paths until actual process exit.
        if config_mod.profile_path() != self._service_profile:
            raise DaemonLeaseUnavailable("daemon startup refused: profile lease unavailable")
        self._daemon_lease = acquire_daemon_lease(self._service_profile)

        from .supervisor import ServiceSupervisor

        # Open the unified state db before anything else touches state —
        # this runs schema migrations (including the one-shot import of
        # legacy state/<id>.json files into the plugin_state table) so
        # the very first state.load() call below sees a populated db.
        # Idempotent: a subsequent open() in this thread is a cache hit.
        try:
            _db.open()
        except Exception:
            logging.getLogger("fulcra_collect").exception(
                "state.db open/migrate failed; the daemon cannot run "
                "without a working state store",
            )
            raise

        server = ControlServer(_control_socket_path(), self.handle_request)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        # Account-switch pre-flight: invalidate cached def_ids + tag_ids
        # across all plugin state when the bearer-token's account changed
        # since the daemon last booted, so the user doesn't see orphaned
        # "Run failed: <422>" on their first dashboard open. Run on a
        # background thread AFTER the control socket is bound (above): it
        # reads the keychain, which on macOS can block on an ACL prompt,
        # and that must never delay reachability. See the note in __init__.
        def _fingerprint_preflight() -> None:
            try:
                self._check_account_fingerprint()
            except Exception:
                logging.getLogger("fulcra_collect.daemon").exception(
                    "account-fingerprint pre-flight failed"
                )
        threading.Thread(target=_fingerprint_preflight, daemon=True).start()

        # Start the HTTP server alongside the UDS control server
        try:
            from .web import serve as _web_serve
            _web_url, _web_thread = _web_serve(self)
            self._web_url = _web_url
            logging.getLogger("fulcra_collect").info("web UI: %s", _web_url)
        except Exception:
            logging.getLogger("fulcra_collect").exception(
                "web UI failed to start; the daemon will keep running without it",
            )

        # Best-effort launch of the macOS menubar app so the user gets a
        # status icon without needing a separate command. Non-fatal —
        # see menubar_launcher.try_launch_menubar docstring.
        if self._launch_menubar:
            try:
                from . import menubar_launcher
                menubar_launcher.try_launch_menubar()
            except Exception:
                # try_launch_menubar already swallows its own errors; this
                # outer guard is for the import itself in case the module
                # fails to load on a weird platform.
                logging.getLogger("fulcra_collect").exception(
                    "menubar auto-launch hook crashed (non-fatal)",
                )

        supervisor = ServiceSupervisor()
        try:
            while True:
                now = datetime.now(timezone.utc)
                self._dispatch_tick(supervisor, now=now)
                time.sleep(tick_seconds)
        finally:
            server.shutdown()
            supervisor.shutdown_all()
            # Terminate any still-running scheduled-run worker processes so
            # they do not survive as orphans after the daemon exits.
            with self._inflight_lock:
                procs = list(self._inflight_procs.values())
            for proc in procs:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    pass

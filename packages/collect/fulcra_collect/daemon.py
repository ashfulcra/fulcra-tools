"""The hub daemon: holds the registry + config, answers control-socket
requests, and runs the scheduler + supervisor loop.

The request handler and status snapshot are pure enough to unit-test;
`serve` runs the full loop: control socket, service supervision, and
scheduled dispatch.
"""
from __future__ import annotations

import importlib.metadata as _im
import logging
import subprocess
import threading
import time
from datetime import datetime, timezone

import httpx

from fulcra_common import BaseFulcraClient

from . import activity as _activity
from . import config as config_mod
from . import db as _db
from . import runner, state
from .config import Config
from .control import ControlServer
from .registry import RegistryResult, discover
from .scheduler import due_plugins


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


def _parse_iso8601(s: str) -> datetime:
    """Parse an ISO-8601 timestamp into a timezone-aware datetime.

    Accepts both the trailing 'Z' shorthand (which datetime.fromisoformat
    in 3.10 doesn't accept) and the explicit '+00:00' offset form. Raises
    ValueError on garbage so callers can return a clean error.

    Used by ``_record_annotation`` when the menubar sends start_time /
    end_time strings for Duration records.
    """
    if not isinstance(s, str) or not s:
        raise ValueError("empty timestamp")
    normalised = s.replace("Z", "+00:00") if s.endswith("Z") else s
    dt = datetime.fromisoformat(normalised)
    if dt.tzinfo is None:
        # Naive datetimes are ambiguous; refuse rather than guess UTC.
        raise ValueError(f"timestamp {s!r} has no timezone")
    return dt


class _QuickRecordClient(BaseFulcraClient):
    """BaseFulcraClient subclass for the daemon's quick-record + tombstone
    POSTs. Preserves the legacy site's 10s timeout (vs BaseFulcraClient's
    default 30s) and short-circuits the `fulcra` CLI shell-out — the
    daemon already manages the user's bearer token via the user-level
    keychain, so we override get_token() to return it directly.

    Introduced in refactor #69 so that `_record_annotation` and
    `_delete_annotation` can share a single ingest path
    (IngestPipeline.ingest_one) instead of each maintaining its own
    inline httpx.Client + wire.build_record + wire.encode_batch block.
    """
    USER_AGENT = "fulcra-collect/0.1"
    FOLLOW_REDIRECTS = True

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def get_token(self) -> str:
        return self._token

    def _client(self) -> httpx.Client:
        # Override BaseFulcraClient._client to use the 10s timeout the
        # legacy menubar POST site has always used. Slow Fulcra responses
        # would otherwise block the menubar UI for 30s.
        if self._http is None:
            self._http = httpx.Client(
                base_url=self.base_url,
                transport=self._transport,
                timeout=10.0,
                headers={"User-Agent": self.USER_AGENT},
                follow_redirects=self.FOLLOW_REDIRECTS,
            )
        return self._http


def is_online(*, timeout: float = 2.0) -> bool:
    """Best-effort connectivity probe — can a TCP connection to a
    well-known host be opened? Used to defer (not fail) network-requiring
    scheduled plugins while the machine is offline."""
    import socket
    for host, port in (("1.1.1.1", 53), ("8.8.8.8", 53)):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


class Daemon:
    def __init__(self, registry: RegistryResult | None = None,
                 config: Config | None = None) -> None:
        self.registry = registry if registry is not None else discover()
        self.config = config if config is not None else config_mod.load()
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

    def _check_account_fingerprint(self) -> None:
        """Detect a Fulcra-account switch since the previous boot and
        invalidate per-plugin caches that hold per-account UUIDs.

        Fingerprint is a SHA-256 prefix of the bearer-token. False
        positives on token rotation (same account → new JWT after refresh)
        are acceptable: the recovery is just a cache rebuild on the
        first run of each plugin, no data loss.
        """
        import hashlib
        from . import credentials as _creds
        token = _creds.get_user_secret("bearer-token") or ""
        if not token:
            # Not signed in yet — nothing to invalidate, nothing to
            # remember. The fingerprint file gets written the first time
            # we boot WITH a token.
            return
        fingerprint = hashlib.sha256(token.encode()).hexdigest()[:16]
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
        if cmd == "set_credential":
            return self._set_credential(
                request.get("plugin", ""), request.get("key", ""),
                request.get("secret", ""),
            )
        if cmd == "delete_credential":
            return self._delete_credential(
                request.get("plugin", ""), request.get("key", ""),
            )
        if cmd == "quick_record_list":
            return self._quick_record_list()
        if cmd == "record_annotation":
            return self._record_annotation(
                request.get("definition_id", ""),
                request.get("comment", None),
                start_time=request.get("start_time"),
                end_time=request.get("end_time"),
            )
        if cmd == "delete_annotation":
            return self._delete_annotation(request.get("source_id", ""))
        if cmd == "get_quick_record_favorites":
            return self._get_quick_record_favorites()
        if cmd == "set_quick_record_favorites":
            return self._set_quick_record_favorites(
                request.get("favorites", []),
            )
        if cmd == "delete_definition":
            return self._delete_definition(request.get("def_id", ""))
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    def _status(self) -> dict:
        plugins = []
        for pid, plugin in sorted(self.registry.plugins.items()):
            st = state.load(pid)
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
        try:
            out: dict[str, str] = {}
            for cred in plugin.required_credentials:
                # Probe the SAME keychain scope the plugin actually reads:
                # user-level ("fulcra-collect:user") for user_level creds,
                # plugin-level otherwise. Reading the wrong scope was the
                # historical attention-relay false-"missing" bug (a
                # user-store secret probed plugin-level reported "missing").
                present = (
                    credentials.has_user_secret(cred.key)
                    if getattr(cred, "user_level", False)
                    else credentials.has_secret(plugin_id, cred.key)
                )
                out[cred.key] = "set" if present else "missing"
            return {"ok": True, "credentials": out}
        except Exception:
            import logging
            logging.getLogger("fulcra_collect.daemon").exception(
                "credential_status failed for %s", plugin_id,
            )
            return {"ok": False, "error": "keychain read failed"}

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

    def _quick_record_list(self) -> dict:
        """List non-deleted annotation definitions for the menubar
        popover's quick-record surface. Calls Fulcra's annotation-defs
        endpoint; excludes soft-deleted; sorts by (pinned-first,
        annotation_type, created_at desc). Caches for 60s in-memory.

        Sprint B (2026-05-26) widened this from Moment-only to all
        annotation types so users can record Durations / Watched /
        Listened / Read events from the menubar too.

        Task #64 (2026-05-26) added a ``pinned`` field per def and made
        favorites sort to the top of each annotation_type group. When
        the user has any favorites set, the legacy 40-entry cap is
        relaxed to ``all pinned + up to 20 unpinned`` so the popover's
        "show all" disclosure always has at least a useful unpinned
        slice to display. When NO favorites are set (first-launch
        state), the previous 40-cap behavior is preserved so existing
        users see no regression.

        The shape stays a flat array — the menubar groups client-side
        by ``annotation_type`` for the section headers.
        """
        from . import credentials as _creds
        from . import quick_record_favorites as _favs
        cache_ttl = 60.0
        now = time.monotonic()
        cached = getattr(self, "_quick_record_cache", None)
        if cached and (now - cached["at"]) < cache_ttl:
            return {"ok": True, "definitions": cached["defs"]}
        token = _creds.get_user_secret("bearer-token")
        if not token:
            return {"ok": False, "error": "Fulcra not authenticated", "definitions": []}
        # Use the SP5 refresh-aware wrapper, exactly like _delete_definition.
        # This was the ONE Fulcra-touching path still on a raw httpx client
        # with no refresh-on-401 — and it's typically the FIRST thing to hit
        # an expired token (the menubar polls status, then the user opens
        # "what to log", before any refresh-capable path has run). So when
        # the access token had expired it 401'd and surfaced "Fulcra didn't
        # respond" instead of minting a fresh token via the `fulcra` CLI and
        # retrying — even though /api/definitions etc. recovered fine. Late
        # import so tests that monkeypatch ``fulcra_collect.web.httpx`` still
        # intercept the inner client (same reason as _delete_definition).
        from . import web as _web
        try:
            with _web._RetryingClient(
                token, user_agent="fulcra-collect/daemon",
            ) as client:
                r = client.get("/user/v1alpha1/annotation")
                r.raise_for_status()
                all_defs = r.json()
        except Exception:
            logging.getLogger("fulcra_collect.daemon").exception(
                "_quick_record_list: Fulcra API request failed"
            )
            return {
                "ok": False,
                "error": "Fulcra didn't respond. Check your internet, then try again.",
                "definitions": [],
            }
        # Drop soft-deleted defs but keep ALL annotation types.
        live = [d for d in all_defs if not d.get("deleted_at")]
        # Group ordering: moments first, then durations, then anything else
        # alphabetically; within each group, most-recently-created first.
        # This mirrors how the popover lays out section headers so the
        # daemon's order is already correct without re-sorting client-side.
        _GROUP_ORDER = {"moment": 0, "duration": 1}
        live.sort(key=lambda d: (
            _GROUP_ORDER.get(d.get("annotation_type", ""), 2),
            d.get("annotation_type", ""),
            # Negate via reverse-string trick on created_at: sort desc by
            # using a tuple where the second element is the negated
            # lexicographic order — but Python doesn't negate strings, so
            # we use the trick of sorting by created_at desc separately
            # via a stable two-pass.
        ))
        # Stable sort preserves the group ordering above; then within
        # each group sort pinned-first, then by created_at desc. Apply
        # group-by, sort, flatten. Annotate each def with a ``pinned``
        # boolean so the menubar can render the star state without re-
        # consulting the favorites file.
        favorites = _favs.load()
        from itertools import groupby
        flattened: list[dict] = []
        for _, group in groupby(live, key=lambda d: (
            _GROUP_ORDER.get(d.get("annotation_type", ""), 2),
            d.get("annotation_type", ""),
        )):
            group_list = list(group)
            for entry in group_list:
                entry["pinned"] = entry.get("id") in favorites
            # Two-key sort: pinned (True first) then created_at desc.
            # Python sorts True > False so we negate via ``not pinned``.
            group_list.sort(
                key=lambda d: (not d.get("pinned", False),
                               # reverse via prepending NUL bytes is uglier
                               # than a separate reverse sort — fall back
                               # to a stable two-pass:
                               ""),
            )
            # Within each (pinned-bucket), sort by created_at desc. We
            # do this by splitting the group then re-concatenating, so
            # pinned defs are ordered most-recent-first AMONG pinned
            # and unpinned defs are ordered most-recent-first AMONG
            # unpinned — exactly what the spec asks for.
            pinned_part = [d for d in group_list if d.get("pinned")]
            unpinned_part = [d for d in group_list if not d.get("pinned")]
            pinned_part.sort(key=lambda d: d.get("created_at", ""),
                              reverse=True)
            unpinned_part.sort(key=lambda d: d.get("created_at", ""),
                                reverse=True)
            flattened.extend(pinned_part + unpinned_part)
        # Cap behavior:
        #   - No favorites set → 40-entry cap (legacy first-launch shape).
        #   - Favorites set → keep ALL pinned + at most 20 unpinned so
        #     the "show all" disclosure has something meaningful to
        #     surface but the popover doesn't grow unbounded on accounts
        #     with hundreds of defs. Preserve the existing flattened
        #     order (which already has pinned-first WITHIN each group)
        #     by walking the list once and dropping unpinned entries
        #     past index 20.
        if favorites:
            kept: list[dict] = []
            unpinned_kept = 0
            for d in flattened:
                if d.get("pinned"):
                    kept.append(d)
                elif unpinned_kept < 20:
                    kept.append(d)
                    unpinned_kept += 1
            flattened = kept
        else:
            flattened = flattened[:40]
        self._quick_record_cache = {"at": now, "defs": flattened}
        return {"ok": True, "definitions": flattened}

    # ---- favorites dispatch -------------------------------------------

    def _get_quick_record_favorites(self) -> dict:
        """Return the user's saved favorite def_ids. Always succeeds —
        absence is just an empty list."""
        from . import quick_record_favorites as _favs
        return {"ok": True, "favorites": sorted(_favs.load())}

    def _set_quick_record_favorites(self, favorites) -> dict:
        """Replace the favorites list and invalidate the quick-record
        cache so the very next list call surfaces the new ordering.

        Defensive on the input shape — anything non-stringy in the
        list is dropped silently so a malformed client doesn't poison
        the file.
        """
        from . import quick_record_favorites as _favs
        if not isinstance(favorites, list):
            return {"ok": False, "error": "favorites must be a list of def ids"}
        cleaned = {x for x in favorites if isinstance(x, str) and x}
        try:
            _favs.save(cleaned)
        except OSError as exc:
            logging.getLogger("fulcra_collect.daemon").exception(
                "set_quick_record_favorites: save failed",
            )
            return {"ok": False, "error": f"could not persist favorites: {exc}"}
        # Bust the cached list so the next _quick_record_list re-sorts
        # against the freshly-written favorites. Without this, the
        # menubar would keep showing the old order for up to 60s.
        self._quick_record_cache = None
        return {"ok": True}

    def _record_annotation(self, definition_id: str, comment: str | None,
                           start_time: str | None = None,
                           end_time: str | None = None) -> dict:
        """Write one annotation immediately to Fulcra. Used by the
        menubar's quick-record buttons and the web UI's /api/annotations
        endpoint.

        Modes:

        - Moment (default): pass neither ``start_time`` nor ``end_time``;
          the daemon writes a MomentAnnotation at now.

        - Duration: pass BOTH ``start_time`` and ``end_time`` as
          ISO-8601 UTC strings; the daemon writes a DurationAnnotation
          with ``recorded_at = {start_time, end_time}`` and a
          ``duration_seconds`` field in the data payload. Sprint B
          (2026-05-26) added this so the menubar can record finished
          movies / listening sessions / reading sessions inline.

        Uses the same /ingest/v1/record/batch + CloudEvents wire format
        as every plugin importer (see fulcra_common.wire). Per-call
        source_id (uuid) — duplicate clicks intentionally produce
        duplicate events; that's the menubar button's whole job.
        """
        import uuid
        from fulcra_common.ingest import (
            DurationEvent, IngestableEvent, IngestPipeline, MomentEvent,
        )
        from . import credentials as _creds
        if not definition_id:
            return {"ok": False, "error": "definition_id required"}
        # Partial duration spec is a caller bug — surface clearly rather
        # than silently fall back to Moment.
        if (start_time is None) != (end_time is None):
            return {
                "ok": False,
                "error": "start_time and end_time must both be set or both be omitted",
            }
        # Validate the duration range upfront — surface clear errors
        # before we waste a network round-trip on the def lookup.
        parsed_start: datetime | None = None
        parsed_end: datetime | None = None
        if start_time is not None and end_time is not None:
            try:
                parsed_start = _parse_iso8601(start_time)
                parsed_end = _parse_iso8601(end_time)
            except ValueError as exc:
                return {"ok": False, "error": f"invalid timestamp: {exc}"}
            if parsed_end <= parsed_start:
                return {"ok": False, "error": "end_time must be after start_time"}
        token = _creds.get_user_secret("bearer-token")
        if not token:
            return {"ok": False, "error": "Fulcra not authenticated"}

        # We need the def's tags to attach to the event so Fulcra associates
        # the moment with the same tag membership the def declares. Cheapest
        # path: the _quick_record_list cache already has the full def dict.
        # If the cache is cold or stale (the user posts before opening the
        # popover), warm it on demand — same source of truth, no second URL.
        cached = getattr(self, "_quick_record_cache", None)
        def_dict: dict | None = None
        if cached:
            def_dict = next((d for d in cached["defs"]
                             if d.get("id") == definition_id), None)
        if def_dict is None:
            warm = self._quick_record_list()
            if not warm.get("ok"):
                return {"ok": False,
                        "error": warm.get("error",
                                          "Could not look up the annotation "
                                          "definition.")}
            def_dict = next((d for d in warm.get("definitions", [])
                             if d.get("id") == definition_id), None)
        if def_dict is None:
            return {"ok": False,
                    "error": f"unknown definition id {definition_id!r}"}

        now = datetime.now(timezone.utc)
        source_id = (
            f"com.fulcradynamics.fulcra-collect.quick-record.{uuid.uuid4()}"
        )
        tags = tuple(def_dict.get("tags") or [])
        if parsed_start is not None and parsed_end is not None:
            # Duration record. ISO strings were already validated above.
            # Refactor #69 normalization: the legacy site emitted
            # duration_seconds as a FLOAT (.total_seconds() returns
            # float). The pipeline emits it as an int. Float→int on a
            # whole-second duration is observably identical to every
            # Fulcra consumer, but the bytes differ — called out in the
            # refactor-#69 commits.
            event: IngestableEvent = DurationEvent(
                definition_id=definition_id,
                source_id=source_id,
                tags=tags,
                comment=comment or "",
                start=parsed_start,
                end=parsed_end,
            )
        else:
            event = MomentEvent(
                definition_id=definition_id,
                source_id=source_id,
                tags=tags,
                comment=comment or "",
                ts=now,
            )
        try:
            IngestPipeline(
                client=_QuickRecordClient(token=token),
            ).ingest_one(event)
        except Exception as exc:
            logging.getLogger("fulcra_collect.daemon").exception(
                "_record_annotation(%s): Fulcra API request failed",
                definition_id,
            )
            # Record the failed attempt in the activity buffer so the user
            # sees something happened
            self.activity.add(plugin_id="quick-record",
                              summary=f"failed: {exc}", ok=False)
            return {
                "ok": False,
                "error": "Fulcra didn't accept that request. Check your internet, then try again.",
            }
        # Surface in the activity buffer with the def name (the id prefix
        # was opaque to the user).
        name = def_dict.get("name") or definition_id[:8] + "…"
        self.activity.add(plugin_id="quick-record",
                          summary=f"Recorded \"{name}\"",
                          ok=True)
        # Return the source_id so the caller (menubar) can stash it in
        # the "Recently recorded" list and reference it later if the
        # user clicks Undo. We don't return a fabricated event_id because
        # Fulcra's ingest endpoint doesn't echo one — source_id is the
        # only handle the user side has for this event.
        return {"ok": True, "source_id": source_id, "name": name}

    def _delete_annotation(self, source_id: str) -> dict:
        """Write a "deleted" sentinel annotation referencing ``source_id``.

        IMPORTANT: this is a SOFT marker, not a hard delete. Fulcra's
        backend offers no per-event delete primitive (verified
        2026-05-26 via packages/media-helpers/scripts/probe_soft_delete_3.py
        — the matrix shows 405 / 404 across every {GET, POST, PUT,
        PATCH, DELETE} attempt on /data/v1alpha1/event/...). The
        original record stays in the user's timeline indefinitely.

        What we DO write is a separate annotation tagged with the
        original source_id in its data payload — a "tombstone" that the
        Fulcra UI may or may not surface as a strikethrough on the
        original. The menubar uses this purely as a paper trail so the
        user can see (in the activity buffer) that an undo happened. The
        menubar ALSO greys out the row in its in-memory "Recently
        recorded" list so the user doesn't keep clicking Undo.

        If the user is paying attention to their Fulcra timeline, they
        will still see the original event. The menubar's UI calls this
        out in the Undo button's tooltip.
        """
        import uuid
        from fulcra_common.ingest import IngestPipeline, MomentEvent
        from . import credentials as _creds
        if not source_id:
            return {"ok": False, "error": "source_id required"}
        token = _creds.get_user_secret("bearer-token")
        if not token:
            return {"ok": False, "error": "Fulcra not authenticated"}

        now = datetime.now(timezone.utc)
        tombstone_source_id = (
            f"com.fulcradynamics.fulcra-collect.quick-record.undo."
            f"{uuid.uuid4()}"
        )
        # Tombstone has no annotation definition attached — Fulcra
        # accepts the event and the tombstone is identifiable purely via
        # its source_id prefix + the supersedes_source_id pointer.
        tombstone = MomentEvent(
            definition_id=None,
            source_id=tombstone_source_id,
            tags=(),
            comment="[deleted via Fulcra Collect menubar undo]",
            superseded_by="deleted",
            supersedes_source_id=source_id,
            ts=now,
        )
        try:
            IngestPipeline(
                client=_QuickRecordClient(token=token),
            ).ingest_one(tombstone)
        except Exception as exc:
            logging.getLogger("fulcra_collect.daemon").exception(
                "_delete_annotation(%s): Fulcra API request failed",
                source_id,
            )
            self.activity.add(plugin_id="quick-record",
                              summary=f"undo failed: {exc}", ok=False)
            return {
                "ok": False,
                "error": "Fulcra didn't accept the undo request. Check your internet, then try again.",
            }
        self.activity.add(
            plugin_id="quick-record",
            summary=f"Undid recording (tombstone for {source_id[:12]}…)",
            ok=True,
        )
        return {"ok": True, "tombstone_source_id": tombstone_source_id}

    def _delete_definition(self, def_id: str) -> dict:
        """Soft-delete an annotation definition via Fulcra, then clean up
        locally — clear any plugin state bound to it, prune from favorites,
        bust the quick-record cache.

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
        - Also prune the def from quick-record favorites if it was pinned,
          and bust the in-memory quick-record cache so the next
          ``_quick_record_list`` call doesn't briefly resurrect it.
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
        # Also drop this def from quick-record favorites if it was pinned.
        # Without this the favorites file would accumulate orphan UUIDs
        # the menubar would keep trying to surface but Fulcra would no
        # longer return. Best-effort: a favorites I/O failure shouldn't
        # roll back the (successful) Fulcra-side delete.
        try:
            from . import quick_record_favorites as _favs
            current = _favs.load()
            if def_id in current:
                current.discard(def_id)
                _favs.save(current)
                # Bust the daemon's quick-record cache so the next list
                # call doesn't briefly resurrect the deleted def with a
                # stale ``pinned`` flag.
                self._quick_record_cache = None
                _log.info(
                    "delete_definition(%s): removed from quick-record favorites",
                    def_id,
                )
        except Exception:
            _log.exception(
                "delete_definition(%s): could not prune favorites; non-fatal",
                def_id,
            )
        return {"ok": True, "cleared_plugins": cleared}

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

    def _spawn_service(self, plugin_id: str):
        """Spawn a service plugin's worker subprocess (kept alive by the
        ServiceSupervisor)."""
        return subprocess.Popen(runner.worker_command(plugin_id))

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
                service_ids = {
                    pid for pid in self.config.enabled
                    if pid in self.registry.plugins
                    and self.registry.plugins[pid].kind == "service"
                }
                supervisor.tick(now=now, enabled_ids=service_ids,
                                spawn=self._spawn_service)
                states = {pid: state.load(pid) for pid in self.registry.plugins}
                online = is_online()
                for pid in due_plugins(self.registry.plugins, self.config,
                                       states, now, online=online):
                    self._trigger(pid)
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

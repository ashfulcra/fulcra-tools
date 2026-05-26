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

from . import activity as _activity
from . import config as config_mod
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
        # Throttle state for /api/extension/attention activity-feed entries.
        # The extension can POST several events a minute during active
        # browsing — pushing one feed entry per POST would blow through the
        # 50-entry ring in minutes. We coalesce: at most one entry per
        # _attention_activity_interval_s, summarising N events since last
        # push. See note_attention_event for the producer hook and
        # web.py extension_attention for the caller.
        # -inf so the very first POST after a daemon restart fires
        # immediately — the user opening the dashboard wants to see
        # "yes, the extension is alive" without waiting 60s. Subsequent
        # POSTs within the window get coalesced.
        self._attention_activity_last_at: float = float("-inf")
        self._attention_activity_count: int = 0
        self._attention_activity_clients: set[str] = set()
        self._attention_activity_interval_s: float = 60.0
        # Indirection so tests can inject a fake clock without monkeypatching
        # time.monotonic globally (which breaks pytest's own timing). The
        # production callable is set in __init__ and replaced in tests.
        import time as _time_mod
        self._monotonic = _time_mod.monotonic
        # TTL cache for attention-def validation. The extension POST route
        # re-checks every _attention_validation_interval_s that the cached
        # attention_definition_id still exists on the current Fulcra
        # account — protects against the daemon being re-authed to a
        # different account leaving stale def IDs in attention/state.json
        # that ingest happily references but the timeline can't render.
        self._attention_def_validated_at: float = float("-inf")
        self._attention_def_validated_id: str | None = None
        self._attention_validation_interval_s: float = 300.0

        # Account-switch pre-flight: invalidate cached def_ids + tag_ids
        # across all plugin state when the bearer-token's account has
        # changed since the daemon last booted. Lazy per-call recovery
        # already exists for the attention route and per-plugin runs,
        # but doing it eagerly at startup saves the user from seeing
        # "Run failed: <orphan 422>" on their first dashboard open.
        try:
            self._check_account_fingerprint()
        except Exception:
            # Pre-flight failures must NOT block daemon startup. The
            # lazy recovery paths still work as a fallback.
            logging.getLogger("fulcra_collect.daemon").exception(
                "account-fingerprint pre-flight failed"
            )

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
        # Per-plugin state files at state/<id>.json — clear definition_id.
        state_dir = config_mod.config_dir() / "state"
        if state_dir.exists():
            for path in state_dir.glob("*.json"):
                plugin_id = path.stem
                try:
                    st = state.load(plugin_id)
                    if getattr(st, "definition_id", None) is not None:
                        st.definition_id = None
                        state.save(st)
                        cleared.append(f"state/{plugin_id}.json")
                except Exception:
                    logger.exception("invalidate: failed for %s", plugin_id)
        # Per-package state files (media, attention). Deferred import
        # tolerates either being absent.
        for label, module_name, attrs in (
            ("media", "fulcra_media.state",
             ("listened_definition_id", "watched_definition_id",
              "read_definition_id", "activity_definition_id", "tag_ids")),
            ("attention", "fulcra_attention.state",
             ("attention_definition_id", "tag_ids")),
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
        # Reset the in-process attention-def-validation cache too, so the
        # extension route re-validates on the next POST instead of trusting
        # whatever was cached pre-invalidation.
        self._attention_def_validated_id = None
        self._attention_def_validated_at = float("-inf")
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

    def note_attention_event(self, *, client: str | None) -> None:
        """Record one attention POST for the dashboard activity feed.

        Coalesces: per-POST entries would saturate the 50-entry ring in
        minutes during active browsing, so we accumulate until the throttle
        window elapses, then emit one summary entry covering the burst.

        Idempotent w.r.t. failures: this is best-effort UI plumbing, never
        the cause of a failed ingest. The caller (web.extension_attention)
        invokes this only after a successful Fulcra POST.
        """
        self._attention_activity_count += 1
        if client:
            self._attention_activity_clients.add(client)
        now_mono = self._monotonic()
        if (now_mono - self._attention_activity_last_at
                < self._attention_activity_interval_s):
            return
        n = self._attention_activity_count
        clients = ", ".join(sorted(self._attention_activity_clients))
        summary = (
            f"Attention: {n} event"
            f"{'s' if n != 1 else ''} from {clients or 'extension'}"
        )
        self.activity.add(
            plugin_id="attention-relay",
            summary=summary,
            ok=True,
        )
        self._attention_activity_last_at = now_mono
        self._attention_activity_count = 0
        self._attention_activity_clients.clear()

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
            )
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    def _status(self) -> dict:
        plugins = []
        for pid, plugin in sorted(self.registry.plugins.items()):
            st = state.load(pid)
            plugins.append({
                "id": pid,
                "name": plugin.name,
                "kind": plugin.kind,
                "description": plugin.description,
                "category": plugin.category,
                "enabled": pid in self.config.enabled,
                "last_run": st.last_run.isoformat() if st.last_run else None,
                "last_outcome": st.last_outcome,
                "last_error": st.last_error,
                "consecutive_failures": st.consecutive_failures,
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
        version falls back to 'unknown'."""
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
        return {"daemon_version": daemon_version, "plugins": plugins}

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
                out[cred.key] = "set" if credentials.has_secret(plugin_id, cred.key) else "missing"
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

    def _set_credential(self, plugin_id: str, key: str, secret: str) -> dict:
        err = self._check_credential_key(plugin_id, key)
        if err is not None:
            return err
        from . import credentials  # deferred so daemon stays importable without
                                   # a live keychain; tests monkeypatch on this module
        try:
            credentials.set_secret(plugin_id, key, secret)
            return {"ok": True}
        except Exception:
            import logging
            logging.getLogger("fulcra_collect.daemon").exception(
                "set_credential failed for %s/%s", plugin_id, key,
            )
            return {"ok": False, "error": "keychain write failed"}

    def _delete_credential(self, plugin_id: str, key: str) -> dict:
        err = self._check_credential_key(plugin_id, key)
        if err is not None:
            return err
        from . import credentials  # deferred so daemon stays importable without
                                   # a live keychain; tests monkeypatch on this module
        try:
            credentials.delete_secret(plugin_id, key)
            return {"ok": True}
        except Exception:
            import logging
            logging.getLogger("fulcra_collect.daemon").exception(
                "delete_credential failed for %s/%s", plugin_id, key,
            )
            return {"ok": False, "error": "keychain delete failed"}

    def _quick_record_list(self) -> dict:
        """List Moment annotation definitions for the menubar popover's
        quick-record surface. Calls Fulcra's annotation-defs endpoint;
        filters to annotation_type == 'moment'; sorts by most-recent-use.
        Caches for 60s in-memory."""
        from . import credentials as _creds
        cache_ttl = 60.0
        now = time.monotonic()
        cached = getattr(self, "_quick_record_cache", None)
        if cached and (now - cached["at"]) < cache_ttl:
            return {"ok": True, "definitions": cached["defs"]}
        token = _creds.get_user_secret("bearer-token")
        if not token:
            return {"ok": False, "error": "Fulcra not authenticated", "definitions": []}
        try:
            with httpx.Client(timeout=10.0) as client:
                r = client.get(
                    "https://api.fulcradynamics.com/user/v1alpha1/annotation",
                    headers={"Authorization": f"Bearer {token}"},
                )
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
        # Filter to moments, exclude soft-deleted
        moments = [d for d in all_defs
                   if d.get("annotation_type") == "moment"
                   and not d.get("deleted_at")]
        # Sort by created_at descending as a v1 proxy for "recently used"
        # (proper sort by recent annotation event timestamp is v1.5)
        moments.sort(key=lambda d: d.get("created_at", ""), reverse=True)
        # Limit to top 20 so popover doesn't get long
        moments = moments[:20]
        self._quick_record_cache = {"at": now, "defs": moments}
        return {"ok": True, "definitions": moments}

    def _record_annotation(self, definition_id: str, comment: str | None) -> dict:
        """Write one Moment annotation immediately to Fulcra. Used by the
        menubar's quick-record buttons and the web UI's /api/annotations
        endpoint. Records timestamp=now.

        Uses the same /ingest/v1/record/batch + CloudEvents wire format as
        every plugin importer (see fulcra_common.wire). Until 2026-05-25
        this method POSTed to a dead /data/v0/annotations URL with the
        wrong payload shape and got a silent 404 every time."""
        import uuid
        from fulcra_common import wire
        from . import credentials as _creds
        if not definition_id:
            return {"ok": False, "error": "definition_id required"}
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
        record = wire.build_record(
            data_type=wire.MOMENT_ANNOTATION,
            start_time=now,
            data={"comment": comment or ""},
            # Per-call UUID — duplicate clicks should produce duplicate
            # moments rather than dedup, since "I want to record this NOW"
            # is the menubar button's whole job.
            source_id=(f"com.fulcradynamics.fulcra-collect.quick-record."
                       f"{uuid.uuid4()}"),
            tags=def_dict.get("tags") or [],
            definition_id=definition_id,
        )
        body = wire.encode_batch([record])
        try:
            with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                r = client.post(
                    "https://api.fulcradynamics.com/ingest/v1/record/batch",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "content-type": "application/x-jsonl",
                    },
                    content=body,
                )
                r.raise_for_status()
        except Exception as exc:
            logging.getLogger("fulcra_collect.daemon").exception(
                "_record_annotation(%s): Fulcra API request failed", definition_id
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
        return {"ok": True}

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

        server = ControlServer(_control_socket_path(), self.handle_request)
        threading.Thread(target=server.serve_forever, daemon=True).start()

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

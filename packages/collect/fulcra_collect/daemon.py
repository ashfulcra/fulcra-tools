"""The hub daemon: holds the registry + config, answers control-socket
requests, and runs the scheduler + supervisor loop.

The request handler and status snapshot are pure enough to unit-test;
`serve` runs the full loop: control socket, service supervision, and
scheduled dispatch.
"""
from __future__ import annotations

import importlib.metadata as _im
import subprocess
import threading
import time
from datetime import datetime, timezone

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
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    def _status(self) -> dict:
        plugins = []
        for pid, plugin in sorted(self.registry.plugins.items()):
            st = state.load(pid)
            plugins.append({
                "id": pid,
                "name": plugin.name,
                "kind": plugin.kind,
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

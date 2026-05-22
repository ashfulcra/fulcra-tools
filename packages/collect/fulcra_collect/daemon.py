"""The hub daemon: holds the registry + config, answers control-socket
requests, and runs the scheduler + supervisor loop.

The request handler and status snapshot are pure enough to unit-test;
`serve` runs the full loop: control socket, service supervision, and
scheduled dispatch.
"""
from __future__ import annotations

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
            })
        return {"ok": True, "plugins": plugins,
                "load_errors": dict(self.registry.errors)}

    def _run(self, plugin_id: str) -> dict:
        if plugin_id not in self.registry.plugins:
            return {"ok": False, "error": f"unknown plugin {plugin_id!r}"}
        self._trigger(plugin_id)
        return {"ok": True}

    def _trigger(self, plugin_id: str) -> None:
        """Fire one run of a plugin in a background thread — non-blocking,
        so a long run never stalls the tick loop or the control socket.
        Overridden in tests."""
        import threading
        threading.Thread(
            target=runner.run,
            args=(plugin_id, runner.worker_command(plugin_id)),
            kwargs={"now": datetime.now(timezone.utc)},
            daemon=True,
        ).start()

    def _spawn_service(self, plugin_id: str):
        """Spawn a service plugin's worker subprocess (kept alive by the
        ServiceSupervisor)."""
        import subprocess
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
        import threading

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

"""Host wake-exec: let the listener WAKE an agent runtime, not just notify.

WHY this exists (operator directive, 2026-06-10): "that needs to be part of the
product. this can't die if i do other stuff for a bit. the whole point was to
enable multiple simultaneous workflows better." The durable host listener
(launchd/cron ``notify-inbox``) notices actionable bus work while an agent is
idle, but until now its only outputs were a surface file and a notification —
delivery that depends on the OPERATOR being present to open the next session.
In-session watchers don't close the gap either: they die with the session.
So when the operator stepped away, directives and review verdicts sat
unprocessed for hours. This module is the missing actuator: a notify-inbox
tick with pending work can now spawn a configured command (typically "start a
headless agent session and process the inbox") with nobody at the keyboard.

PLATFORM-NEUTRAL BY CONSTRUCTION (the generalization rule, pinned by a grep
test): the mechanism does not know what it spawns. There are no agent-runtime
command strings in core — the command is per-adopter policy in
``${XDG_CONFIG_HOME:-~/.config}/fulcra-coord/wake.json`` (the same optional
fail-safe config-file pattern as review-routing.json), keyed by agent id or
prefix::

    {
      "<agent-id-or-prefix>": {
        "cmd": ["...argv..."],        # REQUIRED: what to spawn (list of strings)
        "min_interval_min": 15,        # throttle: at most one wake per interval
        "max_runtime_s": 900,          # ADVISORY (see below); not enforced here
        "enabled": true
      }
    }

Longest-prefix match on the agent id picks the entry (a short key like
``"my-agent:"`` covers every host/repo instance; a longer key overrides for one
host). The repo ships ``wake.example.json`` with per-platform EXAMPLE entries.

SAFETY RAILS — the spawn is powerful (it runs an arbitrary command with the
host's default permissions), so it is hedged on every side:

  * **Fail-safe**: ``maybe_wake`` never raises into a polling tick (full
    try/except, mirroring the notification tiers). Any config/spawn problem
    degrades to exactly the pre-feature notify-only behavior.
  * **Throttle**: a per-agent mtime marker in the local cache dir (the same
    pattern as the notified-state files) allows at most one spawn per
    ``min_interval_min``. The marker is written only AFTER a successful spawn,
    so a failed spawn does not arm the throttle and the next tick retries.
  * **Single-flight**: a per-agent pidfile records the last spawned pid; if
    that process is still alive, the tick skips (one wake at a time). A stale
    pidfile (dead pid / garbage) is ignored.
  * **Detached**: ``start_new_session=True`` — the wake outlives the listener
    tick and can never block it. stdout/stderr append to
    ``<logs-dir>/wake-<agent-slug>.log`` (the listener's logs dir convention)
    so a misbehaving wake leaves a breadcrumb.
  * **Runaway protection is the SPAWNED side's job**: ``max_runtime_s`` is
    carried in the config as documented operator intent, but this module
    deliberately does NOT babysit the child (no process manager, no watchdog —
    that's a whole new failure surface). Cap runtime with the spawned
    command's own timeout flags; the single-flight pidfile prevents pile-up
    meanwhile.

The spawned command receives context via env (kept deliberately simple):
``FULCRA_COORD_AGENT`` (whose inbox fired) and ``FULCRA_COORD_WAKE_PENDING``
(the pending count). Everything else it should read from the bus itself.

stdlib-only leaf over cache/scheduler_env/views — never imports cli/inbox.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
# Bound into THIS namespace (not used via the subprocess module object) so tests
# can patch fulcra_coord.wake.Popen without mocking Popen for every other module
# that shares the global subprocess import.
from subprocess import DEVNULL, Popen
from typing import Optional

from . import cache, scheduler_env
from .views import agent_slug  # one source of truth for the agent->slug mapping

# Default throttle when an entry omits min_interval_min: at most one wake per
# 15 minutes. Conservative on purpose — a wake spawns a whole agent runtime.
MIN_INTERVAL_MIN_DEFAULT = 15


def _wake_config_path() -> Path:
    """``${XDG_CONFIG_HOME:-~/.config}/fulcra-coord/wake.json`` — per-adopter
    wake policy. The adapter installers' ``--with-wake`` flags write entries
    here through this same helper, so there is exactly one config location."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "fulcra-coord" / "wake.json"


def _load_wake_config() -> dict:
    """Load the optional wake policy. Best-effort: ANY error -> {} (no wake),
    mirroring the review-routing loader — a corrupt config must degrade to
    the pre-feature notify-only behavior, never break a polling tick."""
    try:
        path = _wake_config_path()
        if not path.is_file():
            return {}
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _wake_entry_for(agent: str, cfg: dict) -> Optional[dict]:
    """The config entry whose key is the LONGEST prefix of ``agent``, or None.

    Longest-prefix (not first-match) so a fleet-wide short key ("my-agent:")
    and a host-specific longer key can coexist with the specific one winning —
    the same prefix-matching philosophy as directive addressing. Non-dict
    entries are ignored (malformed config reads as "not configured")."""
    best: Optional[dict] = None
    best_len = -1
    for key, entry in cfg.items():
        if not isinstance(key, str) or not key:
            continue
        if not isinstance(entry, dict):
            continue
        if agent.startswith(key) and len(key) > best_len:
            best, best_len = entry, len(key)
    return best


def _wake_marker_path(agent: str) -> Path:
    """Per-agent throttle marker (mtime = last successful spawn). Lives in the
    local cache dir beside the notified-state files, slugged the same way."""
    return cache.cache_root() / f"wake-last-{agent_slug(agent)}"


def _wake_pidfile_path(agent: str) -> Path:
    """Per-agent single-flight pidfile: the pid of the last spawned wake."""
    return cache.cache_root() / f"wake-pid-{agent_slug(agent)}"


def _wake_log_path(agent: str) -> Path:
    """Append-target for the spawned command's stdout/stderr, under the SAME
    logs dir the listener jobs use — one place to look when a wake misbehaves."""
    return scheduler_env.default_logs_dir() / f"wake-{agent_slug(agent)}.log"


def _pid_alive(pid: int) -> bool:
    """Is ``pid`` a live process? signal-0 probe: EPERM means "alive but not
    ours" (still counts as running for single-flight); any lookup failure means
    the pidfile is stale and the wake slot is free."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _min_interval_min(entry: dict) -> float:
    """The entry's throttle interval, defaulting (and falling back on garbage)
    to MIN_INTERVAL_MIN_DEFAULT — a malformed value must not disable the
    throttle or crash the tick."""
    raw = entry.get("min_interval_min", MIN_INTERVAL_MIN_DEFAULT)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return float(MIN_INTERVAL_MIN_DEFAULT)
    return val if val >= 0 else float(MIN_INTERVAL_MIN_DEFAULT)


def _valid_cmd(entry: dict) -> Optional[list[str]]:
    """The entry's argv iff well-formed (non-empty list of non-empty strings);
    None otherwise. Validated up front so a malformed cmd reads as "not
    configured" instead of an exec error at spawn time."""
    cmd = entry.get("cmd")
    if (isinstance(cmd, list) and cmd
            and all(isinstance(t, str) and t for t in cmd)):
        return list(cmd)
    return None


def maybe_wake(agent: str, pending: int) -> bool:
    """Spawn the configured wake command for ``agent`` if pending work warrants
    it. Returns True iff a process was spawned. NEVER raises (fail-safe: this
    runs inside every notify-inbox tick).

    The gate, in order (each False -> exactly today's notify-only behavior):
    pending > 0, a config entry prefix-matches the agent, the entry is enabled
    with a well-formed cmd, the throttle marker is older than
    ``min_interval_min``, and no previous wake is still running (pidfile).
    On spawn: detached process, output appended to the wake log, env carries
    FULCRA_COORD_AGENT + FULCRA_COORD_WAKE_PENDING, marker + pidfile written,
    one log line emitted to stderr (captured by the listener job's log).
    """
    try:
        if pending <= 0:
            return False
        entry = _wake_entry_for(agent, _load_wake_config())
        if entry is None or not entry.get("enabled", True):
            return False
        cmd = _valid_cmd(entry)
        if cmd is None:
            return False

        # Throttle: at most one spawn per min_interval_min (marker mtime).
        marker = _wake_marker_path(agent)
        try:
            age_min = (time.time() - marker.stat().st_mtime) / 60.0
            if age_min < _min_interval_min(entry):
                return False
        except OSError:
            pass  # no marker yet -> first wake, proceed

        # Single-flight: skip while the previous wake is still running. A
        # stale pidfile (dead pid, garbage content) is ignored, never fatal.
        pidfile = _wake_pidfile_path(agent)
        if pidfile.exists():
            try:
                if _pid_alive(int(pidfile.read_text().strip())):
                    return False
            except (ValueError, OSError):
                pass  # garbage/unreadable pidfile = stale -> proceed

        # Spawn DETACHED so the wake outlives (and can never block) the tick.
        # Output appends to the wake log so a misbehaving command is debuggable.
        log = _wake_log_path(agent)
        log.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["FULCRA_COORD_AGENT"] = agent
        env["FULCRA_COORD_WAKE_PENDING"] = str(pending)
        with log.open("ab") as fh:
            proc = Popen(cmd, stdin=DEVNULL, stdout=fh, stderr=fh,
                         start_new_session=True, env=env)

        # Record state only AFTER a successful spawn: a failed Popen leaves the
        # throttle unarmed so the next tick retries.
        cache.cache_root().mkdir(parents=True, exist_ok=True)
        marker.write_text(str(proc.pid))
        pidfile.write_text(str(proc.pid))
        print(f"[fulcra-coord] wake: spawned {cmd[0]} (pid {proc.pid}) for "
              f"{agent} — {pending} pending; log: {log}", file=sys.stderr)
        return True
    except Exception:
        # Fail-safe contract: a wake problem must never break notify-inbox.
        return False

"""Host wake-exec (operator directive 2026-06-10) — mechanism tests.

THE GAP THIS FEATURE CLOSES: the host listener (launchd/cron ``notify-inbox``)
detects actionable bus work but could only NOTIFY (webhook / desktop banner) —
it could not wake an agent runtime. When the operator is away and a session is
idle/dead, directives and verdicts sit unprocessed. The operator's directive:
"that needs to be part of the product. this can't die if i do other stuff for a
bit. the whole point was to enable multiple simultaneous workflows better."

The mechanism under test (``fulcra_coord.wake``) is the platform-NEUTRAL core:
it loads an optional per-fleet ``wake.json`` config, and when a notify-inbox
tick finds pending work for a configured agent it spawns the configured command
DETACHED — throttled by a per-agent marker, single-flighted by a pidfile, and
fail-safe end to end (it must never raise into a polling tick).

Purity is pinned by a grep test below: the core mechanism contains zero
platform strings (no claude/codex/openclaw) — what gets spawned is entirely
per-adopter config, exactly like the review-routing seeds.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fulcra_coord import cache, schema, wake


def _write_config(cfg: dict) -> Path:
    """Materialize a wake.json under the (test-redirected) XDG_CONFIG_HOME."""
    base = Path(os.environ["XDG_CONFIG_HOME"]) / "fulcra-coord"
    base.mkdir(parents=True, exist_ok=True)
    p = base / "wake.json"
    p.write_text(json.dumps(cfg))
    return p


def _entry(cmd=None, **ov) -> dict:
    e = {"cmd": cmd or ["/bin/echo", "wake"], "min_interval_min": 15,
         "max_runtime_s": 900, "enabled": True}
    e.update(ov)
    return e


class _WakeEnvBase(unittest.TestCase):
    """Redirect XDG_CONFIG_HOME (the conftest already redirects XDG_CACHE_HOME)
    so no test can ever read the operator's REAL wake.json — which could spawn
    a real agent runtime mid-suite."""

    def setUp(self):
        self.cfg_tmp = tempfile.mkdtemp()
        self._prev_cfg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.cfg_tmp

    def tearDown(self):
        if self._prev_cfg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._prev_cfg
        shutil.rmtree(self.cfg_tmp, ignore_errors=True)


class TestWakeConfigLoader(_WakeEnvBase):
    def test_absent_config_yields_empty(self):
        self.assertEqual(wake._load_wake_config(), {})

    def test_malformed_config_yields_empty(self):
        base = Path(self.cfg_tmp) / "fulcra-coord"
        base.mkdir(parents=True, exist_ok=True)
        (base / "wake.json").write_text("{ not json")
        self.assertEqual(wake._load_wake_config(), {})

    def test_non_dict_config_yields_empty(self):
        _write_config(["not", "a", "dict"])  # type: ignore[arg-type]
        self.assertEqual(wake._load_wake_config(), {})

    def test_prefix_match_longest_wins(self):
        cfg = {
            "agent-a:": _entry(["/bin/echo", "broad"]),
            "agent-a:host1:": _entry(["/bin/echo", "narrow"]),
            "agent-b:": _entry(["/bin/echo", "other"]),
        }
        e = wake._wake_entry_for("agent-a:host1:repo", cfg)
        self.assertIsNotNone(e)
        self.assertEqual(e["cmd"][1], "narrow")

    def test_prefix_no_match_yields_none(self):
        cfg = {"agent-b:": _entry()}
        self.assertIsNone(wake._wake_entry_for("agent-a:host1:repo", cfg))

    def test_non_dict_entry_ignored(self):
        cfg = {"agent-a:": "not-a-dict"}
        self.assertIsNone(wake._wake_entry_for("agent-a:host1:repo", cfg))


class TestMaybeWake(_WakeEnvBase):
    AGENT = "agent-a:host1:repo"

    def _popen_mock(self):
        proc = MagicMock()
        proc.pid = 4242
        return MagicMock(return_value=proc)

    # -- the exactly-today's-behavior guarantees ------------------------------
    def test_no_config_no_spawn(self):
        popen = self._popen_mock()
        with patch("fulcra_coord.wake.Popen", popen):
            self.assertFalse(wake.maybe_wake(self.AGENT, 3))
        popen.assert_not_called()

    def test_pending_zero_no_spawn(self):
        _write_config({"agent-a:": _entry()})
        popen = self._popen_mock()
        with patch("fulcra_coord.wake.Popen", popen):
            self.assertFalse(wake.maybe_wake(self.AGENT, 0))
        popen.assert_not_called()

    def test_disabled_entry_no_spawn(self):
        _write_config({"agent-a:": _entry(enabled=False)})
        popen = self._popen_mock()
        with patch("fulcra_coord.wake.Popen", popen):
            self.assertFalse(wake.maybe_wake(self.AGENT, 3))
        popen.assert_not_called()

    def test_malformed_config_no_spawn_no_raise(self):
        base = Path(self.cfg_tmp) / "fulcra-coord"
        base.mkdir(parents=True, exist_ok=True)
        (base / "wake.json").write_text("{ definitely not json")
        popen = self._popen_mock()
        with patch("fulcra_coord.wake.Popen", popen):
            self.assertFalse(wake.maybe_wake(self.AGENT, 3))  # must not raise
        popen.assert_not_called()

    def test_malformed_cmd_no_spawn(self):
        _write_config({"agent-a:": _entry(cmd="not-a-list")})
        popen = self._popen_mock()
        with patch("fulcra_coord.wake.Popen", popen):
            self.assertFalse(wake.maybe_wake(self.AGENT, 3))
        popen.assert_not_called()

    # -- the spawn path -------------------------------------------------------
    def test_stale_marker_spawns_once_with_argv_and_env(self):
        _write_config({"agent-a:": _entry(["/bin/echo", "hello"])})
        # Stale marker: older than min_interval_min.
        marker = wake._wake_marker_path(self.AGENT)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("")
        old = time.time() - 3600
        os.utime(marker, (old, old))

        popen = self._popen_mock()
        with patch("fulcra_coord.wake.Popen", popen):
            self.assertTrue(wake.maybe_wake(self.AGENT, 2))

        popen.assert_called_once()
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["/bin/echo", "hello"])
        self.assertIsNone(kwargs.get("cwd"))
        self.assertTrue(kwargs.get("start_new_session"),
                        "wake must spawn DETACHED (start_new_session=True)")
        env = kwargs.get("env") or {}
        self.assertEqual(env.get("FULCRA_COORD_AGENT"), self.AGENT)
        self.assertEqual(env.get("FULCRA_COORD_WAKE_PENDING"), "2")
        # Marker refreshed (throttle re-arms) + pidfile written (single-flight).
        self.assertGreater(marker.stat().st_mtime, old + 1)
        self.assertEqual(wake._wake_pidfile_path(self.AGENT).read_text().strip(),
                         "4242")

    def test_spawn_uses_configured_working_directory(self):
        cwd = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(cwd, ignore_errors=True))
        _write_config({"agent-a:": _entry(["/bin/echo", "hello"], cwd=cwd)})
        popen = self._popen_mock()
        with patch("fulcra_coord.wake.Popen", popen):
            self.assertTrue(wake.maybe_wake(self.AGENT, 2))
        self.assertEqual(popen.call_args.kwargs.get("cwd"), cwd)

    def test_invalid_working_directory_no_spawn(self):
        _write_config({"agent-a:": _entry(cwd="/definitely/not/a/worktree")})
        popen = self._popen_mock()
        with patch("fulcra_coord.wake.Popen", popen):
            self.assertFalse(wake.maybe_wake(self.AGENT, 2))
        popen.assert_not_called()

    def test_first_wake_with_no_marker_spawns(self):
        _write_config({"agent-a:": _entry()})
        popen = self._popen_mock()
        with patch("fulcra_coord.wake.Popen", popen):
            self.assertTrue(wake.maybe_wake(self.AGENT, 1))
        popen.assert_called_once()

    def test_fresh_marker_throttles(self):
        _write_config({"agent-a:": _entry()})
        marker = wake._wake_marker_path(self.AGENT)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("")  # mtime = now -> inside min_interval_min
        popen = self._popen_mock()
        with patch("fulcra_coord.wake.Popen", popen):
            self.assertFalse(wake.maybe_wake(self.AGENT, 3))
        popen.assert_not_called()

    def test_spawn_uses_longest_prefix_entry(self):
        _write_config({
            "agent-a:": _entry(["/bin/echo", "broad"]),
            "agent-a:host1:": _entry(["/bin/echo", "narrow"]),
        })
        popen = self._popen_mock()
        with patch("fulcra_coord.wake.Popen", popen):
            self.assertTrue(wake.maybe_wake(self.AGENT, 1))
        self.assertEqual(popen.call_args[0][0], ["/bin/echo", "narrow"])

    # -- single-flight via pidfile --------------------------------------------
    def test_live_pidfile_skips_spawn(self):
        _write_config({"agent-a:": _entry()})
        pidfile = wake._wake_pidfile_path(self.AGENT)
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        pidfile.write_text(str(os.getpid()))  # this test process: definitely alive
        popen = self._popen_mock()
        with patch("fulcra_coord.wake.Popen", popen):
            self.assertFalse(wake.maybe_wake(self.AGENT, 3))
        popen.assert_not_called()

    def test_dead_pidfile_is_stale_and_ignored(self):
        _write_config({"agent-a:": _entry()})
        # A real pid that has exited+been reaped -> os.kill(pid, 0) fails.
        proc = subprocess.Popen(["/usr/bin/true"])
        proc.wait()
        pidfile = wake._wake_pidfile_path(self.AGENT)
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        pidfile.write_text(str(proc.pid))
        popen = self._popen_mock()
        with patch("fulcra_coord.wake.Popen", popen):
            self.assertTrue(wake.maybe_wake(self.AGENT, 1))
        popen.assert_called_once()

    def test_garbage_pidfile_is_stale_and_ignored(self):
        _write_config({"agent-a:": _entry()})
        pidfile = wake._wake_pidfile_path(self.AGENT)
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        pidfile.write_text("not-a-pid")
        popen = self._popen_mock()
        with patch("fulcra_coord.wake.Popen", popen):
            self.assertTrue(wake.maybe_wake(self.AGENT, 1))
        popen.assert_called_once()

    # -- 2026-06-11 bug hunt S3: PID recycling, TOCTOU, marker-failure leak ----
    def test_stale_by_age_pidfile_is_reclaimed(self):
        """A pidfile older than max_runtime_s is STALE even when its pid looks
        alive — pids recycle, so after the runtime ceiling a 'live' pid is far
        more likely an unrelated process than our wake (S3 PID-recycling)."""
        _write_config({"agent-a:": _entry(max_runtime_s=900)})
        pidfile = wake._wake_pidfile_path(self.AGENT)
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        pidfile.write_text(str(os.getpid()))  # alive — but the file is ancient
        old = time.time() - 3600              # 1h > max_runtime_s=900
        os.utime(pidfile, (old, old))
        popen = self._popen_mock()
        with patch("fulcra_coord.wake.Popen", popen):
            self.assertTrue(wake.maybe_wake(self.AGENT, 1))
        popen.assert_called_once()

    def test_fresh_pidfile_with_live_pid_still_skips(self):
        """Companion pin: a fresh (within max_runtime_s) pidfile with a live
        pid keeps single-flight semantics."""
        _write_config({"agent-a:": _entry(max_runtime_s=900)})
        pidfile = wake._wake_pidfile_path(self.AGENT)
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        pidfile.write_text(str(os.getpid()))
        popen = self._popen_mock()
        with patch("fulcra_coord.wake.Popen", popen):
            self.assertFalse(wake.maybe_wake(self.AGENT, 3))
        popen.assert_not_called()

    def test_concurrent_ticks_spawn_exactly_once(self):
        """S3 TOCTOU: two ticks racing through the exists()/alive() check used
        to BOTH spawn. The pidfile is now created O_CREAT|O_EXCL BEFORE the
        spawn (the inter-tick mutex); a second tick arriving inside the
        winner's spawn window must skip. Simulated by firing a nested tick
        from within the winner's Popen call — the exact race window."""
        _write_config({"agent-a:": _entry()})
        nested_results = []

        def popen_side_effect(*a, **kw):
            nested_results.append(wake.maybe_wake(self.AGENT, 3))
            proc = MagicMock()
            proc.pid = 4242
            return proc

        with patch("fulcra_coord.wake.Popen", side_effect=popen_side_effect):
            self.assertTrue(wake.maybe_wake(self.AGENT, 3))
        # The nested (concurrent) tick lost the mutex and did not spawn.
        self.assertEqual(nested_results, [False])

    def test_throttle_marker_armed_before_spawn(self):
        """S3 marker-failure leak: the throttle marker must be armed BEFORE
        Popen. If it were written after and the write failed, a crashing
        marker path would allow immediate respawn every tick (a spawn storm
        of full agent runtimes). The inverse failure — marker armed but spawn
        failed — merely delays one interval, the right side to fail on."""
        _write_config({"agent-a:": _entry()})
        marker = wake._wake_marker_path(self.AGENT)
        seen = []

        def popen_side_effect(*a, **kw):
            seen.append(marker.exists())   # observed AT spawn time
            proc = MagicMock()
            proc.pid = 4242
            return proc

        with patch("fulcra_coord.wake.Popen", side_effect=popen_side_effect):
            self.assertTrue(wake.maybe_wake(self.AGENT, 1))
        self.assertEqual(seen, [True])

    # -- fail-safe contract ----------------------------------------------------
    def test_popen_raising_never_propagates_and_arms_throttle(self):
        """A raising Popen must not propagate (fail-safe contract). Updated for
        the 2026-06-11 bug hunt S3: the throttle marker is now armed BEFORE the
        spawn, so a failed spawn leaves it armed — the retry waits one interval
        instead of hammering a broken command every tick. (The pre-S3 pin of
        'failed spawn leaves no marker' is the inverse failure mode S3
        deliberately trades away.) The pidfile mutex, though, must be released
        so the retry isn't blocked for max_runtime_s."""
        _write_config({"agent-a:": _entry()})
        with patch("fulcra_coord.wake.Popen",
                   side_effect=OSError("no such binary")):
            self.assertFalse(wake.maybe_wake(self.AGENT, 3))  # must not raise
        self.assertTrue(wake._wake_marker_path(self.AGENT).exists())
        self.assertFalse(wake._wake_pidfile_path(self.AGENT).exists())


class TestWakeMechanismIsPlatformNeutral(unittest.TestCase):
    """Generalization pin (same spirit as the fleet-id / forge-agnostic greps):
    the core mechanism must not know WHAT it spawns. Platform strings belong in
    per-adopter wake.json (and the adapter installers), never in wake.py."""

    def test_wake_module_has_zero_platform_strings(self):
        src = (Path(wake.__file__)).read_text(encoding="utf-8").lower()
        for needle in ("claude", "codex", "openclaw", "chatgpt"):
            self.assertNotIn(
                needle, src,
                f"wake.py must stay platform-neutral but mentions {needle!r}; "
                "platform commands are per-adopter wake.json config "
                "(see wake.example.json), never core code.")


class TestNotifyInboxWakeIntegration(_WakeEnvBase):
    """notify-inbox hands its pending count to the wake mechanism each tick."""

    def setUp(self):
        super().setUp()
        self.fake_backend = ["false"]

    def _directive(self, assignee: str) -> dict:
        t = schema.make_task(title="Please do X", workstream="general",
                             agent="agent-1", owner_agent="agent-1",
                             assignee=assignee)
        return t

    def test_notify_inbox_invokes_wake_with_pending_count(self):
        from fulcra_coord.cli import cmd_notify_inbox
        d = self._directive("agent-a:h:r")
        cache.write_cached_task(d)
        with patch("fulcra_coord.wake.maybe_wake") as mw, \
             patch("fulcra_coord.listener.emit_notification"):
            rc = cmd_notify_inbox(types.SimpleNamespace(agent="agent-a:h:r"),
                                  backend=self.fake_backend)
        self.assertEqual(rc, 0)
        mw.assert_called_once()
        self.assertEqual(mw.call_args[0][0], "agent-a:h:r")
        self.assertEqual(mw.call_args[0][1], 1)

    def test_notify_inbox_without_config_spawns_nothing(self):
        """Config absent -> EXACTLY today's behavior: surface written, notify
        emitted, and no process is ever spawned."""
        from fulcra_coord.cli import cmd_notify_inbox
        d = self._directive("agent-a:h:r")
        cache.write_cached_task(d)
        popen = MagicMock()
        with patch("fulcra_coord.wake.Popen", popen), \
             patch("fulcra_coord.listener.emit_notification"):
            rc = cmd_notify_inbox(types.SimpleNamespace(agent="agent-a:h:r"),
                                  backend=self.fake_backend)
        self.assertEqual(rc, 0)
        popen.assert_not_called()

    def test_wake_raising_never_breaks_the_tick(self):
        from fulcra_coord.cli import cmd_notify_inbox
        d = self._directive("agent-a:h:r")
        cache.write_cached_task(d)
        with patch("fulcra_coord.wake.maybe_wake",
                   side_effect=Exception("boom")), \
             patch("fulcra_coord.listener.emit_notification"):
            rc = cmd_notify_inbox(types.SimpleNamespace(agent="agent-a:h:r"),
                                  backend=self.fake_backend)
        self.assertEqual(rc, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

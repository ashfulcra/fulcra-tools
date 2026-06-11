"""Version self-incorporation (operator directive 2026-06-10) — mechanism tests.

THE GAP THIS FEATURE CLOSES: every fulcra-coord release used to need a manual
"UPDATE NOW" broadcast plus the operator walking each host through
``git pull && uv tool install --reinstall``. The operator's directive
(2026-06-10): "i'm not going to go around and wake the entire fleet for each
incremental upgrade." So the bus now carries a canonical VERSION MANIFEST
(``runtime/version.json``) and every session-start / listener tick checks it
and — DEFAULT ON, env opt-out via FULCRA_COORD_SELF_UPDATE=0 — runs the update
from LOCAL config.

THE SAFETY BOUNDARY UNDER TEST (the reconciled spec's non-negotiable rail,
docs/superpowers/specs/2026-06-08-greenfield-reconciled.md): the bus carries a
version POINTER, never a code payload — no cmd/argv/script keys in the
manifest (the validator REJECTS extra keys, pinned below), and the update argv
is built IN CODE from local config, never from anything read off the bus. An
agent that cannot update safely degrades VISIBLY (stale marker + roster
suffix) and never crashes a session boot or a polling tick.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fulcra_coord import __version__, remote, schema, selfupdate


def _manifest(version="99.0.0", commit="deadbeef", min_supported=None):
    return schema.make_version_manifest(version, commit, min_supported=min_supported)


class _CfgEnvBase(unittest.TestCase):
    """Redirect XDG_CONFIG_HOME (the conftest already redirects XDG_CACHE_HOME)
    so no test can ever read the operator's REAL update config — which could
    trigger a real ``uv tool install`` mid-suite."""

    def setUp(self):
        self.cfg_tmp = tempfile.mkdtemp()
        self._prev_cfg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.cfg_tmp
        self._prev_toggle = os.environ.pop("FULCRA_COORD_SELF_UPDATE", None)

    def tearDown(self):
        if self._prev_cfg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._prev_cfg
        if self._prev_toggle is not None:
            os.environ["FULCRA_COORD_SELF_UPDATE"] = self._prev_toggle
        shutil.rmtree(self.cfg_tmp, ignore_errors=True)

    def _write_cfg(self, name: str, data) -> Path:
        base = Path(self.cfg_tmp) / "fulcra-coord"
        base.mkdir(parents=True, exist_ok=True)
        p = base / name
        p.write_text(json.dumps(data) if not isinstance(data, str) else data)
        return p


# ---------------------------------------------------------------------------
# Manifest schema: round-trip, validator, the pointer-rule pin
# ---------------------------------------------------------------------------

class TestVersionManifestSchema(unittest.TestCase):
    def test_path_helper(self):
        self.assertEqual(remote.version_manifest_path(),
                         f"{remote.remote_root()}/runtime/version.json")

    def test_round_trip_valid(self):
        m = _manifest("0.16.0", "abc1234", min_supported="0.15.0")
        self.assertEqual(m["schema"], "fulcra.coordination.version.v1")
        self.assertEqual(m["package_version"], "0.16.0")
        self.assertEqual(m["release_commit"], "abc1234")
        self.assertEqual(m["min_supported"], "0.15.0")
        self.assertTrue(m["published_at"].endswith("Z"))
        self.assertEqual(schema.validate_version_manifest(m), [])

    def test_min_supported_optional(self):
        m = _manifest("0.16.0", "abc1234")
        self.assertIsNone(m["min_supported"])
        self.assertEqual(schema.validate_version_manifest(m), [])

    def test_validator_rejects_missing_or_empty_version(self):
        m = _manifest()
        m["package_version"] = ""
        self.assertNotEqual(schema.validate_version_manifest(m), [])
        del m["package_version"]
        self.assertNotEqual(schema.validate_version_manifest(m), [])

    def test_validator_rejects_wrong_schema(self):
        m = _manifest()
        m["schema"] = "fulcra.coordination.task.v1"
        self.assertNotEqual(schema.validate_version_manifest(m), [])

    def test_validator_rejects_non_dict(self):
        self.assertNotEqual(schema.validate_version_manifest(None), [])
        self.assertNotEqual(schema.validate_version_manifest([1]), [])

    def test_pointer_rule_pin_extra_keys_rejected(self):
        """THE SAFETY BOUNDARY: the manifest is a pointer, never a payload.
        Any extra key — and specifically command-shaped keys — must fail
        validation, so a tampered manifest can never smuggle an instruction
        to the updater (which ignores everything but the version anyway)."""
        for evil_key in ("cmd", "argv", "script", "url", "anything_else"):
            m = _manifest()
            m[evil_key] = ["rm", "-rf", "/"]
            errs = schema.validate_version_manifest(m)
            self.assertNotEqual(errs, [], f"extra key {evil_key!r} must be rejected")

    def test_make_manifest_emits_no_command_keys(self):
        """Belt-and-braces for the pointer rule: the producer side can only
        ever emit the closed key set (version + commit + window + metadata)."""
        self.assertEqual(
            set(_manifest().keys()),
            {"schema", "package_version", "release_commit", "min_supported",
             "published_at"})


# ---------------------------------------------------------------------------
# is_behind — the pure comparison
# ---------------------------------------------------------------------------

class TestIsBehind(unittest.TestCase):
    def test_behind(self):
        self.assertTrue(selfupdate.is_behind("0.15.2", _manifest("0.16.0")))
        # double-digit segment compares numerically, not lexically
        self.assertTrue(selfupdate.is_behind("0.15.2", _manifest("0.15.10")))

    def test_equal_is_not_behind(self):
        self.assertFalse(selfupdate.is_behind("0.15.2", _manifest("0.15.2")))

    def test_ahead_is_not_behind(self):
        self.assertFalse(selfupdate.is_behind("0.16.0", _manifest("0.15.2")))
        self.assertFalse(selfupdate.is_behind("0.15.10", _manifest("0.15.2")))

    def test_shorter_tuple_pads(self):
        self.assertTrue(selfupdate.is_behind("0.15", _manifest("0.15.1")))
        self.assertFalse(selfupdate.is_behind("0.15.0", _manifest("0.15")))

    def test_malformed_manifest_never_behind(self):
        """A garbage/tampered/absent manifest must read as 'not behind' — the
        degrade-gracefully rail: a bad bus record can never trigger an update."""
        self.assertFalse(selfupdate.is_behind("0.15.2", None))
        self.assertFalse(selfupdate.is_behind("0.15.2", {}))
        self.assertFalse(selfupdate.is_behind("0.15.2", {"package_version": "0.16.0"}))
        m = _manifest("not-a-version")
        self.assertFalse(selfupdate.is_behind("0.15.2", m))
        m2 = _manifest("0.16.0")
        m2["cmd"] = ["evil"]  # pointer-rule violation -> invalid -> never behind
        self.assertFalse(selfupdate.is_behind("0.15.2", m2))

    def test_malformed_installed_never_behind(self):
        self.assertFalse(selfupdate.is_behind("garbage", _manifest("0.16.0")))
        self.assertFalse(selfupdate.is_behind("", _manifest("0.16.0")))


# ---------------------------------------------------------------------------
# maybe_self_update — the I/O orchestration
# ---------------------------------------------------------------------------

class TestMaybeSelfUpdate(_CfgEnvBase):
    def _behind(self):
        """Patch the manifest read to report a canonical version we're behind."""
        return patch("fulcra_coord.selfupdate._download_manifest",
                     return_value=_manifest("99.0.0", "abc"))

    def test_env_zero_skips_everything(self):
        os.environ["FULCRA_COORD_SELF_UPDATE"] = "0"
        with self._behind() as dl, \
             patch("fulcra_coord.selfupdate._run_proc") as run:
            self.assertEqual(selfupdate.maybe_self_update(), "disabled")
        dl.assert_not_called()
        run.assert_not_called()

    def test_default_on_no_env_needed(self):
        """DEFAULT ON (operator call 2026-06-10, supersedes the spec's opt-in
        note): with NO env var set, a behind check attempts the update."""
        self.assertNotIn("FULCRA_COORD_SELF_UPDATE", os.environ)
        self._write_cfg("update-cmd.json", {"cmd": ["/usr/bin/true"]})
        with self._behind(), \
             patch("fulcra_coord.selfupdate._run_proc",
                   return_value=types.SimpleNamespace(returncode=0)) as run:
            self.assertEqual(selfupdate.maybe_self_update(), "updated")
        run.assert_called()

    def test_current_clears_stale_marker(self):
        selfupdate._write_stale_marker("0.1.0", "0.2.0")
        with patch("fulcra_coord.selfupdate._download_manifest",
                   return_value=_manifest(__version__)):
            self.assertEqual(selfupdate.maybe_self_update(), "current")
        self.assertEqual(selfupdate.stale_summary_suffix(), "")

    def test_invalid_manifest_preserves_existing_stale_marker(self):
        """Fail-closed garbage must not erase a known-behind roster suffix.

        ``is_behind`` returns False for invalid manifests so they cannot trigger
        an update, but that is not evidence the host is current. During a
        manifest outage/tamper, an already-stale host must stay visible.
        """
        selfupdate._write_stale_marker("0.1.0", "0.2.0")
        with patch("fulcra_coord.selfupdate._download_manifest",
                   return_value={"package_version": "999.0.0"}):
            self.assertEqual(selfupdate.maybe_self_update(), "current")
        suffix = selfupdate.stale_summary_suffix()
        self.assertIn("0.1.0", suffix)
        self.assertIn("0.2.0", suffix)

    def test_behind_with_cmd_config_spawns_exact_argv_and_logs(self):
        """The update command comes from LOCAL config only — exact argv, exact
        cwd, output to the cache-dir log (the visible breadcrumb)."""
        self._write_cfg("update-cmd.json",
                        {"cmd": ["/usr/bin/my-updater", "--flag"], "cwd": "/tmp"})
        with self._behind(), \
             patch("fulcra_coord.selfupdate._run_proc",
                   return_value=types.SimpleNamespace(returncode=0)) as run:
            self.assertEqual(selfupdate.maybe_self_update(), "updated")
        (argv,), kwargs = run.call_args
        self.assertEqual(argv, ["/usr/bin/my-updater", "--flag"])
        self.assertEqual(kwargs["cwd"], "/tmp")
        self.assertEqual(kwargs["timeout"], selfupdate.UPDATE_TIMEOUT_S)
        self.assertTrue(selfupdate._update_log_path().exists())
        # success -> no stale marker -> no roster suffix
        self.assertEqual(selfupdate.stale_summary_suffix(), "")

    def test_behind_with_checkout_config_builds_argv_in_code(self):
        """The built-in default: update.json names the canonical checkout and
        the TWO argvs (git pull --ff-only, uv tool install) are built IN CODE
        from that path — never from anything the bus said (the pointer rule).

        Updated for the 2026-06-11 bug hunt S1 branch guard: a rev-parse
        branch probe now legitimately PRECEDES the pull (and must report the
        configured branch for the update to proceed), so the spawn sequence
        is probe -> pull -> install."""
        checkout = self.cfg_tmp  # any existing dir
        self._write_cfg("update.json", {"checkout": checkout})
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if "rev-parse" in argv:
                return types.SimpleNamespace(returncode=0, stdout="main\n")
            return types.SimpleNamespace(returncode=0)

        with self._behind(), \
             patch("fulcra_coord.selfupdate._run_proc", side_effect=run):
            self.assertEqual(selfupdate.maybe_self_update(), "updated")
        self.assertEqual(calls[0], ["git", "-C", checkout, "rev-parse",
                                    "--abbrev-ref", "HEAD"])
        self.assertEqual(calls[1], ["git", "-C", checkout, "pull", "--ff-only"])
        self.assertEqual(calls[2],
                         ["uv", "tool", "install", "--reinstall", "--force",
                          f"{checkout}/packages/fulcra-coord"])

    def test_no_config_degrades_visibly_no_spawn(self):
        """No update-cmd.json AND no update.json -> WARN once + stale marker
        (the roster suffix) + NO process spawned. Degraded, never broken."""
        import io, contextlib
        stderr = io.StringIO()
        with self._behind(), \
             patch("fulcra_coord.selfupdate._run_proc") as run, \
             contextlib.redirect_stderr(stderr):
            self.assertEqual(selfupdate.maybe_self_update(), "degraded-no-config")
            # the warn fires ONCE — a second pass stays quiet
            mid = stderr.getvalue()
            self.assertIn("update.json", mid)
            self.assertEqual(selfupdate.maybe_self_update(), "degraded-no-config")
            self.assertEqual(stderr.getvalue(), mid)
        run.assert_not_called()
        suffix = selfupdate.stale_summary_suffix()
        self.assertIn(__version__, suffix)
        self.assertIn("99.0.0", suffix)
        self.assertIn("behind canonical", suffix)

    def test_update_failure_degrades_visibly(self):
        self._write_cfg("update-cmd.json", {"cmd": ["/usr/bin/false"]})
        with self._behind(), \
             patch("fulcra_coord.selfupdate._run_proc",
                   return_value=types.SimpleNamespace(returncode=1)):
            self.assertEqual(selfupdate.maybe_self_update(), "update-failed")
        self.assertIn("behind canonical", selfupdate.stale_summary_suffix())

    def test_spawn_exception_never_raises(self):
        self._write_cfg("update-cmd.json", {"cmd": ["/nonexistent/updater"]})
        with self._behind(), \
             patch("fulcra_coord.selfupdate._run_proc",
                   side_effect=OSError("no such file")):
            # must not raise — fail-safe contract for both call sites
            self.assertEqual(selfupdate.maybe_self_update(), "update-failed")

    def test_manifest_read_failure_never_raises(self):
        with patch("fulcra_coord.selfupdate._download_manifest",
                   side_effect=RuntimeError("boom")):
            self.assertIn(selfupdate.maybe_self_update(), ("error", "current"))

    def test_throttle_honored_on_tick_path(self):
        """throttle=True (the notify-inbox tick): at most one manifest check
        per FULCRA_COORD_SELF_UPDATE_INTERVAL_H (default 6h, mtime marker)."""
        with patch("fulcra_coord.selfupdate._download_manifest",
                   return_value=_manifest(__version__)) as dl:
            self.assertEqual(selfupdate.maybe_self_update(throttle=True), "current")
            self.assertEqual(dl.call_count, 1)
            self.assertEqual(selfupdate.maybe_self_update(throttle=True), "throttled")
            self.assertEqual(dl.call_count, 1)  # no second remote read

    def test_throttle_interval_env_override(self):
        with patch("fulcra_coord.selfupdate._download_manifest",
                   return_value=_manifest(__version__)) as dl:
            selfupdate.maybe_self_update(throttle=True)
            # age the marker past a tiny interval -> due again
            marker = selfupdate._throttle_marker_path()
            old = time.time() - 3600  # 1h old
            os.utime(marker, (old, old))
            prev = os.environ.get("FULCRA_COORD_SELF_UPDATE_INTERVAL_H")
            os.environ["FULCRA_COORD_SELF_UPDATE_INTERVAL_H"] = "0.5"
            try:
                self.assertEqual(selfupdate.maybe_self_update(throttle=True), "current")
                self.assertEqual(dl.call_count, 2)
            finally:
                if prev is None:
                    os.environ.pop("FULCRA_COORD_SELF_UPDATE_INTERVAL_H", None)
                else:
                    os.environ["FULCRA_COORD_SELF_UPDATE_INTERVAL_H"] = prev

    def test_connect_path_is_unthrottled(self):
        """throttle=False (session-start connect) always checks — a fresh
        session should never boot stale just because a tick checked recently."""
        with patch("fulcra_coord.selfupdate._download_manifest",
                   return_value=_manifest(__version__)) as dl:
            selfupdate.maybe_self_update(throttle=True)
            self.assertEqual(selfupdate.maybe_self_update(), "current")
            self.assertEqual(dl.call_count, 2)


# ---------------------------------------------------------------------------
# 2026-06-11 bug hunt S1: branch guard + update lock + attempt throttle
# ---------------------------------------------------------------------------

class TestSelfUpdateGuards(_CfgEnvBase):
    """S1 (P1): the updater used to (a) ff-pull whatever branch the checkout
    happened to be on, (b) re-run the full git+uv reinstall on EVERY connect
    while an update kept failing or not taking effect, and (c) let a
    concurrent connect+tick double-run git/uv with no lock."""

    def _behind(self, version="99.0.0"):
        return patch("fulcra_coord.selfupdate._download_manifest",
                     return_value=_manifest(version, "abc"))

    # -- (a) branch guard ----------------------------------------------------

    def test_checkout_on_wrong_branch_refuses_to_pull(self):
        """A checkout parked on a feature branch must NOT be ff-pulled (that
        either fails noisily forever or, worse, fast-forwards a feature
        branch onto origin's state). Refuse: warn + stale marker, no spawn
        of pull/install."""
        checkout = self.cfg_tmp
        self._write_cfg("update.json", {"checkout": checkout})
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if "rev-parse" in argv:
                return types.SimpleNamespace(returncode=0,
                                             stdout="feature/wip\n")
            return types.SimpleNamespace(returncode=0)

        with self._behind(), \
             patch("fulcra_coord.selfupdate._run_proc", side_effect=run):
            self.assertEqual(selfupdate.maybe_self_update(), "wrong-branch")
        # ONLY the branch probe ran — never the pull or the reinstall.
        self.assertTrue(calls, "the branch probe must run")
        self.assertTrue(all("rev-parse" in argv for argv in calls), calls)
        # Degraded VISIBLY: the roster suffix shows the host is behind.
        self.assertIn("behind canonical", selfupdate.stale_summary_suffix())

    def test_checkout_branch_override_in_update_json(self):
        """update.json may pin a non-main canonical branch; the guard then
        accepts exactly that branch."""
        checkout = self.cfg_tmp
        self._write_cfg("update.json",
                        {"checkout": checkout, "branch": "release"})
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if "rev-parse" in argv:
                return types.SimpleNamespace(returncode=0,
                                             stdout="release\n")
            return types.SimpleNamespace(returncode=0)

        with self._behind(), \
             patch("fulcra_coord.selfupdate._run_proc", side_effect=run):
            self.assertEqual(selfupdate.maybe_self_update(), "updated")
        self.assertIn(["git", "-C", checkout, "pull", "--ff-only"], calls)

    def test_undeterminable_branch_refuses(self):
        """No git / not a repo / probe error -> the branch is unknowable, so
        the pull would be a blind mutation of an unknown working tree.
        Fail closed (refuse), degrade visibly."""
        checkout = self.cfg_tmp
        self._write_cfg("update.json", {"checkout": checkout})
        with self._behind(), \
             patch("fulcra_coord.selfupdate._run_proc",
                   side_effect=OSError("git missing")):
            self.assertEqual(selfupdate.maybe_self_update(), "wrong-branch")
        self.assertIn("behind canonical", selfupdate.stale_summary_suffix())

    # -- (b) the update lock ---------------------------------------------------

    def test_lock_held_skips_concurrent_attempt(self):
        """A FRESH lock file means another process (connect or tick) is
        mid-update right now: the second attempt must skip, not double-run
        git/uv over the same checkout."""
        self._write_cfg("update-cmd.json", {"cmd": ["/usr/bin/true"]})
        lock = selfupdate._update_lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("12345")
        with self._behind(), \
             patch("fulcra_coord.selfupdate._run_proc") as run:
            self.assertEqual(selfupdate.maybe_self_update(), "locked")
        run.assert_not_called()
        self.assertTrue(lock.exists())   # never steal a live holder's lock

    def test_stale_lock_is_broken_and_update_proceeds(self):
        """A lock older than UPDATE_TIMEOUT_S can only be a crashed holder
        (every update step is bounded by that timeout) — break it."""
        self._write_cfg("update-cmd.json", {"cmd": ["/usr/bin/true"]})
        lock = selfupdate._update_lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("dead")
        old = time.time() - (selfupdate.UPDATE_TIMEOUT_S * 2 + 60)
        os.utime(lock, (old, old))
        with self._behind(), \
             patch("fulcra_coord.selfupdate._run_proc",
                   return_value=types.SimpleNamespace(returncode=0)):
            self.assertEqual(selfupdate.maybe_self_update(), "updated")
        self.assertFalse(lock.exists())   # released after the run

    # -- (c) attempt throttle on the connect path ------------------------------

    def test_ineffective_update_attempt_throttles_next_connect(self):
        """An attempt that doesn't change __version__ (failed install, or a
        'successful' one that didn't take) must not re-run the whole
        git+uv pipeline on EVERY subsequent session start — arm the same
        6h marker the tick uses and skip within it."""
        self._write_cfg("update-cmd.json", {"cmd": ["/usr/bin/true"]})
        with self._behind(), \
             patch("fulcra_coord.selfupdate._run_proc",
                   return_value=types.SimpleNamespace(returncode=0)) as run:
            self.assertEqual(selfupdate.maybe_self_update(), "updated")
            self.assertEqual(run.call_count, 1)
            # Next connect: still behind the SAME canonical -> no re-spawn.
            self.assertEqual(selfupdate.maybe_self_update(),
                             "attempt-throttled")
            self.assertEqual(run.call_count, 1)
        # Still degraded VISIBLY while throttled.
        self.assertIn("behind canonical", selfupdate.stale_summary_suffix())

    def test_failed_update_attempt_throttles_next_connect(self):
        self._write_cfg("update-cmd.json", {"cmd": ["/usr/bin/false"]})
        with self._behind(), \
             patch("fulcra_coord.selfupdate._run_proc",
                   return_value=types.SimpleNamespace(returncode=1)) as run:
            self.assertEqual(selfupdate.maybe_self_update(), "update-failed")
            self.assertEqual(selfupdate.maybe_self_update(),
                             "attempt-throttled")
            self.assertEqual(run.call_count, 1)

    def test_new_canonical_release_resets_the_attempt_throttle(self):
        """The throttle is per-canonical-version: a NEW release must get one
        immediate attempt even inside the window (the operator just shipped
        a fix — quite possibly for the broken updater)."""
        self._write_cfg("update-cmd.json", {"cmd": ["/usr/bin/true"]})
        with self._behind("99.0.0"), \
             patch("fulcra_coord.selfupdate._run_proc",
                   return_value=types.SimpleNamespace(returncode=0)) as run:
            self.assertEqual(selfupdate.maybe_self_update(), "updated")
        with self._behind("99.1.0"), \
             patch("fulcra_coord.selfupdate._run_proc",
                   return_value=types.SimpleNamespace(returncode=0)) as run2:
            self.assertEqual(selfupdate.maybe_self_update(), "updated")
            self.assertEqual(run2.call_count, 1)


# ---------------------------------------------------------------------------
# Wiring: connect (ordering + roster suffix) and the listener tick
# ---------------------------------------------------------------------------

class TestConnectWiring(_CfgEnvBase):
    def _args(self, **kw):
        base = dict(agent="claude-code:h:r", workstream=None, summary="",
                    format="json", can_review=False, role=None)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_presence_write_precedes_update_check(self):
        """2026-06-11 bug hunt S2 (P2): this test previously PINNED the buggy
        order (update BEFORE presence). The update step can legitimately run
        for minutes (git pull + a cold uv build, bounded at 300s) — during
        which the booting session was INVISIBLE on the roster. Presence is
        the boot-critical write; the staleness suffix can ride the next
        connect/heartbeat off the persisted marker. Pin: the presence upload
        precedes the manifest read / update attempt."""
        from fulcra_coord.presence import cmd_connect
        order = []
        with patch("fulcra_coord.selfupdate.maybe_self_update",
                   side_effect=lambda **kw: order.append("update") or "current"), \
             patch("fulcra_coord.presence._write_presence",
                   side_effect=lambda rec, backend=None: order.append("presence") or True), \
             patch("fulcra_coord.presence._derive_workstreams_from_open_tasks",
                   return_value=[]):
            cmd_connect(self._args(), backend=["false"])
        self.assertEqual(order[0], "presence")
        self.assertIn("update", order)
        self.assertLess(order.index("presence"), order.index("update"))

    def test_stale_marker_suffixes_connect_summary(self):
        """Visible degradation: when the host is KNOWN behind (the marker a
        previous attempt persisted), the presence summary carries
        '(vX behind canonical Y)' on the roster.

        Updated for S2 (2026-06-11 bug hunt): presence now writes BEFORE the
        update check, so the suffix comes from the marker persisted by the
        PREVIOUS episode — seeded here directly — and a marker written by
        THIS connect's update attempt surfaces on the FOLLOWING connect."""
        from fulcra_coord.presence import cmd_connect
        selfupdate._write_stale_marker(__version__, "99.0.0")
        captured = {}
        with patch("fulcra_coord.selfupdate.maybe_self_update",
                   return_value="degraded-no-config"), \
             patch("fulcra_coord.presence._write_presence",
                   side_effect=lambda rec, backend=None: captured.update(rec=rec) or True), \
             patch("fulcra_coord.presence._derive_workstreams_from_open_tasks",
                   return_value=[]):
            cmd_connect(self._args(summary="building things"), backend=["false"])
        self.assertIn("building things", captured["rec"]["summary"])
        self.assertIn(f"(v{__version__} behind canonical 99.0.0)",
                      captured["rec"]["summary"])

    def test_marker_written_this_connect_suffixes_the_following_connect(self):
        """The S2 ride-the-next-heartbeat contract end-to-end: connect #1's
        update attempt writes the marker (AFTER presence uploaded, so #1's
        record carries no suffix); connect #2 picks it up."""
        from fulcra_coord.presence import cmd_connect
        records = []
        with patch("fulcra_coord.selfupdate.maybe_self_update",
                   side_effect=lambda **kw: selfupdate._write_stale_marker(
                       __version__, "99.0.0") or "update-failed"), \
             patch("fulcra_coord.presence._write_presence",
                   side_effect=lambda rec, backend=None: records.append(rec) or True), \
             patch("fulcra_coord.presence._derive_workstreams_from_open_tasks",
                   return_value=[]):
            cmd_connect(self._args(summary="on it"), backend=["false"])
            cmd_connect(self._args(summary="on it"), backend=["false"])
        suffix = f"(v{__version__} behind canonical 99.0.0)"
        self.assertNotIn(suffix, records[0]["summary"])
        self.assertIn(suffix, records[1]["summary"])

    def test_selfupdate_failure_never_fails_connect(self):
        from fulcra_coord.presence import cmd_connect
        with patch("fulcra_coord.selfupdate.maybe_self_update",
                   side_effect=RuntimeError("boom")), \
             patch("fulcra_coord.presence._write_presence", return_value=True), \
             patch("fulcra_coord.presence._derive_workstreams_from_open_tasks",
                   return_value=[]):
            self.assertEqual(cmd_connect(self._args(), backend=["false"]), 0)


class TestListenerTickWiring(_CfgEnvBase):
    def test_notify_inbox_calls_throttled_check(self):
        from fulcra_coord.inbox import cmd_notify_inbox
        with patch("fulcra_coord.inbox._load_inbox", return_value=[]), \
             patch("fulcra_coord.inbox._notify_new_needs_me"), \
             patch("fulcra_coord.selfupdate.maybe_self_update",
                   return_value="throttled") as msu:
            args = types.SimpleNamespace(agent="claude-code:h:r")
            self.assertEqual(cmd_notify_inbox(args, backend=["false"]), 0)
        msu.assert_called_once()
        self.assertTrue(msu.call_args.kwargs.get("throttle"))

    def test_tick_survives_selfupdate_explosion(self):
        from fulcra_coord.inbox import cmd_notify_inbox
        with patch("fulcra_coord.inbox._load_inbox", return_value=[]), \
             patch("fulcra_coord.inbox._notify_new_needs_me"), \
             patch("fulcra_coord.selfupdate.maybe_self_update",
                   side_effect=RuntimeError("boom")):
            args = types.SimpleNamespace(agent="claude-code:h:r")
            self.assertEqual(cmd_notify_inbox(args, backend=["false"]), 0)


# ---------------------------------------------------------------------------
# announce-version — the maintainer's release-time publish
# ---------------------------------------------------------------------------

class TestAnnounceVersion(_CfgEnvBase):
    def _args(self, **kw):
        base = dict(min_supported=None, format="table")
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_uploads_validated_manifest_and_verifies(self):
        uploaded = {}
        with patch("fulcra_coord.selfupdate.remote") as rmock:
            rmock.version_manifest_path.return_value = "/coordination/runtime/version.json"
            rmock.upload_json.side_effect = (
                lambda data, path, backend=None: uploaded.update(data=data, path=path) or True)
            rmock.stat.return_value = {"size": 1, "hash": "x"}
            with patch("fulcra_coord.selfupdate._local_release_commit",
                       return_value="abc1234"):
                rc = selfupdate.cmd_announce_version(self._args(), backend=None)
        self.assertEqual(rc, 0)
        self.assertEqual(uploaded["path"], "/coordination/runtime/version.json")
        m = uploaded["data"]
        self.assertEqual(schema.validate_version_manifest(m), [])
        self.assertEqual(m["package_version"], __version__)
        self.assertEqual(m["release_commit"], "abc1234")
        # verify-after-write (the writepipe post-stat pattern)
        rmock.stat.assert_called_once()

    def test_upload_failure_returns_nonzero(self):
        with patch("fulcra_coord.selfupdate.remote") as rmock:
            rmock.version_manifest_path.return_value = "/x/runtime/version.json"
            rmock.upload_json.return_value = False
            rc = selfupdate.cmd_announce_version(self._args(), backend=None)
        self.assertNotEqual(rc, 0)

    def test_verify_failure_returns_nonzero(self):
        with patch("fulcra_coord.selfupdate.remote") as rmock:
            rmock.version_manifest_path.return_value = "/x/runtime/version.json"
            rmock.upload_json.return_value = True
            rmock.stat.return_value = None  # write landed nowhere
            rc = selfupdate.cmd_announce_version(self._args(), backend=None)
        self.assertNotEqual(rc, 0)

    def test_commit_lookup_is_best_effort(self):
        with patch("fulcra_coord.selfupdate.remote") as rmock:
            rmock.version_manifest_path.return_value = "/x/runtime/version.json"
            rmock.upload_json.return_value = True
            rmock.stat.return_value = {"size": 1}
            with patch("fulcra_coord.selfupdate._run_proc",
                       side_effect=OSError("git missing")):
                rc = selfupdate.cmd_announce_version(self._args(), backend=None)
        self.assertEqual(rc, 0)

    def test_cli_surface_registered(self):
        from fulcra_coord.entry import COMMAND_MAP, build_parser
        self.assertIn("announce-version", COMMAND_MAP)
        args = build_parser().parse_args(["announce-version"])
        self.assertEqual(args.command, "announce-version")


if __name__ == "__main__":
    unittest.main()

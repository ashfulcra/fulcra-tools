"""install-claude-code --with-wake: the adapter side of host wake-exec.

The core mechanism (fulcra_coord.wake, see test_host_wake.py) is platform-
neutral by pinned invariant — so the ONLY place a concrete agent-runtime
command may appear is per-adopter config. This adapter flag seeds that config:
``install-claude-code --with-wake`` merges a documented default entry for this
agent into ``${XDG_CONFIG_HOME:-~/.config}/fulcra-coord/wake.json`` and prints
a loud review note (the spawned command runs with the host's default
permissions — the operator must confirm it).

Merge semantics under test: other agents' entries are never clobbered, an
existing entry for THIS agent is preserved (the config file is the operator's
customization point — a reinstall must not undo their edits), uninstall
removes only this agent's entry, dry-run writes nothing.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fulcra_coord import claude_code, wake


class _ConfigEnvBase(unittest.TestCase):
    """Redirect XDG_CONFIG_HOME so the installer never touches the operator's
    real wake.json (same hazard rationale as the conftest isolation)."""

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

    def _config(self) -> dict:
        return json.loads(wake._wake_config_path().read_text())


class TestInstallWake(_ConfigEnvBase):
    AGENT = "claude-code:host1:repo"

    def test_writes_default_entry(self):
        plan = claude_code.install_wake(self.AGENT)
        self.assertFalse(plan["dry_run"])
        cfg = self._config()
        self.assertIn(self.AGENT, cfg)
        entry = cfg[self.AGENT]
        # Operator-decided default (2026-06-10): full-auto headless run — a
        # woken session that stalls on permission prompts is a notifier, not a
        # worker. The flag is removable per host in wake.json.
        self.assertEqual(entry["cmd"][0], "claude")
        self.assertEqual(entry["cmd"][1], "-p")
        self.assertEqual(entry["cmd"][2], "--dangerously-skip-permissions")
        self.assertIn(self.AGENT, entry["cmd"][3])
        self.assertIn("inbox", entry["cmd"][3])
        self.assertEqual(entry["cwd"], str(Path.cwd()))
        self.assertEqual(entry["min_interval_min"], 15)
        self.assertEqual(entry["max_runtime_s"], 900)
        self.assertTrue(entry["enabled"])

    def test_merges_without_clobbering_other_agents(self):
        path = wake._wake_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        other = {"codex:host1:": {"cmd": ["codex", "exec"], "enabled": True}}
        path.write_text(json.dumps(other))
        claude_code.install_wake(self.AGENT)
        cfg = self._config()
        self.assertEqual(cfg["codex:host1:"], other["codex:host1:"])
        self.assertIn(self.AGENT, cfg)

    def test_existing_entry_for_same_agent_is_preserved(self):
        """The config file is the operator's customization point: a reinstall
        must not overwrite their tuned cmd/interval with the defaults."""
        path = wake._wake_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        custom = {self.AGENT: {"cmd": ["/usr/local/bin/my-wrapper"],
                               "min_interval_min": 60, "enabled": False}}
        path.write_text(json.dumps(custom))
        plan = claude_code.install_wake(self.AGENT)
        self.assertTrue(plan["preserved"])
        self.assertEqual(self._config()[self.AGENT], custom[self.AGENT])

    def test_uninstall_removes_only_this_agents_entry(self):
        path = wake._wake_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            self.AGENT: {"cmd": ["claude", "-p", "x"], "enabled": True},
            "codex:host1:": {"cmd": ["codex", "exec"], "enabled": True},
        }))
        claude_code.install_wake(self.AGENT, uninstall=True)
        cfg = self._config()
        self.assertNotIn(self.AGENT, cfg)
        self.assertIn("codex:host1:", cfg)

    def test_dry_run_writes_nothing(self):
        plan = claude_code.install_wake(self.AGENT, dry_run=True)
        self.assertTrue(plan["dry_run"])
        self.assertIsNotNone(plan.get("would_write"))
        self.assertFalse(wake._wake_config_path().exists())

    def test_unparseable_config_backed_up_not_destroyed(self):
        path = wake._wake_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ operator typo")
        claude_code.install_wake(self.AGENT)
        # The agent's entry landed and the original bytes were preserved.
        self.assertIn(self.AGENT, self._config())
        bak = path.with_suffix(path.suffix + ".bak")
        self.assertEqual(bak.read_text(), "{ operator typo")


class TestCmdInstallClaudeCodeWithWake(_ConfigEnvBase):
    """The CLI surface: --with-wake runs in install/uninstall/dry-run modes and
    prints the loud review note. The hooks side is stubbed (it writes to real
    HOME); only the wake add-on is exercised for real."""

    AGENT = "claude-code:host1:repo"

    def _run(self, **kw):
        from fulcra_coord import installers
        ns = types.SimpleNamespace(scope="global", uninstall=False,
                                   dry_run=False, with_wake=True,
                                   agent=self.AGENT)
        for k, v in kw.items():
            setattr(ns, k, v)
        stub_plan = {"settings": "/dev/null", "hooks_dir": "/dev/null",
                     "events": ["SessionStart"], "scripts": []}
        out = io.StringIO()
        with patch("fulcra_coord.installers.claude_code.install_claude_code",
                   return_value=stub_plan), \
             contextlib.redirect_stdout(out):
            rc = installers.cmd_install_claude_code(ns)
        return rc, out.getvalue()

    def test_with_wake_writes_entry_and_prints_review_note(self):
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn(self.AGENT, self._config())
        # The loud post-install note: review the config — the spawned session
        # runs with the host's default permissions.
        self.assertIn("wake.json", out)
        self.assertIn("REVIEW", out.upper())
        self.assertIn("permission", out.lower())

    def test_with_wake_dry_run_writes_nothing(self):
        rc, out = self._run(dry_run=True)
        self.assertEqual(rc, 0)
        self.assertFalse(wake._wake_config_path().exists())
        self.assertIn("wake", out.lower())

    def test_with_wake_uninstall_removes_entry(self):
        claude_code.install_wake(self.AGENT)
        rc, _ = self._run(uninstall=True)
        self.assertEqual(rc, 0)
        self.assertNotIn(self.AGENT, self._config())

    def test_without_flag_never_touches_wake_config(self):
        rc, _ = self._run(with_wake=False)
        self.assertEqual(rc, 0)
        self.assertFalse(wake._wake_config_path().exists())


class TestWakeExampleConfig(unittest.TestCase):
    def test_example_file_ships_and_parses(self):
        root = Path(__file__).resolve().parents[1]
        p = root / "wake.example.json"
        self.assertTrue(p.exists(), "wake.example.json must ship in the repo")
        data = json.loads(p.read_text())
        self.assertIsInstance(data, dict)
        # Clearly marked as examples, with at least one well-formed entry.
        self.assertIn("EXAMPLE", data.get("_comment", "").upper())
        entries = [v for k, v in data.items()
                   if isinstance(v, dict) and "cmd" in v]
        self.assertGreaterEqual(len(entries), 2)
        for e in entries:
            self.assertIsInstance(e["cmd"], list)
            self.assertIn("cwd", e)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

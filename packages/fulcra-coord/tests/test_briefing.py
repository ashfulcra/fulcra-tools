"""The ``briefing`` subcommand — session-start consolidation (perf wave item 2).

THE MEASURED PROBLEM: the SessionStart hooks (Claude Code + the Codex twin) ran
identity + status + inbox + needs-me as FOUR separate CLI processes, each paying
its own ``views/summaries.json`` download (4 spawns + 4 view reads per session
start; under the stale-view guard each process could re-run the whole
direct-listing fallback independently — up to 4 repair-shaped bursts for one
hook fire).

THE FIX under test: one ``briefing`` subcommand resolves identity and folds the
status / inbox / needs-me sections from a SINGLE summaries load (the #173
summaries-threading idiom), and both hook scripts call it as their one
foreground CLI process. Deliberate degraded-mode benefit: at most ONE stale-view
fallback per session start instead of 3-4.

Pins here:
  * the combined JSON carries every section the hooks consume, shape-compatible
    with the individual commands' JSON outputs;
  * EXACTLY one summaries download on the happy path (the call-count pin);
  * both hook templates invoke ``briefing`` and no longer run the four-process
    shape (script-level assertions + an end-to-end render through bash).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

import pytest

from fulcra_coord import cli, remote, schema, views
from fulcra_coord.timeutil import now_iso


def _ns(**kw):
    base = {"format": "json", "agent": None}
    base.update(kw)
    return types.SimpleNamespace(**base)


def _capture_json(fn, args, backend):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(args, backend=backend)
    assert rc == 0
    return json.loads(buf.getvalue())


ME = "claude-code:testhost:testrepo"
HUMAN = "testhuman"


def _seed_bus(backend):
    """Three tasks exercising all three briefing sections:
    mine (active, owned by ME), a directive FOR ME (proposed, assigned to me by
    someone else), and an ask ON THE HUMAN (blocked, assigned to the human)."""
    mine = schema.make_task(title="my work", workstream="ws", agent=ME)
    mine["status"] = "active"

    directive = schema.make_task(title="do the thing", workstream="ws",
                                 agent="boss:h:r", assignee=ME)

    on_human = schema.make_task(title="approve deploy", workstream="ws",
                                agent="worker:h:r", assignee=HUMAN)
    on_human["status"] = "blocked"
    on_human["blocked_on"] = "approve it"

    tasks = [mine, directive, on_human]
    for t in tasks:
        remote.upload_json(t, remote.task_remote_path(t["id"]), backend=backend)
    # Materialize the summaries aggregate (fresh generated_at -> fast path).
    remote.upload_json(
        views.build_summaries(tasks),
        remote.view_remote_path("summaries"), backend=backend)
    return mine, directive, on_human


@pytest.fixture
def briefing_env(monkeypatch):
    monkeypatch.setenv("FULCRA_COORD_AGENT", ME)
    monkeypatch.setenv("FULCRA_COORD_HUMAN", HUMAN)


# ---------------------------------------------------------------------------
# Output sections
# ---------------------------------------------------------------------------

def test_briefing_json_carries_all_sections(coord_backend, briefing_env):
    mine, directive, on_human = _seed_bus(coord_backend)
    out = _capture_json(cli.cmd_briefing, _ns(), coord_backend)

    assert out["agent"] == ME
    assert out["human"] == HUMAN

    # status section == the build_index shape (what `status --format json` prints).
    active_ids = {t["id"] for t in out["status"]["active"]}
    assert mine["id"] in active_ids
    assert on_human["id"] in active_ids  # blocked tasks ride the active list

    # inbox section == the `inbox --format json` shape.
    assert out["inbox"]["agent"] == ME
    assert [i["id"] for i in out["inbox"]["inbox"]] == [directive["id"]]
    assert out["inbox"]["count"] == 1
    assert "hidden_aged" in out["inbox"]

    # needs_me section == the `needs-me --format json` shape.
    assert out["needs_me"]["human"] == HUMAN
    assert [i["id"] for i in out["needs_me"]["items"]] == [on_human["id"]]
    assert out["needs_me"]["count"] == 1
    assert "upcoming" in out["needs_me"]


def test_briefing_sections_agree_with_individual_commands(coord_backend,
                                                          briefing_env):
    """The hooks swapped four command outputs for the briefing's sections —
    so each section must agree with the command it replaced (same folds over
    the same summaries; only the process count changed)."""
    _seed_bus(coord_backend)
    b = _capture_json(cli.cmd_briefing, _ns(), coord_backend)
    status = _capture_json(cli.cmd_status, _ns(workstream=None), coord_backend)
    inbox = _capture_json(cli.cmd_inbox, _ns(ack=None, all=False), coord_backend)
    needs = _capture_json(cli.cmd_needs_me, _ns(human=None, all=False),
                          coord_backend)

    assert ({t["id"] for t in b["status"]["active"]}
            == {t["id"] for t in status["active"]})
    assert b["status"]["counts"] == status["counts"]
    assert b["inbox"] == inbox
    assert b["needs_me"] == needs


def test_briefing_empty_bus_is_empty_not_an_error(coord_backend, briefing_env):
    out = _capture_json(cli.cmd_briefing, _ns(), coord_backend)
    assert out["status"]["active"] == []
    assert out["inbox"]["inbox"] == []
    assert out["needs_me"]["items"] == []


# ---------------------------------------------------------------------------
# THE call-count pin: one summaries load per briefing
# ---------------------------------------------------------------------------

def test_briefing_loads_summaries_exactly_once(coord_backend, briefing_env):
    """The whole point of the consolidation: identity + status + inbox +
    needs-me from ONE views/summaries.json download (was 4, one per process),
    zero task-body fetches, zero listings, and none of the N+3 id-seeding
    views (index / next / search-index)."""
    _seed_bus(coord_backend)
    downloads: list[str] = []
    lists: list[str] = []
    real_dl, real_list = remote.download_json, remote.list_files

    def download_json(path, **kw):
        downloads.append(path)
        return real_dl(path, **kw)

    def list_files(prefix, **kw):
        lists.append(prefix)
        return real_list(prefix, **kw)

    with mock.patch.multiple(remote, download_json=download_json,
                             list_files=list_files):
        out = _capture_json(cli.cmd_briefing, _ns(), coord_backend)

    assert out["inbox"]["count"] == 1  # the load actually fed all sections
    summaries_path = remote.view_remote_path("summaries")
    assert downloads.count(summaries_path) == 1, (
        f"summaries downloaded {downloads.count(summaries_path)}x per "
        "briefing — every section must share the one load")
    tasks_prefix = f"{remote.remote_root()}/tasks/"
    assert [p for p in downloads if p.startswith(tasks_prefix)] == []
    assert lists == []
    for view in ("index", "next", "search-index"):
        assert remote.view_remote_path(view) not in downloads


# ---------------------------------------------------------------------------
# Hook scripts call the new command (script-level pins + bash E2E)
# ---------------------------------------------------------------------------

class TestHooksUseBriefing(unittest.TestCase):
    def test_claude_code_session_start_calls_briefing_once(self):
        from fulcra_coord import claude_code as cc
        body = cc.SESSION_START_SH
        self.assertIn('briefing --format json', body)
        # The four-process shape must be gone: status/inbox/needs-me/identity
        # are no longer separate foreground CLI calls.
        for old_call in ('" status --format json', '" inbox --format json',
                         '" needs-me --format json', '" identity --format json'):
            self.assertNotIn(old_call.replace('" ', '"${FULCRA_COORD[@]}" '),
                             body, f"hook still runs the old call: {old_call}")
        self.assertNotIn('"${FULCRA_COORD[@]}" status', body)
        self.assertNotIn('"${FULCRA_COORD[@]}" inbox', body)
        self.assertNotIn('"${FULCRA_COORD[@]}" needs-me', body)
        self.assertNotIn('"${FULCRA_COORD[@]}" identity', body)
        # I1: the briefing call must not pin --agent (it would override a
        # persisted identity, same reasoning as the old inbox call).
        self.assertNotIn("briefing --agent", body)
        # The background presence connect is still present, now via a shell-safe
        # flag array so installers can bake role declarations into the hook.
        self.assertIn('CONNECT_FLAGS=(__FULCRA_COORD_CONNECT_FLAGS__)', body)
        self.assertIn('"${FULCRA_COORD[@]}" connect "${CONNECT_FLAGS[@]}" '
                      '>/dev/null 2>&1 &', body)

    def test_codex_session_start_calls_briefing_with_codex_transforms(self):
        from fulcra_coord import codex
        body = codex.SESSION_START_SH
        self.assertIn('briefing --format json', body)
        self.assertNotIn('"${FULCRA_COORD[@]}" status', body)
        # Codex transforms survive: review-capability connect + watch re-arm +
        # codex-derived fallback id.
        self.assertIn("CONNECT_FLAGS=(--can-review", body)
        self.assertIn('"${FULCRA_COORD[@]}" connect "${CONNECT_FLAGS[@]}"',
                      body)
        self.assertIn("ensure-codex-watch", body)
        self.assertIn('AGENT="codex:${HOST}:${REPO}"', body)


class TestSessionStartBriefingE2E(unittest.TestCase):
    """End-to-end through bash: the rendered hook drives everything off ONE
    `briefing` call — banner sections, title, fail-safe empty exit."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.briefing_json = os.path.join(self.tmp, "briefing.json")
        self.args_log = os.path.join(self.tmp, "args.log")
        fake = os.path.join(self.bin, "fulcra-coord")
        with open(fake, "w") as f:
            f.write("#!/usr/bin/env bash\n"
                    'echo "$@" >> "%s"\n'
                    'if [ "$1" = "briefing" ]; then cat "%s" 2>/dev/null; exit 0; fi\n'
                    'exit 0\n' % (self.args_log, self.briefing_json))
        os.chmod(fake, 0o755)
        from fulcra_coord.cli_invocation import PLACEHOLDER_ARGV, materialize_argv
        from fulcra_coord import claude_code as cc
        self.hook = os.path.join(self.tmp, "session-start.sh")
        with open(self.hook, "w") as f:
            f.write(cc.SESSION_START_SH.replace(
                PLACEHOLDER_ARGV, materialize_argv([fake])))
        os.chmod(self.hook, 0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, briefing: str):
        with open(self.briefing_json, "w") as f:
            f.write(briefing)
        env = dict(os.environ)
        env["PATH"] = self.bin + os.pathsep + env["PATH"]
        return subprocess.run(["bash", self.hook],
                              input=json.dumps({"cwd": self.tmp}),
                              capture_output=True, text=True, env=env)

    def test_briefing_drives_all_banner_sections(self):
        briefing = json.dumps({
            "agent": "declared:custom:id",
            "human": "ash",
            "status": {"active": [
                {"id": "TASK-mine", "title": "my work", "status": "active",
                 "owner_agent": "declared:custom:id",
                 "updated_at": now_iso(), "next_action": "do X"}]},
            "inbox": {"agent": "declared:custom:id", "count": 1,
                      "hidden_aged": 0, "inbox": [
                          {"id": "TASK-dir", "title": "Migrate",
                           "owner_agent": "boss:h:r",
                           "next_action": "run the migration"}]},
            "needs_me": {"human": "ash", "count": 1, "items": [
                {"id": "TASK-ask", "title": "approve deploy",
                 "status": "blocked", "owner_agent": "worker:h:r",
                 "blocked_on": "approve it", "next_action": "",
                 "updated_at": "2026-06-01T00:00:00Z"}],
                "upcoming": []},
        })
        r = self._run(briefing)
        self.assertEqual(r.returncode, 0)
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        # All three sections render, blocked-on-you leading.
        self.assertTrue(ctx.startswith("⛔ BLOCKED ON YOU"))
        self.assertIn("TASK-mine", ctx)
        self.assertIn("Directives for you", ctx)
        self.assertIn("TASK-dir", ctx)
        # Title = first active task owned by the briefing-resolved agent.
        self.assertEqual(
            json.loads(r.stdout)["hookSpecificOutput"]["sessionTitle"],
            "my work")
        # The declared (CLI-resolved) id drives the mine filter + resume hint.
        self.assertIn("--agent declared:custom:id", ctx)
        # ONE briefing call, no --agent pinned, and none of the old four calls.
        calls = open(self.args_log).read().splitlines()
        briefing_calls = [c for c in calls if c.startswith("briefing")]
        self.assertEqual(len(briefing_calls), 1, calls)
        self.assertNotIn("--agent", briefing_calls[0])
        for old in ("status", "inbox", "needs-me", "identity"):
            self.assertFalse(
                any(c.split() and c.split()[0] == old for c in calls),
                f"hook still spawned `{old}`: {calls}")

    def test_silent_clean_exit_on_empty_briefing_output(self):
        # Old/missing CLI (no briefing subcommand) -> empty output -> the
        # fail-safe contract: exit 0, inject nothing.
        r = self._run("")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

    def test_silent_when_bus_clean(self):
        briefing = json.dumps({
            "agent": "declared:custom:id", "human": "ash",
            "status": {"active": []},
            "inbox": {"agent": "declared:custom:id", "count": 0,
                      "hidden_aged": 0, "inbox": []},
            "needs_me": {"human": "ash", "count": 0, "items": [],
                         "upcoming": []},
        })
        r = self._run(briefing)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

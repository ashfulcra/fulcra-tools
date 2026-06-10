"""Continuity integration tests (spec 2026-06-10-continuity-integration-design).

The integration makes a continuity checkpoint a PAYLOAD REF on coordination
primitives instead of a side-tree only retention knows about:

  * ``handoff`` — a kind=dispatch loop whose payload carries ``checkpoint_ref``
    (the recipient resumes from the ref, then closes the loop = the work
    continued);
  * ``checkpoint --role`` — the role registry's ``checkpoint_ref`` is the
    role's durable "where I left off", surviving every session death
    (roles phase 2: claim → resume);
  * ``park`` — the best-effort session-exit hook body: checkpoint every held
    role via the OPTIONAL ``fulcra-continuity`` CLI, never blocking exit.

TWO NON-NEGOTIABLE PINS (the decoupling the spec demands):

  1. coord NEVER imports ``fulcra_continuity`` — the checkpoint schema is
     owned by that package; coord touches checkpoints only as opaque JSON
     blobs (its own stdlib bridge) or through the CLI as a subprocess.
  2. refs are OPAQUE STRINGS — coord stores and forwards them verbatim,
     never parsing structure out of them (no schema coupling).

STORAGE REALITY (verified in implementation, 2026-06-10): the standalone
``fulcra-continuity`` CLI writes LOCAL files only, but coord's own bridge
(``fulcra_coord.continuity``) already uploads the same checkpoint JSON shape
to the REMOTE ``{root}/continuity/...`` bus tree (the tree the retention
walker prunes). So a handoff that is given a LOCAL checkpoint file publishes
it to the remote tree first and carries the REMOTE archive path as the ref —
cross-host resume works; the inline-payload fallback only fires when that
publish fails.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from unittest.mock import patch

from fulcra_coord import cli, continuity, remote, schema


def _ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def _handoff_args(**overrides) -> argparse.Namespace:
    base = dict(
        to="arcbot:hetzner:openclaw",
        title="Carry on the migration",
        summary="",
        next="",
        workstream="general",
        priority="P2",
        checkpoint=None,
        format="table",
    )
    base["from"] = "claude-code:mbp:fulcra-tools"
    base.update(overrides)
    return _ns(**base)


def _read_directives(coord_backend) -> list[dict]:
    """Every top-level directive record currently in the fake store."""
    prefix = remote.directives_prefix()
    out = []
    for path, rec in remote.list_json(prefix, backend=coord_backend):
        rel = path[len(prefix):] if path.startswith(prefix) else path
        if "/" in rel or not rel.endswith(".json"):
            continue
        out.append(rec)
    return out


class TestHandoffCreatesDispatchLoopWithRef:
    def test_handoff_creates_open_dispatch_loop_carrying_checkpoint_ref(
            self, coord_backend, monkeypatch):
        monkeypatch.setenv("FULCRA_COORD_BACKEND", " ".join(coord_backend))
        ref = "/coordination/continuity/ws-x/agent-y/task-z/checkpoints/chk-1.json"
        rc = cli.cmd_handoff(_handoff_args(checkpoint=ref),
                             backend=coord_backend)
        assert rc == 0
        loops_ = _read_directives(coord_backend)
        assert len(loops_) == 1
        loop = loops_[0]
        assert loop["kind"] == "dispatch"
        assert loop["expects_response"] is True
        assert loop["state"] == "assigned"
        assert loop["audience"] == "arcbot:hetzner:openclaw"
        assert loop["checkpoint_ref"] == ref
        # The authoritative task carries the ref too (the loop record mirrors it).
        task = remote.download_json(
            remote.task_remote_path(loop["task_id"]), backend=coord_backend)
        assert task["checkpoint_ref"] == ref

    def test_handoff_without_checkpoint_still_opens_a_dispatch_loop(
            self, coord_backend, monkeypatch):
        monkeypatch.setenv("FULCRA_COORD_BACKEND", " ".join(coord_backend))
        rc = cli.cmd_handoff(_handoff_args(), backend=coord_backend)
        assert rc == 0
        (loop,) = _read_directives(coord_backend)
        assert loop["kind"] == "dispatch"
        assert loop.get("checkpoint_ref") is None

    def test_handoff_requires_a_recipient(self, coord_backend, capsys):
        rc = cli.cmd_handoff(_handoff_args(to=None), backend=coord_backend)
        assert rc == 1
        assert "--to" in capsys.readouterr().err

    def test_handoff_to_role_audience_passes_through(self, coord_backend,
                                                     monkeypatch):
        monkeypatch.setenv("FULCRA_COORD_BACKEND", " ".join(coord_backend))
        rc = cli.cmd_handoff(_handoff_args(to="@arc-maintainer"),
                             backend=coord_backend)
        assert rc == 0
        (loop,) = _read_directives(coord_backend)
        assert loop["audience"] == "@arc-maintainer"


class TestRefsAreOpaqueStrings:
    """PIN: coord must forward a ref VERBATIM — no parsing, no validation, no
    schema knowledge. Any weird string that is not a local file must land in
    the payload byte-for-byte."""

    def test_arbitrary_opaque_ref_lands_verbatim(self, coord_backend,
                                                 monkeypatch):
        monkeypatch.setenv("FULCRA_COORD_BACKEND", " ".join(coord_backend))
        ref = "weird-scheme://no/such/structure?x=1#frag — not a path at all"
        rc = cli.cmd_handoff(_handoff_args(checkpoint=ref),
                             backend=coord_backend)
        assert rc == 0
        (loop,) = _read_directives(coord_backend)
        assert loop["checkpoint_ref"] == ref


class TestLocalCheckpointFilePublishesToRemoteTree:
    """STORAGE REALITY: the fulcra-continuity CLI writes local files; the bus
    tree is coord's own bridge. A handoff handed a LOCAL checkpoint file must
    publish it to the remote ``continuity/...`` tree and carry the REMOTE
    archive path as the ref (cross-host resume), with inline-payload fallback
    when the publish fails."""

    def _local_checkpoint(self, tmp_path: Path) -> Path:
        ckpt = {
            "schema_version": "fulcra.continuity.checkpoint.v1",
            "checkpoint_id": "CHK-20260610T010203z-demo-abcd1234",
            "task_id": "TASK-20260610-demo-00000000",
            "title": "Demo",
            "objective": "Resume the demo",
            "created_at": "2026-06-10T01:02:03Z",
            "identity": {
                "workstream_id": "general",
                "agent_id": "claude-code:mbp:fulcra-tools",
                "coord_task_id": "TASK-20260610-demo-00000000",
                "coord_owner_agent": "claude-code:mbp:fulcra-tools",
            },
            "next_actions": ["continue"],
        }
        p = tmp_path / "checkpoint.json"
        p.write_text(json.dumps(ckpt), encoding="utf-8")
        return p

    def test_local_file_ref_becomes_remote_archive_path(
            self, coord_backend, monkeypatch, tmp_path):
        monkeypatch.setenv("FULCRA_COORD_BACKEND", " ".join(coord_backend))
        local = self._local_checkpoint(tmp_path)
        rc = cli.cmd_handoff(_handoff_args(checkpoint=str(local)),
                             backend=coord_backend)
        assert rc == 0
        (loop,) = _read_directives(coord_backend)
        ref = loop["checkpoint_ref"]
        assert "/continuity/" in ref
        assert "/checkpoints/" in ref            # immutable archive, not latest
        assert ref != str(local)                 # no local path on the bus
        # The body actually landed at the ref — the other host can fetch it.
        body = remote.download_json(ref, backend=coord_backend)
        assert body["checkpoint_id"] == "CHK-20260610T010203z-demo-abcd1234"

    def test_publish_failure_falls_back_to_inline_payload(
            self, coord_backend, monkeypatch, tmp_path):
        monkeypatch.setenv("FULCRA_COORD_BACKEND", " ".join(coord_backend))
        local = self._local_checkpoint(tmp_path)
        with patch("fulcra_coord.continuity.publish_checkpoint_file",
                   return_value=(None, json.loads(local.read_text()))):
            rc = cli.cmd_handoff(_handoff_args(checkpoint=str(local)),
                                 backend=coord_backend)
        assert rc == 0
        (loop,) = _read_directives(coord_backend)
        # Small docs ride inline when the remote tree is unreachable, so the
        # recipient can still resume; the local path stays as provenance.
        assert loop["checkpoint_ref"] == str(local)
        assert loop["checkpoint_inline"]["checkpoint_id"] == \
            "CHK-20260610T010203z-demo-abcd1234"


class TestPickupSurfacesTheRef:
    """The recipient's claim (``update --status active``) must surface the ref
    — and, when the OPTIONAL fulcra-continuity CLI is installed, the rendered
    resume brief. Never a hard dependency: a missing CLI degrades to the bare
    ref line."""

    def _claimable_task(self) -> dict:
        task = schema.make_task(
            title="Carry on the migration", workstream="general",
            agent="claude-code:mbp:fulcra-tools",
            owner_agent="claude-code:mbp:fulcra-tools",
            assignee="arcbot:hetzner:openclaw")
        task["checkpoint_ref"] = "/coordination/continuity/x/y/z/checkpoints/c.json"
        return task

    def test_claim_prints_checkpoint_ref(self, coord_backend, capsys):
        task = self._claimable_task()
        args = _ns(task_id=task["id"], summary=None, next=None,
                   blocked_on=None, status="active",
                   agent="arcbot:hetzner:openclaw")
        with patch("fulcra_coord.lifecycle._load_task", return_value=task), \
             patch("fulcra_coord.lifecycle._write_task_and_views",
                   return_value=True), \
             patch("fulcra_coord.continuity.render_brief_for_ref",
                   return_value=None):
            rc = cli.cmd_update(args, backend=coord_backend)
        out = capsys.readouterr().out
        assert rc == 0
        assert task["checkpoint_ref"] in out

    def test_claim_renders_brief_when_continuity_cli_present(
            self, coord_backend, capsys):
        task = self._claimable_task()
        args = _ns(task_id=task["id"], summary=None, next=None,
                   blocked_on=None, status="active",
                   agent="arcbot:hetzner:openclaw")
        with patch("fulcra_coord.lifecycle._load_task", return_value=task), \
             patch("fulcra_coord.lifecycle._write_task_and_views",
                   return_value=True), \
             patch("fulcra_coord.continuity.render_brief_for_ref",
                   return_value="Resume brief for TASK-x\nNext: continue\n"):
            rc = cli.cmd_update(args, backend=coord_backend)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Resume brief for TASK-x" in out

    def test_brief_render_failure_never_fails_the_claim(self, coord_backend):
        task = self._claimable_task()
        args = _ns(task_id=task["id"], summary=None, next=None,
                   blocked_on=None, status="active",
                   agent="arcbot:hetzner:openclaw")
        with patch("fulcra_coord.lifecycle._load_task", return_value=task), \
             patch("fulcra_coord.lifecycle._write_task_and_views",
                   return_value=True), \
             patch("fulcra_coord.continuity.render_brief_for_ref",
                   side_effect=RuntimeError("boom")):
            rc = cli.cmd_update(args, backend=coord_backend)
        assert rc == 0

    def test_inbox_lists_checkpoint_ref_on_the_directive(self, capsys):
        summary = {
            "id": "TASK-20260610-hand-00000000", "title": "Carry on",
            "priority": "P2", "owner_agent": "a", "next_action": "",
            "checkpoint_ref": "/coordination/continuity/x/y/z/checkpoints/c.json",
        }
        args = _ns(agent="arcbot:hetzner:openclaw", format="table",
                   ack=None, all=False)
        with patch("fulcra_coord.inbox._load_task_summaries",
                   return_value=[]), \
             patch("fulcra_coord.inbox._my_roles", return_value=set()), \
             patch("fulcra_coord.inbox.views.inbox_for",
                   return_value=[summary]), \
             patch("fulcra_coord.inbox.views.aged_out_inbox_count",
                   return_value=0):
            rc = cli.cmd_inbox(args)
        out = capsys.readouterr().out
        assert rc == 0
        assert summary["checkpoint_ref"] in out


class TestSummaryCarriesRef:
    def test_task_summary_carries_checkpoint_ref_and_is_idempotent(self):
        task = schema.make_task(title="t", workstream="ws", agent="a")
        task["checkpoint_ref"] = "opaque-ref-123"
        s = schema.task_summary(task)
        assert s["checkpoint_ref"] == "opaque-ref-123"
        assert schema.task_summary(s)["checkpoint_ref"] == "opaque-ref-123"


class TestRoleCheckpoint:
    """``checkpoint --role X [--ref R]`` — the role registry's checkpoint_ref
    is the role's durable "where I left off" (roles phase 2). It must update
    ONLY that field: a role's runbook/SLA/maintainer survive every
    checkpoint."""

    def _seed_role(self, coord_backend) -> dict:
        from fulcra_coord import role_ops
        rec = schema.make_role(
            "arc-maintainer", "Keeps Arc healthy",
            standing_instructions="Read the runbook; check the heartbeat.",
            policy="exclusive", sla_hours=24, maintainer="ash")
        assert role_ops.upsert_role(rec, backend=coord_backend)
        return rec

    def test_checkpoint_role_round_trip_preserves_registry_fields(
            self, coord_backend):
        from fulcra_coord import role_ops
        before = self._seed_role(coord_backend)
        args = _ns(role="arc-maintainer", ref="opaque-chk-ref-1",
                   format="table")
        rc = cli.cmd_checkpoint(args, backend=coord_backend)
        assert rc == 0
        after = role_ops.read_role("arc-maintainer", backend=coord_backend)
        assert after["checkpoint_ref"] == "opaque-chk-ref-1"
        for field in ("description", "standing_instructions", "policy",
                      "sla_hours", "maintainer", "created_at"):
            assert after[field] == before[field], field

    def test_checkpoint_unregistered_role_self_registers(self, coord_backend):
        from fulcra_coord import role_ops
        args = _ns(role="fresh-role", ref="opaque-chk-ref-2", format="table")
        rc = cli.cmd_checkpoint(args, backend=coord_backend)
        assert rc == 0
        rec = role_ops.read_role("fresh-role", backend=coord_backend)
        assert rec["checkpoint_ref"] == "opaque-chk-ref-2"

    def test_checkpoint_without_ref_shows_current(self, coord_backend, capsys):
        self._seed_role(coord_backend)
        cli.cmd_checkpoint(_ns(role="arc-maintainer", ref="the-ref",
                               format="table"), backend=coord_backend)
        capsys.readouterr()
        rc = cli.cmd_checkpoint(_ns(role="arc-maintainer", ref=None,
                                    format="table"), backend=coord_backend)
        out = capsys.readouterr().out
        assert rc == 0
        assert "the-ref" in out

    def test_roles_set_preserves_checkpoint_ref(self, coord_backend, capsys):
        """Tightening one knob via `roles set` must never wipe the role's
        resume point (the _pick/preserve contract extended to this field)."""
        from fulcra_coord import role_ops
        self._seed_role(coord_backend)
        cli.cmd_checkpoint(_ns(role="arc-maintainer", ref="keep-me",
                               format="table"), backend=coord_backend)
        args = _ns(roles_action="set", name="arc-maintainer",
                   description=None, instructions=None, policy=None,
                   sla_hours=48, maintainer=None, format="table")
        rc = cli.cmd_roles(args, backend=coord_backend)
        assert rc == 0
        after = role_ops.read_role("arc-maintainer", backend=coord_backend)
        assert after["sla_hours"] == 48
        assert after["checkpoint_ref"] == "keep-me"


class TestClaimAndConnectResume:
    """Role claim → resume: when the claimed role's registry record carries a
    checkpoint_ref, both lease paths (`roles claim` and `connect --role`)
    print the ref + best-effort rendered brief — the where-I-left-off that
    survives session death."""

    def _role_with_ref(self, coord_backend) -> None:
        from fulcra_coord import role_ops
        rec = schema.make_role("arc-maintainer", "Keeps Arc healthy",
                               checkpoint_ref="role-resume-ref-9")
        assert role_ops.upsert_role(rec, backend=coord_backend)

    def test_roles_claim_prints_ref_and_brief(self, coord_backend, capsys):
        self._role_with_ref(coord_backend)
        args = _ns(roles_action="claim", name="arc-maintainer",
                   agent="arcbot:hetzner:openclaw", format="table")
        with patch("fulcra_coord.continuity.render_brief_for_ref",
                   return_value="Resume brief for ROLE-arc\n"):
            rc = cli.cmd_roles(args, backend=coord_backend)
        out = capsys.readouterr().out
        assert rc == 0
        assert "role-resume-ref-9" in out
        assert "Resume brief for ROLE-arc" in out

    def test_roles_claim_without_ref_prints_no_resume(self, coord_backend,
                                                      capsys):
        from fulcra_coord import role_ops
        role_ops.upsert_role(schema.make_role("plain-role", "no resume"),
                             backend=coord_backend)
        args = _ns(roles_action="claim", name="plain-role",
                   agent="arcbot:hetzner:openclaw", format="table")
        rc = cli.cmd_roles(args, backend=coord_backend)
        out = capsys.readouterr().out
        assert rc == 0
        assert "checkpoint" not in out.lower()

    def test_claim_resume_failure_never_fails_the_claim(self, coord_backend):
        self._role_with_ref(coord_backend)
        args = _ns(roles_action="claim", name="arc-maintainer",
                   agent="arcbot:hetzner:openclaw", format="table")
        with patch("fulcra_coord.continuity.render_brief_for_ref",
                   side_effect=RuntimeError("boom")):
            rc = cli.cmd_roles(args, backend=coord_backend)
        assert rc == 0

    def test_connect_with_role_prints_ref(self, coord_backend, capsys):
        self._role_with_ref(coord_backend)
        args = _ns(agent="arcbot:hetzner:openclaw", format="table",
                   summary="", workstream=None, role=["arc-maintainer"],
                   can_review=False)
        with patch("fulcra_coord.continuity.render_brief_for_ref",
                   return_value=None):
            rc = cli.cmd_connect(args, backend=coord_backend)
        out = capsys.readouterr().out
        assert rc == 0
        assert "role-resume-ref-9" in out


class TestParkIsBestEffort:
    """``park`` — the PreCompact/SessionEnd hook body: checkpoint every held
    role via the OPTIONAL fulcra-continuity CLI and point the role's
    checkpoint_ref at the published ref. CONTRACT: never blocks or fails
    session exit — always exit 0, silent no-op when continuity is missing or
    no role is held."""

    def _hold_role(self, coord_backend, agent="arcbot:hetzner:openclaw",
                   role="arc-maintainer"):
        from fulcra_coord import presence
        rec = schema.make_presence(agent, capabilities=[role])
        assert presence._write_presence(rec, backend=coord_backend)
        return agent, role

    def test_park_with_continuity_missing_is_silent_noop(
            self, coord_backend, capsys):
        from fulcra_coord import role_ops
        agent, role = self._hold_role(coord_backend)
        # NB: assert on the CLI-invocation helper, not subprocess.run —
        # subprocess is a process-global module shared with the fake-backend
        # transport, so patching .run would intercept the bus I/O too.
        with patch("fulcra_coord.continuity_ops.shutil.which",
                   return_value=None), \
             patch("fulcra_coord.continuity_ops._write_role_checkpoint_via_cli") as run:
            rc = cli.cmd_park(_ns(agent=agent, summary=""),
                              backend=coord_backend)
        assert rc == 0
        run.assert_not_called()
        assert capsys.readouterr().out == ""
        assert role_ops.read_role(role, backend=coord_backend) is None

    def test_park_with_no_held_roles_is_silent_noop(self, coord_backend,
                                                    capsys):
        with patch("fulcra_coord.continuity_ops.shutil.which") as which:
            rc = cli.cmd_park(_ns(agent="nobody:host:repo", summary=""),
                              backend=coord_backend)
        assert rc == 0
        which.assert_not_called()   # roles probed BEFORE the CLI probe
        assert capsys.readouterr().out == ""

    def test_park_checkpoints_each_held_role_and_updates_its_ref(
            self, coord_backend, capsys):
        from fulcra_coord import role_ops
        agent, role = self._hold_role(coord_backend)
        checkpoint = {
            "schema_version": "fulcra.continuity.checkpoint.v1",
            "checkpoint_id": "CHK-park-1",
            "identity": {"workstream_id": role, "agent_id": agent,
                         "coord_task_id": f"ROLE-{role}",
                         "coord_owner_agent": agent},
        }
        with patch("fulcra_coord.continuity_ops.shutil.which",
                   return_value="/usr/local/bin/fulcra-continuity"), \
             patch("fulcra_coord.continuity_ops._write_role_checkpoint_via_cli",
                   return_value=checkpoint):
            rc = cli.cmd_park(_ns(agent=agent, summary=""),
                              backend=coord_backend)
        assert rc == 0
        rec = role_ops.read_role(role, backend=coord_backend)
        ref = rec["checkpoint_ref"]
        assert "/continuity/" in ref and "/checkpoints/" in ref
        # The checkpoint body actually landed at the ref (cross-host resume).
        assert remote.download_json(ref, backend=coord_backend)[
            "checkpoint_id"] == "CHK-park-1"
        assert f"Parked role '{role}'" in capsys.readouterr().out

    def test_park_never_exits_nonzero_even_when_everything_raises(
            self, coord_backend):
        agent, _role = self._hold_role(coord_backend)
        with patch("fulcra_coord.continuity_ops.shutil.which",
                   return_value="/usr/local/bin/fulcra-continuity"), \
             patch("fulcra_coord.continuity_ops._write_role_checkpoint_via_cli",
                   side_effect=RuntimeError("boom")):
            rc = cli.cmd_park(_ns(agent=agent, summary=""),
                              backend=coord_backend)
        assert rc == 0

    def test_park_cli_failure_skips_role_without_touching_its_ref(
            self, coord_backend):
        from fulcra_coord import role_ops
        agent, role = self._hold_role(coord_backend)
        role_ops.upsert_role(
            schema.make_role(role, "x", checkpoint_ref="prior-ref"),
            backend=coord_backend)
        with patch("fulcra_coord.continuity_ops.shutil.which",
                   return_value="/usr/local/bin/fulcra-continuity"), \
             patch("fulcra_coord.continuity_ops._write_role_checkpoint_via_cli",
                   return_value=None):
            rc = cli.cmd_park(_ns(agent=agent, summary=""),
                              backend=coord_backend)
        assert rc == 0
        rec = role_ops.read_role(role, backend=coord_backend)
        assert rec["checkpoint_ref"] == "prior-ref"   # a failed park keeps
        # the last good resume point rather than clobbering it with nothing.


class TestParkRidesTheSessionExitHooks:
    """The claude-code adapter's PreCompact + SessionEnd hooks must carry the
    best-effort park line — BACKGROUNDED (&) so it can never block session
    exit, and BEFORE the session-task early-exits so a session that holds a
    role but owns no coord task still parks."""

    def test_hooks_carry_a_backgrounded_park_line(self):
        from fulcra_coord import claude_code as cc
        for body in (cc.PRE_COMPACT_SH, cc.SESSION_END_SH):
            park_lines = [ln for ln in body.splitlines() if " park " in ln]
            assert park_lines, "hook is missing the park line"
            assert any(ln.rstrip().endswith("&") for ln in park_lines), \
                "park must be backgrounded — it may never block session exit"

    def test_park_runs_before_the_session_task_early_exit(self):
        from fulcra_coord import claude_code as cc
        for body in (cc.PRE_COMPACT_SH, cc.SESSION_END_SH):
            park_at = body.index(" park ")
            task_exit_at = body.index('[ -z "$TASK" ] && exit 0')
            assert park_at < task_exit_at, (
                "park must fire even when the session has no coord task — "
                "holding a role is enough")


class TestCoordNeverImportsFulcraContinuity:
    """PIN: the checkpoint schema belongs to the fulcra-continuity package.
    coord may subprocess its CLI; it may NEVER import it (an import couples
    coord to the schema's Python types and breaks independent install).
    AST scan (not grep) so docstring mentions don't false-positive."""

    def test_no_module_imports_fulcra_continuity(self):
        pkg_dir = Path(continuity.__file__).resolve().parent
        offenders = []
        for py in sorted(pkg_dir.glob("*.py")):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(a.name.split(".")[0] == "fulcra_continuity"
                           for a in node.names):
                        offenders.append(py.name)
                elif isinstance(node, ast.ImportFrom):
                    if (node.module or "").split(".")[0] == "fulcra_continuity":
                        offenders.append(py.name)
        assert offenders == [], (
            f"fulcra_coord modules import fulcra_continuity: {offenders}. "
            "coord stores REFS and shells out to the CLI; it never imports "
            "the package (spec 2026-06-10, schema ownership).")

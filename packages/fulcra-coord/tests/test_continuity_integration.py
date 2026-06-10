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

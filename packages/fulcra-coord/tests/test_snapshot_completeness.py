"""Fitness test — the dual-write snapshot event must carry a FULL-task deep-copy.

THE INVARIANT (the safety this guards):

``events.fold_task`` treats a payload with a truthy ``schema`` + ``id`` as a
full-task SNAPSHOT and does ``state = dict(payload)`` — it REPLACES the
accumulated state wholesale, dropping every field from pre-snapshot events as
"stale" (see ``events._is_snapshot_payload`` / ``fold_task`` rule 3). That
wholesale replace is only correct if a snapshot really does carry the COMPLETE
task. There is no runtime guard enforcing this — the fold simply trusts that
the sole snapshot emitter writes the full task.

So the invariant has to live at the SOURCE: the only place that emits a
snapshot event is ``writepipe._write_task_and_views``, and it must build the
event payload from a deep-copy of the WHOLE task (``copy.deepcopy(task)``), not
a hand-picked field subset. If a future change ever starts snapshotting a
partial dict (``payload={"status": ...}``), the fold would silently reconstruct
a task missing every other field while ``fold_is_complete`` still returns True —
a silent correctness hazard. This test trips on exactly that change.

WHY an AST scan and not a grep: ``writepipe.py`` legitimately *mentions*
``copy.deepcopy`` elsewhere (the optimistic-merge path deep-copies remote/newer
tasks) and the docstrings discuss snapshots in prose. A grep can't tell the
snapshot ``make_event(... payload=copy.deepcopy(task) ...)`` call apart from
those. We parse the module and inspect the actual ``make_event`` call node —
specifically its ``payload=`` keyword — so only the real construction is
asserted on. Mirrors the AST-scan idiom of ``test_layering_boundaries.py`` and
``test_forge_agnostic.py``.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from fulcra_coord import continuity


def _writepipe_source() -> str:
    pkg_dir = Path(__file__).resolve().parent.parent / "fulcra_coord"
    path = pkg_dir / "writepipe.py"
    assert path.exists(), f"writepipe.py not found at {path}"
    return path.read_text(encoding="utf-8")


def _payload_is_full_task_deepcopy(payload_node: ast.AST) -> bool:
    """True iff *payload_node* is the expression ``copy.deepcopy(task)``.

    Matches the call ``copy.deepcopy(<Name>)`` where the single positional arg is
    the bare name ``task`` — i.e. the whole task object, not a subscript
    (``task["x"]``), attribute, or dict/comprehension literal that would be a
    partial subset. Any of those partial shapes returns False and trips the test.
    """
    if not isinstance(payload_node, ast.Call):
        return False
    func = payload_node.func
    # callee must be ``copy.deepcopy`` (Attribute ``deepcopy`` on Name ``copy``)
    if not (
        isinstance(func, ast.Attribute)
        and func.attr == "deepcopy"
        and isinstance(func.value, ast.Name)
        and func.value.id == "copy"
    ):
        return False
    # exactly one positional arg, the bare name ``task`` (the FULL task object)
    if len(payload_node.args) != 1 or payload_node.keywords:
        return False
    arg = payload_node.args[0]
    return isinstance(arg, ast.Name) and arg.id == "task"


def _find_snapshot_make_event_payloads(source: str) -> list[ast.AST]:
    """Return the ``payload=`` value node of every snapshot ``make_event`` call.

    The snapshot emitter is the dual-write ``make_event`` in
    ``_write_task_and_views``: a call to ``make_event`` (resolved as
    ``_events.make_event`` or a bare ``make_event``) carrying a ``payload=``
    keyword. We collect each such call's payload expression so the caller can
    assert it is a full-task deep-copy.
    """
    tree = ast.parse(source)
    payloads: list[ast.AST] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # ``_events.make_event(...)`` or ``make_event(...)``
        is_make_event = (
            (isinstance(func, ast.Attribute) and func.attr == "make_event")
            or (isinstance(func, ast.Name) and func.id == "make_event")
        )
        if not is_make_event:
            continue
        for kw in node.keywords:
            if kw.arg == "payload":
                payloads.append(kw.value)
    return payloads


class SnapshotEmitterWritesFullTaskTest(unittest.TestCase):
    """The real scan: writepipe's snapshot make_event payload is copy.deepcopy(task)."""

    def test_snapshot_payload_is_full_task_deepcopy(self) -> None:
        source = _writepipe_source()
        payloads = _find_snapshot_make_event_payloads(source)

        self.assertTrue(
            payloads,
            "Expected to find at least one make_event(payload=...) call in "
            "writepipe.py (the dual-write snapshot emitter). Found none — has "
            "the snapshot emission moved or been renamed?",
        )
        # Every make_event payload in writepipe IS the snapshot emission, and
        # each must be the full-task deep-copy. (Today there is exactly one.)
        for payload_node in payloads:
            self.assertTrue(
                _payload_is_full_task_deepcopy(payload_node),
                "writepipe snapshot make_event(payload=...) must be "
                "``copy.deepcopy(task)`` (the FULL task), not a field subset. "
                "fold_task does a wholesale ``state = dict(payload)`` on a "
                "snapshot, dropping every pre-snapshot field as stale — so a "
                "partial snapshot would silently reconstruct an incomplete "
                "task while fold_is_complete still returned True. "
                f"Found payload node: {ast.dump(payload_node)}",
            )


class SnapshotCheckerNonVacuityTest(unittest.TestCase):
    """Non-vacuity: prove the matcher REJECTS partial-snapshot shapes.

    Without these, the green real-file scan could pass simply because the
    matcher accepts anything. Each synthetic shape that would be a partial /
    wrong snapshot must be flagged as NOT a full-task deep-copy, and the genuine
    shape must be accepted.
    """

    def _payload_of(self, src: str) -> ast.AST:
        payloads = _find_snapshot_make_event_payloads(src)
        self.assertEqual(len(payloads), 1, f"expected one make_event payload in {src!r}")
        return payloads[0]

    def test_accepts_full_task_deepcopy(self) -> None:
        node = self._payload_of("make_event(payload=copy.deepcopy(task))\n")
        self.assertTrue(_payload_is_full_task_deepcopy(node))

    def test_rejects_partial_field_subset_dict(self) -> None:
        node = self._payload_of('make_event(payload={"status": task["status"]})\n')
        self.assertFalse(_payload_is_full_task_deepcopy(node))

    def test_rejects_shallow_copy_of_task(self) -> None:
        # A shallow ``dict(task)`` is full-width but not a deep copy — the
        # emitter docstring spells out WHY the copy must be deep (a later
        # in-place mutation of the shared task object would retro-alter the
        # already-emitted immutable event). Reject anything but copy.deepcopy.
        node = self._payload_of("make_event(payload=dict(task))\n")
        self.assertFalse(_payload_is_full_task_deepcopy(node))

    def test_rejects_deepcopy_of_subset(self) -> None:
        node = self._payload_of("make_event(payload=copy.deepcopy(task['source']))\n")
        self.assertFalse(_payload_is_full_task_deepcopy(node))


class ContinuityCheckpointQualityTest(unittest.TestCase):
    """Coord-generated Continuity checkpoints should self-report thin spots."""

    def test_make_checkpoint_derives_thin_context_warnings(self) -> None:
        checkpoint = continuity.make_checkpoint(
            {
                "id": "TASK-QUALITY",
                "title": "Quality gate",
                "status": "active",
                "workstream": "fulcra-coord",
                "owner_agent": "codex:box:repo",
                "current_summary": "Build snapshot quality gate.",
                "next_action": "Add warning tests.",
                "task_file": "/coordination/tasks/TASK-QUALITY.json",
            },
            agent="codex:box:repo",
            reason="manual",
        )

        self.assertNotIn("quality_warnings", checkpoint)
        warnings = continuity.quality_warnings(checkpoint)
        self.assertIn("thin checkpoint: no decisions recorded", warnings)
        self.assertIn("thin checkpoint: no open_questions recorded", warnings)
        self.assertNotIn("missing objective/current state", warnings)
        self.assertNotIn("missing concrete next_actions", warnings)
        self.assertFalse(
            any(w.startswith("artifact refs may be local-only") for w in warnings),
            warnings,
        )

    def test_quality_warnings_flag_local_only_artifact_refs(self) -> None:
        warnings = continuity.quality_warnings(
            {
                "objective": "Hand work to another host.",
                "next_actions": ["Resume from portable artifacts."],
                "identity": {
                    "workstream_id": "fulcra-coord",
                    "agent_id": "codex:box:repo",
                    "coord_task_id": "TASK-QUALITY",
                },
                "decisions": ["Do not assume shared filesystem."],
                "open_questions": ["Which host picks this up?"],
                "artifacts": [
                    {"path": "packages/fulcra-coord/README.md"},
                    {"path": "/coordination/tasks/TASK-QUALITY.json"},
                    {"path": "https://github.com/ashfulcra/fulcra-tools/pull/220"},
                ],
            }
        )

        self.assertIn(
            "artifact refs may be local-only: packages/fulcra-coord/README.md",
            warnings,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

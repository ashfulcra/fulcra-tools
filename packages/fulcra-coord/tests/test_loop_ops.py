"""Response sub-log + cmd_respond: the loop return leg, on the bus."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fulcra_coord import loop_ops, loops, remote, schema


def _seed_review_loop(backend, *, requester="author:h:r", audience="rev:h:r"):
    d = schema.make_directive(
        directive_type="review", from_agent=requester, audience=audience,
        title="review PR 7", workstream="general",
        kind="review", state="requested", expects_response=True, sla_hours=24,
    )
    assert remote.upload_json(d, remote.directive_remote_path(d["id"]),
                              backend=backend)
    return d


def test_append_and_read_response_events(coord_backend):
    d = _seed_review_loop(coord_backend)
    ok = loop_ops.append_loop_response(
        d["id"],
        {"by": "rev:h:r", "outcome": {"verdict": "approve", "notes": "clean"}},
        backend=coord_backend)
    assert ok
    events = loop_ops.read_loop_responses(d["id"], backend=coord_backend)
    assert len(events) == 1
    assert events[0]["by"] == "rev:h:r"
    assert events[0]["outcome"]["verdict"] == "approve"
    assert events[0]["at"]          # stamped server-side by the writer


def test_concurrent_responses_never_clobber(coord_backend):
    d = _seed_review_loop(coord_backend, audience="@reviewer")
    loop_ops.append_loop_response(d["id"], {"by": "rev1:h:r", "outcome": {"v": 1}},
                                  backend=coord_backend)
    loop_ops.append_loop_response(d["id"], {"by": "rev2:h:r", "outcome": {"v": 2}},
                                  backend=coord_backend)
    events = loop_ops.read_loop_responses(d["id"], backend=coord_backend)
    assert {e["by"] for e in events} == {"rev1:h:r", "rev2:h:r"}


def test_cmd_respond_closes_the_loop(coord_backend):
    d = _seed_review_loop(coord_backend)
    args = SimpleNamespace(loop_id=d["id"], outcome="approve",
                           evidence="suite green; no findings",
                           agent="rev:h:r", format="table")
    rc = loop_ops.cmd_respond(args, backend=coord_backend)
    assert rc == 0
    # The response shard is the durable truth...
    events = loop_ops.read_loop_responses(d["id"], backend=coord_backend)
    assert events and events[0]["outcome"]["verdict"] == "approve"
    # ...and the LWW snapshot reflects closure: outcome set, terminal state.
    snap = remote.download_json(remote.directive_remote_path(d["id"]),
                                backend=coord_backend)
    assert snap["outcome"]["verdict"] == "approve"
    assert not loops.is_open_loop(snap)


def test_cmd_respond_refresh_preserves_concurrent_snapshot_update(coord_backend):
    """2026-06-11 bug hunt C6: the snapshot refresh must fold onto a FRESH
    download, not the body read at command start. A concurrent snapshot write
    (an ack, a summary edit) landing between respond's initial download and
    its refresh upload used to be silently reverted by the stale re-upload."""
    d = _seed_review_loop(coord_backend)
    path = remote.directive_remote_path(d["id"])
    real_append = loop_ops.append_loop_response

    def append_then_concurrent_update(*a, **kw):
        ok = real_append(*a, **kw)
        # Simulate the interleaving: another host updates the LWW snapshot
        # AFTER respond downloaded its copy but BEFORE the refresh upload.
        snap = remote.download_json(path, backend=coord_backend)
        snap["acked_by"] = ["other:h:r"]
        snap["current_summary"] = "concurrent update from another host"
        assert remote.upload_json(snap, path, backend=coord_backend)
        return ok

    args = SimpleNamespace(loop_id=d["id"], outcome="approve",
                           evidence="", agent="rev:h:r", format="table")
    with patch("fulcra_coord.loop_ops.append_loop_response",
               side_effect=append_then_concurrent_update):
        assert loop_ops.cmd_respond(args, backend=coord_backend) == 0

    snap = remote.download_json(path, backend=coord_backend)
    # The refresh still reflects closure (the fold did its job)...
    assert snap["outcome"]["verdict"] == "approve"
    assert not loops.is_open_loop(snap)
    # ...AND the concurrent writer's fields survive — not reverted.
    assert snap["acked_by"] == ["other:h:r"]
    assert snap["current_summary"] == "concurrent update from another host"


def test_cmd_respond_unknown_loop_is_an_error(coord_backend):
    args = SimpleNamespace(loop_id="DIR-19700101-review-deadbeef",
                           outcome="approve", evidence="", agent="rev:h:r",
                           format="table")
    assert loop_ops.cmd_respond(args, backend=coord_backend) == 1


# ---------------------------------------------------------------------------
# Evidence sub-log (phase 2 Task 1): the THIRD sub-log, for forge-mirrored
# signals. Same shard idioms as responses (round-trip, no clobber) PLUS the
# trust property: the writer force-stamps source=forge-mirror — a caller can
# never forge first-party-ness. Closure-immunity lives in
# test_loop_conformance.py (the invariant test).
# ---------------------------------------------------------------------------


def test_append_and_read_evidence_events(coord_backend):
    d = _seed_review_loop(coord_backend)
    ok = loop_ops.append_loop_evidence(
        d["id"],
        {"forge": "github", "kind": "comment-verdict", "summary": "LGTM on PR"},
        backend=coord_backend)
    assert ok
    events = loop_ops.read_loop_evidence(d["id"], backend=coord_backend)
    assert len(events) == 1
    assert events[0]["summary"] == "LGTM on PR"
    assert events[0]["at"]          # stamped server-side by the writer
    assert events[0]["source"] == "forge-mirror"
    # The evidence sub-log is DISJOINT from the responses sub-log: a mirrored
    # event never appears where the closure fold reads.
    assert loop_ops.read_loop_responses(d["id"], backend=coord_backend) == []


def test_concurrent_evidence_never_clobber(coord_backend):
    d = _seed_review_loop(coord_backend)
    loop_ops.append_loop_evidence(d["id"], {"forge": "github", "summary": "e1"},
                                  backend=coord_backend)
    loop_ops.append_loop_evidence(d["id"], {"forge": "github", "summary": "e2"},
                                  backend=coord_backend)
    events = loop_ops.read_loop_evidence(d["id"], backend=coord_backend)
    assert {e["summary"] for e in events} == {"e1", "e2"}


def test_evidence_source_cannot_be_forged(coord_backend):
    # A caller claiming first-party-ness gets overwritten: the writer FORCE-sets
    # source=forge-mirror unconditionally, so mirrored events are always marked.
    d = _seed_review_loop(coord_backend)
    assert loop_ops.append_loop_evidence(
        d["id"], {"source": "first-party", "summary": "sneaky"},
        backend=coord_backend)
    events = loop_ops.read_loop_evidence(d["id"], backend=coord_backend)
    assert events[0]["source"] == "forge-mirror"


def test_outcome_fold_is_bus_only(coord_backend):
    # The fold derives outcome/closure ONLY from bus response events — there is
    # no other input. A loop with no response events folds to outcome None/open.
    d = _seed_review_loop(coord_backend)
    folded = loop_ops.fold_loop(d, backend=coord_backend)
    assert folded["outcome"] is None and loops.is_open_loop(folded)
    loop_ops.append_loop_response(d["id"], {"by": "rev:h:r",
                                            "outcome": {"verdict": "approve"}},
                                  backend=coord_backend)
    folded = loop_ops.fold_loop(d, backend=coord_backend)
    assert folded["outcome"]["verdict"] == "approve"
    assert not loops.is_open_loop(folded)

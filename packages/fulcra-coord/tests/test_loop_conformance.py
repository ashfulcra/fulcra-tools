"""CONFORMANCE: a coordination loop completes end-to-end ON THE BUS.

This is the spec's structural kill of the 2026-06-09 out-of-band-verdict bug:
requester opens a loop -> recipient acks -> recipient responds -> requester
observes closure — every leg a bus record, ZERO forge/platform involvement.
If any future change makes closure require anything but bus reads, this fails.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fulcra_coord import directives, loop_ops, loops, remote, schema

REQUESTER = "author:hostA:repo"
RESPONDER = "reviewer:hostB:repo"


def test_full_loop_handshake_on_the_bus(coord_backend):
    # 1. Requester opens a review loop (bus write).
    d = schema.make_directive(
        directive_type="review", from_agent=REQUESTER, audience=RESPONDER,
        title="review branch feat/x", workstream="general",
        kind="review", state="requested", expects_response=True, sla_hours=24,
    )
    assert remote.upload_json(d, remote.directive_remote_path(d["id"]),
                              backend=coord_backend)

    # 2. Responder acks (durable per-agent ack shard — bus write).
    assert directives.write_directive_ack(d["id"], RESPONDER,
                                          backend=coord_backend)

    # 3. Responder delivers the verdict (bus response shard via respond).
    args = SimpleNamespace(loop_id=d["id"], outcome="approve",
                           evidence="1286 passed", agent=RESPONDER,
                           format="table")
    assert loop_ops.cmd_respond(args, backend=coord_backend) == 0

    # 4. Requester observes closure FROM THE BUS ALONE: re-download + fold.
    snap = remote.download_json(remote.directive_remote_path(d["id"]),
                                backend=coord_backend)
    folded = loop_ops.fold_loop(snap, backend=coord_backend)
    assert folded["outcome"]["verdict"] == "approve"
    assert not loops.is_open_loop(folded)
    assert RESPONDER in directives.read_directive_acks(d["id"],
                                                       backend=coord_backend)

    # 5. The negative control: BEFORE any response, the loop reads OPEN — a
    #    verdict living anywhere else (a forge comment) cannot close it.
    d2 = schema.make_directive(
        directive_type="review", from_agent=REQUESTER, audience=RESPONDER,
        title="review branch feat/y", workstream="general",
        kind="review", state="requested", expects_response=True,
    )
    assert remote.upload_json(d2, remote.directive_remote_path(d2["id"]),
                              backend=coord_backend)
    folded2 = loop_ops.fold_loop(d2, backend=coord_backend)
    assert loops.is_open_loop(folded2)
    assert folded2["outcome"] is None


def test_dispatch_loop_handshake_on_the_bus(coord_backend):
    d = schema.make_directive(
        directive_type="tell", from_agent="boss:h:r", audience="worker:h:r",
        title="build the thing", workstream="general",
        kind="dispatch", state="assigned", expects_response=True, sla_hours=72,
    )
    assert remote.upload_json(d, remote.directive_remote_path(d["id"]),
                              backend=coord_backend)
    args = SimpleNamespace(loop_id=d["id"], outcome="delivered",
                           evidence="artifact at packages/x", agent="worker:h:r",
                           format="table")
    assert loop_ops.cmd_respond(args, backend=coord_backend) == 0
    folded = loop_ops.fold_loop(
        remote.download_json(remote.directive_remote_path(d["id"]),
                             backend=coord_backend),
        backend=coord_backend)
    assert folded["outcome"]["verdict"] == "delivered"
    assert not loops.is_open_loop(folded)


def test_request_review_to_review_done_closes_loop_on_bus(coord_backend):
    """The live-bug killer: the actual request-review -> review-done command
    pair produces a bus-closed review loop. Uses the commands end-to-end.

    Arg names are bound to the REAL parser surfaces in entry.py:
      * request-review: pr (dest of the ARTIFACT positional), repo, agent
        (the author), candidate_list, dry_run, format.
      * review-done: artifact, verdict (choices approve|changes), note, repo,
        to, from (reserved word — set via setattr), dry_run, format.
    """
    from fulcra_coord import routing_ops, presence

    # A live, review-capable reviewer in presence (so routing resolves).
    rec = schema.make_presence(RESPONDER, capabilities=["review"])
    presence._write_presence(rec, backend=coord_backend)

    rr = SimpleNamespace(pr="42", repo="org/repo", agent=REQUESTER,
                         candidate_list=None, dry_run=False, format="table")
    assert routing_ops.cmd_request_review(rr, backend=coord_backend) == 0

    # The dual-written directive is a kindful OPEN review loop.
    recs = [r for _p, r in remote.list_json(remote.directives_prefix(),
                                            backend=coord_backend)
            if isinstance(r, dict) and r.get("directive_type") == "review"]
    assert recs, "request-review must dual-write a review directive"
    loop = recs[0]
    assert loop["kind"] == "review" and loops.is_open_loop(loop)

    rd = SimpleNamespace(artifact="42", verdict="approve",
                         note="suite green", repo=None, to=None,
                         dry_run=False, format="table")
    setattr(rd, "from", RESPONDER)
    assert routing_ops.cmd_review_done(rd, backend=coord_backend) == 0

    # The verdict landed as a bus response event and the loop folds CLOSED.
    folded = loop_ops.fold_loop(
        remote.download_json(remote.directive_remote_path(loop["id"]),
                             backend=coord_backend) or loop,
        backend=coord_backend)
    assert folded["outcome"], "verdict must land as a bus response event"
    assert not loops.is_open_loop(folded)
    # The response sub-log of the ORIGINAL review loop carries the verdict.
    events = loop_ops.read_loop_responses(loop["id"], backend=coord_backend)
    assert events and events[-1]["outcome"]["verdict"] == "approve"
    assert events[-1]["by"] == RESPONDER

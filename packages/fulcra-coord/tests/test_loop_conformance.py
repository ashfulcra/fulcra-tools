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

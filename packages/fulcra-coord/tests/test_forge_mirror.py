"""forge-mirror: the ONE sanctioned forge poller (phase 2 Task 2).

Pins the bridge's contract: verdict-shaped GitHub signals for OPEN review
loops are mirrored into the evidence sub-log with DETERMINISTIC event ids
(idempotent re-runs), force-marked source=forge-mirror — and a mirrored
signal NEVER closes a loop (the invariant re-pinned at this layer).

NEVER invokes the real ``gh`` CLI: every test patches
``fulcra_coord.forge_mirror.subprocess.run`` (the same no-real-forge
discipline as test_fulcra_coord.py's test_no_forge_subprocess_call).
"""
from __future__ import annotations

import json
import subprocess as _subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fulcra_coord import forge_mirror, loop_ops, loops, remote, schema

# ``subprocess`` is a process-global singleton module, so patching
# ``fulcra_coord.forge_mirror.subprocess.run`` also intercepts the fake
# coordination-bus backend (the store shells out via subprocess.run too — see
# remote.py's patch-point note). The fakes below therefore DISPATCH: gh
# invocations get the canned forge response; everything else forwards to the
# real run so the bus keeps working under the patch.
_REAL_RUN = _subprocess.run


def _seed_loop(backend, *, kind="review", state=None, artifact_ref=None,
               requester="author:h:r", audience="rev:h:r"):
    """A directive/loop record uploaded as a top-level shard (the same seeding
    idiom as test_loop_ops._seed_review_loop, plus artifact_ref)."""
    # directive_type and loop `kind` are separate vocabularies: only "review"
    # exists in both; any other kind rides a plain "tell" directive record.
    d = schema.make_directive(
        directive_type=kind if kind == "review" else "tell",
        from_agent=requester, audience=audience,
        title=f"{kind} loop", workstream="general",
        kind=kind, state=state or loops.initial_state(kind),
        expects_response=loops.KINDS[kind]["expects_response"] or None,
        sla_hours=loops.KINDS[kind]["sla_hours"],
        artifact_ref=artifact_ref,
    )
    assert remote.upload_json(d, remote.directive_remote_path(d["id"]),
                              backend=backend)
    return d


def _args(**kw):
    base = dict(once=True, repo=None, format="table")
    base.update(kw)
    return SimpleNamespace(**base)


# A verdict-shaped gh payload: merged + one APPROVED latest review + one
# verdict-shaped comment (and one chatter comment that must NOT mirror).
_VERDICT_PAYLOAD = {
    "state": "MERGED",
    "mergedAt": "2026-06-09T10:00:00Z",
    "url": "https://github.com/ashfulcra/fulcra-tools/pull/101",
    "latestReviews": [
        {"author": {"login": "alice"}, "state": "APPROVED", "body": "ship it"},
    ],
    "comments": [
        {"id": "IC_abc123", "author": {"login": "codex"},
         "createdAt": "2026-06-09T09:00:00Z",
         "body": "Codex review complete: no findings"},
        {"id": "IC_def456", "author": {"login": "bob"},
         "createdAt": "2026-06-09T09:30:00Z",
         "body": "nice refactor, learned something"},
    ],
}


def _gh_fake(payload=None, *, fail=None, gh_calls=None):
    """A dispatching subprocess.run fake: gh -> the canned outcome (success
    payload, nonzero exit, or a raised exception — never the real gh); any
    other command (the fake bus backend) -> the real subprocess.run.
    ``gh_calls`` (a list) records every gh argv for never-probed assertions."""
    def fake_run(cmd, **kw):
        if cmd and cmd[0] == "gh":
            if gh_calls is not None:
                gh_calls.append(list(cmd))
            if fail == "raise":
                raise FileNotFoundError("gh not installed")
            if fail == "exit":
                return SimpleNamespace(returncode=1, stdout="",
                                       stderr="gh: boom")
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload),
                                   stderr="")
        return _REAL_RUN(cmd, **kw)
    return fake_run


def test_verdict_payload_mirrors_exactly_three_marked_shards(coord_backend):
    """merged + APPROVED review + verdict comment -> exactly 3 evidence shards,
    every one force-marked source=forge-mirror; the chatter comment is NOT
    mirrored. A RERUN stays at exactly 3 — deterministic forge_event_ids make
    the sweep idempotent (shard id = event id, so re-runs overwrite)."""
    d = _seed_loop(coord_backend,
                   artifact_ref={"pr": 101, "repo": "ashfulcra/fulcra-tools"})
    with patch("fulcra_coord.forge_mirror.subprocess.run",
               side_effect=_gh_fake(_VERDICT_PAYLOAD)):
        assert forge_mirror.cmd_forge_mirror(_args(), backend=coord_backend) == 0
        events = loop_ops.read_loop_evidence(d["id"], backend=coord_backend)
        assert len(events) == 3
        assert all(e["source"] == "forge-mirror" for e in events)
        assert {e["kind"] for e in events} == {
            "merged", "review-approved", "comment-verdict"}
        # RERUN: same payload, same deterministic ids -> still exactly 3.
        assert forge_mirror.cmd_forge_mirror(_args(), backend=coord_backend) == 0
        rerun = loop_ops.read_loop_evidence(d["id"], backend=coord_backend)
        assert len(rerun) == 3


def test_mirrored_evidence_never_closes_the_loop(coord_backend):
    """THE invariant, re-pinned at the mirror layer: after a full verdict-shaped
    mirror sweep the loop is STILL OPEN — fold_loop sees no outcome, no terminal
    state. Closure stays bus-response-only; the requester closes explicitly."""
    d = _seed_loop(coord_backend,
                   artifact_ref={"pr": 101, "repo": "ashfulcra/fulcra-tools"})
    with patch("fulcra_coord.forge_mirror.subprocess.run",
               side_effect=_gh_fake(_VERDICT_PAYLOAD)):
        assert forge_mirror.cmd_forge_mirror(_args(), backend=coord_backend) == 0
    snap = remote.download_json(remote.directive_remote_path(d["id"]),
                                backend=coord_backend)
    folded = loop_ops.fold_loop(snap, backend=coord_backend)
    assert loops.is_open_loop(folded)
    assert folded.get("outcome") is None


def test_non_review_and_closed_loops_are_never_probed(coord_backend):
    """A non-review loop and a CLOSED review loop produce zero gh probes —
    subprocess.run is never called, even though both carry a usable
    artifact_ref."""
    _seed_loop(coord_backend, kind="dispatch",
               artifact_ref={"pr": 7, "repo": "ashfulcra/fulcra-tools"})
    _seed_loop(coord_backend, kind="review", state="closed",
               artifact_ref={"pr": 8, "repo": "ashfulcra/fulcra-tools"})
    gh_calls: list = []
    with patch("fulcra_coord.forge_mirror.subprocess.run",
               side_effect=_gh_fake(_VERDICT_PAYLOAD, gh_calls=gh_calls)):
        assert forge_mirror.cmd_forge_mirror(_args(), backend=coord_backend) == 0
    assert gh_calls == []


def test_missing_artifact_ref_is_skipped_silently(coord_backend):
    """An open review loop without a {pr, repo} artifact_ref is skipped — no
    probe, no shard, rc 0 (there is nothing on the forge to mirror)."""
    d = _seed_loop(coord_backend, artifact_ref=None)
    d2 = _seed_loop(coord_backend, artifact_ref={"ref": "feat/x"})  # no pr/repo
    gh_calls: list = []
    with patch("fulcra_coord.forge_mirror.subprocess.run",
               side_effect=_gh_fake(_VERDICT_PAYLOAD, gh_calls=gh_calls)):
        assert forge_mirror.cmd_forge_mirror(_args(), backend=coord_backend) == 0
    assert gh_calls == []
    assert loop_ops.read_loop_evidence(d["id"], backend=coord_backend) == []
    assert loop_ops.read_loop_evidence(d2["id"], backend=coord_backend) == []


def test_gh_failure_skips_loop_without_raising(coord_backend):
    """gh exiting nonzero -> that loop is skipped: zero evidence shards, no
    exception, rc still 0 (the mirror is best-effort by construction)."""
    d = _seed_loop(coord_backend,
                   artifact_ref={"pr": 101, "repo": "ashfulcra/fulcra-tools"})
    with patch("fulcra_coord.forge_mirror.subprocess.run",
               side_effect=_gh_fake(fail="exit")):
        assert forge_mirror.cmd_forge_mirror(_args(), backend=coord_backend) == 0
    assert loop_ops.read_loop_evidence(d["id"], backend=coord_backend) == []


def test_missing_gh_binary_skips_without_raising(coord_backend):
    """subprocess.run raising (gh not installed / timeout) is the same skip:
    no shard, no raise, rc 0."""
    d = _seed_loop(coord_backend,
                   artifact_ref={"pr": 101, "repo": "ashfulcra/fulcra-tools"})
    with patch("fulcra_coord.forge_mirror.subprocess.run",
               side_effect=_gh_fake(fail="raise")):
        assert forge_mirror.cmd_forge_mirror(_args(), backend=coord_backend) == 0
    assert loop_ops.read_loop_evidence(d["id"], backend=coord_backend) == []


def test_repo_filter_excludes_other_repos(coord_backend):
    """--repo R probes only loops whose artifact_ref targets R: a loop on a
    different repo gets no gh call and no shards."""
    d = _seed_loop(coord_backend,
                   artifact_ref={"pr": 101, "repo": "ashfulcra/other-repo"})
    gh_calls: list = []
    with patch("fulcra_coord.forge_mirror.subprocess.run",
               side_effect=_gh_fake(_VERDICT_PAYLOAD, gh_calls=gh_calls)):
        rc = forge_mirror.cmd_forge_mirror(
            _args(repo="ashfulcra/fulcra-tools"), backend=coord_backend)
    assert rc == 0
    assert gh_calls == []
    assert loop_ops.read_loop_evidence(d["id"], backend=coord_backend) == []


def test_production_ref_shape_is_probed(coord_backend):
    """REGRESSION: production records (directives.directive_from_task ~:336)
    store the opaque artifact as artifact_ref["ref"], not "pr". Keying the
    mirror on "pr" alone silently skipped every real request-review loop —
    the mirror must accept the production shape."""
    d = _seed_loop(coord_backend,
                   artifact_ref={"ref": "101", "repo": "ashfulcra/fulcra-tools"})
    gh_calls: list = []
    with patch("fulcra_coord.forge_mirror.subprocess.run",
               side_effect=_gh_fake(_VERDICT_PAYLOAD, gh_calls=gh_calls)):
        rc = forge_mirror.cmd_forge_mirror(_args(), backend=coord_backend)
    assert rc == 0
    assert len(gh_calls) == 1                       # the loop WAS probed
    evidence = loop_ops.read_loop_evidence(d["id"], backend=coord_backend)
    assert evidence                                  # and evidence landed
    assert all(e["source"] == "forge-mirror" for e in evidence)

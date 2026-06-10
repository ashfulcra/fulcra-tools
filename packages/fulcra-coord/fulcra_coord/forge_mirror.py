"""THE one sanctioned forge poller in the entire system — there must be no other.

WHY exactly one, and why it exists at all: ad-hoc platform pollers hide
coordination-layer holes. When an agent quietly shells out to a forge to ask
"did my review land?", the answer never touches the bus — so a non-compliant
responder (one who left their verdict as a GitHub comment instead of a bus
response) or a human acting directly on the forge stays INVISIBLE to the
coordination layer, and every such poller is one more place closure semantics
can silently fork. This bridge centralizes that: it mirrors verdict-shaped
forge signals onto the bus as MARKED evidence (force-stamped
``source=forge-mirror`` by the writer — see loop_ops.append_loop_evidence), so
out-of-band activity becomes VISIBLE on the board, while closure remains
bus-response-only: a mirrored event can NEVER close a loop (fold_loop's
invariant); the requester closes explicitly, citing the evidence.

Layering: PRODUCTION-side, above core. May import stdlib, remote, loops,
loop_ops, schema, log, output, identity — never cli/views/lifecycle/inbox/
query/presence/listener. The pin runs in REVERSE too: no core module may
import this one (tests/test_fulcra_coord.py::TestLoopsLayering::
test_no_core_module_imports_forge_mirror), so forge polling can never creep
back into the coordination layer.

Idempotency: every mirrored event carries a DETERMINISTIC ``forge_event_id``
derived from the forge object (merge/review/comment), used as the evidence
shard id — re-running the sweep overwrites the same shards instead of
duplicating them.
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Optional

from . import loop_ops, loops, remote
from . import log as ops_log
from .output import info as _info, print_json as _print_json


# A comment is "verdict-shaped" when a verdict keyword appears within the
# first ~40 chars (e.g. "Codex review complete: no findings", "Verdict:
# approve"). Deliberately loose — this feeds DETECTION (out-of-band flags),
# never closure, so a false positive costs an extra evidence shard, not a
# wrongly-closed loop.
_VERDICT_COMMENT_RE = re.compile(
    r"(?i)^.{0,40}(verdict|review complete|approve[d]?\b|changes requested)")

# The gh fields the mirror needs and nothing more: merge state, review
# verdicts, comments (for verdict-shaped bodies), and the PR url for evidence
# provenance.
_GH_JSON_FIELDS = "state,mergedAt,comments,latestReviews,url"


def _log_probe_failure(loop_id: str, error: str) -> None:
    """Best-effort ops-log entry for a failed gh probe — the mirror must keep
    sweeping the remaining loops, so a probe failure is recorded, never raised."""
    try:
        ops_log.log_op("forge-mirror", loop_id, status="forge_probe_failed",
                       error=error)
    except Exception:
        pass


def _probe_pr(pr: Any, repo: Any, loop_id: str) -> Optional[dict[str, Any]]:
    """One bounded ``gh pr view`` probe -> parsed payload dict, or None.

    Every failure mode (gh missing, timeout, nonzero exit, unparseable JSON)
    collapses to None + an ops-log entry: the sweep is best-effort per loop,
    and a forge outage must never take the command down."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", str(pr), "--repo", str(repo),
             "--json", _GH_JSON_FIELDS],
            capture_output=True, text=True, timeout=10)
    except Exception as e:           # FileNotFoundError, TimeoutExpired, ...
        _log_probe_failure(loop_id, f"{type(e).__name__}: {e}")
        return None
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:200] or f"gh exited {proc.returncode}"
        _log_probe_failure(loop_id, err)
        return None
    try:
        payload = json.loads(proc.stdout)
    except Exception as e:
        _log_probe_failure(loop_id, f"unparseable gh output: {e}")
        return None
    return payload if isinstance(payload, dict) else None


def _verdict_events(pr: Any, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Map a gh payload to evidence events, each with a deterministic
    ``forge_event_id`` (the idempotency key — shard id on the bus):

    * ``mergedAt`` set        -> one ``merged`` event (``gh-merged-<pr>``)
    * latest review APPROVED/
      CHANGES_REQUESTED       -> one ``review-<state>`` event per reviewer
                                 (``gh-review-<pr>-<author>`` — gh's
                                 latestReviews is already one-per-reviewer)
    * verdict-shaped comment  -> one ``comment-verdict`` event
                                 (``gh-comment-<comment id>``)

    Pure: no I/O, no clock. Summaries are bounded (<= ~300 chars) so evidence
    shards stay skimmable on the board."""
    events: list[dict[str, Any]] = []
    url = payload.get("url")
    merged_at = payload.get("mergedAt")
    if merged_at:
        events.append({
            "forge_event_id": f"gh-merged-{pr}", "kind": "merged",
            "summary": f"PR #{pr} merged at {merged_at}", "url": url,
        })
    for rev in payload.get("latestReviews") or []:
        state = (rev.get("state") or "").upper()
        if state not in ("APPROVED", "CHANGES_REQUESTED"):
            continue
        author = (rev.get("author") or {}).get("login") or "unknown"
        body = (rev.get("body") or "").strip()[:200]
        events.append({
            "forge_event_id": f"gh-review-{pr}-{author}",
            "kind": "review-" + state.lower(),
            "summary": f"{author}: {state}" + (f" — {body}" if body else ""),
            "url": url,
        })
    for comment in payload.get("comments") or []:
        body = (comment.get("body") or "")
        if not _VERDICT_COMMENT_RE.search(body):
            continue
        cid = comment.get("id") or comment.get("createdAt") or ""
        author = (comment.get("author") or {}).get("login") or "unknown"
        events.append({
            "forge_event_id": f"gh-comment-{cid}", "kind": "comment-verdict",
            "summary": f"{author}: {body.strip()[:200]}", "url": url,
        })
    return events


def cmd_forge_mirror(args: Any, backend: Optional[list[str]] = None) -> int:
    """One mirror sweep: probe every OPEN kind:review loop with a usable
    ``artifact_ref`` (``{pr, repo}``) via ``gh pr view``, and append each
    verdict-shaped signal to that loop's evidence sub-log.

    Evidence only, NEVER closure — the writer force-stamps
    ``source=forge-mirror``, fold_loop never reads the evidence prefix, and
    the board flags the loop out-of-band so the requester closes it
    explicitly, citing the mirror. ``--once`` is the only mode: scheduling
    rides the existing listener/digest cadence later — deliberately no
    daemon here. JSON counters: ``probed`` = loops gh answered for,
    ``mirrored`` = evidence events appended, ``skipped`` = open review loops
    not probed (unusable artifact_ref, --repo filtered) or whose probe failed."""
    prefix = remote.directives_prefix()
    try:
        listed = remote.list_json(prefix, backend=backend)
    except Exception:
        listed = []
    records: list[dict[str, Any]] = []
    for path, rec in listed:
        # TOP-LEVEL-ONLY FILTER (same load-bearing idiom as
        # cli._loop_health_check): the directives prefix holds sub-log
        # subtrees (<id>/acks|routing|responses|evidence/) beside the
        # top-level <id>.json loop records — a shard mistaken for a record
        # would be probed as a loop.
        rel = path[len(prefix):] if path.startswith(prefix) else path
        if "/" in rel:
            continue  # ack/routing/response/evidence shard — never a loop record
        if not rel.endswith(".json"):
            continue
        if isinstance(rec, dict):
            records.append(rec)

    repo_filter = getattr(args, "repo", None)
    probed = mirrored = skipped = 0
    for r in records:
        # Only OPEN review loops have a forge artifact worth probing; every
        # other record is silently not the mirror's business.
        if loops.loop_kind_of(r) != "review" or not loops.is_open_loop(r):
            continue
        ref = r.get("artifact_ref")
        # Production records (directives.directive_from_task) store the opaque
        # artifact under "ref"; "pr" is tolerated for forward-compat/hand-built
        # records. Keying on "pr" alone silently skipped every real loop.
        if not isinstance(ref, dict) or "repo" not in ref:
            skipped += 1   # open review loop, but nothing probeable on a forge
            continue
        pr = ref.get("pr") or ref.get("ref")
        repo = ref["repo"]
        if not pr:
            skipped += 1
            continue
        if repo_filter and str(repo) != str(repo_filter):
            skipped += 1
            continue
        loop_id = r.get("id") or ""
        payload = _probe_pr(pr, repo, loop_id)
        if payload is None:
            skipped += 1   # probe failed — logged by _probe_pr, sweep continues
            continue
        probed += 1
        for event in _verdict_events(pr, payload):
            # The deterministic forge_event_id doubles as the shard's
            # event_id (append_loop_evidence honours a caller-set event_id),
            # so a re-run overwrites the same shard: idempotent by path.
            shard = {**event, "forge": "github",
                     "event_id": event["forge_event_id"]}
            if loop_ops.append_loop_evidence(loop_id, shard, backend=backend):
                mirrored += 1

    if getattr(args, "format", "table") == "json":
        _print_json({"probed": probed, "mirrored": mirrored,
                     "skipped": skipped})
    else:
        _info(f"forge-mirror: {probed} loop(s) probed, "
              f"{mirrored} evidence event(s) mirrored")
    return 0

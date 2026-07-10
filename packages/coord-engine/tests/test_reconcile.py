import json

from coord_engine import annotate, reconcile
from coord_engine.transport import TransportError


class FakeTransport:
    """In-memory Fulcra File Store: {path: content} + per-path mtime."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.mtimes: dict[str, str] = {}
        self.fail_list = False

    def put(self, path, content, mtime="2026-07-01 04:00PM UTC"):
        self.store[path] = content
        self.mtimes[path] = mtime

    def list_dir(self, prefix):
        if self.fail_list:
            raise TransportError("boom")
        out = []
        seen_dirs = set()
        for p in sorted(self.store):
            if not p.startswith(prefix):
                continue
            rest = p[len(prefix):]
            head = rest.rstrip("/")
            if "/" in head:
                # deeper than a direct child -> synthesize the intermediate dir
                # entry, matching `fulcra-api file list` (which shows `sub/`).
                seg = head.split("/", 1)[0] + "/"
                if seg not in seen_dirs:
                    seen_dirs.add(seg)
                    out.append({"name": seg, "mtime": None, "is_dir": True})
                continue
            out.append({"name": rest, "mtime": self.mtimes.get(p),
                        "is_dir": rest.endswith("/")})
        return out

    def read(self, path):
        return self.store.get(path)

    def write(self, path, content):
        self.store[path] = content
        return True

    def delete(self, path):
        return self.store.pop(path, None) is not None


def _task(title, status, priority="P2"):
    return f"---\ntype: Task\ntitle: {title}\nstatus: {status}\npriority: {priority}\n---\nbody"


def _run(t):
    return reconcile.reconcile(t, "r", now="2026-07-01T00:00:00Z", today="2026-07-01", host="h")


def test_reconcile_builds_index_and_aggregate():
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    t.put("team/r/task/b.md", _task("Bravo", "done"))
    res = _run(t)
    assert res["tasks"] == 2
    idx = t.store["team/r/task/index.md"]
    assert "## Active" in idx and "[Alpha](a.md)" in idx
    agg = json.loads(t.store["team/r/_coord/summaries.json"])
    assert agg["team"] == "r"
    assert {r["name"] for r in agg["rows"]} == {"a", "b"}


def test_reconcile_skips_index_and_non_task_docs():
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    t.put("team/r/task/index.md", "# Tasks\n(stale)")
    t.put("team/r/task/note.md", "---\ntype: Reference\n---\nnot a task")
    assert _run(t)["tasks"] == 1


def _task_ts(title, status, ts):
    return (f"---\ntype: Task\ntitle: {title}\nstatus: {status}\n"
            f"timestamp: {ts}\n---\nbody")


def test_reconcile_persists_pending_when_resolution_live():
    # opt-in on the bus -> reconcile threads the pass's STRUCTURED transitions to
    # the projection artifact (ts = the task's own updated_at, normalized).
    t = FakeTransport()
    t.put(annotate.resolution_path("r"), "transitions\n")
    t.put("team/r/task/a.md", _task_ts("Alpha", "active", "2026-07-09T09:00:00Z"))
    _run(t)  # first pass: creation transition for Alpha
    assert annotate.pending_path("r") in t.store
    pend = json.loads(t.store[annotate.pending_path("r")])
    assert pend["transitions"] == [
        {"task_id": "a", "kind": "create", "ts": "2026-07-09T09:00:00Z", "title": "Alpha"}]
    # bullets/log.md are still produced unchanged (behavior-preserving)
    assert "Creation" in t.store["team/r/task/log.md"]


def test_reconcile_no_pending_when_resolution_off():
    # default (no resolution on the bus) -> no projection artifact written at all.
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _run(t)
    assert annotate.pending_path("r") not in t.store


def test_reconcile_empty_diff_does_not_wipe_live_pending():
    # CRITICAL-1: a reconcile whose diff is empty must NOT overwrite a pending
    # transition the projection fold is still holding LIVE — i.e. one that has not
    # landed (absent from the cursor's seen_ids). The deployed
    # `reconcile && annotate project` topology reconciles every beat; a blind
    # overwrite with [] here would DROP that moment before projection ever emits it.
    t = FakeTransport()
    t.put(annotate.resolution_path("r"), "transitions\n")
    t.put("team/r/task/a.md", _task_ts("Alpha", "active", "2026-07-09T09:00:00Z"))
    _run(t)  # first pass: create-a persisted to pending; cursor never advanced
    pend1 = json.loads(t.store[annotate.pending_path("r")])["transitions"]
    assert [x["task_id"] for x in pend1] == ["a"]
    # second pass: task a unchanged -> empty diff. Merge-and-carry keeps the
    # un-landed create-a (cursor has no seen_ids) rather than wiping to [].
    _run(t)
    pend2 = json.loads(t.store[annotate.pending_path("r")])["transitions"]
    assert [x["task_id"] for x in pend2] == ["a"]


def test_reconcile_is_orphan_proof():
    t = FakeTransport()
    prior = {"schema": "coord.teams.summaries.v1", "rows": [
        {"id": "ghost", "name": "ghost", "status": "active", "mtime": "old"}]}
    t.put("team/r/_coord/summaries.json", json.dumps(prior))
    t.put("team/r/task/live.md", _task("Live", "active"))
    _run(t)
    agg = json.loads(t.store["team/r/_coord/summaries.json"])
    assert {r["name"] for r in agg["rows"]} == {"live"}  # ghost pruned
    assert "Deprecation" in t.store["team/r/task/log.md"]


def test_reconcile_incremental_reuses_unchanged_without_download():
    t = FakeTransport()
    prior = {"rows": [{"id": "a", "name": "a", "status": "active", "title": "A",
                       "mtime": "2026-07-01 04:00PM UTC", "description": "d"}]}
    t.put("team/r/_coord/summaries.json", json.dumps(prior))
    t.put("team/r/task/a.md", _task("A", "active"), mtime="2026-07-01 04:00PM UTC")
    reads = []
    orig = t.read
    t.read = lambda p: (reads.append(p), orig(p))[1]
    res = _run(t)
    assert res["reused"] == 1 and res["parsed"] == 0
    assert "team/r/task/a.md" not in reads  # unchanged mtime -> never downloaded


def test_reconcile_reparses_when_mtime_changes():
    t = FakeTransport()
    prior = {"rows": [{"id": "a", "name": "a", "status": "active", "title": "A",
                       "mtime": "2026-07-01 04:00PM UTC", "description": "d"}]}
    t.put("team/r/_coord/summaries.json", json.dumps(prior))
    t.put("team/r/task/a.md", _task("A", "done"), mtime="2026-07-01 05:00PM UTC")
    res = _run(t)
    assert res["parsed"] == 1
    agg = json.loads(t.store["team/r/_coord/summaries.json"])
    assert agg["rows"][0]["status"] == "done"


def test_reconcile_unparseable_keeps_prior_row_and_warns():
    t = FakeTransport()
    prior = {"rows": [{"id": "a", "name": "a", "status": "active", "title": "A",
                       "mtime": "old", "description": ""}]}
    t.put("team/r/_coord/summaries.json", json.dumps(prior))
    t.put("team/r/task/a.md", "garbage, no frontmatter", mtime="new")
    res = _run(t)
    agg = json.loads(t.store["team/r/_coord/summaries.json"])
    assert {r["name"] for r in agg["rows"]} == {"a"}
    assert any("unparseable" in w for w in res["warnings"])


def test_reconcile_degraded_on_list_failure_writes_nothing():
    t = FakeTransport()
    t.fail_list = True
    t.put("team/r/_coord/summaries.json", json.dumps({"rows": [{"id": "a", "name": "a"}]}))
    before = dict(t.store)
    res = _run(t)
    assert res["degraded"] is True
    assert t.store == before  # no truncated index written


# --- data-updates fast path ---

def _reconciled(t, now="2026-07-01T12:00:00Z"):
    from coord_engine import reconcile as rec
    return rec.reconcile(t, "r", now=now, today=now[:10], host="h")


def _with_updates(t, changes):
    t.updates_calls = []
    def updates(period):
        t.updates_calls.append(period)
        return changes
    t.updates = updates
    return t


def test_fast_path_skips_full_pass_when_no_relevant_changes():
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)                                     # seed aggregate at 12:00
    _with_updates(t, [{"full_name": "/team/r/presence/x.md"},   # irrelevant churn
                      {"full_name": "/other/thing.md"}])
    index_before = t.store["team/r/task/index.md"]
    t.store["team/r/task/a.md"] = _task("Alpha CHANGED", "active")  # sneaky edit the feed missed
    res = _reconciled(t, now="2026-07-01T12:30:00Z")
    assert res.get("fast_path") is True
    assert t.store["team/r/task/index.md"] == index_before   # index untouched (fold of unchanged inputs)
    assert res["tasks"] == 1 and res["parsed"] == 0


def test_fast_path_declines_on_task_change():
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)
    _with_updates(t, [{"full_name": "/team/r/task/b.md"}])
    t.put("team/r/task/b.md", _task("Bravo", "active"))
    res = _reconciled(t, now="2026-07-01T12:30:00Z")
    assert not res.get("fast_path")
    assert res["tasks"] == 2


def test_fast_path_declines_on_ack_change():
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)
    _with_updates(t, [{"full_name": "/team/r/_coord/acks/x.json"}])
    res = _reconciled(t, now="2026-07-01T12:30:00Z")
    assert not res.get("fast_path")


def test_fast_path_declines_on_malformed_feed_entry():
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)
    _with_updates(t, [{"full_name": "/team/r/presence/x.md"}, {}])
    res = _reconciled(t, now="2026-07-01T12:30:00Z")
    assert not res.get("fast_path")


def test_fast_path_declines_without_updates_support_or_on_error():
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)
    res = _reconciled(t, now="2026-07-01T12:30:00Z")   # no .updates attr
    assert not res.get("fast_path")
    def broken(period):
        return None
    t.updates = broken
    res = _reconciled(t, now="2026-07-01T12:35:00Z")   # updates errors -> None
    assert not res.get("fast_path")


def test_fast_path_declines_when_aggregate_stale():
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)                                     # generated_at 12:00
    _with_updates(t, [])
    res = _reconciled(t, now="2026-07-01T19:00:00Z")   # 7h later > 6h guard
    assert not res.get("fast_path")


def test_fast_path_still_writes_health_shard():
    from coord_engine import health as health_mod
    from coord_engine.tasks import agent_key
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)
    shard_path = f"{health_mod.health_prefix('r')}{agent_key('h')}.json"
    t.store.pop(shard_path, None)                      # wipe; fast path must re-beat
    _with_updates(t, [])
    res = _reconciled(t, now="2026-07-01T12:30:00Z")
    assert res.get("fast_path") is True
    assert shard_path in t.store                       # host doesn't go dark on fast path


def test_fast_path_ignores_own_derived_artifacts_in_feed():
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)
    # the previous pass's own index/log writes show up in the store feed
    _with_updates(t, [{"full_name": "/team/r/task/index.md"},
                      {"full_name": "/team/r/task/log.md"}])
    res = _reconciled(t, now="2026-07-01T12:30:00Z")
    assert res.get("fast_path") is True


def test_fast_path_normalizes_missing_leading_slash():
    # a relevant change WITHOUT the leading slash must still decline (fail-closed)
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)
    _with_updates(t, [{"full_name": "team/r/task/b.md"}])
    res = _reconciled(t, now="2026-07-01T12:30:00Z")
    assert not res.get("fast_path")


def test_fast_path_declines_on_unparseable_feed_entries():
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)
    for bad in (["just-a-string"], [{"full_name": 7}], [{"no_name": "x"}], [None]):
        _with_updates(t, bad)
        res = _reconciled(t, now="2026-07-01T12:30:00Z")
        assert not res.get("fast_path"), f"feed {bad!r} must be doubt -> full pass"


def test_fast_path_declines_on_deletion_entry():
    # LIVE-CAPTURED feed shape for a deleted file (2026-07-05, fulcra data-updates):
    # {"id": "6b369982-...", "full_name": "/team/fulcra/_scratch/del-probe.txt",
    #  "scan_state": "unscanned", "size": 6, "uploaded_at": "2026-07-05T12:46:43Z",
    #  "archived_at": null, "deleted_at": "2026-07-05T12:46:43.832485Z",
    #  "state": "deleted"}
    # -> deletions DO carry full_name; a deleted task file declines the fast path.
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)
    _with_updates(t, [{"full_name": "/team/r/task/gone.md", "state": "deleted",
                       "deleted_at": "2026-07-01T12:10:00Z"}])
    res = _reconciled(t, now="2026-07-01T12:30:00Z")
    assert not res.get("fast_path")

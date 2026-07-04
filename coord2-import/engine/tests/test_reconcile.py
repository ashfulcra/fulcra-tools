import json

from coord_engine import reconcile
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

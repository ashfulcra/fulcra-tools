"""E1 — incremental reconcile (feed-cursor fold).

Contract from docs/coord/wake-router-ADDENDUM-1-event-substrate.md §3.1 + DAG row
E1: reconcile consumes the ``data-updates`` feed since a durable cursor, reads
ONLY the changed coordination shards, and updates ``summaries.json`` in place.
The periodic full scan stays as (a) the fail-closed fallback on ANY cursor/feed
doubt and (b) a scheduled drift self-check (loud divergence ⇒ rebuild).

Red-first tests, one per DAG-row requirement plus the direct-read guarantees:
  * feed-unavailable ⇒ full pass
  * corrupt cursor ⇒ full pass
  * incremental result equals full-scan result on a fixture window
  * drift detection triggers a loud rebuild
  * an incremental pass reads ONLY the changed shards
  * a deleted shard drops its row without a full listing
  * the cursor advances across an incremental pass
"""

import json

from coord_engine import reconcile
from coord_engine.transport import TransportError


class CountingTransport:
    """In-memory store that counts reads/lists and serves a normalized feed.

    Mirrors the real ``FulcraFileTransport``: ``list_dir`` sorts by name and
    reports a byte size per entry; ``updates(since, team=...)`` returns the
    normalized ``{path, state, uploaded_at}`` shape (never the raw ``full_name``
    feed — that normalization is the transport's job, see transport.updates)."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.mtimes: dict[str, str] = {}
        self.sizes: dict[str, str] = {}
        self.reads: list[str] = []
        self.lists: list[str] = []
        self.fail_list = False
        self._feed = None  # None => no updates() support at all

    # --- seeding -----------------------------------------------------------
    def put(self, path, content, mtime="2026-07-01 04:00PM UTC", size=None):
        self.store[path] = content
        self.mtimes[path] = mtime
        self.sizes[path] = size if size is not None else f"{len(content)}B"

    def drop(self, path):
        self.store.pop(path, None)
        self.mtimes.pop(path, None)
        self.sizes.pop(path, None)

    def set_feed(self, entries):
        """entries: list of {path, state, uploaded_at} (normalized), or None."""
        self._feed = entries

    # --- transport surface -------------------------------------------------
    def updates(self, since, *, team=None):
        if self._feed is None:
            raise TypeError("no feed")  # emulate a transport without updates()
        prefix = f"team/{team}/" if team else None
        out = []
        for e in self._feed:
            if e is None:
                return None
            path = e.get("path")
            if prefix is not None and isinstance(path, str) and not path.startswith(prefix):
                continue
            out.append(e)
        return out

    def list_dir(self, prefix):
        self.lists.append(prefix)
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
                seg = head.split("/", 1)[0] + "/"
                if seg not in seen_dirs:
                    seen_dirs.add(seg)
                    out.append({"name": seg, "mtime": None, "is_dir": True})
                continue
            out.append({"name": rest, "mtime": self.mtimes.get(p),
                        "size": self.sizes.get(p), "is_dir": rest.endswith("/")})
        return sorted(out, key=lambda e: e.get("name") or "")

    def read(self, path):
        self.reads.append(path)
        return self.store.get(path)

    def write(self, path, content):
        self.store[path] = content
        return True

    def delete(self, path):
        return self.store.pop(path, None) is not None


def _task(title, status, priority="P2"):
    return f"---\ntype: Task\ntitle: {title}\nstatus: {status}\npriority: {priority}\n---\nbody"


def _up(path, state="uploaded", at="2026-07-01T12:15:30Z"):
    return {"path": path, "state": state, "uploaded_at": at}


def _run(t, now):
    return reconcile.reconcile(t, "r", now=now, today=now[:10], host="h")


def _agg(t):
    return json.loads(t.store["team/r/_coord/summaries.json"])


def _rows_by_name(agg):
    return {r["name"]: r for r in agg["rows"]}


# --- feed-unavailable ⇒ full pass ------------------------------------------

def test_feed_unavailable_falls_back_to_full_pass():
    t = CountingTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _run(t, "2026-07-01T12:00:00Z")            # seed (full scan)
    # A real change lands, but the feed is DOWN (updates() returns None).
    t.put("team/r/task/b.md", _task("Bravo", "active"))
    t.set_feed(None)
    t.lists.clear()
    res = _run(t, "2026-07-01T12:30:00Z")
    assert not res.get("incremental")           # fell back to full scan
    assert t.lists                               # a full listing was taken
    assert {r["name"] for r in _agg(t)["rows"]} == {"a", "b"}


# --- corrupt cursor ⇒ full pass --------------------------------------------

def test_corrupt_cursor_falls_back_to_full_pass():
    t = CountingTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _run(t, "2026-07-01T12:00:00Z")
    # Corrupt the persisted cursor; a change lands the feed WOULD serve.
    agg = _agg(t)
    agg[reconcile.RECONCILE_CURSOR_KEY] = "{not json"
    t.store["team/r/_coord/summaries.json"] = json.dumps(agg)
    t.put("team/r/task/b.md", _task("Bravo", "active"))
    t.set_feed([_up("team/r/task/b.md")])
    t.lists.clear()
    res = _run(t, "2026-07-01T12:30:00Z")
    assert not res.get("incremental")           # corrupt cursor => fail-closed full pass
    assert t.lists
    assert {r["name"] for r in _agg(t)["rows"]} == {"a", "b"}
    # and the cursor is rebuilt to a valid shape
    good, reason = reconcile._parse_reconcile_cursor(
        _agg(t).get(reconcile.RECONCILE_CURSOR_KEY))
    assert good is not None and reason is None


# --- incremental == full-scan on a fixture window --------------------------

def test_incremental_equals_full_scan_on_fixture_window():
    # Two transports diverge only in HOW they fold the same fixture of changes:
    #   inc_t — carries a cursor, so it folds via the feed delta;
    #   full_t — has no cursor, so it full-scans the identical end state.
    # The resulting summaries rows must be identical.
    def seed(t):
        t.put("team/r/task/a.md", _task("Alpha", "active"))
        t.put("team/r/task/b.md", _task("Bravo", "active"))
        t.put("team/r/task/c.md", _task("Charlie", "active"))

    inc_t = CountingTransport()
    seed(inc_t)
    _run(inc_t, "2026-07-01T12:00:00Z")          # seed cursor via a full scan

    # Fixture: modify b (active→done), add d, delete c — same clock-minute.
    at, mt = "2026-07-01T12:15:30Z", "2026-07-01 12:15PM UTC"
    inc_t.put("team/r/task/b.md", _task("Bravo", "done"), mtime=mt)
    inc_t.put("team/r/task/d.md", _task("Delta", "active"), mtime=mt)
    inc_t.drop("team/r/task/c.md")
    inc_t.set_feed([_up("team/r/task/b.md", at=at),
                    _up("team/r/task/d.md", at=at),
                    _up("team/r/task/c.md", state="deleted", at=at)])
    inc_res = _run(inc_t, "2026-07-01T12:30:00Z")
    assert inc_res.get("incremental") is True

    # Full-scan the identical end state on a fresh transport (no cursor).
    full_t = CountingTransport()
    full_t.put("team/r/task/a.md", _task("Alpha", "active"))
    full_t.put("team/r/task/b.md", _task("Bravo", "done"), mtime=mt)
    full_t.put("team/r/task/d.md", _task("Delta", "active"), mtime=mt)
    _run(full_t, "2026-07-01T12:30:00Z")

    inc_rows = _rows_by_name(_agg(inc_t))
    full_rows = _rows_by_name(_agg(full_t))
    assert set(inc_rows) == set(full_rows) == {"a", "b", "d"}
    for name in inc_rows:
        assert inc_rows[name] == full_rows[name], f"row {name} diverged"


# --- an incremental pass reads ONLY the changed shards ---------------------

def test_incremental_reads_only_changed_shards():
    t = CountingTransport()
    for n, ttl in (("a", "Alpha"), ("b", "Bravo"), ("c", "Charlie")):
        t.put(f"team/r/task/{n}.md", _task(ttl, "active"))
    _run(t, "2026-07-01T12:00:00Z")

    at, mt = "2026-07-01T12:15:30Z", "2026-07-01 12:15PM UTC"
    t.put("team/r/task/b.md", _task("Bravo", "done"), mtime=mt)
    t.set_feed([_up("team/r/task/b.md", at=at)])
    t.reads.clear()
    t.lists.clear()
    res = _run(t, "2026-07-01T12:30:00Z")
    assert res.get("incremental") is True
    # ONLY the changed shard b was read; a and c were reused from the aggregate.
    assert "team/r/task/b.md" in t.reads
    assert "team/r/task/a.md" not in t.reads
    assert "team/r/task/c.md" not in t.reads
    # no full listing of task/ was taken on the incremental path
    assert "team/r/task/" not in t.lists
    assert _rows_by_name(_agg(t))["b"]["status"] == "done"
    assert res["parsed"] == 1 and res["reused"] == 2


# --- a deleted shard drops its row without a full listing ------------------

def test_incremental_removes_deleted_shard():
    t = CountingTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    t.put("team/r/task/b.md", _task("Bravo", "active"))
    _run(t, "2026-07-01T12:00:00Z")
    t.drop("team/r/task/b.md")
    t.set_feed([_up("team/r/task/b.md", state="deleted")])
    t.lists.clear()
    res = _run(t, "2026-07-01T12:30:00Z")
    assert res.get("incremental") is True
    assert {r["name"] for r in _agg(t)["rows"]} == {"a"}
    assert "team/r/task/" not in t.lists


# --- drift detection triggers a loud rebuild -------------------------------

def test_drift_check_detects_and_rebuilds_loudly():
    t = CountingTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    t.put("team/r/task/b.md", _task("Bravo", "active"))
    _run(t, "2026-07-01T12:00:00Z")
    # b really changed, but the feed NEVER reports it (feed lag / dropped event).
    t.put("team/r/task/b.md", _task("Bravo", "done"), mtime="2026-07-01 12:10PM UTC")
    t.set_feed([])                               # empty but healthy feed
    # Cross the MAX_FAST_PATH_HOURS backstop so a full-scan drift self-check fires.
    res = _run(t, "2026-07-01T19:00:00Z")
    assert res.get("incremental") is not True    # the periodic full scan ran
    assert res.get("drift_detected") is True
    assert any("drift" in w.lower() and "b" in w for w in res["warnings"])
    # the rebuild adopts the authoritative full-scan value
    assert _rows_by_name(_agg(t))["b"]["status"] == "done"


# --- the cursor advances across an incremental pass ------------------------

def test_incremental_advances_cursor():
    t = CountingTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _run(t, "2026-07-01T12:00:00Z")
    first, _ = reconcile._parse_reconcile_cursor(_agg(t)[reconcile.RECONCILE_CURSOR_KEY])
    at, mt = "2026-07-01T12:15:30Z", "2026-07-01 12:15PM UTC"
    t.put("team/r/task/a.md", _task("Alpha", "done"), mtime=mt)
    t.set_feed([_up("team/r/task/a.md", at=at)])
    res = _run(t, "2026-07-01T12:30:00Z")
    assert res.get("incremental") is True
    second, reason = reconcile._parse_reconcile_cursor(
        _agg(t)[reconcile.RECONCILE_CURSOR_KEY])
    assert reason is None and second is not None
    assert second["watermark"] == "2026-07-01T12:30:00Z"
    assert second["watermark"] != first["watermark"]


# --- streak backstop forces a periodic full scan ---------------------------

def test_streak_backstop_forces_full_scan(monkeypatch):
    monkeypatch.setenv("COORD_RECONCILE_FULL_EVERY", "2")
    t = CountingTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _run(t, "2026-07-01T12:00:00Z")              # full scan, streak -> 0
    # pass 2: incremental (streak 0 -> 1)
    t.put("team/r/task/a.md", _task("Alpha", "done"), mtime="2026-07-01 12:15PM UTC")
    t.set_feed([_up("team/r/task/a.md")])
    r2 = _run(t, "2026-07-01T12:20:00Z")
    assert r2.get("incremental") is True
    # pass 3: streak now 1, +1 >= 2 => forced full scan (no change in feed)
    t.set_feed([])
    t.lists.clear()
    r3 = _run(t, "2026-07-01T12:25:00Z")
    assert r3.get("incremental") is not True
    assert "team/r/task/" in t.lists             # a real full listing happened

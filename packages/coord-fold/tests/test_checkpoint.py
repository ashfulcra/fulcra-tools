import json
from coord_fold import checkpoint as cp
from coord_fold_fakes import FakeReader, FakeStore, FakeWriter
NOW = "2026-09-04T13:45:00Z"


def _ev(kind, slug="s1", rid="r1", **kw):
    b = {"v": 1, "at": NOW, "from": "boss", "to": "me", "kind": kind, "slug": slug, "pri": "P1", "ptr": f"team/r/task/{slug}.md", "record_id": rid}
    b.update(kw)
    return b


def test_empty_has_exactly_the_eight_fields():
    assert set(cp.empty(NOW)) == {"v", "cursor", "open", "unread_events", "unreadable_pointers", "seen", "generation", "writer"}
    assert cp.empty(NOW)["generation"] == 0


def test_open_adds_close_and_release_remove_claim_annotates():
    st = cp.empty(NOW); cp.apply(st, _ev("open"))
    assert st["open"]["s1"] == {"pri": "P1", "from": "boss", "ptr": "team/r/task/s1.md", "at": NOW}
    cp.apply(st, _ev("claim", rid="r2", **{"from": "me"})); assert st["open"]["s1"]["claimed_by"] == "me"
    cp.apply(st, _ev("release", rid="r3")); assert "s1" not in st["open"]
    cp.apply(st, _ev("open", rid="r4")); cp.apply(st, _ev("close", rid="r5")); assert st["open"] == {}


def test_close_of_unknown_slug_is_a_noop():
    st = cp.empty(NOW); cp.apply(st, _ev("close")); assert st["open"] == {}


def test_a_record_id_seen_before_is_not_applied_twice():
    st = cp.empty(NOW); cp.apply(st, _ev("open", rid="same")); cp.apply(st, _ev("close", rid="same"))
    assert "s1" in st["open"]


def test_load_states_and_save_roundtrip():
    store = FakeStore({}, []); r, w = FakeReader(store), FakeWriter(store)
    assert cp.load(r, "r", "me")[1] == "fresh"
    store.docs[cp.path("r", "me")] = "not json"; assert cp.load(r, "r", "me")[1] == "corrupt"
    bad = FakeStore({}, []); bad.fail_reads = True; assert cp.load(FakeReader(bad), "r", "me")[1] == "error"
    s = cp.empty(NOW); cp.apply(s, _ev("open")); s["cursor"] = NOW
    assert cp.save(w, "r", "me", s)
    back, src = cp.load(r, "r", "me"); assert src == "ok" and back["open"] == s["open"]

"""Per-body provenance sidecar APIs in cache.py (root cause A, Step 1).

The write-soundness fix for events-mode (FULCRA_COORD_READ_SOURCE=events) needs
to know, at write time, WHERE the body it is about to upload came from and what
the fold-at-read looked like — so a fold-sourced write can do a 3-way merge
against the fold base rather than blindly clobbering newer file fields. That
read->write hand-off rides on a LOCAL-ONLY sidecar keyed by task_id, mirroring
the existing read_meta/write_meta/_meta_key pattern. These tests pin the
round-trip, the absent/malformed-safe reads, and clear().
"""

from fulcra_coord import cache


def test_write_then_read_provenance_round_trips():
    prov = {
        "source": "fold",
        "file_stat_at_read": {"version": "v1", "size": 10},
        "fold_base": {"id": "TASK-X", "current_summary": "base"},
        "fold_complete": True,
    }
    cache.write_provenance("TASK-X", prov)
    got = cache.read_provenance("TASK-X")
    assert got == prov


def test_read_provenance_absent_returns_none():
    assert cache.read_provenance("TASK-DOES-NOT-EXIST") is None


def test_clear_provenance_removes_sidecar():
    cache.write_provenance("TASK-Y", {"source": "file"})
    assert cache.read_provenance("TASK-Y") is not None
    cache.clear_provenance("TASK-Y")
    assert cache.read_provenance("TASK-Y") is None


def test_clear_provenance_missing_is_noop():
    # Clearing an absent sidecar must not raise (ignore-missing).
    cache.clear_provenance("TASK-NEVER-WRITTEN")


def test_read_provenance_malformed_returns_none():
    # Mirror read_meta's try/except: a corrupt sidecar yields None, not a crash.
    cache.write_provenance("TASK-Z", {"source": "file"})
    # Corrupt the on-disk sidecar.
    import hashlib
    key = hashlib.sha1("TASK-Z".encode()).hexdigest()[:16]
    path = cache.meta_dir() / f"{key}.prov.json"
    path.write_text("{not valid json")
    assert cache.read_provenance("TASK-Z") is None

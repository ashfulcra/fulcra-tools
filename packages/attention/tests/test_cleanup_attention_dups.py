"""Unit tests for the attention duplicate-cleanup planner (pure logic).

The fetch/delete transports are exercised with fakes; no network.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parents[1]
          / "scripts" / "cleanup_attention_dups.py")
spec = importlib.util.spec_from_file_location("cleanup_attention_dups", SCRIPT)
mod = importlib.util.module_from_spec(spec)
sys.modules["cleanup_attention_dups"] = mod
spec.loader.exec_module(mod)


def rec(rid, sid, start="2026-06-01T16:48:48+00:00",
        end="2026-06-01T16:49:48+00:00", extra_sources=()):
    return {
        "id": rid,
        "recorded_at": {"start_time": start, "end_time": end},
        "sources": [sid, *extra_sources],
    }


ATT = "com.fulcra.attention.v2.aabbccdd00112233"


class TestBuildPlan:
    def test_keeps_one_clone_deletes_rest(self):
        records = [rec("r3", ATT), rec("r1", ATT), rec("r2", ATT)]
        plan = mod.build_plan(records)
        assert plan.keep[ATT] == "r1"          # lowest (start, id) wins
        assert sorted(plan.delete) == ["r2", "r3"]
        assert plan.total_attention_records == 3

    def test_keep_choice_is_deterministic_across_input_order(self):
        a = mod.build_plan([rec("rB", ATT), rec("rA", ATT)])
        b = mod.build_plan([rec("rA", ATT), rec("rB", ATT)])
        assert a.keep == b.keep == {ATT: "rA"}
        assert a.delete == b.delete == ["rB"]

    def test_single_record_groups_delete_nothing(self):
        plan = mod.build_plan([rec("r1", ATT)])
        assert plan.delete == []
        assert plan.keep[ATT] == "r1"

    def test_non_attention_records_ignored(self):
        plan = mod.build_plan([
            rec("r1", "com.fulcra.media.lastfm.v1.0011223344556677"),
            rec("r2", "com.fulcradynamics.annotation.x"),
        ])
        assert plan.total_attention_records == 0
        assert plan.delete == []

    def test_diverging_timestamps_are_skipped_not_deleted(self):
        # Same source_id but a DIFFERENT recorded_at on the second record:
        # not the verified clone shape — must be reported, never deleted.
        records = [
            rec("r1", ATT, start="2026-06-01T16:48:48+00:00"),
            rec("r2", ATT, start="2026-06-01T17:00:00+00:00"),
            rec("r3", ATT, start="2026-06-01T16:48:48+00:00"),
        ]
        plan = mod.build_plan(records)
        assert plan.keep[ATT] == "r1"
        assert plan.delete == ["r3"]            # true clone of keeper
        assert plan.skipped_nonclone == {ATT: ["r2"]}

    def test_keep_and_delete_disjoint_across_many_groups(self):
        records = []
        for i in range(50):
            sid = f"com.fulcra.attention.v2.{i:016x}"
            records += [rec(f"k{i}", sid), rec(f"d{i}a", sid),
                        rec(f"d{i}b", sid)]
        plan = mod.build_plan(records)
        assert not set(plan.keep.values()) & set(plan.delete)
        assert len(plan.delete) == 100

    def test_version_and_day_attribution(self):
        v3 = "com.fulcra.attention.v3.ffeeddccbbaa9988"
        plan = mod.build_plan([
            rec("a1", ATT), rec("a2", ATT),
            rec("b1", v3, start="2026-05-29T08:00:00+00:00"),
            rec("b2", v3, start="2026-05-29T08:00:00+00:00"),
        ])
        assert plan.by_version == {"v2": 1, "v3": 1}
        assert plan.by_day["2026-06-01"] == 1
        assert plan.by_day["2026-05-29"] == 1


class TestChunks:
    def test_iter_chunks_covers_range_without_overlap(self):
        s = datetime(2026, 4, 1, tzinfo=timezone.utc)
        e = datetime(2026, 4, 5, 12, tzinfo=timezone.utc)
        chunks = list(mod.iter_chunks(s, e, days=2))
        assert chunks[0][0] == s
        assert chunks[-1][1] == e
        for (lo1, hi1), (lo2, _) in zip(chunks, chunks[1:]):
            assert hi1 == lo2


class TestParseRecords:
    def test_ndjson_and_array_shapes(self):
        nd = b'{"id": "a"}\n{"id": "b"}\n'
        arr = b'[{"id": "a"}, {"id": "b"}]'
        assert [r["id"] for r in mod._parse_records(nd)] == ["a", "b"]
        assert [r["id"] for r in mod._parse_records(arr)] == ["a", "b"]
        assert mod._parse_records(b"") == []


class TestRunDeletes:
    def test_journal_resume_skips_done(self, tmp_path):
        journal = tmp_path / "j"
        journal.write_text("a\n")
        calls = []
        deleted, failed = mod.run_deletes(
            ["a", "b"], lambda rid: calls.append(rid) or True,
            journal, rate_per_sec=0)
        assert calls == ["b"]
        assert deleted == 1 and failed == 0
        assert set(journal.read_text().split()) == {"a", "b"}

    def test_stops_after_consecutive_failures(self, tmp_path):
        ids = [f"x{i}" for i in range(20)]
        deleted, failed = mod.run_deletes(
            ids, lambda rid: False, tmp_path / "j", rate_per_sec=0)
        assert failed == mod.MAX_CONSECUTIVE_FAILURES
        assert deleted == 0

    def test_failure_streak_resets_on_success(self, tmp_path):
        outcomes = iter([False, False, True] * 10)
        deleted, failed = mod.run_deletes(
            [f"x{i}" for i in range(30)], lambda rid: next(outcomes),
            tmp_path / "j", rate_per_sec=0)
        assert deleted == 10 and failed == 20


class TestDeleteViaApi:
    def test_2xx_and_404_count_as_gone(self, monkeypatch):
        codes = iter([204, 404, 500])
        monkeypatch.setattr(mod, "_http",
                            lambda m, u, t: (next(codes), b""))
        ep = "https://x/{id}"
        assert mod.delete_via_api("r1", ep, "tok") is True
        assert mod.delete_via_api("r2", ep, "tok") is True
        assert mod.delete_via_api("r3", ep, "tok") is False

    def test_endpoint_template_renders_id(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            mod, "_http",
            lambda m, u, t: seen.update(method=m, url=u) or (204, b""))
        mod.delete_via_api("RID-1", "https://api/x/{id}", "tok")
        assert seen["method"] == "DELETE"
        assert seen["url"] == "https://api/x/RID-1"


class TestMultiSourceClones:
    """Live-data shape: one record can carry BOTH a v1 and a v2 attention
    source. Clones of the same visit may list overlapping-but-unequal
    attention source sets — grouping by any single source splits the visit
    and (caught by the disjointness rail on real data) can put one record
    in both keep and delete. Groups must be connected components over
    shared attention sources."""

    V1 = "com.fulcra.attention.v1.1111111111111111"
    V2 = "com.fulcra.attention.v2.2222222222222222"

    def test_overlapping_source_sets_form_one_group(self):
        records = [
            rec("r1", self.V1, extra_sources=(self.V2,)),
            rec("r2", self.V2),
            rec("r3", self.V1),
        ]
        plan = mod.build_plan(records)
        assert len(plan.keep) == 1
        assert sorted(plan.delete) == ["r2", "r3"]
        assert plan.keep[next(iter(plan.keep))] == "r1"
        assert not set(plan.keep.values()) & set(plan.delete)

    def test_disjoint_sources_stay_separate_groups(self):
        records = [rec("a1", self.V1), rec("b1", self.V2)]
        plan = mod.build_plan(records)
        assert len(plan.keep) == 2
        assert plan.delete == []


class TestFetchDuplicates:
    """Chunked fetching returns a midnight-spanning DurationAnnotation in
    BOTH adjacent day-chunks (the API matches by interval overlap), so the
    same record id can appear twice in the input. It must be deduplicated
    by id before planning — otherwise a record becomes its own clone and
    lands in keep AND delete (caught live by the disjointness rail)."""

    def test_same_record_fetched_twice_is_not_its_own_clone(self):
        r = rec("r1", ATT)
        plan = mod.build_plan([r, dict(r)])
        assert plan.delete == []
        assert list(plan.keep.values()) == ["r1"]
        assert plan.total_attention_records == 1

"""Unit 4: immutable projection generations and their publication fence."""

import json

from coord_engine.change_detection import Change, ChangeBatch, Coverage
from coord_engine import generation, projection


TEAM = "r"


class MemoryTransport:
    def __init__(self):
        self.store = {}
        self.before_current_write = None

    def read(self, path):
        return self.store.get(path)

    def write(self, path, content):
        self.store[path] = content
        return True

    def write_if_unchanged(self, path, content, expected):
        if self.before_current_write:
            self.before_current_write(self)
        if self.store.get(path) != expected:
            return False
        self.store[path] = content
        return True


def _batch(*, at="2026-08-20T00:00:00Z"):
    return ChangeBatch(
        changes=(Change("u-1", "team/r/task/a.md", "uploaded", at, "tasks"),),
        coverage={name: Coverage.DATA for name in generation.REQUIRED_SECTIONS},
        trusted=True,
    )


def _sections(state="DATA"):
    return {name: generation.SectionResult(name, state, {"rows": [name]})
            for name in generation.REQUIRED_SECTIONS}


def test_identical_inputs_have_identical_generation_id_and_bytes():
    one = generation.build_generation(
        prior_generation="prior", source_watermark="w-1", batch=_batch(),
        sections=_sections(), engine_version="2.0.0")
    two = generation.build_generation(
        prior_generation="prior", source_watermark="w-1", batch=_batch(),
        sections=_sections(), engine_version="2.0.0")

    assert one.id == two.id
    assert one.bytes == two.bytes
    assert json.loads(one.bytes)["id"] == one.id


def test_each_required_section_has_its_own_deadline_and_unknown_never_seals():
    result = generation.build_generation(
        prior_generation=None, source_watermark="w-1", batch=_batch(),
        sections=_sections())
    assert result.complete is True

    incomplete = _sections()
    incomplete["roles"] = generation.SectionResult("roles", "UNKNOWN", {"rows": []})
    result = generation.build_generation(
        prior_generation=None, source_watermark="w-1", batch=_batch(),
        sections=incomplete)
    assert result.complete is False
    assert result.incomplete == ("roles",)


def test_write_read_verified_generation_publishes_digest_bound_manifest():
    transport = MemoryTransport()
    built = generation.build_generation(
        prior_generation=None, source_watermark="w-1", batch=_batch(),
        sections=_sections(), engine_version="2.0.0")

    outcome = generation.publish(transport, TEAM, built)

    assert outcome.published is True
    generation_path = generation.generation_path(TEAM, built.id)
    current_path = generation.current_path(TEAM)
    assert transport.read(generation_path) == built.bytes
    manifest = json.loads(transport.read(current_path))
    assert manifest == {
        "generation_id": built.id,
        "source_watermark": "w-1",
        "schemas": built.schemas,
        "engine_version": "2.0.0",
        "content_digest": built.content_digest,
    }
    assert generation.load_current(transport, TEAM).id == built.id


def test_crash_after_generation_write_leaves_old_manifest_current():
    transport = MemoryTransport()
    old = generation.build_generation(
        prior_generation=None, source_watermark="old", batch=_batch(), sections=_sections())
    assert generation.publish(transport, TEAM, old).published
    old_manifest = transport.read(generation.current_path(TEAM))
    new = generation.build_generation(
        prior_generation=old.id, source_watermark="new", batch=_batch(), sections=_sections())

    outcome = generation.publish(transport, TEAM, new, fail_before_manifest=True)

    assert outcome.published is False
    assert transport.read(generation.generation_path(TEAM, new.id)) == new.bytes
    assert transport.read(generation.current_path(TEAM)) == old_manifest


def test_stale_writer_manifest_race_refuses_to_replace_newer_current():
    transport = MemoryTransport()
    old = generation.build_generation(
        prior_generation=None, source_watermark="old", batch=_batch(), sections=_sections())
    assert generation.publish(transport, TEAM, old).published
    stale = generation.build_generation(
        prior_generation=old.id, source_watermark="stale", batch=_batch(), sections=_sections())
    winner = generation.build_generation(
        prior_generation=old.id, source_watermark="winner", batch=_batch(), sections=_sections())

    def race(store):
        store.before_current_write = None
        assert generation.publish(store, TEAM, winner).published

    transport.before_current_write = race
    outcome = generation.publish(transport, TEAM, stale)

    assert outcome.published is False
    assert outcome.reason == "current manifest changed"
    assert generation.load_current(transport, TEAM).id == winner.id


def test_projection_validation_reads_only_a_digest_verified_generation():
    transport = MemoryTransport()
    built = generation.build_generation(
        prior_generation=None, source_watermark="w-1", batch=_batch(), sections=_sections())
    assert generation.publish(transport, TEAM, built).published

    section, reason = projection.generation_section(transport, TEAM, "reviews")

    assert reason == ""
    assert section == {"rows": ["reviews"]}

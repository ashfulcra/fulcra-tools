"""Unit 4: immutable projection generations and their publication fence."""

import json

import pytest

from coord_engine.change_detection import Change, ChangeBatch, Coverage
from coord_engine import generation, projection


TEAM = "r"


class MemoryTransport:
    conditional_writes_supported = True

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


class LastWriterWinsTransport(MemoryTransport):
    """Deployed-like store with explicit non-CAS publication capability."""

    conditional_writes_supported = False

    def __init__(self):
        super().__init__()
        self.conditional_write_calls = 0

    def write_if_unchanged(self, path, content, expected):
        self.conditional_write_calls += 1
        return False


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


def test_each_required_section_has_its_own_deadline_and_exhaustion_never_seals(monkeypatch):
    from coord_engine import reconcile
    from coord_engine.change_detection import Coverage

    opened = []

    class OpenDeadline:
        def __init__(self, name):
            self.name = name
            self.reads = 0

        def expired(self):
            # The roles read is a real production `_tree_section` read.  It
            # exhausts only that section; every later section still opens and
            # receives an independent deadline.
            return self.name == "roles" and self.reads > 0

    def deadline_for(name):
        opened.append(OpenDeadline(name))
        return opened[-1]

    monkeypatch.setattr(reconcile, "_section_deadline", deadline_for)
    batch = ChangeBatch(
        (), {"tasks": Coverage.CLEAR, "reviews": Coverage.CLEAR,
             "forge": Coverage.CLEAR, "presence_roles": Coverage.CLEAR,
             "acknowledgments_responses": Coverage.CLEAR}, True,
        watermark="w-1")
    transport = MemoryTransport()
    def list_dir(prefix):
        return [{"name": "one.md", "is_dir": False}] if prefix.endswith("roles/") else []

    def read_classified(_path, *, deadline):
        deadline.reads += 1
        return "---\ntype: Role\n---\n", "ok"

    transport.list_dir = list_dir
    transport.read_classified = read_classified
    sections = reconcile._generation_sections(
        transport, TEAM, batch=batch, rows=[],
        proj_state={
            "reviews": {"schema": projection.REVIEWS_SCHEMA, "complete": True, "rows": []},
            "forge": {"schema": projection.FORGE_SCHEMA, "complete": True,
                      "responsible": {}, "feedback": {}},
        })
    assert [deadline.name for deadline in opened] == list(generation.REQUIRED_SECTIONS)
    assert len({id(deadline) for deadline in opened}) == len(generation.REQUIRED_SECTIONS)
    assert sections["roles"].state == "UNKNOWN"
    assert all(sections[name].state == "CLEAR"
               for name in generation.REQUIRED_SECTIONS if name != "roles")

    result = generation.build_generation(
        prior_generation=None, source_watermark="w-1", batch=_batch(),
        sections=sections)
    assert result.complete is False
    assert result.incomplete == ("roles",)


def test_generation_sections_reject_unparseable_canonical_bytes():
    from coord_engine import reconcile
    from coord_engine.change_detection import Coverage

    class Transport(MemoryTransport):
        def list_dir(self, prefix):
            return [{"name": "broken.md", "is_dir": False}] if prefix.endswith("roles/") else []

        def read_classified(self, _path, *, deadline):
            return "not an OKF document", "ok"

    batch = ChangeBatch(
        (), {"tasks": Coverage.CLEAR, "reviews": Coverage.CLEAR,
             "forge": Coverage.CLEAR, "presence_roles": Coverage.CLEAR,
             "acknowledgments_responses": Coverage.CLEAR}, True,
        watermark="w-1")
    sections = reconcile._generation_sections(
        Transport(), TEAM, batch=batch, rows=[],
        proj_state={
            "reviews": {"schema": projection.REVIEWS_SCHEMA, "complete": True, "rows": []},
            "forge": {"schema": projection.FORGE_SCHEMA, "complete": True,
                      "responsible": {}, "feedback": {}},
        })

    assert sections["roles"].state == "UNKNOWN"


def test_generation_sections_reject_parseable_wrong_section_documents():
    """Type-correct frontmatter is insufficient when it belongs elsewhere."""
    from coord_engine import reconcile
    from coord_engine.change_detection import Coverage

    class Transport(MemoryTransport):
        def list_dir(self, prefix):
            return [{"name": "bad.md", "is_dir": False}] if prefix in {
                "team/r/roles/", "team/r/presence/", "team/r/_coord/acks/",
                "team/r/response/",
            } else []

        def read_classified(self, path, *, deadline):
            documents = {
                "team/r/roles/bad.md": "---\ntype: Ack\nagent: amy\n---\n",
                "team/r/presence/bad.md": "---\ntype: Presence\n---\n",
                "team/r/_coord/acks/bad.md": "---\ntype: Response\nagent: amy\noutcome: done\n---\n",
                "team/r/response/bad.md": "---\ntype: Response\nagent: amy\n---\n",
            }
            return documents[path], "ok"

    batch = ChangeBatch(
        (), {"tasks": Coverage.CLEAR, "reviews": Coverage.CLEAR,
             "forge": Coverage.CLEAR, "presence_roles": Coverage.CLEAR,
             "acknowledgments_responses": Coverage.CLEAR}, True,
        watermark="w-1")
    sections = reconcile._generation_sections(
        Transport(), TEAM, batch=batch, rows=[],
        proj_state={
            "reviews": {"schema": projection.REVIEWS_SCHEMA, "complete": True, "rows": []},
            "forge": {"schema": projection.FORGE_SCHEMA, "complete": True,
                      "responsible": {}, "feedback": {}},
        })

    assert all(sections[name].state == "UNKNOWN"
               for name in ("roles", "presence", "acknowledgments", "responses"))
    built = generation.build_generation(
        prior_generation=None, source_watermark="w-1", batch=_batch(), sections=sections)
    assert built.incomplete == ("roles", "presence", "acknowledgments", "responses")


def _generation_sections_for_role_documents(documents):
    from coord_engine import reconcile

    class Transport(MemoryTransport):
        def list_dir(self, prefix):
            children = {}
            for path in documents:
                if not path.startswith(prefix):
                    continue
                relative = path[len(prefix):]
                if not relative:
                    continue
                name, separator, _descendant = relative.partition("/")
                entry_name = name + "/" if separator else name
                children[entry_name] = {
                    "name": entry_name,
                    "is_dir": bool(separator),
                }
            return [children[name] for name in sorted(children)]

        def read_classified(self, path, *, deadline):
            return documents[path], "ok"

    batch = ChangeBatch(
        (), {"tasks": Coverage.CLEAR, "reviews": Coverage.CLEAR,
             "forge": Coverage.CLEAR, "presence_roles": Coverage.CLEAR,
             "acknowledgments_responses": Coverage.CLEAR}, True,
        watermark="w-1")
    return reconcile._generation_sections(
        Transport(), TEAM, batch=batch, rows=[],
        proj_state={
            "reviews": {"schema": projection.REVIEWS_SCHEMA, "complete": True,
                        "rows": []},
            "forge": {"schema": projection.FORGE_SCHEMA, "complete": True,
                      "responsible": {}, "feedback": {}},
        })


@pytest.mark.parametrize(("relative_path", "document"), [
    ("reviewer", "---\ntype: Role\n---\n"),
    ("reviewer/leases", "---\ntype: Lease\nagent: amy\n---\n"),
    ("reviewer/escalations", "---\ntype: Escalation\n---\n"),
    ("reviewer/lease/amy.md", "---\ntype: Lease\nagent: amy\n---\n"),
    ("reviewer/escalation/2026-08-20.md", "---\ntype: Escalation\n---\n"),
    ("reviewer/leases/archive/amy.md", "---\ntype: Lease\nagent: amy\n---\n"),
    ("reviewer/escalations/archive/2026-08-20.md", "---\ntype: Escalation\n---\n"),
])
def test_role_inventory_rejects_malformed_path_hierarchy(relative_path, document):
    sections = _generation_sections_for_role_documents({
        f"team/{TEAM}/roles/{relative_path}": document,
    })

    assert sections["roles"].state == "UNKNOWN"
    built = generation.build_generation(
        prior_generation=None, source_watermark="w-1", batch=_batch(),
        sections=sections)
    assert built.complete is False
    assert built.incomplete == ("roles",)


def test_role_inventory_accepts_only_canonical_role_lease_and_escalation_paths():
    documents = {
        f"team/{TEAM}/roles/reviewer.md": "---\ntype: Role\n---\n",
        f"team/{TEAM}/roles/reviewer/leases/amy.md": (
            "---\ntype: Lease\nagent: amy\n---\n"),
        f"team/{TEAM}/roles/reviewer/escalations/2026-08-20.md": (
            "---\ntype: Escalation\n---\n"),
    }

    sections = _generation_sections_for_role_documents(documents)

    assert sections["roles"].state == "DATA"
    assert [record["path"] for record in sections["roles"].value["records"]] == sorted(documents)
    built = generation.build_generation(
        prior_generation=None, source_watermark="w-1", batch=_batch(),
        sections=sections)
    assert built.complete is True
    assert built.incomplete == ()


def test_generation_sections_reject_wrong_fixed_section_shapes():
    from coord_engine import reconcile
    from coord_engine.change_detection import Coverage

    batch = ChangeBatch(
        (), {"tasks": Coverage.CLEAR, "reviews": Coverage.CLEAR,
             "forge": Coverage.CLEAR, "presence_roles": Coverage.CLEAR,
             "acknowledgments_responses": Coverage.CLEAR}, True,
        watermark="w-1")
    transport = MemoryTransport()
    transport.list_dir = lambda _prefix: []
    sections = reconcile._generation_sections(
        transport, TEAM, batch=batch, rows=[{"name": "task-without-status"}],
        proj_state={
            "reviews": {"schema": projection.REVIEWS_SCHEMA, "complete": True,
                        "rows": [{"name": "review-without-state"}]},
            "forge": {"schema": projection.FORGE_SCHEMA, "complete": True,
                      "responsible": {"pr-1": "amy"}, "feedback": {}},
        })

    assert all(sections[name].state == "UNKNOWN" for name in ("tasks", "reviews", "forge"))


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


def test_explicit_non_cas_transport_publishes_a_verified_complete_generation():
    transport = LastWriterWinsTransport()
    built = generation.build_generation(
        prior_generation=None, source_watermark="w-1", batch=_batch(),
        sections=_sections())

    outcome = generation.publish(transport, TEAM, built)

    assert outcome.published is True
    assert outcome.reason == ""
    assert transport.conditional_write_calls == 0
    assert generation.load_current(transport, TEAM).id == built.id


def test_explicit_non_cas_transport_never_advances_an_incomplete_generation():
    transport = LastWriterWinsTransport()
    old_current = "old current remains"
    transport.store[generation.current_path(TEAM)] = old_current
    sections = _sections()
    sections["roles"] = generation.SectionResult("roles", "UNKNOWN", {})
    built = generation.build_generation(
        prior_generation="old", source_watermark="w-1", batch=_batch(),
        sections=sections)

    outcome = generation.publish(transport, TEAM, built)

    assert outcome.published is False
    assert outcome.reason == "incomplete required section(s): roles"
    assert transport.read(generation.current_path(TEAM)) == old_current
    assert transport.conditional_write_calls == 0


def test_explicit_non_cas_transport_never_advances_an_unverified_generation():
    class UnverifiedGenerationTransport(LastWriterWinsTransport):
        def read(self, path):
            if "/generations/" in path and path in self.store:
                return self.store[path] + "corrupt"
            return super().read(path)

    transport = UnverifiedGenerationTransport()
    old_current = "old current remains"
    transport.store[generation.current_path(TEAM)] = old_current
    built = generation.build_generation(
        prior_generation="old", source_watermark="w-1", batch=_batch(),
        sections=_sections())

    outcome = generation.publish(transport, TEAM, built)

    assert outcome.published is False
    assert outcome.reason == "generation read verification failed"
    assert transport.read(generation.current_path(TEAM)) == old_current
    assert transport.conditional_write_calls == 0


def test_explicit_non_cas_manifest_write_failure_is_not_a_pointer_race():
    class ManifestWriteFails(LastWriterWinsTransport):
        def write(self, path, content):
            if path == generation.current_path(TEAM):
                return False
            return super().write(path, content)

    transport = ManifestWriteFails()
    built = generation.build_generation(
        prior_generation=None, source_watermark="w-1", batch=_batch(),
        sections=_sections())

    outcome = generation.publish(transport, TEAM, built)

    assert outcome.published is False
    assert outcome.reason == "current manifest write failed"
    assert outcome.reason != "current manifest changed"
    assert transport.conditional_write_calls == 0


def test_explicit_non_cas_manifest_read_failure_is_not_a_pointer_race():
    class ManifestReadVerificationFails(LastWriterWinsTransport):
        def write(self, path, content):
            if path == generation.current_path(TEAM):
                self.store[path] = content + "corrupt"
                return True
            return super().write(path, content)

    transport = ManifestReadVerificationFails()
    built = generation.build_generation(
        prior_generation=None, source_watermark="w-1", batch=_batch(),
        sections=_sections())

    outcome = generation.publish(transport, TEAM, built)

    assert outcome.published is False
    assert outcome.reason == "current manifest read verification failed"
    assert outcome.reason != "current manifest changed"
    assert transport.conditional_write_calls == 0


def test_manifest_publish_fails_closed_when_conditional_write_capability_is_unknown():
    class UnknownCapabilityTransport:
        def __init__(self):
            self.store = {}

        def read(self, path):
            return self.store.get(path)

        def write(self, path, content):
            self.store[path] = content
            return True

        def write_if_unchanged(self, path, content, expected):
            if self.store.get(path) != expected:
                return False
            self.store[path] = content
            return True

    transport = UnknownCapabilityTransport()
    built = generation.build_generation(
        prior_generation=None, source_watermark="w-1", batch=_batch(), sections=_sections())

    outcome = generation.publish(transport, TEAM, built)

    assert outcome.published is False
    assert outcome.reason == "conditional manifest write capability unknown"
    assert transport.read(generation.current_path(TEAM)) is None


def test_progress_build_id_binds_base_watermark_and_normalized_updates():
    first = generation.build_id("prior", "feed-1", _batch())
    same = generation.build_id("prior", "feed-1", _batch())
    later = generation.build_id("prior", "feed-2", _batch())

    assert first == same
    assert first != later


def test_recovery_progress_resumes_only_the_exact_immutable_build_id():
    from coord_engine import reconcile

    transport = MemoryTransport()
    build = generation.build_id("prior", "feed-1", _batch())
    progress = {"schema": "coord.projection-build-progress.v1",
                "base_generation": build, "reviews": {"scanned": 3}}
    transport.write(reconcile.projection_progress_path(TEAM, build), json.dumps(progress))

    assert reconcile._load_projection_progress(transport, TEAM, build) == progress
    assert reconcile._load_projection_progress(
        transport, TEAM, generation.build_id("prior", "feed-2", _batch())) == {}


def test_projection_validation_reads_only_a_digest_verified_generation():
    transport = MemoryTransport()
    built = generation.build_generation(
        prior_generation=None, source_watermark="w-1", batch=_batch(), sections=_sections())
    assert generation.publish(transport, TEAM, built).published

    section, reason = projection.generation_section(transport, TEAM, "reviews")

    assert reason == ""
    assert section == {"rows": ["reviews"]}

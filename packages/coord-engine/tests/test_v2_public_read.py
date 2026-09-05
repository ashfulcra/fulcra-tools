"""Unit 5: one digest-verified, bounded public-read authority."""

from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime, timezone
from hashlib import sha256

import pytest

from coord_engine import __version__, generation, okf, public_read, tasks
from coord_engine.change_detection import ChangeBatch, Coverage, NAMESPACES
from coord_engine.generation import SectionResult
from coord_engine_test_helpers import FakeTransport, needs_me_rows


TEAM = "r"
NOW = datetime(2026, 8, 21, 1, 0, tzinfo=timezone.utc)
WATERMARK = "2026-08-21T00:30:00Z"
HORIZON = "2026-08-21T00:59:30Z"
_DEFAULT_RECORD_WINDOW = object()
OVERLAP_START = "2026-08-21T00:29:30Z"


def _batch(watermark=WATERMARK):
    return ChangeBatch(
        (), {name: Coverage.CLEAR for name in NAMESPACES}, True,
        watermark=watermark,
    )


def _sections(rows=(), *, values_override=None):
    values = {
        "tasks": {"rows": list(rows)},
        "reviews": {
            "schema": generation.REVIEW_PROJECTION_SCHEMA,
            "generated_at": WATERMARK,
            "complete": True, "scanned": 0, "total": 0, "rows": [],
            "orphans": [], "orphans_unknown": [], "tombstones": [],
        },
        "forge": {
            "schema": "coord.forge.projection.v1", "generated_at": WATERMARK,
            "complete": True, "responsible": {}, "feedback": {},
        },
        "roles": {"records": []},
        "presence": {"records": []},
        "acknowledgments": {"records": []},
        "responses": {"records": []},
    }
    values.update(values_override or {})
    return {
        name: SectionResult(name, "DATA" if value[next(iter(value))] else "CLEAR", value)
        for name, value in values.items()
    }


class OverlayTransport(FakeTransport):
    public_read_v2_enabled = True
    public_read_epsilon_seconds = 30.0
    public_read_epsilon_verified = True

    def __init__(self, envelope=None, *, record_window=_DEFAULT_RECORD_WINDOW):
        super().__init__()
        self.envelope = envelope if envelope is not None else {
            "after": OVERLAP_START,
            "through": HORIZON,
            "data_types": {"MomentAnnotation/test-reconcile": 0},
            "file_changes": [],
        }
        self.feed_starts = []
        self.record_calls = 0
        self.record_window = (
            {"after": self.envelope.get("after", OVERLAP_START),
             "through": self.envelope.get("through", HORIZON), "records": []}
            if record_window is _DEFAULT_RECORD_WINDOW else record_window
        )

    def data_updates(self, since, *, deadline=None):
        self.feed_starts.append(since)
        self._synthetic_detector_config = json.dumps({
            "data_type": "MomentAnnotation/test-reconcile",
        })
        return self.envelope

    def records_cursor(self, _channel, _since, *, deadline=None):
        self.record_calls += 1
        return self.record_window


def _publish(t, *, rows=(), watermark=WATERMARK, values_override=None):
    sealed = generation.build_generation(
        prior_generation=None,
        source_watermark=watermark,
        batch=_batch(watermark),
        sections=_sections(rows, values_override=values_override),
        engine_version=__version__,
    )
    assert generation.publish(t, TEAM, sealed).published
    return sealed


def _read(t, **kwargs):
    return public_read.read_current(
        t,
        TEAM,
        now=NOW,
        epsilon_seconds=kwargs.pop("epsilon_seconds", 30.0),
        epsilon_verified=kwargs.pop("epsilon_verified", True),
        **kwargs,
    )


def _replace_section_value(t, sealed, section_name, value):
    path = generation.generation_path(TEAM, sealed.id)
    doc = json.loads(t.store[path])
    doc["sections"][section_name]["value"] = value
    forged_bytes = json.dumps(doc, separators=(",", ":"), sort_keys=True)
    t.store[path] = forged_bytes
    manifest = json.loads(t.store[generation.current_path(TEAM)])
    manifest["content_digest"] = sha256(forged_bytes.encode()).hexdigest()
    t.store[generation.current_path(TEAM)] = json.dumps(
        manifest, separators=(",", ":"), sort_keys=True,
    )


def test_valid_generation_runs_one_attested_overlap_and_surfaces_metadata():
    t = OverlayTransport()
    sealed = _publish(t)

    result = _read(t)

    assert result.rc == 0
    assert result.state.value == "CLEAR"
    assert result.generation == sealed.id
    assert result.watermark == WATERMARK
    assert result.coverage_horizon == HORIZON
    assert t.feed_starts == [OVERLAP_START]
    assert result.as_dict()["coverage"] == [
        {"surface": "current-manifest", "state": "CLEAR", "required": True},
        {"surface": "freshness-overlay", "state": "CLEAR", "required": True},
        {"surface": "immutable-generation", "state": "CLEAR", "required": True},
    ]


def test_digest_mismatch_is_unknown_and_never_runs_the_overlay():
    t = OverlayTransport()
    sealed = _publish(t)
    t.store[generation.generation_path(TEAM, sealed.id)] += " "

    result = _read(t)

    assert result.rc == 3
    assert result.state.value == "UNKNOWN"
    assert t.feed_starts == []
    assert result.coverage_by_surface("freshness-overlay").state.value == "NOT_RUN"
    assert "digest" in (result.coverage_by_surface("immutable-generation").reason or "")


def test_generation_id_must_bind_the_validated_immutable_identity():
    t = OverlayTransport()
    sealed = _publish(t)
    generation_path = generation.generation_path(TEAM, sealed.id)
    doc = json.loads(t.store[generation_path])
    doc["normalized_update_digest"] = "0" * 64
    forged_bytes = json.dumps(doc, separators=(",", ":"), sort_keys=True)
    t.store[generation_path] = forged_bytes
    manifest_path = generation.current_path(TEAM)
    manifest = json.loads(t.store[manifest_path])
    manifest["content_digest"] = sha256(forged_bytes.encode()).hexdigest()
    t.store[manifest_path] = json.dumps(
        manifest, separators=(",", ":"), sort_keys=True,
    )

    result = _read(t)

    assert result.rc == 3
    assert t.feed_starts == []
    assert "identity" in (
        result.coverage_by_surface("immutable-generation").reason or ""
    )


def test_unsupported_engine_and_section_schemas_fail_closed_before_overlay():
    t = OverlayTransport()
    sealed = _publish(t)
    generation_path = generation.generation_path(TEAM, sealed.id)
    doc = json.loads(t.store[generation_path])
    doc["engine_version"] = "0.0.0"
    for section in doc["sections"].values():
        section["schema"] = "attacker.schema.v9"
    identity = {
        "prior_generation_id": doc["prior_generation_id"],
        "source_watermark": doc["source_watermark"],
        "normalized_update_digest": doc["normalized_update_digest"],
        "schema_version": doc["schema"],
        "engine_version": doc["engine_version"],
    }
    forged_id = sha256(json.dumps(
        identity, separators=(",", ":"), sort_keys=True,
    ).encode()).hexdigest()
    doc["id"] = forged_id
    forged_bytes = json.dumps(doc, separators=(",", ":"), sort_keys=True)
    del t.store[generation_path]
    t.store[generation.generation_path(TEAM, forged_id)] = forged_bytes
    manifest = {
        "generation_id": forged_id,
        "source_watermark": WATERMARK,
        "schemas": {
            name: "attacker.schema.v9" for name in generation.REQUIRED_SECTIONS
        },
        "engine_version": "0.0.0",
        "content_digest": sha256(forged_bytes.encode()).hexdigest(),
    }
    t.store[generation.current_path(TEAM)] = json.dumps(
        manifest, separators=(",", ":"), sort_keys=True,
    )

    result = _read(t)

    assert result.rc == 3
    assert result.state.value == "UNKNOWN"
    assert t.feed_starts == []
    immutable = result.coverage_by_surface("immutable-generation")
    assert immutable.state.value == "UNKNOWN"
    assert "unsupported engine version 0.0.0" in (immutable.reason or "")
    assert result.coverage_by_surface("freshness-overlay").state.value == "NOT_RUN"


def test_unsupported_section_schema_is_rejected_even_with_supported_engine():
    t = OverlayTransport()
    sealed = _publish(t)
    path = generation.generation_path(TEAM, sealed.id)
    doc = json.loads(t.store[path])
    doc["sections"]["presence"]["schema"] = "attacker.schema.v9"
    forged_bytes = json.dumps(doc, separators=(",", ":"), sort_keys=True)
    t.store[path] = forged_bytes
    manifest = json.loads(t.store[generation.current_path(TEAM)])
    manifest["schemas"]["presence"] = "attacker.schema.v9"
    manifest["content_digest"] = sha256(forged_bytes.encode()).hexdigest()
    t.store[generation.current_path(TEAM)] = json.dumps(
        manifest, separators=(",", ":"), sort_keys=True,
    )

    result = _read(t)

    assert result.rc == 3
    assert t.feed_starts == []
    assert "unsupported presence section schema attacker.schema.v9" in (
        result.coverage_by_surface("immutable-generation").reason or ""
    )


@pytest.mark.parametrize(("value", "reason"), [
    ({}, "presence inventory must contain exactly records"),
    ({"records": {}}, "presence inventory records must be a list"),
    ({"records": ["not-a-record"]}, "presence inventory record 0 must be an object"),
    ({"records": [{
        "path": f"team/{TEAM}/presence/alice.md",
        "content": "---\ntype: Presence\nagent: alice\n---\n",
    }]}, "presence inventory record 0 fields invalid"),
    ({"records": [{
        "path": f"team/{TEAM}/roles/reviewer.md", "content": "valid",
        "frontmatter": {"type": "Role"},
    }]}, "presence inventory record 0 path outside namespace"),
    ({"records": [{
        "path": f"team/{TEAM}/presence/alice.md", "content": None,
        "frontmatter": {"type": "Presence", "agent": "alice"},
    }]}, "presence inventory record 0 content must be a string"),
])
def test_malformed_sealed_inventory_is_unknown_and_never_runs_overlay(
    value, reason,
):
    t = OverlayTransport()
    sealed = _publish(t)
    _replace_section_value(t, sealed, "presence", value)

    result = _read(t)

    assert result.rc == 3
    assert result.state.value == "UNKNOWN"
    assert t.feed_starts == []
    assert reason in (
        result.coverage_by_surface("immutable-generation").reason or ""
    )
    assert result.coverage_by_surface("freshness-overlay").state.value == "NOT_RUN"


@pytest.mark.parametrize(("section_name", "path", "frontmatter"), [
    ("roles", f"team/{TEAM}/roles/reviewer.md", {"type": "Role"}),
    ("presence", f"team/{TEAM}/presence/alice.md",
     {"type": "Presence", "agent": "alice"}),
    ("acknowledgments", f"team/{TEAM}/_coord/acks/a/alice.md",
     {"type": "Ack", "agent": "alice"}),
    ("responses", f"team/{TEAM}/_coord/responses/a/one.md",
     {"type": "Response", "agent": "alice", "outcome": "done"}),
])
def test_every_inventory_section_rejects_non_string_content(
    section_name, path, frontmatter,
):
    t = OverlayTransport()
    sealed = _publish(t)
    _replace_section_value(t, sealed, section_name, {
        "records": [{"path": path, "content": None, "frontmatter": frontmatter}],
    })

    result = _read(t)

    assert result.rc == 3
    assert t.feed_starts == []
    assert f"{section_name} inventory record 0 content must be a string" in (
        result.coverage_by_surface("immutable-generation").reason or ""
    )


@pytest.mark.parametrize(("section_name", "path", "frontmatter", "body"), [
    ("acknowledgments", f"team/{TEAM}/_coord/acks/task-1/alice.md",
     {"type": "Ack", "agent": "alice"}, "seen"),
    ("responses",
     f"team/{TEAM}/_coord/responses/task-1/20260821T010000Z-alice.md",
     {"type": "Response", "agent": "alice", "outcome": "done"},
     "handled"),
])
def test_canonical_ack_and_response_inventories_are_served_from_sealed_generation(
    section_name, path, frontmatter, body,
):
    record = _record(path, frontmatter, body)
    t = OverlayTransport()
    _publish(t, values_override={section_name: {"records": [record]}})

    authority = _read(t)

    assert authority.rc == 0
    sealed = public_read.SealedGenerationTransport(t, TEAM, authority)
    assert sealed.read(path) == record["content"]
    listing = sealed.list_dir(path.rsplit("/", 1)[0] + "/")
    assert [(item["name"], item["is_dir"]) for item in listing] == [
        (path.rsplit("/", 1)[1], False),
    ]


@pytest.mark.parametrize(("section_name", "path", "frontmatter"), [
    ("presence", f"team/{TEAM}/presence/subdir/alice.md",
     {"type": "Presence", "agent": "alice"}),
    ("presence", f"team/{TEAM}/presence/alice.txt",
     {"type": "Presence", "agent": "alice"}),
    ("presence", f"team/{TEAM}/presence/.hidden.md",
     {"type": "Presence", "agent": "alice"}),
    ("acknowledgments", f"team/{TEAM}/_coord/acks/alice.md",
     {"type": "Ack", "agent": "alice"}),
    ("acknowledgments", f"team/{TEAM}/_coord/acks/task-1/alice.txt",
     {"type": "Ack", "agent": "alice"}),
    ("responses", f"team/{TEAM}/_coord/responses/alice.md",
     {"type": "Response", "agent": "alice", "outcome": "done"}),
    ("responses", f"team/{TEAM}/_coord/responses/task-1/alice.txt",
     {"type": "Response", "agent": "alice", "outcome": "done"}),
    ("acknowledgments", f"team/{TEAM}/_coord/acks/../alice.md",
     {"type": "Ack", "agent": "alice"}),
    ("acknowledgments", f"team/{TEAM}/_coord/acks/./alice.md",
     {"type": "Ack", "agent": "alice"}),
    ("acknowledgments", f"team/{TEAM}/_coord/acks/task.md/alice.md",
     {"type": "Ack", "agent": "alice"}),
    ("acknowledgments", f"team/{TEAM}/_coord/acks/task-1/.hidden.md",
     {"type": "Ack", "agent": "alice"}),
    ("responses", f"team/{TEAM}/_coord/responses/../alice.md",
     {"type": "Response", "agent": "alice", "outcome": "done"}),
    ("responses", f"team/{TEAM}/_coord/responses/./alice.md",
     {"type": "Response", "agent": "alice", "outcome": "done"}),
    ("responses", f"team/{TEAM}/_coord/responses/task.md/alice.md",
     {"type": "Response", "agent": "alice", "outcome": "done"}),
    ("responses", f"team/{TEAM}/_coord/responses/task-1/.hidden.md",
     {"type": "Response", "agent": "alice", "outcome": "done"}),
])
def test_public_authority_rejects_noncanonical_inventory_components(
    section_name, path, frontmatter,
):
    content = okf.render_frontmatter(frontmatter) + "\nrecord\n"
    t = OverlayTransport()
    sealed = _publish(t)
    _replace_section_value(t, sealed, section_name, {"records": [{
        "path": path,
        "content": content,
        "frontmatter": okf.parse_frontmatter(content),
    }]})

    result = _read(t)

    assert result.rc == 3
    assert result.state.value == "UNKNOWN"
    assert t.feed_starts == []
    assert result.coverage_by_surface("freshness-overlay").state.value == "NOT_RUN"


@pytest.mark.parametrize(("section_name", "argv"), [
    ("presence", ["presence", "show", TEAM]),
    ("roles", ["roles", "status", TEAM, "reviewer"]),
    ("acknowledgments", ["briefing", TEAM, "--agent", "alice"]),
    ("responses", ["briefing", TEAM, "--agent", "alice"]),
])
def test_affected_public_folds_surface_malformed_inventory_at_top_level(
    capsys, monkeypatch, section_name, argv,
):
    from coord_engine import cli

    monkeypatch.setattr(cli, "_now", lambda: NOW)
    t = OverlayTransport()
    sealed = _publish(t)
    _replace_section_value(t, sealed, section_name, {"records": [{
        "path": f"team/{TEAM}/{section_name}/bad.md",
        "content": None,
        "frontmatter": {},
    }]})

    rc = cli.main([*argv, "--json"], transport=t)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 3
    assert payload["state"] == "UNKNOWN"
    assert payload["result"] is None
    assert t.feed_starts == []


def _valid_review_projection_row(name="pr1"):
    return {
        "name": name, "state": "PENDING", "settled": False,
        "pending_required": ["alice"], "required": ["alice"],
        "requested_by": "bob", "artifact": None,
        "of": "task/a", "head": "a" * 40, "mtime": None, "size": None,
        "tally": {
            "state": "PENDING", "approvals": [], "changes": [],
            "required": ["alice"], "pending_required": ["alice"],
            "evidence": "proof", "of": "task/a", "head": "a" * 40,
            "winning": {},
        },
    }


def _valid_review_projection_value():
    return {
        "schema": generation.REVIEW_PROJECTION_SCHEMA,
        "generated_at": WATERMARK,
        "complete": True,
        "scanned": 1,
        "total": 1,
        "rows": [_valid_review_projection_row()],
        "orphans": [],
        "orphans_unknown": [],
        "tombstones": [],
    }


@pytest.mark.parametrize("mutate", [
    lambda value: value["rows"][0].pop("of"),
    lambda value: value["rows"][0].pop("head"),
    lambda value: value["rows"].append(_valid_review_projection_row()),
    lambda value: value["rows"][0].update({
        "settled": True, "state": "PENDING",
    }),
    lambda value: value.update({"orphans": "pr1"}),
    lambda value: value.update({"orphans_unknown": [""]}),
    lambda value: value.update({"tombstones": {"pr1": True}}),
])
def test_public_authority_rejects_malformed_review_v3_before_overlay(mutate):
    value = _valid_review_projection_value()
    mutate(value)
    t = OverlayTransport()
    sealed = _publish(t)
    _replace_section_value(t, sealed, "reviews", value)

    result = _read(t)

    assert result.rc == 3
    assert result.state.value == "UNKNOWN"
    assert t.feed_starts == []


def test_overlay_not_run_for_unverified_epsilon_is_unknown_not_clean():
    t = OverlayTransport()
    _publish(t)

    result = _read(t, epsilon_verified=False)

    assert result.rc == 3
    assert result.coverage_horizon is None
    overlay = result.coverage_by_surface("freshness-overlay")
    assert overlay.state.value == "NOT_RUN"
    assert "epsilon" in (overlay.reason or "")
    assert t.feed_starts == []


def test_feed_that_does_not_attest_the_overlap_or_horizon_is_unknown():
    for envelope, expected in [
        ({"through": HORIZON, "data_types": {"MomentAnnotation/test-reconcile": 0},
          "file_changes": []}, "boundary"),
        ({"after": OVERLAP_START, "through": "2026-08-21T00:59:29Z",
          "data_types": {"MomentAnnotation/test-reconcile": 0},
          "file_changes": []}, "horizon"),
    ]:
        t = OverlayTransport(envelope)
        _publish(t)

        result = _read(t)

        assert result.rc == 3
        assert result.coverage_horizon is None
        assert expected in (result.coverage_by_surface("freshness-overlay").reason or "")


@pytest.mark.parametrize("record_window", [
    None,
    {"after": OVERLAP_START, "through": HORIZON, "records": []},
])
def test_nonzero_coordination_signal_without_enumerated_records_is_unknown(
    record_window,
):
    """Measured nonzero count plus no identities must never become clean."""
    t = OverlayTransport({
        "after": OVERLAP_START,
        "through": HORIZON,
        "data_types": {"MomentAnnotation/test-reconcile": 1444},
        "file_changes": [],
    }, record_window=record_window)
    _publish(t)

    result = _read(t)

    assert t.record_calls == 1
    assert result.rc == 3
    assert result.state.value == "UNKNOWN"
    assert result.coverage_horizon is None
    overlay = result.coverage_by_surface("freshness-overlay")
    assert overlay.state.value == "NOT_RUN"
    assert "feed coverage UNKNOWN" in (overlay.reason or "")


@pytest.mark.parametrize(("count", "record_window"), [
    (0, {
        "after": OVERLAP_START,
        "through": OVERLAP_START,
        "records": [],
    }),
    (2737, {
        "after": OVERLAP_START,
        "through": "2026-08-21T00:45:00Z",
        "records": [{"id": "r-1", "recorded_at": "2026-08-21T00:40:00Z"}],
    }),
])
def test_public_overlay_rejects_empty_and_nonempty_lagging_record_cursors(
    count, record_window,
):
    """The feed horizon cannot outrun the record channel it claims to cover."""
    t = OverlayTransport({
        "after": OVERLAP_START,
        "through": HORIZON,
        "data_types": {"MomentAnnotation/test-reconcile": count},
        "file_changes": [],
    }, record_window=record_window)
    _publish(t)

    result = _read(t)

    assert t.record_calls == 1
    assert result.rc == 3
    assert result.state.value == "UNKNOWN"
    assert result.coverage_horizon is None
    overlay = result.coverage_by_surface("freshness-overlay")
    assert overlay.state.value == "NOT_RUN"
    assert "record cursor coverage horizon precedes data-updates frontier" in (
        overlay.reason or ""
    )


@pytest.mark.parametrize("cursor_through", [
    HORIZON,
    "2026-08-20T20:59:30.000001-04:00",
])
def test_public_overlay_accepts_equal_or_later_record_cursor_horizon(
    cursor_through,
):
    t = OverlayTransport(record_window={
        "after": OVERLAP_START,
        "through": cursor_through,
        "records": [],
    })
    _publish(t)

    result = _read(t)

    assert result.rc == 0
    assert result.state.value == "CLEAR"
    assert result.coverage_horizon == HORIZON


def test_supported_task_delta_is_applied_and_overlap_redelivery_is_idempotent():
    changed = {
        "path": "team/r/task/a.md",
        "state": "uploaded",
        "uploaded_at": "2026-08-21T00:40:00Z",
        "update_id": "a-v2",
    }
    old_redelivery = {
        "path": "team/r/task/removed.md",
        "state": "deleted",
        "deleted_at": "2026-08-21T00:29:45Z",
        "update_id": "removed-v1",
    }
    t = OverlayTransport({
        "after": OVERLAP_START,
        "through": HORIZON,
        "data_types": {"MomentAnnotation/test-reconcile": 0},
        "file_changes": [changed, changed, old_redelivery],
    })
    _publish(t, rows=[
        {"name": "a", "id": "a", "title": "Old", "status": "active"},
        {"name": "removed", "id": "removed", "title": "Still current", "status": "active"},
    ])
    t.put(
        "team/r/task/a.md",
        "---\ntype: Task\ntitle: New\nstatus: done\npriority: P1\n---\nbody",
    )

    result = _read(t)

    assert result.rc == 0
    assert result.state.value == "DATA"
    rows = {row["name"]: row for row in result.section("tasks")["rows"]}
    assert rows["a"]["title"] == "New"
    assert rows["a"]["status"] == "done"
    assert "removed" in rows, "pre-watermark redelivery must not undo sealed state"
    assert result.applied_update_ids == ("a-v2",)


def test_unsupported_post_watermark_delta_is_unknown_with_recovery_action():
    t = OverlayTransport({
        "after": OVERLAP_START,
        "through": HORIZON,
        "data_types": {"MomentAnnotation/test-reconcile": 0},
        "file_changes": [{
            "path": "team/r/review/pr1/verdicts/head--alice.md",
            "state": "uploaded",
            "uploaded_at": "2026-08-21T00:40:00Z",
            "update_id": "verdict-v1",
        }],
    })
    _publish(t)

    result = _read(t)

    assert result.rc == 3
    overlay = result.coverage_by_surface("freshness-overlay")
    assert overlay.state.value == "UNKNOWN"
    assert "reconcile" in (overlay.reason or "")


def test_manifest_that_changes_during_overlay_is_rejected():
    class RacingTransport(OverlayTransport):
        def __init__(self):
            super().__init__()
            self.current_reads = 0
            self.armed = False

        def read(self, path):
            value = super().read(path)
            if self.armed and path == generation.current_path(TEAM):
                self.current_reads += 1
                if self.current_reads == 2:
                    return json.dumps({"generation_id": "racing-writer"})
            return value

    t = RacingTransport()
    _publish(t)
    t.armed = True

    result = _read(t)

    assert result.rc == 3
    assert "changed during freshness overlay" in (
        result.coverage_by_surface("current-manifest").reason or "")


def test_cli_status_uses_generation_rows_and_adds_public_read_metadata(capsys, monkeypatch):
    from coord_engine import cli

    monkeypatch.setattr(cli, "_now", lambda: NOW)
    t = OverlayTransport()
    sealed = _publish(t, rows=[
        {"name": "a", "id": "a", "title": "A", "status": "active"},
    ])

    rc = cli.main(["status", TEAM, "--json"], transport=t)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == {"active": 1}
    assert payload["state"] == "CLEAR"
    assert payload["generation"] == sealed.id
    assert payload["watermark"] == WATERMARK
    assert payload["coverage_horizon"] == HORIZON


def test_cli_unknown_overlay_emits_one_json_value_and_nonzero(capsys, monkeypatch):
    from coord_engine import cli

    monkeypatch.setattr(cli, "_now", lambda: NOW)
    t = OverlayTransport({
        "after": OVERLAP_START,
        "through": "2026-08-21T00:40:00Z",
        "data_types": {"MomentAnnotation/test-reconcile": 0},
        "file_changes": [],
    })
    _publish(t)

    assert cli.main(["status", TEAM, "--json"], transport=t) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "UNKNOWN"
    assert payload["result"] is None


def _put_stale_role_with_fresh_presence(t):
    from coord_engine import okf, tasks

    t.put(
        "team/r/roles/reviewer.md",
        okf.render_frontmatter({
            "type": "Role", "policy": "shared", "sla_hours": 24,
        }) + "\nrole\n",
    )
    t.put(
        "team/r/roles/reviewer/leases/alice.md",
        okf.render_frontmatter({
            "type": "Lease", "agent": "alice",
            "timestamp": "2026-08-19T00:00:00Z",
        }) + "\nlease\n",
    )
    t.put(
        f"team/r/presence/{tasks.agent_key('alice')}.md",
        okf.render_frontmatter({
            "type": "Presence", "agent": "alice",
            "timestamp": "2026-08-21T00:59:00Z",
        }) + "\npresence\n",
    )


def test_role_status_seals_lease_and_presence_into_one_liveness_fact(
    capsys, monkeypatch,
):
    from coord_engine import cli

    monkeypatch.setattr(cli, "_now", lambda: NOW)
    t = FakeTransport()
    _put_stale_role_with_fresh_presence(t)

    assert cli.main(["roles", "status", TEAM, "reviewer", "--json"], transport=t) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "LAPSED"
    assert payload["status"] != "VACANT"
    assert payload["liveness_fact"] == {
        "state": "LAPSED",
        "observations": {
            "lease": {"state": "LAPSED", "holders": ["alice"]},
            "presence": {"state": "live", "holders": ["alice"]},
        },
        "provenance": [
            "team/r/roles/reviewer/leases/",
            "team/r/presence/",
        ],
    }


def test_role_attendance_not_run_is_distinct_from_checked_truncation(
    capsys, monkeypatch,
):
    from coord_engine import cli

    monkeypatch.setattr(cli, "_now", lambda: NOW)
    t = FakeTransport()
    _put_stale_role_with_fresh_presence(t)

    assert cli.main(["roles", "status", TEAM, "reviewer", "--json"], transport=t) == 0
    not_run = json.loads(capsys.readouterr().out)
    assert not_run["attendance"] == {
        "state": "NOT_RUN", "scanned": 0, "total": 0,
        "reason": "--check-attendance not requested",
    }

    for index in range(45):
        t.put(
            f"team/r/review/pr{index}.md",
            "---\ntype: Review\nrequired: [bob]\n---\nreview",
        )
        t.put(
            f"team/r/review/pr{index}/verdicts/head--bob.md",
            "---\ntype: Verdict\nverdict: approve\n---\nverdict",
        )
    assert cli.main([
        "roles", "status", TEAM, "reviewer", "--check-attendance", "--json",
    ], transport=t) != 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["attendance"] == {
        "state": "UNKNOWN", "scanned": 40, "total": 45,
        "reason": "attendance check budget-truncated",
    }


class CanonicalPoisonTransport(OverlayTransport):
    """Generation reads work; any reopened canonical public section explodes."""

    poisoned_prefixes = (
        f"team/{TEAM}/roles/",
        f"team/{TEAM}/presence/",
        f"team/{TEAM}/review/",
        f"team/{TEAM}/_coord/forge/watch/",
        f"team/{TEAM}/_coord/forge/feedback/",
        f"team/{TEAM}/_coord/acks/",
        f"team/{TEAM}/_coord/responses/",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.canonical_attempts = []

    def list_dir(self, path):
        if self.poisoned and any(path.startswith(prefix)
                                 for prefix in self.poisoned_prefixes):
            self.canonical_attempts.append(("list", path))
            raise RuntimeError(f"live canonical listing reopened: {path}")
        return super().list_dir(path)

    def read(self, path):
        if self.poisoned and any(path.startswith(prefix)
                                 for prefix in self.poisoned_prefixes):
            self.canonical_attempts.append(("read", path))
            raise RuntimeError(f"live canonical read reopened: {path}")
        return super().read(path)


def _record(path, fields, body):
    content = okf.render_frontmatter(fields) + f"\n{body}\n"
    # Mirror the generation builder: the sealed frontmatter is the parse of
    # the exact sealed bytes, not the caller's pre-render Python values.
    return {
        "path": path,
        "content": content,
        "frontmatter": okf.parse_frontmatter(content),
    }


def test_non_task_public_folds_read_the_sealed_generation_not_live_canonical(
    capsys, monkeypatch,
):
    from coord_engine import cli

    monkeypatch.setattr(cli, "_now", lambda: NOW)
    presence_path = f"team/{TEAM}/presence/{tasks.agent_key('alice')}.md"
    review_tally = {
        "state": "APPROVED", "approvals": ["alice"], "changes": [],
        "required": ["alice"], "pending_required": [], "evidence": "proof",
        "of": "task/a", "head": "a" * 40, "round": 1,
        # a compatible writer records which shard won (review-winning-envelope)
        "winning": {"alice": {"name": "a" * 40 + "--alice--2026-08-18T21:05:53Z-0badf00d.md",
                              "verdict": "approve", "sort_key": "2026-08-18T21:05:53.000000Z"}},
    }
    values = {
        "presence": {"records": [_record(presence_path, {
            "type": "Presence", "agent": "alice", "timestamp": WATERMARK,
            "workstreams": ["activation"], "summary": "sealed",
        }, "presence")]},
        "roles": {"records": [
            _record(f"team/{TEAM}/roles/reviewer.md", {
                "type": "Role", "sla_hours": 24,
            }, "role"),
            _record(f"team/{TEAM}/roles/reviewer/leases/alice.md", {
                "type": "Lease", "agent": "alice", "timestamp": WATERMARK,
            }, "lease"),
        ]},
        "reviews": {
            "schema": generation.REVIEW_PROJECTION_SCHEMA,
            "generated_at": WATERMARK,
            "complete": True, "scanned": 1, "total": 1,
            "rows": [{
                "name": "pr1", "state": "APPROVED", "settled": True,
                "pending_required": [], "required": ["alice"],
                "requested_by": "alice", "artifact": None,
                "of": "task/a", "head": "a" * 40,
                "mtime": None, "size": None, "tally": review_tally,
            }],
            "orphans": [], "orphans_unknown": [], "tombstones": [],
        },
    }
    t = CanonicalPoisonTransport()
    t.poisoned = False
    _publish(t, values_override=values)
    t.poisoned = True

    assert cli.main(["presence", "show", TEAM, "--json"], transport=t) == 0
    presence_payload = json.loads(capsys.readouterr().out)
    assert presence_payload["result"][0]["agent"] == "alice"
    assert presence_payload["result"][0]["summary"] == "sealed"

    assert cli.main([
        "roles", "status", TEAM, "reviewer", "--json",
    ], transport=t) == 0
    role_payload = json.loads(capsys.readouterr().out)
    assert role_payload["result"]["fresh_holders"] == ["alice"]

    assert cli.main([
        "review", "status", TEAM, "pr1", "--json",
    ], transport=t) == 0
    review_payload = json.loads(capsys.readouterr().out)
    assert review_payload["result"] == {
        **review_tally, "team": TEAM, "slug": "pr1", "contract": 2,
    }


def test_needs_me_serves_validated_review_v3_without_reopening_live_review(
    capsys, monkeypatch,
):
    from coord_engine import cli

    monkeypatch.setattr(cli, "_now", lambda: NOW)
    reviews = _valid_review_projection_value()
    t = CanonicalPoisonTransport()
    t.poisoned = False
    _publish(t, values_override={"reviews": reviews})
    t.poisoned = True

    rc = cli.main([
        "needs-me", TEAM, "--agent", "alice", "--json",
    ], transport=t)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    rows = needs_me_rows(payload["result"])
    assert any(row.get("type") == "review-pending"
               and row.get("name") == "pr1" for row in rows)


def test_needs_me_rejects_digest_valid_malformed_forge_before_domain_scan(
    capsys, monkeypatch,
):
    from coord_engine import cli

    monkeypatch.setattr(cli, "_now", lambda: NOW)
    t = CanonicalPoisonTransport()
    t.poisoned = False
    sealed = _publish(t)
    _replace_section_value(t, sealed, "forge", {
        "schema": generation.FORGE_PROJECTION_SCHEMA,
        "complete": True,
        "responsible": {"pr1": "alice"},
        "feedback": {},
    })
    t.poisoned = True

    rc = cli.main([
        "needs-me", TEAM, "--agent", "alice", "--json",
    ], transport=t)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert rc == 3
    assert payload["state"] == "UNKNOWN"
    assert payload["result"] is None
    assert payload["coverage"] == [
        {"surface": "current-manifest", "state": "CLEAR", "required": True},
        {
            "surface": "freshness-overlay", "state": "NOT_RUN",
            "required": True,
            "reason": "immutable generation validation did not license overlay",
        },
        {
            "surface": "immutable-generation", "state": "UNKNOWN",
            "required": True,
            "reason": "forge projection nested structure invalid",
        },
    ]
    assert t.feed_starts == []
    assert t.canonical_attempts == []


def test_generation_backed_checked_truncation_is_top_level_unknown(
    capsys, monkeypatch,
):
    from coord_engine import cli

    monkeypatch.setattr(cli, "_now", lambda: NOW)
    values = {
        "presence": {"records": [_record(
            f"team/{TEAM}/presence/{tasks.agent_key('alice')}.md",
            {"type": "Presence", "agent": "alice", "timestamp": WATERMARK},
            "presence",
        )]},
        "roles": {"records": [
            _record(f"team/{TEAM}/roles/reviewer.md", {
                "type": "Role", "sla_hours": 24,
            }, "role"),
            _record(f"team/{TEAM}/roles/reviewer/leases/alice.md", {
                "type": "Lease", "agent": "alice", "timestamp": WATERMARK,
            }, "lease"),
        ]},
        "reviews": {
            "schema": generation.REVIEW_PROJECTION_SCHEMA,
            "generated_at": WATERMARK,
            "complete": True, "scanned": 45, "total": 45,
            "rows": [{
                "name": f"pr{index}", "state": "PENDING", "settled": False,
                "pending_required": ["bob"], "required": ["bob"],
                "requested_by": "bob", "artifact": None,
                "of": None, "head": None, "mtime": None, "size": None,
                "tally": {
                    "state": "PENDING", "approvals": [], "changes": [],
                    "required": ["bob"], "pending_required": ["bob"], "winning": {},
                    "evidence": "", "of": None,
                },
            } for index in range(45)],
            "orphans": [], "orphans_unknown": [], "tombstones": [],
        },
    }
    t = CanonicalPoisonTransport()
    t.poisoned = False
    _publish(t, values_override=values)
    t.poisoned = True

    rc = cli.main([
        "roles", "status", TEAM, "reviewer", "--check-attendance", "--json",
    ], transport=t)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 3
    assert payload["state"] == "UNKNOWN"
    assert payload["result"]["attendance"] == {
        "state": "UNKNOWN", "scanned": 40, "total": 45,
        "reason": "attendance check budget-truncated",
    }
    assert any(item["surface"] == "domain-result"
               and item["state"] == "UNKNOWN" for item in payload["coverage"])


MIGRATED_PUBLIC_FOLDS = [
    ["status", TEAM],
    ["board", TEAM],
    ["needs-me", TEAM, "--agent", "alice"],
    ["search", TEAM, "A"],
    ["inbox", TEAM, "--agent", "alice"],
    ["digest", TEAM],
    ["asks", TEAM],
    ["review", "status", TEAM, "pr1"],
    ["roles", "status", TEAM, "reviewer"],
    ["presence", "show", TEAM],
    ["briefing", TEAM, "--agent", "alice"],
]


def _public_fold_transport():
    t = OverlayTransport()
    _publish(
        t,
        rows=[
            {"name": "a", "id": "a", "title": "A", "description": "",
             "status": "active", "priority": "P2", "assignee": "alice",
             "tags": [], "acked_by": []},
        ],
        values_override={
            "reviews": {
                "schema": generation.REVIEW_PROJECTION_SCHEMA,
                "generated_at": WATERMARK, "complete": True,
                "scanned": 1, "total": 1,
                "rows": [{
                    "name": "pr1", "state": "PENDING", "settled": False,
                    "pending_required": [], "required": [],
                    "requested_by": "alice", "artifact": None,
                    "of": None, "head": None, "mtime": None, "size": None,
                    "tally": {
                        "state": "PENDING", "approvals": [], "changes": [],
                        "required": [], "pending_required": [], "evidence": "", "winning": {},
                        "of": None,
                    },
                }],
                "orphans": [], "orphans_unknown": [], "tombstones": [],
            },
        },
    )
    t.put(
        "team/r/review/pr1.md",
        okf.render_frontmatter({
            "type": "Review", "required": [], "requested_by": "alice",
        }) + "\nreview\n",
    )
    t.put(
        "team/r/roles/reviewer.md",
        okf.render_frontmatter({"type": "Role", "sla_hours": 24}) + "\nrole\n",
    )
    t.put(
        "team/r/roles/reviewer/leases/alice.md",
        okf.render_frontmatter({
            "type": "Lease", "agent": "alice", "timestamp": WATERMARK,
        }) + "\nlease\n",
    )
    t.put(
        f"team/r/presence/{tasks.agent_key('alice')}.md",
        okf.render_frontmatter({
            "type": "Presence", "agent": "alice", "timestamp": WATERMARK,
        }) + "\npresence\n",
    )
    return t


def test_every_migrated_public_fold_has_one_value_json_and_text_metadata_parity(
    capsys, monkeypatch,
):
    from coord_engine import cli

    monkeypatch.setattr(cli, "_now", lambda: NOW)
    for argv in MIGRATED_PUBLIC_FOLDS:
        json_transport = _public_fold_transport()
        json_rc = cli.main([*argv, "--json"], transport=json_transport)
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["state"] in ("CLEAR", "DATA"), argv
        assert payload["generation"], argv
        assert payload["watermark"] == WATERMARK, argv
        assert payload["coverage_horizon"] == HORIZON, argv
        assert json_transport.feed_starts == [OVERLAP_START], argv

        text_transport = _public_fold_transport()
        text_rc = cli.main(argv, transport=text_transport)
        text = capsys.readouterr().out
        assert text_rc == json_rc, argv
        assert f"public-read: {payload['state']}" in text, argv
        assert f"generation={payload['generation']}" in text, argv
        assert f"watermark={payload['watermark']}" in text, argv
        assert f"coverage_horizon={payload['coverage_horizon']}" in text, argv
        assert text_transport.feed_starts == [OVERLAP_START], argv


def test_deployed_transport_keeps_generation_serving_dormant():
    from coord_engine import cli
    from coord_engine.transport import FulcraFileTransport

    transport = FulcraFileTransport(command=["fulcra-api"])
    args = Namespace(func=cli.cmd_status, team=TEAM)

    assert cli._begin_v2_public_read(args, transport) is None


def test_projection_generation_section_cannot_bypass_an_unrun_overlay():
    from coord_engine import projection

    t = OverlayTransport()
    _publish(t)
    t.public_read_epsilon_verified = False

    section, reason = projection.generation_section(
        t, TEAM, "tasks", now=NOW,
    )

    assert section is None
    assert "NOT_RUN" in reason
    assert "epsilon" in reason
    assert t.feed_starts == []


def test_domain_failure_remains_nonzero_and_makes_public_state_unknown(
    capsys, monkeypatch,
):
    from coord_engine import cli

    monkeypatch.setattr(cli, "_now", lambda: NOW)
    json_transport = OverlayTransport()
    _publish(json_transport)

    json_rc = cli.main(
        ["review", "status", TEAM, "missing", "--json"],
        transport=json_transport,
    )
    json_capture = capsys.readouterr()
    payload = json.loads(json_capture.out)

    assert json_rc == 1, "preserve the review fold's stronger domain rc"
    assert payload["state"] == "UNKNOWN"
    assert payload["result"] is None
    assert any(
        item["surface"] == "domain-result" and item["state"] == "UNKNOWN"
        for item in payload["coverage"]
    )

    text_transport = OverlayTransport()
    _publish(text_transport)
    text_rc = cli.main(
        ["review", "status", TEAM, "missing"], transport=text_transport,
    )
    text_capture = capsys.readouterr()
    assert text_rc == json_rc
    assert "public-read: UNKNOWN" in text_capture.out
    assert "domain-result: UNKNOWN" in text_capture.err


def test_projection_generation_section_rejects_unknown_section_name():
    from coord_engine import projection

    t = OverlayTransport()
    _publish(t)

    section, reason = projection.generation_section(
        t, TEAM, "not-a-section", now=NOW,
    )

    assert section is None
    assert "unrecognized" in reason

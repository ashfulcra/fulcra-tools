"""Unit 5: one digest-verified, bounded public-read authority."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256

from coord_engine import __version__, generation, okf, public_read, tasks
from coord_engine.change_detection import ChangeBatch, Coverage, NAMESPACES
from coord_engine.generation import SectionResult
from coord_engine_test_helpers import FakeTransport


TEAM = "r"
NOW = datetime(2026, 8, 21, 1, 0, tzinfo=timezone.utc)
WATERMARK = "2026-08-21T00:30:00Z"
HORIZON = "2026-08-21T00:59:30Z"
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
            "schema": "coord.reviews.projection.v2", "generated_at": WATERMARK,
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

    def __init__(self, envelope=None):
        super().__init__()
        self.envelope = envelope if envelope is not None else {
            "after": OVERLAP_START,
            "through": HORIZON,
            "data_types": {"MomentAnnotation/test-reconcile": 0},
            "file_changes": [],
        }
        self.feed_starts = []

    def data_updates(self, since, *, deadline=None):
        self.feed_starts.append(since)
        self._synthetic_detector_config = json.dumps({
            "data_type": "MomentAnnotation/test-reconcile",
        })
        return self.envelope


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
          "file_changes": []}, "overlap"),
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
        f"team/{TEAM}/_coord/acks/",
        f"team/{TEAM}/response/",
    )

    def list_dir(self, path):
        if self.poisoned and any(path.startswith(prefix)
                                 for prefix in self.poisoned_prefixes):
            raise RuntimeError(f"live canonical listing reopened: {path}")
        return super().list_dir(path)

    def read(self, path):
        if self.poisoned and any(path.startswith(prefix)
                                 for prefix in self.poisoned_prefixes):
            raise RuntimeError(f"live canonical read reopened: {path}")
        return super().read(path)


def _record(path, fields, body):
    content = okf.render_frontmatter(fields) + f"\n{body}\n"
    return {"path": path, "content": content, "frontmatter": fields}


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
            "schema": "coord.reviews.projection.v2", "generated_at": WATERMARK,
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
            "schema": "coord.reviews.projection.v2", "generated_at": WATERMARK,
            "complete": True, "scanned": 45, "total": 45,
            "rows": [{
                "name": f"pr{index}", "state": "PENDING", "settled": False,
                "pending_required": ["bob"], "required": ["bob"],
                "requested_by": "bob", "artifact": None,
                "of": None, "head": None, "mtime": None, "size": None,
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
                "schema": "coord.reviews.projection.v2",
                "generated_at": WATERMARK, "complete": True,
                "scanned": 1, "total": 1,
                "rows": [{
                    "name": "pr1", "state": "PENDING", "settled": False,
                    "pending_required": [], "required": [],
                    "requested_by": "alice", "artifact": None,
                    "of": None, "head": None, "mtime": None, "size": None,
                    "tally": {
                        "state": "PENDING", "approvals": [], "changes": [],
                        "required": [], "pending_required": [], "evidence": "",
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


def test_deployed_transport_keeps_public_reads_gated_until_unit6_measurement():
    from coord_engine.transport import FulcraFileTransport

    transport = FulcraFileTransport(command=["fulcra-api"])
    assert transport.public_read_v2_enabled is True
    assert transport.public_read_epsilon_seconds > 0
    assert transport.public_read_epsilon_verified is False


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

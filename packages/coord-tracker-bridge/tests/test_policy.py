import json

import pytest

from coord_tracker_bridge import load_policy


def test_bundled_policy_is_versioned_and_hashed():
    policy = load_policy()

    assert policy.version == "2"
    assert len(policy.hash) == 64
    assert policy.owns("labels") == "merge"
    assert policy.included_lanes == frozenset({
        "active", "blocked", "backlog", "asks", "threads-missed",
    })


def test_policy_hash_changes_with_semantics(tmp_path):
    one = load_policy()
    doc = dict(one.document)
    doc["close_absent"] = False
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(doc))

    assert load_policy(path).hash != one.hash


def test_taxonomy_cardinality_is_bounded(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "version": "1",
        "managed_labels": ["a", "b"],
        "max_managed_labels": 1,
    }))

    with pytest.raises(ValueError, match="taxonomy"):
        load_policy(path)


def test_invalid_field_owner_is_rejected(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": "1", "field_ownership": {"title": "both"}}))

    with pytest.raises(ValueError, match="ownership"):
        load_policy(path)


def test_omitted_lane_allowlist_excludes_every_lane(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": "2"}))

    assert load_policy(path).included_lanes == frozenset()


def test_lane_allowlist_must_be_a_list(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": "2", "included_lanes": "active"}))

    with pytest.raises(ValueError, match="included_lanes"):
        load_policy(path)


def test_each_included_lane_requires_an_explicit_semantic_state(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": "2", "included_lanes": ["active"]}))

    with pytest.raises(ValueError, match="missing lane_states"):
        load_policy(path)

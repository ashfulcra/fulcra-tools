"""Tests for the definition resolver."""
from __future__ import annotations

import pytest

from fulcra_common.definitions import (
    DefinitionSchemaMismatch,
    _spec_matches,
)


def test_spec_matches_moment_by_annotation_type_only():
    existing = {"annotation_type": "moment", "name": "x"}
    assert _spec_matches(existing, {"annotation_type": "moment"}) is True
    assert _spec_matches(existing, {"annotation_type": "duration"}) is False


def test_spec_matches_duration_compares_measurement_spec():
    spec = {
        "annotation_type": "duration",
        "measurement_spec": {"unit": "seconds", "kind": "interval"},
    }
    existing = dict(spec, name="y")
    assert _spec_matches(existing, spec) is True

    different_unit = dict(spec)
    different_unit["measurement_spec"] = {"unit": "minutes", "kind": "interval"}
    assert _spec_matches(existing, different_unit) is False


def test_spec_matches_mixed_types_never_match():
    existing = {"annotation_type": "duration",
                "measurement_spec": {"unit": "seconds"}}
    assert _spec_matches(existing, {"annotation_type": "moment"}) is False


def test_definition_schema_mismatch_message_includes_both_shapes():
    existing = {"annotation_type": "duration",
                "measurement_spec": {"unit": "seconds"}}
    expected = {"annotation_type": "moment"}
    err = DefinitionSchemaMismatch("attention", existing, expected)
    msg = str(err)
    assert "attention" in msg
    assert "duration" in msg
    assert "moment" in msg

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


class _FakeClient:
    """A fake fulcra_client for resolver tests. Records every call."""

    def __init__(self, existing: list[dict] | None = None) -> None:
        self.existing = list(existing or [])
        self.list_calls: list[dict] = []
        self.create_calls: list[dict] = []
        self._next_id = 100

    def list_definitions(self, *, name: str) -> list[dict]:
        self.list_calls.append({"name": name})
        return [d for d in self.existing if d.get("name") == name]

    def create_definition(self, *, name: str, **spec) -> dict:
        self.create_calls.append({"name": name, **spec})
        self._next_id += 1
        new = {"id": f"new-{self._next_id}", "name": name, **spec}
        self.existing.append(new)
        return new


from fulcra_common.definitions import resolve_definition_id


def test_resolve_creates_when_not_found():
    client = _FakeClient()
    out = resolve_definition_id(
        canonical_name="attention",
        expected_spec={"annotation_type": "duration",
                       "measurement_spec": {"unit": "seconds"}},
        fulcra_client=client,
    )
    assert out == "new-101"
    assert client.create_calls == [
        {"name": "attention", "annotation_type": "duration",
         "measurement_spec": {"unit": "seconds"}}
    ]


def test_resolve_adopts_existing_when_schema_matches():
    spec = {"annotation_type": "moment"}
    client = _FakeClient(existing=[{"id": "abc", "name": "lastfm-listens", **spec}])
    out = resolve_definition_id(
        canonical_name="lastfm-listens",
        expected_spec=spec, fulcra_client=client,
    )
    assert out == "abc"
    assert client.create_calls == []   # never created — adopted


def test_resolve_raises_on_schema_mismatch():
    client = _FakeClient(existing=[
        {"id": "abc", "name": "attention",
         "annotation_type": "moment"},
    ])
    with pytest.raises(DefinitionSchemaMismatch) as exc_info:
        resolve_definition_id(
            canonical_name="attention",
            expected_spec={"annotation_type": "duration",
                           "measurement_spec": {"unit": "seconds"}},
            fulcra_client=client,
        )
    assert exc_info.value.name == "attention"
    assert client.create_calls == []


def test_resolve_force_new_creates_even_when_match_exists():
    spec = {"annotation_type": "moment"}
    client = _FakeClient(existing=[{"id": "abc", "name": "attention", **spec}])
    out = resolve_definition_id(
        canonical_name="attention", expected_spec=spec,
        fulcra_client=client, force_new=True, machine_id="mini",
    )
    assert out == "new-101"
    assert client.create_calls == [
        {"name": "attention (mini)", "annotation_type": "moment"}
    ]


def test_resolve_force_new_defaults_machine_id_to_platform_node(monkeypatch):
    monkeypatch.setattr("platform.node", lambda: "Ash-MacBook.local")
    client = _FakeClient()
    out = resolve_definition_id(
        canonical_name="attention",
        expected_spec={"annotation_type": "moment"},
        fulcra_client=client, force_new=True,
    )
    assert out == "new-101"
    # Hostname suffix only the first dotted component:
    assert client.create_calls == [
        {"name": "attention (Ash-MacBook)", "annotation_type": "moment"}
    ]

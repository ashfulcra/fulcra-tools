"""Annotation-definition resolver — shared by every typed-annotation
plugin.

Multi-machine coherence works by adopting an existing Fulcra annotation
definition with the same canonical name across machines, instead of
each machine creating its own duplicate. See
`docs/superpowers/specs/2026-05-23-fulcra-common-definition-resolver-design.md`.
"""
from __future__ import annotations

import platform
from typing import Any


class DefinitionSchemaMismatch(RuntimeError):
    """Raised when an existing Fulcra definition with the requested
    canonical name has a schema the caller did not expect. The caller
    (typically the menubar) is expected to either retry with
    `force_new=True` or change the canonical name."""

    def __init__(self, name: str, existing: dict, expected: dict) -> None:
        self.name = name
        self.existing = existing
        self.expected = expected
        super().__init__(
            f"Fulcra definition {name!r} exists but its schema does not "
            f"match what the plugin expects; existing={existing}, "
            f"expected={expected}"
        )


def _spec_matches(existing: dict, expected: dict) -> bool:
    """Compare `existing` (as returned by Fulcra) with `expected` (as
    declared by the plugin). For Moment annotations only the
    `annotation_type` is compared (Moments carry no measurement_spec).
    For Duration annotations both `annotation_type` and
    `measurement_spec` must match."""
    if existing.get("annotation_type") != expected.get("annotation_type"):
        return False
    if expected.get("annotation_type") == "moment":
        return True
    return existing.get("measurement_spec") == expected.get("measurement_spec")


def resolve_definition_id(
    *,
    canonical_name: str,
    expected_spec: dict,
    fulcra_client: Any,
    force_new: bool = False,
    machine_id: str | None = None,
) -> str:
    """Find an existing Fulcra definition with `canonical_name`, or
    create one. Returns the definition's id.

    `expected_spec` is the shape the plugin expects: at minimum an
    `annotation_type` key; for Duration annotations also a
    `measurement_spec` dict.

    `fulcra_client` exposes `list_definitions(name=...)` and
    `create_definition(name=..., **spec)`. It is injected (not built
    here) so tests can pass a fake and so the resolver itself never
    holds an HTTP connection.

    `force_new=True` always creates a new definition. The name carries
    `machine_id` (or `platform.node()`'s first dotted component) as a
    suffix so the new and existing defs are distinguishable in Fulcra.

    Raises `DefinitionSchemaMismatch` when an existing def with the
    same name has a different schema."""
    if force_new:
        suffix = machine_id or platform.node().split(".", 1)[0]
        new_name = f"{canonical_name} ({suffix})"
        return fulcra_client.create_definition(name=new_name, **expected_spec)["id"]

    candidates = fulcra_client.list_definitions(name=canonical_name)
    if not candidates:
        return fulcra_client.create_definition(name=canonical_name, **expected_spec)["id"]

    existing = candidates[0]
    if _spec_matches(existing, expected_spec):
        return existing["id"]
    raise DefinitionSchemaMismatch(canonical_name, existing, expected_spec)

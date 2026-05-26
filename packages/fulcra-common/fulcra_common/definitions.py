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
        existing_shape = {
            "annotation_type": existing.get("annotation_type"),
            "measurement_spec": existing.get("measurement_spec"),
        }
        super().__init__(
            f"Fulcra definition {name!r} exists but its schema does not "
            f"match what the plugin expects; existing={existing_shape}, "
            f"expected={expected}"
        )


def _spec_matches(existing: dict, expected: dict) -> bool:
    """Compare `existing` (as returned by Fulcra) with `expected` (as
    declared by the plugin). For Moment annotations only the
    `annotation_type` is compared (Moments carry no measurement_spec).
    For Duration annotations the annotation_type must match AND every
    field the *expected* measurement_spec specifies must equal what's
    in existing.

    Permissive on Fulcra-side extras: when Fulcra adds server-side
    defaults like `metric_kind: discrete` that the client never sent,
    the def is still semantically what the client wanted. Strict
    equality used to mis-flag those as schema mismatches and rejected
    cross-plugin def adoption (the 2026-05-26 Apple Podcasts +
    Generic RSS failures both hit this). Compare each expected field
    individually; ignore extras on the existing side.
    """
    if existing.get("annotation_type") != expected.get("annotation_type"):
        return False
    if expected.get("annotation_type") == "moment":
        return True
    exp_ms = expected.get("measurement_spec") or {}
    cur_ms = existing.get("measurement_spec") or {}
    for k, v in exp_ms.items():
        if cur_ms.get(k) != v:
            return False
    return True


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
        suffix = machine_id or platform.node().split(".", 1)[0] or "unknown-host"
        new_name = f"{canonical_name} ({suffix})"
        return fulcra_client.create_definition(name=new_name, **expected_spec)["id"]

    candidates = fulcra_client.list_definitions(name=canonical_name)
    if not candidates:
        return fulcra_client.create_definition(name=canonical_name, **expected_spec)["id"]

    # Sort deterministically so every machine converges on the same def.
    # Prefer oldest by created_at (matching the attention package's
    # _find_attention_definition policy); fall back to id-sort when
    # created_at is absent so order is still stable.
    candidates = sorted(
        candidates,
        key=lambda d: (d.get("created_at") is None, d.get("created_at", ""), d.get("id", "")),
    )
    existing = candidates[0]
    if _spec_matches(existing, expected_spec):
        return existing["id"]
    raise DefinitionSchemaMismatch(canonical_name, existing, expected_spec)

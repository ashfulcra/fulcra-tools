"""Annotation-definition resolver — shared by every typed-annotation
plugin.

Multi-machine coherence works by adopting an existing Fulcra annotation
definition with the same canonical name across machines, instead of
each machine creating its own duplicate. See
`docs/superpowers/specs/2026-05-23-fulcra-common-definition-resolver-design.md`.
"""
from __future__ import annotations

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

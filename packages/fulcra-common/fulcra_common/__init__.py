"""Shared Fulcra API client for the fulcra-tools packages."""
from __future__ import annotations

from .client import (
    DEFAULT_BASE_URL,
    DEFINITION_UPDATABLE_FIELDS,
    BaseFulcraClient,
    ImportResult,
    merge_definition_update,
    validate_definition_update,
)
from .definitions import DefinitionSchemaMismatch, resolve_definition_id
from .ingest import (
    DurationEvent,
    IngestableEvent,
    IngestPipeline,
    MomentEvent,
)

__all__ = [
    "BaseFulcraClient",
    "ImportResult",
    "DEFAULT_BASE_URL",
    "DEFINITION_UPDATABLE_FIELDS",
    "merge_definition_update",
    "validate_definition_update",
    "resolve_definition_id",
    "DefinitionSchemaMismatch",
    "IngestableEvent",
    "MomentEvent",
    "DurationEvent",
    "IngestPipeline",
]

# Bind the `annotations` submodule as a package attribute. Without this,
# `from fulcra_common import annotations` resolves to the name bound at the TOP
# of this module by `from __future__ import annotations` (a `__future__._Feature`),
# not the submodule — a silent shadowing footgun. A plain `from . import
# annotations` does NOT fix it: the future-feature attribute already exists on
# the package, so the from-import binds THAT instead of importing the submodule.
# Import it explicitly and overwrite the module-level name (which IS the package
# attribute). Done at the BOTTOM so it can't participate in an import cycle.
from importlib import import_module as _import_module  # noqa: E402

annotations = _import_module(f"{__name__}.annotations")  # noqa: E402

__all__.append("annotations")

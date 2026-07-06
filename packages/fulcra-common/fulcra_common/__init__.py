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

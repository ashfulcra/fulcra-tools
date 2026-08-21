"""Immutable, digest-addressed projection generations.

The current pointer is deliberately tiny.  A builder first seals every
required section into one deterministic JSON document, writes and reads that
document back, and only then advances ``current.json``.  Progress that cannot
make that proof remains outside this module's public paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Mapping, Optional

from . import __version__


GENERATION_SCHEMA = "coord.projections.generation.v1"
SECTION_SCHEMA = "coord.projection-section.v1"
REVIEW_PROJECTION_SCHEMA = "coord.reviews.projection.v3"
FORGE_PROJECTION_SCHEMA = "coord.forge.projection.v1"
REQUIRED_SECTIONS = (
    "tasks", "reviews", "forge", "roles", "presence", "acknowledgments",
    "responses",
)
COMPLETE_STATES = frozenset(("CLEAR", "DATA"))
# A public reader accepts identifiers we ship, not arbitrary strings whose only
# credential is that the mutable manifest and immutable document repeat them.
# Keep the registry here beside the builder so writers and readers cannot drift.
SUPPORTED_ENGINE_VERSIONS = frozenset((__version__,))
SUPPORTED_SECTION_SCHEMAS = {
    name: frozenset((SECTION_SCHEMA,)) for name in REQUIRED_SECTIONS
}
INVENTORY_PREFIXES = {
    "roles": "roles/",
    "presence": "presence/",
    "acknowledgments": "_coord/acks/",
    "responses": "response/",
}


def inventory_prefix(team: str, section: str) -> Optional[str]:
    relative = INVENTORY_PREFIXES.get(section)
    return f"team/{team}/{relative}" if relative is not None else None


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def canonical_inventory_document(
    section: str, path: str, document: Mapping[str, Any],
) -> bool:
    """One semantic classifier shared by generation writers and readers."""
    doc_type = document.get("type")
    if section == "roles":
        relative = path.split("/roles/", 1)[-1]
        parts = relative.split("/")
        if len(parts) == 1:
            role_file = parts[0]
            return (role_file.endswith(".md") and len(role_file) > len(".md")
                    and doc_type == "Role")
        if (len(parts) != 3 or not parts[0]
                or parts[0].endswith(".md")
                or not parts[2].endswith(".md")
                or len(parts[2]) <= len(".md")):
            return False
        if parts[1] == "leases":
            return doc_type == "Lease" and _nonempty_text(document.get("agent"))
        if parts[1] == "escalations":
            return doc_type == "Escalation"
        return False
    if section == "presence":
        return doc_type == "Presence" and _nonempty_text(document.get("agent"))
    if section == "acknowledgments":
        return doc_type == "Ack" and _nonempty_text(document.get("agent"))
    if section == "responses":
        return (doc_type == "Response" and _nonempty_text(document.get("agent"))
                and _nonempty_text(document.get("outcome")))
    return False


def review_tally_reason(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "tally must be an object"
    required = {
        "state", "approvals", "changes", "required", "pending_required",
        "evidence", "of",
    }
    if not required.issubset(value):
        return "tally fields invalid"
    if value.get("state") not in ("PENDING", "APPROVED", "CHANGES"):
        return "tally state invalid"
    for key in ("approvals", "changes", "required", "pending_required"):
        items = value.get(key)
        if (not isinstance(items, list)
                or not all(_nonempty_text(item) for item in items)):
            return f"tally {key} invalid"
    if not isinstance(value.get("evidence"), str):
        return "tally evidence invalid"
    if value.get("of") is not None and not _nonempty_text(value.get("of")):
        return "tally of invalid"
    if "head" in value and value.get("head") is not None and not _nonempty_text(value.get("head")):
        return "tally head invalid"
    return ""


def review_row_reason(row: Any) -> str:
    if not isinstance(row, Mapping):
        return "row must be an object"
    if not _nonempty_text(row.get("name")):
        return "row name invalid"
    if row.get("state") not in ("PENDING", "APPROVED", "CHANGES"):
        return "row state invalid"
    if not isinstance(row.get("settled"), bool):
        return "row settled invalid"
    tally_reason = review_tally_reason(row.get("tally"))
    if tally_reason:
        return tally_reason
    tally = row["tally"]
    if tally.get("state") != row.get("state"):
        return "row/tally state mismatch"
    if tally.get("pending_required") != row.get("pending_required"):
        return "row/tally pending_required mismatch"
    if tally.get("required") != row.get("required"):
        return "row/tally required mismatch"
    return ""


def _json(value: Any) -> str:
    """The one compact, key-sorted encoding used for all generation bytes."""
    return json.dumps(value, separators=(",", ":"), sort_keys=True,
                      ensure_ascii=False, default=_json_default)


def _json_default(value: Any) -> Any:
    # ChangeBatch deliberately freezes mappings with MappingProxyType and uses
    # Enums for coverage.  Both are semantic values, not builder identity.
    if isinstance(value, Mapping):
        return dict(value)
    raw = getattr(value, "value", None)
    if isinstance(raw, str):
        return raw
    raise TypeError(f"not generation-json: {type(value).__name__}")


def _digest(value: Any) -> str:
    return sha256(_json(value).encode("utf-8")).hexdigest()


def generation_path(team: str, generation_id: str) -> str:
    return f"team/{team}/_coord/projections/generations/{generation_id}.json"


def current_path(team: str) -> str:
    return f"team/{team}/_coord/projections/current.json"


@dataclass(frozen=True)
class SectionResult:
    """Pure output of one independently budgeted projection section."""

    name: str
    state: str
    value: Mapping[str, Any]
    schema: str = SECTION_SCHEMA

    @property
    def complete(self) -> bool:
        return self.state in COMPLETE_STATES

    def document(self) -> dict[str, Any]:
        return {"schema": self.schema, "state": self.state, "value": dict(self.value)}


@dataclass(frozen=True)
class Generation:
    id: str
    bytes: str
    content_digest: str
    source_watermark: str
    schemas: dict[str, str]
    engine_version: str
    complete: bool
    incomplete: tuple[str, ...]


@dataclass(frozen=True)
class PublishOutcome:
    published: bool
    reason: str = ""


def _batch_digest(batch: Any) -> str:
    """Digest the sealed detector output without host/session observations."""
    changes = []
    for change in getattr(batch, "changes", ()):
        changes.append({
            "update_id": change.update_id, "path": change.path,
            "state": change.state, "at": change.at,
            "namespace": change.namespace, "record": change.record,
        })
    coverage = dict(getattr(batch, "coverage", {}))
    return _digest({"trusted": bool(getattr(batch, "trusted", False)),
                    "watermark": getattr(batch, "watermark", None),
                    "changes": sorted(changes, key=lambda row: (
                        row["update_id"], row["path"], row["state"], row["at"])),
                    "coverage": coverage})


def build_id(prior_generation: Optional[str], source_watermark: str, batch: Any) -> str:
    """Immutable recovery identity for one exact base/feed/build attempt."""
    return _digest({"prior_generation_id": prior_generation,
                    "source_watermark": source_watermark,
                    "normalized_update_digest": _batch_digest(batch),
                    "schema_version": GENERATION_SCHEMA})


def build_generation(
    *, prior_generation: Optional[str], source_watermark: str, batch: Any,
    sections: Mapping[str, SectionResult], engine_version: str = "unknown",
    schema_version: str = GENERATION_SCHEMA,
) -> Generation:
    """Seal a generation.  This is pure: identical inputs mean identical bytes."""
    incomplete = tuple(name for name in REQUIRED_SECTIONS
                       if name not in sections or not sections[name].complete)
    schemas = {name: sections[name].schema for name in REQUIRED_SECTIONS
               if name in sections}
    normalized_updates = _batch_digest(batch)
    identity = {
        "prior_generation_id": prior_generation,
        "source_watermark": source_watermark,
        "normalized_update_digest": normalized_updates,
        "schema_version": schema_version,
        "engine_version": engine_version,
    }
    generation_id = _digest(identity)
    doc = {
        "schema": schema_version,
        "id": generation_id,
        "prior_generation_id": prior_generation,
        "source_watermark": source_watermark,
        "normalized_update_digest": normalized_updates,
        "engine_version": engine_version,
        "sections": {name: sections[name].document() for name in REQUIRED_SECTIONS
                     if name in sections},
    }
    # The ID above names the identity inputs.  content_digest names the exact
    # immutable bytes readers will verify through current.json.
    raw = _json(doc)
    return Generation(generation_id, raw, sha256(raw.encode("utf-8")).hexdigest(),
                      source_watermark, schemas, engine_version, not incomplete,
                      incomplete)


def _valid_generation(raw: Any, expected: Generation) -> bool:
    if not isinstance(raw, str) or raw != expected.bytes:
        return False
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return (parsed.get("id") == expected.id
            and sha256(raw.encode("utf-8")).hexdigest() == expected.content_digest)


def _read_manifest(transport: Any, team: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    raw = transport.read(current_path(team))
    if raw is None:
        return None, None
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return None, raw
    if not isinstance(doc, dict):
        return None, raw
    required = {"generation_id", "source_watermark", "schemas", "engine_version", "content_digest"}
    if set(doc) != required or not isinstance(doc["generation_id"], str):
        return None, raw
    return doc, raw


def load_current(transport: Any, team: str) -> Optional[Generation]:
    """Read and validate the current pointer plus its immutable target."""
    manifest, _raw = _read_manifest(transport, team)
    if manifest is None:
        return None
    raw = transport.read(generation_path(team, manifest["generation_id"]))
    if not isinstance(raw, str) or sha256(raw.encode("utf-8")).hexdigest() != manifest["content_digest"]:
        return None
    try:
        doc = json.loads(raw)
    except ValueError:
        return None
    if (not isinstance(doc, dict) or doc.get("id") != manifest["generation_id"]
            or doc.get("source_watermark") != manifest["source_watermark"]):
        return None
    return Generation(doc["id"], raw, manifest["content_digest"],
                      manifest["source_watermark"], dict(manifest["schemas"]),
                      manifest["engine_version"], True, ())


def publish(transport: Any, team: str, generation: Generation, *,
            fail_before_manifest: bool = False) -> PublishOutcome:
    """Write/read-verify the generation, then publish and verify current.json.

    ``conditional_writes_supported is True`` uses ``write_if_unchanged`` with
    the previously read manifest.  Explicit ``False`` uses one last-writer-wins
    manifest write plus exact read-back verification; freshness remains the
    reader overlay's responsibility.  A missing or invalid capability fails
    closed.
    """
    if not generation.complete:
        return PublishOutcome(False, "incomplete required section(s): " + ", ".join(generation.incomplete))
    path = generation_path(team, generation.id)
    existing = transport.read(path)
    if existing is None:
        if not transport.write(path, generation.bytes):
            return PublishOutcome(False, "generation write failed")
    elif existing != generation.bytes:
        return PublishOutcome(False, "generation id collision")
    if not _valid_generation(transport.read(path), generation):
        return PublishOutcome(False, "generation read verification failed")
    if fail_before_manifest:
        return PublishOutcome(False, "interrupted after generation write")

    manifest = _json({
        "generation_id": generation.id,
        "source_watermark": generation.source_watermark,
        "schemas": generation.schemas,
        "engine_version": generation.engine_version,
        "content_digest": generation.content_digest,
    })
    capability = getattr(transport, "conditional_writes_supported", None)
    if capability is True:
        conditional = getattr(transport, "write_if_unchanged", None)
        if not callable(conditional):
            return PublishOutcome(False, "conditional manifest write unavailable")
        _prior, prior_raw = _read_manifest(transport, team)
        if not conditional(current_path(team), manifest, prior_raw):
            return PublishOutcome(False, "current manifest changed")
    elif capability is False:
        if not transport.write(current_path(team), manifest):
            return PublishOutcome(False, "current manifest write failed")
    else:
        return PublishOutcome(False, "conditional manifest write capability unknown")
    if transport.read(current_path(team)) != manifest:
        return PublishOutcome(False, "current manifest read verification failed")
    return PublishOutcome(True)

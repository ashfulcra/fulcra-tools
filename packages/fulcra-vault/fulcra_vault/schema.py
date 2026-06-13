"""Schema and path helpers for fulcra-vault.

This module is deliberately pure: it validates the first-run structure spec and
normalizes note paths before any store or CLI code can lean on loose dicts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from pathlib import PurePosixPath
from typing import Any


SCHEMA_VERSION = 1
VAULT_ROOT = "vault"
_SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


class SchemaError(ValueError):
    """Raised when a user-provided structure spec is invalid."""


@dataclass(frozen=True)
class SectionSpec:
    slug: str
    title: str
    description: str = ""
    seed_notes: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SectionSpec":
        if not isinstance(data, dict):
            raise SchemaError("section must be an object")
        slug = _require_str(data, "slug")
        title = _require_str(data, "title")
        description = data.get("description", "")
        if not isinstance(description, str):
            raise SchemaError(f"section {slug}: description must be a string")
        seed_notes_raw = data.get("seed_notes", [])
        if not isinstance(seed_notes_raw, list):
            raise SchemaError(f"section {slug}: seed_notes must be a list")
        seed_notes = tuple(normalize_note_path(n) for n in seed_notes_raw)
        validate_slug(slug, label="section slug")
        if not title.strip():
            raise SchemaError(f"section {slug}: title must not be empty")
        return cls(slug=slug, title=title, description=description,
                   seed_notes=seed_notes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "title": self.title,
            "description": self.description,
            "seed_notes": list(self.seed_notes),
        }


@dataclass(frozen=True)
class StructureSpec:
    sections: tuple[SectionSpec, ...]
    exclusions: tuple[str, ...] = ()
    map_highlights: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StructureSpec":
        if not isinstance(data, dict):
            raise SchemaError("structure spec must be an object")
        version = data.get("schema_version", data.get("v", SCHEMA_VERSION))
        if version != SCHEMA_VERSION:
            raise SchemaError(f"unsupported schema_version: {version}")
        sections_raw = data.get("sections")
        if not isinstance(sections_raw, list) or not sections_raw:
            raise SchemaError("sections must be a non-empty list")
        sections = tuple(SectionSpec.from_dict(s) for s in sections_raw)
        slugs = [s.slug for s in sections]
        if len(slugs) != len(set(slugs)):
            raise SchemaError("duplicate section slug")
        exclusions_raw = data.get("exclusions", [])
        if not isinstance(exclusions_raw, list):
            raise SchemaError("exclusions must be a list")
        highlights_raw = data.get("map_highlights", [])
        if not isinstance(highlights_raw, list):
            raise SchemaError("map_highlights must be a list")
        exclusions = tuple(_validate_vault_relative(p, label="exclusion")
                           for p in exclusions_raw)
        highlights = tuple(normalize_note_path(p) for p in highlights_raw)
        return cls(sections=sections, exclusions=exclusions,
                   map_highlights=highlights, schema_version=version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sections": [s.to_dict() for s in self.sections],
            "exclusions": list(self.exclusions),
            "map_highlights": list(self.map_highlights),
        }

    def canonical_json(self) -> str:
        return canonical_json(self.to_dict())


@dataclass(frozen=True)
class VaultMeta:
    spec: StructureSpec
    created_at: str
    updated_at: str
    schema_version: int = SCHEMA_VERSION
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        reserved = {"schema_version", "created_at", "updated_at", "spec"}
        extras = {k: v for k, v in self.extra.items() if k not in reserved}
        return {
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "spec": self.spec.to_dict(),
            **extras,
        }


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def validate_slug(slug: str, *, label: str = "slug") -> None:
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        raise SchemaError(
            f"{label} must be lowercase kebab-case starting with a letter"
        )


def normalize_note_path(name: str) -> str:
    """Return a vault-relative markdown path such as ``people/Ash.md``."""
    if not isinstance(name, str) or not name.strip():
        raise SchemaError("note path must be a non-empty string")
    raw = name.strip().replace("\\", "/")
    if raw.startswith("/"):
        raise SchemaError("note path must be vault-relative, not absolute")
    path = PurePosixPath(raw)
    if any(part in ("", ".", "..") for part in path.parts):
        raise SchemaError("note path must not contain traversal")
    if path.parts and path.parts[0] == VAULT_ROOT:
        if len(path.parts) == 1:
            raise SchemaError("note path must name a file under vault/")
        path = PurePosixPath(*path.parts[1:])
    if path.suffix and path.suffix != ".md":
        raise SchemaError("note path must be markdown (.md)")
    if not path.suffix:
        path = path.with_suffix(".md")
    return path.as_posix()


def vault_relative_path(name: str) -> str:
    rel = normalize_note_path(name)
    return f"{VAULT_ROOT}/{rel}"


def fulcra_absolute_path(name: str) -> str:
    return "/" + vault_relative_path(name)


def _validate_vault_relative(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"{label} must be a non-empty string")
    raw = value.strip().replace("\\", "/")
    if raw.startswith("/"):
        raise SchemaError(f"{label} must be vault-relative")
    path = PurePosixPath(raw)
    if any(part in ("", ".", "..") for part in path.parts):
        raise SchemaError(f"{label} must not contain traversal")
    return path.as_posix()


def _require_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise SchemaError(f"{key} must be a string")
    return value

"""Vault scaffold and additive restructure planning."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .frontmatter import update_keys
from .links import build_index
from .map import render_hot, render_map, select_hot_items
from .schema import SCHEMA_VERSION, StructureSpec, VaultMeta, canonical_json


AGENT = "fulcra-vault"


class InitializedVaultError(RuntimeError):
    """Raised when onboarding would overwrite an initialized vault."""


class RestructureError(ValueError):
    """Raised when a restructure would require destructive migration."""


@dataclass(frozen=True)
class WriteOp:
    path: str
    content: str


class TextStore(Protocol):
    def read_text(self, path: str) -> str:
        ...

    def write_text(self, path: str, content: str) -> None:
        ...


def plan_scaffold(spec: StructureSpec, now: datetime) -> list[WriteOp]:
    stamp = now.isoformat()
    note_map = {
        path: _seed_note(path, section_slug=section.slug, now=now)
        for section in spec.sections
        for path in section.seed_notes
    }
    links = build_index(note_map)
    meta = VaultMeta(spec=spec, created_at=stamp, updated_at=stamp)
    return [
        WriteOp("/vault/meta.json", canonical_json(meta.to_dict()) + "\n"),
        WriteOp("/vault/MAP.md", render_map(spec, note_map, links)),
        WriteOp("/vault/HOT.md", render_hot(select_hot_items(note_map, links, now))),
        WriteOp("/vault/LOG.md", _log_line("initialized vault", now)),
        *[
            WriteOp(f"/vault/{path}", content)
            for path, content in sorted(note_map.items())
        ],
    ]


def onboard(spec: StructureSpec, store: TextStore, now: datetime,
            *, force: bool = False) -> list[WriteOp]:
    if not force and _exists(store, "/vault/meta.json"):
        raise InitializedVaultError("vault is already initialized")
    ops = plan_scaffold(spec, now)
    for op in ops:
        store.write_text(op.path, op.content)
    return ops


def plan_restructure(meta: VaultMeta, new_spec: StructureSpec,
                     existing_notes: dict[str, str], now: datetime) -> list[WriteOp]:
    _validate_additive_restructure(meta, new_spec)
    stamp = now.isoformat()
    existing = {
        path.removeprefix("/vault/"): text
        for path, text in existing_notes.items()
    }
    old_seed_notes = {
        path
        for section in meta.spec.sections
        for path in section.seed_notes
    }
    note_map = dict(existing)
    new_seed_ops: list[WriteOp] = []
    for section in new_spec.sections:
        for path in section.seed_notes:
            if path in old_seed_notes or path in existing:
                continue
            content = _seed_note(path, section_slug=section.slug, now=now)
            note_map[path] = content
            new_seed_ops.append(WriteOp(f"/vault/{path}", content))
    new_meta = VaultMeta(
        spec=new_spec,
        created_at=meta.created_at,
        updated_at=stamp,
        schema_version=meta.schema_version,
        extra=meta.extra,
    )
    links = build_index(note_map)
    return [
        WriteOp("/vault/meta.json", canonical_json(new_meta.to_dict()) + "\n"),
        WriteOp("/vault/MAP.md", render_map(new_spec, note_map, links)),
        WriteOp("/vault/LOG.md", _log_line("restructured vault", now)),
        *sorted(new_seed_ops, key=lambda op: op.path),
    ]


def apply_restructure(meta: VaultMeta, new_spec: StructureSpec,
                      store: TextStore, now: datetime) -> list[WriteOp]:
    existing = _read_existing_seed_notes(store, meta, new_spec)
    ops = plan_restructure(meta, new_spec, existing, now)
    for op in ops:
        if op.path.startswith("/vault/") and op.path.endswith(".md"):
            try:
                store.read_text(op.path)
            except FileNotFoundError:
                pass
            else:
                raise RestructureError(f"target changed before write: {op.path}")
    for op in ops:
        store.write_text(op.path, op.content)
    return ops


def _validate_additive_restructure(meta: VaultMeta, new_spec: StructureSpec) -> None:
    if meta.schema_version > SCHEMA_VERSION:
        raise RestructureError("schema downgrade is not supported")
    old_slugs = {section.slug for section in meta.spec.sections}
    new_slugs = {section.slug for section in new_spec.sections}
    removed = old_slugs - new_slugs
    if removed:
        raise RestructureError(
            f"cannot remove section in additive restructure: {sorted(removed)[0]}"
        )
    old_notes = {
        path
        for section in meta.spec.sections
        for path in section.seed_notes
    }
    new_notes = {
        path
        for section in new_spec.sections
        for path in section.seed_notes
    }
    removed_notes = old_notes - new_notes
    if removed_notes:
        raise RestructureError(
            f"cannot remove seed note in additive restructure: {sorted(removed_notes)[0]}"
        )


def _read_existing_seed_notes(store: TextStore, meta: VaultMeta,
                              new_spec: StructureSpec) -> dict[str, str]:
    paths = {
        path
        for section in (*meta.spec.sections, *new_spec.sections)
        for path in section.seed_notes
    }
    existing: dict[str, str] = {}
    for path in sorted(paths):
        try:
            existing[path] = store.read_text(f"/vault/{path}")
        except FileNotFoundError:
            continue
    return existing


def _seed_note(path: str, *, section_slug: str, now: datetime) -> str:
    title = path.rsplit("/", 1)[-1].removesuffix(".md")
    stamp = now.isoformat()
    body = (
        f"# {title}\n\n"
        f"<!-- section:{section_slug} owner:{AGENT} -->\n"
        "Seed note. Replace this with durable context.\n"
        f"<!-- /section:{section_slug} -->\n\n"
        "## Log\n"
        f"- {stamp} {AGENT}: created seed note\n"
    )
    return update_keys(body, {
        "section": section_slug,
        "status": "seed",
        "title": title,
        "updated_at": stamp,
    })


def _exists(store: TextStore, path: str) -> bool:
    try:
        store.read_text(path)
    except FileNotFoundError:
        return False
    return True


def _log_line(message: str, now: datetime) -> str:
    return f"- {now.isoformat()} {AGENT}: {message}\n"

"""The OKF Bundle: a directory of concept files plus reserved files."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .concept import Concept, concept_id_for
from .frontmatter import FrontmatterError, dump, parse

RESERVED_NAMES = {"index.md", "log.md"}


@dataclass
class Bundle:
    root: Path | None = None
    concepts: dict[str, Concept] = field(default_factory=dict)
    okf_version: str | None = None
    parse_errors: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def load_dir(cls, path: str | Path, *, lenient: bool = False) -> "Bundle":
        root = Path(path)
        bundle = cls(root=root)
        for md in sorted(root.rglob("*.md")):
            rel = md.relative_to(root).as_posix()
            if md.name in RESERVED_NAMES:
                if rel == "index.md":
                    bundle.okf_version = _read_okf_version(md)
                continue
            text = md.read_text()
            try:
                cid = concept_id_for(rel)
                bundle.concepts[cid] = Concept.from_text(text, cid)
            except FrontmatterError as e:
                if lenient:
                    bundle.parse_errors.append((rel, str(e)))
                else:
                    raise
        return bundle

    def write_dir(self, path: str | Path) -> None:
        out = Path(path)
        for concept in self.concepts.values():
            target = out / (concept.id + ".md")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(render_concept(concept))


def _read_okf_version(index_md: Path) -> str | None:
    try:
        fm, _ = parse(index_md.read_text())
    except FrontmatterError:
        return None
    value = fm.get("okf_version")
    return str(value) if value is not None else None


def render_concept(concept: Concept) -> str:
    fm: dict[str, Any] = {"type": concept.type}
    for name in ("title", "description", "resource", "timestamp"):
        value = getattr(concept, name)
        if value is not None:
            fm[name] = value
    if concept.tags:
        fm["tags"] = list(concept.tags)
    fm.update(concept.extra)
    return "---\n" + dump(fm) + "---\n" + concept.body

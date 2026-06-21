"""The OKF Concept: one markdown file with frontmatter."""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from .frontmatter import parse

_KNOWN = ("type", "title", "description", "resource", "timestamp", "tags")
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def concept_id_for(rel_path: str) -> str:
    """Concept ID = bundle-relative path, POSIX separators, ``.md`` removed."""
    norm = rel_path.replace("\\", "/")
    if norm.endswith(".md"):
        norm = norm[: -len(".md")]
    return norm


@dataclass
class Concept:
    id: str
    type: str
    title: str | None = None
    description: str | None = None
    resource: str | None = None
    timestamp: str | None = None
    tags: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    @classmethod
    def from_text(cls, text: str, concept_id: str) -> "Concept":
        fm, body = parse(text)
        tags = fm.get("tags", [])
        if isinstance(tags, str):
            tags = [tags]
        elif not isinstance(tags, list):
            tags = [tags]
        extra = {k: v for k, v in fm.items() if k not in _KNOWN}
        return cls(
            id=concept_id,
            type=str(fm.get("type", "") or ""),
            title=fm.get("title"),
            description=fm.get("description"),
            resource=fm.get("resource"),
            timestamp=fm.get("timestamp"),
            tags=[str(t) for t in tags],
            extra=extra,
            body=body,
        )

    def links(self) -> list[str]:
        """Outbound markdown links resolved to concept IDs; external URLs skipped."""
        out: list[str] = []
        base_dir = posixpath.dirname(self.id)
        for target in _LINK_RE.findall(self.body):
            if "://" in target or target.startswith("#") or target.startswith("mailto:"):
                continue
            if target.startswith("/"):
                resolved = target.lstrip("/")
            else:
                resolved = posixpath.normpath(posixpath.join(base_dir, target))
            out.append(concept_id_for(resolved))
        return out

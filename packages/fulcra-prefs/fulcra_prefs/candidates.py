"""Durable auto-capture candidate queues.

Agents use this module when they notice a preference during a session but want a
lifecycle hook to ingest the signals later in one batch.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


DEFAULT_CANDIDATE_ROOT = Path.home() / ".local/state/fulcra-prefs/candidates"


def candidate_root(root: str | Path | None = None) -> Path:
    if root is not None:
        return Path(root)
    env = os.environ.get("FULCRA_PREFS_CANDIDATE_DIR", "").strip()
    return Path(env) if env else DEFAULT_CANDIDATE_ROOT


def validate_slug(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    value = value.strip()
    if "/" in value or "\\" in value or ".." in value:
        raise ValueError(f"{label} must not contain path separators or traversal")
    return value


def candidate_file(platform: str, session_id: str,
                   root: str | Path | None = None) -> Path:
    platform = validate_slug(platform, label="platform")
    session_id = validate_slug(session_id, label="session")
    return candidate_root(root) / platform / f"{session_id}.json"


def read_candidates(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON array")
    for i, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"{path} item {i} must be an object")
    return data


def append_candidate(path: Path, spec: dict[str, Any]) -> int:
    existing = read_candidates(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = [*existing, spec]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(merged, sort_keys=True, indent=2) + "\n")
    tmp.replace(path)
    return len(merged)


def mark_captured(path: Path) -> Path:
    captured = path.with_name(path.name + ".captured")
    # A re-drain of the same (platform, session) must not clobber the prior
    # archive: pick the next free .captured[.N] name instead of overwriting.
    if captured.exists():
        i = 1
        while True:
            alt = path.with_name(f"{path.name}.captured.{i}")
            if not alt.exists():
                captured = alt
                break
            i += 1
    path.replace(captured)
    return captured

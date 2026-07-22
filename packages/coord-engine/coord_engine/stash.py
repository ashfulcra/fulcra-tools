"""Durable per-agent tooling stash — the engine half of fulcra-agent-durable-state.

`team/<team>/_coord/agents/<agent>/stash/` holds an agent's operational bundle
(scripts, loops, config templates) so ephemeral machines can restore instead of
rebuild. This module owns the deterministic bookkeeping the SKILL's prose can't:
a `manifest.json` with per-file sha256 + size + exec bit, and the FAIL-CLOSED
secrets guard. The transport stays the plain duck-typed file interface — stash
is a thin layer, not a second sync engine.

The guard's asymmetry is deliberate (see the SKILL): every agent on the bus can
read `team/<team>/**`, so a refused harmless file costs one override flag while
a leaked credential costs a rotation and an incident. When in doubt, refuse.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

MANIFEST_SCHEMA = "coord.stash.manifest.v1"
MANIFEST_NAME = "manifest.json"

#: Name shapes that are secrets until proven otherwise. Basename match,
#: case-insensitive. `.env` in any position (`.env`, `prod.env`, `.env.local`),
#: key material extensions, ssh identity names, and the credential word-family.
_NAME_PATTERNS = (
    re.compile(r"(^|\.)env(\.|$)", re.IGNORECASE),
    re.compile(r"\.(key|pem|p12|pfx)$", re.IGNORECASE),
    re.compile(r"^id_(rsa|dsa|ecdsa|ed25519)", re.IGNORECASE),
    re.compile(r"token|secret|credential|passwd|password", re.IGNORECASE),
)

#: Content shapes of known credentials. Token-shaped, not substring-shaped:
#: bare "sk-" also lives inside every "task-1", so prefixes require a word
#: boundary + a real token tail. Any PEM boundary refuses — certificates are
#: not secrets, but a guard that parses PEM taxonomy is a guard with bugs.
_CONTENT_PATTERNS = (
    re.compile(r"\blin_oauth_[A-Za-z0-9]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"-----BEGIN [A-Z0-9 ]+-----"),
)


def secret_reason(name: str, content: str) -> Optional[str]:
    """Why (name, content) looks like a secret, or None if it doesn't.

    First filter is the filename, second is known credential shapes in the
    content. Returns a human reason naming the tripped rule so the refusal is
    actionable, never the matched secret text itself.
    """
    for pat in _NAME_PATTERNS:
        if pat.search(name):
            return f"name {name!r} is secret-shaped (matches /{pat.pattern}/)"
    for pat in _CONTENT_PATTERNS:
        if pat.search(content):
            return f"content of {name!r} matches credential shape /{pat.pattern}/"
    return None


def sha256_hex(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def parse_manifest(raw: Optional[str]) -> dict[str, Any]:
    """The manifest, or a fresh empty one when absent/corrupt.

    Corrupt JSON degrades to empty rather than raising: the store copy is
    last-writer-wins and the next push rewrites it whole, so an unreadable
    manifest must not brick push/list — pull still verifies per-file checksums
    against whatever survives here.
    """
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and isinstance(data.get("files"), dict):
                return data
        except ValueError:
            pass
    return {"schema": MANIFEST_SCHEMA, "files": {}}


def render_manifest(manifest: dict[str, Any], *, agent: str, now: str) -> str:
    manifest = dict(manifest)
    manifest["schema"] = MANIFEST_SCHEMA
    manifest["agent"] = agent
    manifest["updated_at"] = now
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def file_entry(content: str, *, executable: bool, now: str) -> dict[str, Any]:
    return {
        "sha256": sha256_hex(content),
        "size": len(content.encode("utf-8")),
        "exec": executable,
        "updated_at": now,
    }


def safe_name(name: str) -> bool:
    """True when ``name`` is a plain stash-relative filename. Pull writes
    ``dest/<name>``, so a separator or ``..`` segment would escape dest —
    manifest and listing names are remote data, not trusted paths."""
    return bool(name) and "/" not in name and "\\" not in name and name != ".." \
        and not name.startswith(".." ) and name not in (".", MANIFEST_NAME)

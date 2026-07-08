"""Engagement lifecycle commands — init / load / set_phase / status / list.

Every function takes an injected ``transport`` (see transport.py) so the whole
module is testable without the network. The remote tree is the source of truth;
the local mirror (sync.py) is a working copy.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from . import model

REMOTE_ROOT = "fde/engagements"

# The artifacts each phase is expected to produce (spec §State). status() and
# resume() fold presence of these into "where are we / what's next".
EXPECTED_ARTIFACTS: dict[str, list[str]] = {
    "intake": ["intake/brief.md"],
    "interview": ["interview/plan.md", "interview/findings.md"],
    "architecture": ["architecture.md"],
    "plan": ["plan.md"],
    "prototype": ["prototype/verification.md"],
    "build": ["build/log.md"],
    "retro": ["retro.md"],
}

NEXT_HINT: dict[str, str] = {
    "intake": "produce intake/brief.md from the source materials, then `fde-engine phase <slug> interview`",
    "interview": "write interview/plan.md (prioritized topic map), run the adaptive interview into interview/findings.md, then `fde-engine phase <slug> architecture`",
    "architecture": "write architecture.md (capability map + gap register + tenancy decision), get user approval, then `fde-engine phase <slug> plan`",
    "plan": "write plan.md (prototype verification plan + provisional production plan), then `fde-engine phase <slug> prototype`",
    "prototype": "build the verification prototype, record results in prototype/verification.md, then `fde-engine phase <slug> build` — or back to architecture/plan if findings invalidate them",
    "build": "execute production milestones, logging to build/log.md, then `fde-engine phase <slug> retro`",
    "retro": "write retro.md and append repeatable patterns to fde/playbook.md",
}


class EngagementError(RuntimeError):
    pass


# Slugs become both remote store paths (fde/engagements/<slug>/...) and local
# mirror paths (./fde/<slug>/...) — an unvalidated slug (e.g. "../../etc") is
# a path-traversal vector in both directions. This is deliberately narrow
# (lowercase/digits/hyphen, must start alnum) rather than a denylist of ".."
# and "/": an allowlist can't be bypassed by an encoding or separator we
# didn't think of.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _validate_slug(slug: str) -> str:
    """Slugs become remote store paths AND local mirror paths — an unvalidated
    slug is a path-traversal vector in both directions. Chokepoint check so
    every caller (CLI or library) is covered."""
    if not _SLUG_RE.match(slug or ""):
        raise EngagementError(
            f"invalid slug '{slug}' — use lowercase letters/digits/hyphens "
            f"(try: '{model.slugify(slug)}')"
        )
    return slug


def remote_path(slug: str, rel: str = "") -> str:
    _validate_slug(slug)
    base = f"{REMOTE_ROOT}/{slug}"
    return f"{base}/{rel}" if rel else base


def _raw_doc(transport, slug: str) -> Optional[str]:
    """Read engagement.md's raw text without parsing it. Separating this from
    load_engagement lets callers distinguish "doesn't exist" (None) from
    "exists but is corrupt" (non-None, fails model.parse_engagement) instead
    of collapsing both into the same "no engagement" conclusion — the bug
    that let `init` silently overwrite a hand-corrupted doc."""
    return transport.read(remote_path(slug, "engagement.md"))


def load_engagement(transport, slug: str) -> Optional[dict]:
    return model.parse_engagement(_raw_doc(transport, slug))


def _confirm_absent(transport) -> None:
    """Preflight before concluding "no engagement". transport.read()
    returning None is ambiguous between "genuinely doesn't exist" and "store
    unreachable" (expired auth, offline, etc.) — both look identical from
    read()'s Optional[str] contract. list_dir() raises TransportError on a
    real outage, so calling it here turns a silent misdiagnosis (which would
    tell an agent to `init` fresh state on top of a store it can't actually
    see) into a loud, correct one."""
    transport.list_dir(REMOTE_ROOT + "/")


def _corrupt_doc_error(slug: str) -> EngagementError:
    return EngagementError(
        f"engagement.md for '{slug}' exists but does not parse (corrupt "
        "frontmatter or wrong schema) — fix it or restore a prior version "
        "with `fulcra file stat`; refusing to overwrite"
    )


def _require_engagement(transport, slug: str, *, absent_message: str) -> dict:
    """Load engagement meta, or raise a diagnosis that distinguishes genuine
    absence from a corrupt doc or an unreachable store. Shared by set_phase
    and status so both give the same honest answer instead of "no
    engagement" for cases that are actually "can't tell" or "it's broken"."""
    meta = load_engagement(transport, slug)
    if meta is not None:
        return meta
    if _raw_doc(transport, slug) is not None:
        raise _corrupt_doc_error(slug)
    _confirm_absent(transport)
    raise EngagementError(absent_message)


def init_engagement(transport, slug: str, title: str, *, now: str) -> dict:
    raw = _raw_doc(transport, slug)
    if raw is not None:
        if model.parse_engagement(raw) is None:
            raise _corrupt_doc_error(slug)
        raise EngagementError(f"engagement '{slug}' already exists")
    _confirm_absent(transport)
    meta = {
        "schema": model.SCHEMA,
        "slug": slug,
        "title": title,
        "phase": model.PHASES[0],
        "created_at": now,
        "updated_at": now,
        "phase_history": [f"{model.PHASES[0]} {now}"],
    }
    if not transport.write(remote_path(slug, "engagement.md"),
                           model.render_engagement(meta)):
        raise EngagementError(f"failed to write engagement doc for '{slug}'")
    return meta


def set_phase(transport, slug: str, new_phase: str, *, now: str) -> dict:
    meta = _require_engagement(
        transport, slug,
        absent_message=f"no engagement '{slug}' — run `fde-engine init` first",
    )
    current = meta.get("phase", "")
    if not model.valid_transition(current, new_phase):
        allowed = sorted(model.TRANSITIONS.get(current, set()))
        raise EngagementError(
            f"invalid transition {current} -> {new_phase}; allowed: {allowed}"
        )
    meta["phase"] = new_phase
    meta["updated_at"] = now
    meta["phase_history"].append(f"{new_phase} {now}")
    if not transport.write(remote_path(slug, "engagement.md"),
                           model.render_engagement(meta)):
        raise EngagementError(f"failed to persist phase change for '{slug}'")
    return meta


def status(transport, slug: str) -> dict[str, Any]:
    """Meta + per-artifact presence + the next-move hint. Deterministic fold —
    an agent should never eyeball the tree to answer 'where are we'."""
    meta = _require_engagement(transport, slug, absent_message=f"no engagement '{slug}'")
    artifacts: dict[str, bool] = {}
    for phase in model.PHASES:
        for rel in EXPECTED_ARTIFACTS[phase]:
            artifacts[rel] = transport.read(remote_path(slug, rel)) is not None
    return {
        "slug": slug,
        "title": meta.get("title", ""),
        "phase": meta.get("phase", ""),
        "updated_at": meta.get("updated_at", ""),
        "phase_history": list(meta.get("phase_history", [])),
        "artifacts": artifacts,
        "next": NEXT_HINT.get(meta.get("phase", ""), ""),
    }


def list_engagements(transport) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in transport.list_dir(REMOTE_ROOT + "/"):
        if not entry.get("is_dir"):
            continue
        slug = entry["name"].rstrip("/")
        meta = load_engagement(transport, slug)
        if meta is None:
            continue  # tolerate junk dirs; only schema-valid docs count
        rows.append({"slug": slug, "title": meta.get("title", ""),
                     "phase": meta.get("phase", "")})
    return sorted(rows, key=lambda r: r["slug"])

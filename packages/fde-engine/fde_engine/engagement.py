"""Engagement lifecycle commands — init / load / set_phase / status / list.

Every function takes an injected ``transport`` (see transport.py) so the whole
module is testable without the network. The remote tree is the source of truth;
the local mirror (sync.py) is a working copy.
"""

from __future__ import annotations

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


def remote_path(slug: str, rel: str = "") -> str:
    base = f"{REMOTE_ROOT}/{slug}"
    return f"{base}/{rel}" if rel else base


def load_engagement(transport, slug: str) -> Optional[dict]:
    return model.parse_engagement(transport.read(remote_path(slug, "engagement.md")))


def init_engagement(transport, slug: str, title: str, *, now: str) -> dict:
    if load_engagement(transport, slug) is not None:
        raise EngagementError(f"engagement '{slug}' already exists")
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
    meta = load_engagement(transport, slug)
    if meta is None:
        raise EngagementError(f"no engagement '{slug}' — run `fde-engine init` first")
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
    meta = load_engagement(transport, slug)
    if meta is None:
        raise EngagementError(f"no engagement '{slug}'")
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

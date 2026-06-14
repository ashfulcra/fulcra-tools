"""Optional Fulcra Continuity bridge for coord tasks.

The bridge deliberately stays stdlib-only and does not import the
``fulcra-continuity`` package. It writes the same checkpoint JSON shape so coord
and continuity can interoperate while remaining independently useful.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import remote_root
from . import remote, views

SCHEMA_VERSION = "fulcra.continuity.checkpoint.v1"
DEFAULT_BOOTSTRAP_PRIMER = (
    "This is a Fulcra Continuity checkpoint. Resume it with "
    "`fulcra-continuity resume <checkpoint>` or read this JSON directly. "
    "Use objective, identity, decisions, artifacts, open_questions, "
    "next_actions, and memory_writes to continue without the original "
    "transcript."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _slug(value: str) -> str:
    text = value.lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    return text.strip("-") or "default"


def _short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]


def identity_for_task(task: dict[str, Any], *, agent: str = "") -> dict[str, str]:
    owner = str(task.get("owner_agent") or agent or "")
    return {
        "workstream_id": str(task.get("workstream") or ""),
        "agent_id": str(agent or owner),
        "coord_task_id": str(task.get("id") or ""),
        "coord_owner_agent": owner,
    }


def remote_prefix(identity: dict[str, str]) -> str:
    workstream = identity.get("workstream_id", "")
    agent = identity.get("agent_id", "")
    task_id = identity.get("coord_task_id", "")
    workstream_part = f"{_slug(workstream)}-{_short_hash(workstream)}"
    agent_part = views.agent_slug(agent)
    task_part = _slug(task_id)
    return f"{remote_root()}/continuity/{workstream_part}/{agent_part}/{task_part}"


def latest_remote_path(identity: dict[str, str]) -> str:
    return f"{remote_prefix(identity)}/latest.json"


def checkpoint_remote_path(checkpoint_id: str, identity: dict[str, str]) -> str:
    return f"{remote_prefix(identity)}/checkpoints/{_slug(checkpoint_id)}.json"


def make_checkpoint(
    task: dict[str, Any],
    *,
    agent: str = "",
    reason: str = "manual",
    transcript_path: str = "",
    decisions: Optional[list[str]] = None,
    open_questions: Optional[list[str]] = None,
    next_actions: Optional[list[str]] = None,
    tags: Optional[list[str]] = None,
) -> dict[str, Any]:
    created = _now_iso()
    identity = identity_for_task(task, agent=agent)
    task_id = identity["coord_task_id"]
    stamp = created.replace(":", "").replace("-", "").replace("Z", "z")
    checkpoint_id = f"CHK-{stamp}-{_slug(task_id)}-{uuid.uuid4().hex[:8]}"
    nexts = next_actions if next_actions is not None else [str(task.get("next_action") or "").strip()]
    nexts = [item for item in nexts if item]
    tag_list = list(tags or [])
    tag_list.extend(["fulcra-coord", f"reason:{reason}"])
    session_context = (
        f"Created by fulcra-coord during a {reason} checkpoint for task "
        f"{task_id or '(unknown task)'} in workstream "
        f"{task.get('workstream') or '(unknown workstream)'}. Current status: "
        f"{task.get('status') or '(unknown)'}; owner: "
        f"{task.get('owner_agent') or agent or '(unknown)'}."
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "checkpoint_id": checkpoint_id,
        "task_id": task_id,
        "title": str(task.get("title") or ""),
        "objective": str(task.get("current_summary") or task.get("title") or ""),
        "created_at": created,
        "owner_agent": str(task.get("owner_agent") or ""),
        "identity": identity,
        "source": f"fulcra-coord:{reason}",
        "transcript_path": transcript_path,
        "context_used_percent": None,
        "bootstrap_primer": DEFAULT_BOOTSTRAP_PRIMER,
        "session_context": session_context,
        "decisions": decisions or [],
        "artifacts": [
            {
                "path": str(task.get("task_file") or remote.task_remote_path(task_id)),
                "note": "fulcra-coord task state",
            }
        ],
        "open_questions": open_questions or [],
        "next_actions": nexts,
        "memory_writes": [],
        "tags": sorted(set(filter(None, tag_list))),
    }


def _artifact_ref(artifact: Any) -> str:
    if isinstance(artifact, dict):
        return str(
            artifact.get("path")
            or artifact.get("url")
            or artifact.get("ref")
            or artifact.get("remote_path")
            or ""
        )
    return str(artifact or "")


def _is_portable_artifact_ref(ref: str) -> bool:
    if not ref:
        return False
    ref = ref.strip()
    portable_prefixes = (
        "http://",
        "https://",
        "fulcra-file:",
        "continuity-latest:",
        "coord-task-id:",
        "repo=",
        "repo:",
    )
    if ref.startswith(portable_prefixes):
        return True
    # Fulcra Files paths are remote bus paths, not host-local filesystem paths.
    if ref.startswith(f"{remote_root().rstrip('/')}/"):
        return True
    return False


def quality_warnings(checkpoint: dict[str, Any]) -> list[str]:
    """Return checkpoint quality warnings for cold-start handoff usefulness.

    Coord snapshots are best-effort and must not fail hook paths, but they
    should still say when they are thin. These warnings are derived from the
    checkpoint body so the portable checkpoint schema stays identical to the
    standalone fulcra-continuity package.
    """
    warnings: list[str] = []
    if not str(checkpoint.get("objective") or "").strip():
        warnings.append("missing objective/current state")
    if not checkpoint.get("next_actions"):
        warnings.append("missing concrete next_actions")
    identity = checkpoint.get("identity")
    if not isinstance(identity, dict):
        # Best-effort contract: a malformed (non-dict) identity must flag as
        # missing, not raise out of a hook path. Mirrors checkpoint_from_dict.
        identity = {}
    for key in ("workstream_id", "agent_id", "coord_task_id"):
        if not str(identity.get(key) or "").strip():
            warnings.append(f"missing identity.{key}")
    artifacts = checkpoint.get("artifacts") or []
    if not artifacts:
        warnings.append("missing portable artifacts")
    else:
        nonportable = [
            ref for ref in (_artifact_ref(item) for item in artifacts)
            if not _is_portable_artifact_ref(ref)
        ]
        if nonportable:
            warnings.append(
                "artifact refs may be local-only: " + ", ".join(nonportable[:3])
            )
    if not checkpoint.get("decisions"):
        warnings.append("thin checkpoint: no decisions recorded")
    if not checkpoint.get("open_questions"):
        warnings.append("thin checkpoint: no open_questions recorded")
    return warnings


def write_checkpoint(
    checkpoint: dict[str, Any],
    *,
    backend: Optional[list[str]] = None,
) -> tuple[bool, str]:
    identity = checkpoint.get("identity") or {}
    latest_path = latest_remote_path(identity)
    archive_path = checkpoint_remote_path(str(checkpoint.get("checkpoint_id", "")), identity)
    archived = remote.upload_json(checkpoint, archive_path, backend=backend)
    latest = remote.upload_json(checkpoint, latest_path, backend=backend)
    return bool(archived and latest), latest_path


def read_latest_for_task(
    task: dict[str, Any],
    *,
    agent: str = "",
    backend: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    identity = identity_for_task(task, agent=agent)
    return remote.download_json(latest_remote_path(identity), backend=backend)


# ---------------------------------------------------------------------------
# Checkpoint refs as loop/role payload (spec 2026-06-10-continuity-integration)
#
# STORAGE REALITY this section encodes (verified 2026-06-10): the standalone
# ``fulcra-continuity`` CLI writes checkpoints to LOCAL paths only; the REMOTE
# ``{root}/continuity/...`` bus tree exists because THIS bridge uploads to it
# (write_checkpoint above — the tree the retention walker prunes). So "make a
# checkpoint portable" means: publish the local JSON to the remote tree via
# this bridge and hand out the REMOTE archive path as the ref. Coord treats
# the checkpoint body as an OPAQUE JSON BLOB throughout — it never imports
# fulcra_continuity (fitness-pinned) and never interprets fields beyond the
# identity/id it needs to derive the storage path (the same fields
# write_checkpoint already keys the tree by).
# ---------------------------------------------------------------------------


def publish_checkpoint_file(
    path: str, *, backend: Optional[list[str]] = None
) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """Publish a LOCAL checkpoint JSON file to the remote continuity tree.

    Returns ``(remote_ref, checkpoint_dict)``:
      * ``remote_ref`` — the IMMUTABLE archive path (``.../checkpoints/<id>.json``)
        on success, None when the file is unreadable or the upload failed. The
        archive path (not ``latest.json``) is deliberate: a handoff ref must
        keep meaning THIS snapshot even after the producer checkpoints again.
      * ``checkpoint_dict`` — the parsed JSON when readable (even on upload
        failure), so the caller can fall back to carrying it INLINE in the
        loop payload rather than stranding the handoff on a local-only path.

    Best-effort never-raise (None/None on any failure): this rides the
    ``handoff`` send path, which must degrade, not crash."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None, None
    if not isinstance(data, dict):
        return None, None
    try:
        identity = data.get("identity") or {}
        checkpoint_id = str(data.get("checkpoint_id") or "") or uuid.uuid4().hex
        archive_path = checkpoint_remote_path(checkpoint_id, identity)
        latest_path = latest_remote_path(identity)
        archived = remote.upload_json(data, archive_path, backend=backend)
        # latest.json is the read point resume/--with-continuity already uses;
        # refresh it best-effort, but the HANDOFF ref is the archive path and
        # only the archive upload gates success.
        remote.upload_json(data, latest_path, backend=backend)
        return (archive_path if archived else None), data
    except Exception:
        return None, data


def resolve_checkpoint_ref(
    ref: str, *, backend: Optional[list[str]] = None
) -> Optional[dict[str, Any]]:
    """Fetch the checkpoint JSON a ref points at — local file first (the
    producer's own host), then the remote bus (the cross-host case). The ref
    stays OPAQUE: we only ever hand it whole to the filesystem or the
    transport, never parse structure out of it. Best-effort: None."""
    if not ref:
        return None
    try:
        p = Path(ref)
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
    except Exception:
        pass
    try:
        data = remote.download_json(ref, backend=backend)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def render_brief_via_cli(
    checkpoint: dict[str, Any], *, timeout: float = 10.0
) -> Optional[str]:
    """Render a resume brief by SHELLING OUT to the optional fulcra-continuity
    CLI (``fulcra-continuity resume <file>``). The CLI owns the checkpoint
    schema and its rendering; coord deliberately does not re-implement either
    (and must never import the package — subprocess is the sanctioned seam).

    Best-effort never-raise: a missing CLI (shutil.which miss), a non-zero
    exit, a timeout, or any I/O error all return None — callers degrade to
    printing the bare ref."""
    exe = shutil.which("fulcra-continuity")
    if not exe:
        return None
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", suffix=".checkpoint.json", delete=False,
                encoding="utf-8") as tmp:
            json.dump(checkpoint, tmp)
            tmp_name = tmp.name
        proc = subprocess.run(
            [exe, "resume", tmp_name],
            capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            return None
        return proc.stdout or None
    except Exception:
        return None
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def render_brief_for_ref(
    ref: str, *, backend: Optional[list[str]] = None
) -> Optional[str]:
    """Resolve a ref to its checkpoint JSON and render the resume brief via
    the optional CLI. One call for every "surface the where-I-left-off at
    claim time" site (task pickup, role claim, connect). Best-effort: None
    when the ref doesn't resolve or the CLI isn't installed/working."""
    checkpoint = resolve_checkpoint_ref(ref, backend=backend)
    if not checkpoint:
        return None
    return render_brief_via_cli(checkpoint)


def summarize_checkpoint(checkpoint: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not checkpoint:
        return None
    identity = checkpoint.get("identity") or {}
    return {
        "checkpoint_id": checkpoint.get("checkpoint_id", ""),
        "created_at": checkpoint.get("created_at", ""),
        "source": checkpoint.get("source", ""),
        "task_id": checkpoint.get("task_id", ""),
        "title": checkpoint.get("title", ""),
        "identity": identity,
        "next_actions": checkpoint.get("next_actions", []),
        "decisions": checkpoint.get("decisions", []),
        "path": latest_remote_path(identity),
    }

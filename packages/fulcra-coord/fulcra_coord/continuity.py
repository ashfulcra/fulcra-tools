"""Optional Fulcra Continuity bridge for coord tasks.

The bridge deliberately stays stdlib-only and does not import the
``fulcra-continuity`` package. It writes the same checkpoint JSON shape so coord
and continuity can interoperate while remaining independently useful.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from . import remote_root
from . import remote, views

SCHEMA_VERSION = "fulcra.continuity.checkpoint.v1"


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

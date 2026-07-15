"""Fulcra "Agent Tasks" lifecycle annotations — ported writer (feature #3).

PORT NOTE (2026-07-08)
----------------------
This is the fulcra-common home of the Agent-Tasks annotation writer, ported off
the deprecated ``fulcra_coord.annotations`` module (which stays in place with a
tombstone during the deprecation window). Three things changed from the legacy
writer; everything else is behavior-preserving:

  (a) RECORD POST → the typed ingest endpoint. The legacy writer wrapped every
      record in a ``DataRecordV1`` envelope and POSTed it to the unpublished,
      retirement-eligible ``/ingest/v1/record/batch``. This writer POSTs the
      **unwrapped** record to the typed endpoint ``POST /ingest/v1/record/
      MomentAnnotation`` (base type as the path segment) as one-record-per-line
      jsonlines. The custom "Agent Tasks" definition is still referenced by its
      uuid in the record's ``sources`` array
      (``com.fulcradynamics.annotation.<def-uuid>``) — a custom type is NOT a
      valid path segment (``…/MomentAnnotation/<uuid>`` 404s; live-verified
      2026-07-08). The typed schema is flat and CLOSED
      (``{note, recorded_at, tags, sources, id}``) — it SILENTLY STRIPS any other
      top-level key — so the moment's title line is folded into ``note`` (the one
      served free-text slot; the legacy path JSON-encoded title+note inside
      ``data``). Nothing is lost: title + summary + task_id all ride in ``note``.

  (b) FAIL-CLOSED definition resolution — the 2026-07-03 definition-proliferation
      root cause. The legacy resolver could not tell a *failed* catalog lookup
      from a *verified-absent* definition (both surfaced as an empty list) and so
      minted a NEW definition whenever the find-by-name lookup silently failed.
      Here, a catalog LOOKUP ERROR (CLI rc!=0 / timeout / missing) refuses to
      write (returns ``""``; NEVER creates); a definition is created ONLY when the
      catalog reply verifiably contains no matching definition. An in-run memo
      caches resolved ids so a run re-resolves at most once.

  (c) DETERMINISTIC DUPLICATE handling — the 2026-07-03 bug already minted many
      duplicate "Agent Tasks" definitions (same name, different uuids). When
      find-by-name returns multiple live matches, the OLDEST (created_at
      ascending; uuid tiebreak so every host converges) is chosen, all candidate
      ids are logged at WARNING, and another definition is NEVER created.

TRANSPORT
---------
Tags and the annotation definition resolve through the public Fulcra CLI (shelled
out); only the record write is raw REST, over stdlib ``urllib`` (the platform
still exposes no record-write CLI/lib verb). fulcra-common owns the API client but
this writer keeps the stdlib-``urllib`` record path on purpose — its tests stub
the urllib opener rather than injecting an httpx transport.

CONTRACT
--------
``emit_lifecycle_annotation`` / ``emit_needs_user_annotation`` /
``emit_digest_annotation`` are BEST-EFFORT and MUST NEVER raise into the caller:
a coordination task write must succeed or fail on its own merits, never because
an annotation backend was slow, missing, or broken. Everything is wrapped and a
bool is returned (True = a record was actually written this call).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRACK_NAME = "Agent Tasks"

#: Valid lifecycle tags. The CLI hook maps a command/transition onto one of these.
LIFECYCLES = ("create", "pickup", "update", "complete")

#: The task kinds the schema recognizes (ported from fulcra_coord.schema so the
#: desc-blurb's kind extraction stays behavior-identical without importing coord).
VALID_KINDS = {"ops", "feature", "bug", "research", "infra", "config", "comms", "other"}

#: First-segment agent-kind normalization (see ``agent_kind``).
_KIND_MAP = {
    "claude-code": "claude",
    "claude": "claude",
    "openclaw": "openclaw",
    "codex": "chatgpt",
    "chatgpt": "chatgpt",
}


# ---------------------------------------------------------------------------
# Self-contained infra helpers (re-homed from fulcra-coord so this module has
# no fulcra-coord dependency). Cache/config roots follow the SAME XDG handling.
# ---------------------------------------------------------------------------

DEFAULT_REMOTE_ROOT = "/coordination"


def remote_root() -> str:
    """Canonical Fulcra Files coordination root, overridable via env.

    Only used to build the ``library_link`` deep link; ported verbatim so the
    link text is unchanged from the legacy writer."""
    root = os.environ.get("FULCRA_COORD_REMOTE_ROOT", DEFAULT_REMOTE_ROOT).strip()
    return "/" + (root or DEFAULT_REMOTE_ROOT).strip("/")


def _root_slug() -> str:
    """Filesystem-safe slug of the current remote root, for per-root cache
    isolation (mirrors the legacy cache layout so markers/id-caches don't bleed
    across coordination roots on one machine)."""
    root = os.environ.get("FULCRA_COORD_REMOTE_ROOT", DEFAULT_REMOTE_ROOT).strip()
    root = (root or DEFAULT_REMOTE_ROOT).strip("/")
    slug = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in root)
    return slug or "coordination"


def _cache_root() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "fulcra-common"


def annotations_dir() -> Path:
    """Per-root store of annotation idempotency markers + resolved-id caches."""
    return _cache_root() / "roots" / _root_slug() / "annotations"


def config_root() -> Path:
    """Global config dir (XDG_CONFIG_HOME). Not root-scoped: the annotation
    enable/mode is a machine-wide operator preference."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "fulcra-coord"


def _cli_base_cmd() -> list[str]:
    """Resolve the base Fulcra CLI invocation (no subcommand appended).

    Resolution order mirrors the documented backend precedence so every consumer
    honours the SAME configured CLI:
      1. ``FULCRA_CLI_COMMAND`` env var (explicit operator override)
      2. ``fulcra-api`` if found on PATH
      3. ``uv tool run fulcra-api`` (fallback)
    """
    env_cli = os.environ.get("FULCRA_CLI_COMMAND", "").strip()
    if env_cli:
        try:
            return shlex.split(env_cli)
        except ValueError:
            return env_cli.split()
    if shutil.which("fulcra-api"):
        return ["fulcra-api"]
    return ["uv", "tool", "run", "fulcra-api"]


def _write_timeout() -> int:
    """Subprocess timeout (seconds) for annotation CLI shell-outs.

    Legacy parity: honors ``FULCRA_COORD_TIMEOUT_SECONDS`` (the read-timeout
    knob, default 30) with a ``max(60, ...)`` floor — uploads measured at 1-16s
    idle routinely crossed a lower ceiling under host load, so the client
    abandoned writes the server then completed (observed duplicate directives);
    60s clears observed worst-case with margin. A non-numeric value falls back
    to the default rather than crashing every write. ``FULCRA_COORD_WRITE_TIMEOUT``
    is kept as an optional explicit override (floor 1) for callers wanting a
    tighter bound than the legacy floor allows."""
    override = os.environ.get("FULCRA_COORD_WRITE_TIMEOUT")
    if override:
        try:
            return max(1, int(float(override)))
        except ValueError:
            pass
    raw = os.environ.get("FULCRA_COORD_TIMEOUT_SECONDS")
    read = 30
    if raw:
        try:
            read = int(float(raw))
        except ValueError:
            read = 30
    return max(60, read)


def _extract_kind_from_tags(tags: list[str]) -> str:
    """The task's PRIMARY kind from its ``kind:`` tags (ported from coord.schema).

    Prefers a VALID_KINDS member (membership markers like ``kind:idea`` can sort
    ahead of the real schema kind); falls back to the first ``kind:`` suffix,
    then ``ops``."""
    first = ""
    for tag in tags:
        if tag.startswith("kind:"):
            suffix = tag[5:]
            if suffix in VALID_KINDS:
                return suffix
            if not first:
                first = suffix
    return first or "ops"


# ---------------------------------------------------------------------------
# Annotation idempotency markers (per-root local cache)
# ---------------------------------------------------------------------------

def _annotation_marker_path(key: str) -> Path:
    digest = hashlib.sha1(key.encode()).hexdigest()[:24]
    return annotations_dir() / f"ANN-{digest}"


def has_annotation_marker(key: str) -> bool:
    return _annotation_marker_path(key).exists()


def write_annotation_marker(key: str) -> None:
    try:
        annotations_dir().mkdir(parents=True, exist_ok=True)
        _annotation_marker_path(key).write_text("")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Tag / identity derivation
# ---------------------------------------------------------------------------

def agent_kind(agent: Optional[str]) -> str:
    """Map an agent id to its short kind tag (claude/openclaw/chatgpt/...).

    Keys off the FIRST colon-segment of the agent id. ``claude-code`` ->
    ``claude`` and ``codex`` -> ``chatgpt``; everything else is lowercased and
    passed through so an unknown family is still tagged usefully. Empty/None ->
    ``unknown`` (never raises)."""
    if not agent:
        return "unknown"
    first = agent.split(":", 1)[0].strip().lower()
    if not first:
        return "unknown"
    return _KIND_MAP.get(first, first)


def session_tag(agent: Optional[str]) -> Optional[str]:
    """A short session/channel tag derived from the agent id (2nd segment, else
    3rd). None when there is nothing beyond the kind."""
    if not agent:
        return None
    parts = agent.split(":")
    if len(parts) < 2:
        return None
    for seg in parts[1:]:
        seg = seg.strip()
        if seg:
            return seg
    return None


# ---------------------------------------------------------------------------
# Library link
# ---------------------------------------------------------------------------

def library_link(task: dict[str, Any]) -> str:
    """Best-effort deep link to the task in the Fulcra library web app."""
    task_id = task.get("id", "")
    path = f"{remote_root()}/tasks/{task_id}.json".lstrip("/")
    return f"https://library.fulcradynamics.com/files/{path}"


def _transition_at(task: dict[str, Any]) -> Optional[str]:
    """The timestamp of the transition this annotation records — the latest
    event's ``at``, falling back to ``updated_at`` (None when neither)."""
    events = task.get("events") or []
    if events:
        at = events[-1].get("at")
        if at:
            return at
    return task.get("updated_at")


def build_annotation(
    *, lifecycle: str, task: dict[str, Any], agent: str
) -> dict[str, Any]:
    """Build the annotation payload (pure; no I/O). Shape is unchanged from the
    legacy writer — two coexisting tag lists (``tags`` bare + ``cli_tags``
    namespaced), ``name`` (concise, link-free), ``desc`` (work substance), a
    library ``link``, and a ``recorded_at`` transition anchor."""
    title = task.get("title", "(untitled)")
    task_id = task.get("id", "")
    link = library_link(task)
    kind = agent_kind(agent)
    st = session_tag(agent)

    tags = [lifecycle, kind]
    if st:
        tags.append(st)

    cli_tags = ["agent-tasks", lifecycle, f"agent:{kind}"]
    if st:
        cli_tags.append(f"session:{st}")

    name = f"{lifecycle}: {title} ({task_id})"
    text = f"{name} {link}"

    workstream = task.get("workstream", "") or ""
    kind_tag = _extract_kind_from_tags(task.get("tags") or [])
    prefix = "/".join(p for p in (workstream, kind_tag) if p)
    summary = (task.get("current_summary") or "").strip()
    nxt = (task.get("next_action") or "").strip()
    blurb_parts = []
    if prefix:
        blurb_parts.append(f"[{prefix}]")
    blurb_parts.append(title)
    tail = " · ".join(x for x in (summary, (f"next: {nxt}" if nxt else "")) if x)
    blurb = " ".join(blurb_parts) + (f" — {tail}" if tail else "")
    desc = blurb.strip() or link

    return {
        "track": TRACK_NAME,
        "tags": tags,
        "cli_tags": cli_tags,
        "name": name,
        "desc": desc,
        "text": text,
        "link": link,
        "lifecycle": lifecycle,
        "task_id": task_id,
        "agent": agent,
        "recorded_at": _transition_at(task),
    }


#: The track tag for "the operator needs to do something" moments.
NEEDS_USER_TAG = "needs-user"


def build_needs_user_annotation(
    *, task: dict[str, Any], agent: str
) -> dict[str, Any]:
    """Build the ``needs-user`` moment payload for a ``block --on-user`` (pure).
    Same shape as :func:`build_annotation` but tagged ``needs-user`` and the desc
    leads with the ask (``blocked_on``)."""
    title = task.get("title", "(untitled)")
    task_id = task.get("id", "")
    link = library_link(task)
    kind = agent_kind(agent)
    st = session_tag(agent)
    ask = (task.get("blocked_on") or task.get("next_action") or "").strip()

    tags = [NEEDS_USER_TAG, kind]
    if st:
        tags.append(st)
    cli_tags = ["agent-tasks", NEEDS_USER_TAG, f"agent:{kind}"]
    if st:
        cli_tags.append(f"session:{st}")

    name = f"needs-user: {title} ({task_id})"
    desc = (f"{ask} — {link}" if ask else link)
    text = f"{name} {link}"

    return {
        "track": TRACK_NAME,
        "tags": tags,
        "cli_tags": cli_tags,
        "name": name,
        "desc": desc,
        "text": text,
        "link": link,
        "lifecycle": NEEDS_USER_TAG,
        "task_id": task_id,
        "agent": agent,
        "ask": ask,
        "recorded_at": _transition_at(task),
    }


# ---------------------------------------------------------------------------
# Capability gate (env FULCRA_COORD_ANNOTATIONS > persisted config > off).
# The env var and config file are kept identical to the legacy writer so a
# machine already opted in (``annotations on``) keeps emitting after the port.
# ---------------------------------------------------------------------------

def _annotations_config_path() -> Path:
    return config_root() / "annotations"


def _normalize_mode(raw: str) -> Optional[str]:
    """Map a raw mode token to ``"on"``, or None if it does not enable the
    writer. Legacy enable tokens (``http``/``api``/``cli``) still mean on."""
    raw = (raw or "").strip().lower()
    if raw in ("on", "http", "api", "cli"):
        return "on"
    return None


def _persisted_mode() -> Optional[str]:
    path = _annotations_config_path()
    if not path.exists():
        return None
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    return _normalize_mode(raw)


def set_persisted_mode(mode: str) -> Path:
    """Persist ``mode`` (normalized to ``"on"``/``"off"``) so every agent emits."""
    normalized = _normalize_mode(mode) or "off"
    path = _annotations_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(normalized + "\n")
    return path


def clear_persisted_mode() -> bool:
    """Remove the persisted mode file. Returns True if a file was removed."""
    path = _annotations_config_path()
    if path.exists():
        path.unlink()
        return True
    return False


def resolve_mode_source() -> tuple[str, str]:
    """Resolve the annotation mode AND report its source
    (``env`` | ``config`` | ``default``). Env always wins."""
    env_raw = os.environ.get("FULCRA_COORD_ANNOTATIONS", "").strip()
    if env_raw:
        return (_normalize_mode(env_raw) or "off", "env")
    persisted = _persisted_mode()
    if persisted:
        return (persisted, "config")
    return ("off", "default")


def _mode() -> str:
    """Resolve the enable mode. Returns ``off`` | ``on`` (off by default)."""
    return resolve_mode_source()[0]


# ---------------------------------------------------------------------------
# Idempotency marker keying (per lifecycle transition)
# ---------------------------------------------------------------------------

def _transition_anchor(task: dict[str, Any]) -> str:
    """A stable key for "this specific lifecycle transition" — event count, at,
    and type — so a write-retry dedupes while a genuinely new transition emits."""
    events = task.get("events") or []
    if events:
        last = events[-1]
        at = last.get("at") or ""
        if at:
            return f"{len(events)}|{at}|{last.get('type', '')}"
    return task.get("updated_at") or task.get("created_at") or ""


def _marker_key(lifecycle: str, task: dict[str, Any]) -> str:
    return f"{task.get('id', '')}|{lifecycle}|{_transition_anchor(task)}"


def _already_annotated(lifecycle: str, task: dict[str, Any]) -> bool:
    return has_annotation_marker(_marker_key(lifecycle, task))


def _record_annotated(lifecycle: str, task: dict[str, Any]) -> None:
    write_annotation_marker(_marker_key(lifecycle, task))


# ---------------------------------------------------------------------------
# CLI shell-out helpers (tags + definitions resolve via the public Fulcra CLI)
# ---------------------------------------------------------------------------

def _fulcra_cli_json(args: list[str], *, backend: Optional[list[str]] = None) -> Any:
    """Run ``<cli-base> <args>``; return parsed stdout JSON, or None on ANY
    failure (rc!=0, timeout, missing CLI, non-JSON). Never raises."""
    base = backend if backend is not None else _cli_base_cmd()
    try:
        result = subprocess.run(
            list(base) + args, capture_output=True, text=True, timeout=_write_timeout(),
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def _fulcra_cli_json_lines(args: list[str], *, backend: Optional[list[str]] = None) -> list:
    """Run ``<cli-base> <args>``; parse stdout as JSONL, returning the parsed
    list, or ``[]`` on ANY failure. Retained for back-compat; new resolution
    uses :func:`_fulcra_cli_lines_or_error`, which DISTINGUISHES a lookup error
    from a genuinely-empty reply."""
    lines = _fulcra_cli_lines_or_error(args, backend=backend)
    return lines if lines is not None else []


def _fulcra_cli_lines_or_error(
    args: list[str], *, backend: Optional[list[str]] = None
) -> Optional[list]:
    """Run ``<cli-base> <args>`` and parse stdout as JSONL — one JSON object per
    line — returning the parsed list, or **None on a lookup ERROR** (rc!=0,
    timeout, missing CLI). This None-vs-[] distinction is the fail-closed hinge:
    an empty list means the catalog verifiably has no matching definition (safe
    to create), while None means the lookup itself failed (refuse to create).
    An unparseable line is skipped, not fatal. Never raises."""
    base = backend if backend is not None else _cli_base_cmd()
    try:
        result = subprocess.run(
            list(base) + args, capture_output=True, text=True, timeout=_write_timeout(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    out: list = []
    saw_nonempty_line = False
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        saw_nonempty_line = True
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if saw_nonempty_line and not out:
        # rc==0 but stdout carried non-empty lines that ALL failed to parse — a
        # banner / warning / format-drift, NOT a genuinely-empty catalog. Return
        # the lookup-ERROR sentinel (None), never [] — an [] here would read as
        # "verifiably absent" and let the caller create a duplicate definition.
        # Per-line skip still holds when at least one line DID parse.
        return None
    return out


def _resolve_token() -> Optional[str]:
    """Return a Fulcra bearer token, or None if one can't be obtained.

    ``FULCRA_ACCESS_TOKEN`` env when set, else the stdout of
    ``<cli-base> auth print-access-token``. Best-effort; the token is never
    logged."""
    env = os.environ.get("FULCRA_ACCESS_TOKEN")
    if env and env.strip():
        return env.strip()
    try:
        result = subprocess.run(
            [*_cli_base_cmd(), "auth", "print-access-token"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    tok = (result.stdout or "").strip()
    return tok or None


def _request(method: str, url: str, token: str, *,
             body: Optional[bytes] = None,
             content_type: str = "application/json") -> tuple[int, bytes]:
    """Issue one authenticated HTTP request via urllib; return (status, body).

    Raises ``urllib.error.HTTPError`` on non-2xx and ``URLError`` on transport
    failure. A body sets the matching content-type and an explicit
    ``Content-Length`` (required by the typed ingest endpoint). 30s timeout."""
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "fulcra-common-annotations")
    if body is not None:
        req.add_header("Content-Type", content_type)
        req.add_header("Content-Length", str(len(body)))
    with urllib.request.urlopen(req, timeout=30) as resp:
        status = getattr(resp, "status", None) or resp.getcode()
        return status, resp.read()


def _definition_live(def_id: str, token: Optional[str]) -> Optional[bool]:
    """Authoritative liveness of an annotation definition, tri-state.

    True → per-id GET returned 200 with no ``deleted_at``; False → 200 with
    ``deleted_at`` set, or 403/404 (soft-deleted / not this account's);
    None → could not determine (no token, auth flake, 5xx, transport error).
    Needed because the public catalog reports soft-deleted user definitions as
    ``deprecated: false`` — only /user/v1alpha1/annotation/{id} tells the truth
    (mirrors ``FulcraClient.definition_exists``, kept urllib-pure here)."""
    if not token:
        return None
    try:
        status, body = _request(
            "GET", f"{_api_base()}/user/v1alpha1/annotation/{def_id}", token)
        if status == 200:
            return not json.loads(body).get("deleted_at")
        return None
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):
            return False
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Tag-id cache (name -> id, TTL-bounded so a server-side delete self-heals)
# ---------------------------------------------------------------------------

def _tag_cache_path() -> Path:
    return annotations_dir() / "tags.json"


def _load_tag_cache() -> dict[str, str]:
    try:
        path = _tag_cache_path()
        if path.exists():
            data = json.loads(path.read_text())
            if isinstance(data, dict) and _is_fresh(data.get("written_at")):
                tags = data.get("tags")
                if isinstance(tags, dict):
                    return tags
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _store_tag_id(name: str, tag_id: str) -> None:
    try:
        annotations_dir().mkdir(parents=True, exist_ok=True)
        data = _load_tag_cache()
        data[name] = tag_id
        _tag_cache_path().write_text(
            json.dumps({"written_at": _cache_now_iso(), "tags": data}))
    except OSError:
        pass


def _resolve_tag_id(name: str, token: Optional[str] = None) -> str:
    """Return the id of the tag named ``name``, creating it if absent.

    Cache hit -> return immediately. Else ``fulcra tag get <name>`` (a dict with
    an ``id``), else ``fulcra tag create <name>`` (a ``[created_tag]`` list; a
    409 "already exists" yields ``[]``). Total failure -> ``""`` (caller skips
    it; an empty id is never cached). ``token`` is accepted for back-compat but
    unused (the CLI carries its own auth)."""
    cached = _load_tag_cache().get(name)
    if cached:
        return cached

    tag_id: Optional[str] = None
    got = _fulcra_cli_json(["tag", "get", name])
    if isinstance(got, dict) and got.get("id"):
        tag_id = got["id"]
    else:
        made = _fulcra_cli_json(["tag", "create", name])
        if isinstance(made, list) and made and isinstance(made[0], dict) and made[0].get("id"):
            tag_id = made[0]["id"]
        elif isinstance(made, dict) and made.get("id"):
            tag_id = made["id"]

    if not tag_id:
        return ""
    _store_tag_id(name, tag_id)
    return tag_id


# ---------------------------------------------------------------------------
# TTL-bounded resolved-id caches (definition ids)
# ---------------------------------------------------------------------------

_DEFAULT_ANNOTATION_CACHE_TTL_SECONDS = 24 * 60 * 60


def _annotation_cache_ttl_seconds() -> float:
    raw = os.environ.get("FULCRA_COORD_ANNOTATION_CACHE_TTL_SECONDS")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return float(_DEFAULT_ANNOTATION_CACHE_TTL_SECONDS)


def _is_fresh(written_at: Any) -> bool:
    if not written_at:
        return False
    try:
        dt = datetime.fromisoformat(str(written_at).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - dt).total_seconds()
    return age <= _annotation_cache_ttl_seconds()


def _cache_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _definition_cache_path() -> Path:
    return annotations_dir() / "definition.json"


def _cached_definition_id() -> Optional[str]:
    path = _definition_cache_path()
    try:
        if path.exists():
            data = json.loads(path.read_text())
            did = data.get("id")
            if did and (data.get("pinned") or _is_fresh(data.get("written_at"))):
                return did
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _store_definition_id(def_id: str) -> None:
    try:
        annotations_dir().mkdir(parents=True, exist_ok=True)
        _definition_cache_path().write_text(
            json.dumps({"id": def_id, "written_at": _cache_now_iso()}))
    except OSError:
        pass


def pin_definition_id(def_id: str, *, digest: bool = False) -> str:
    """Operator-explicit pin: write a never-expiring cache entry for the
    (Agent Tasks | Digest) definition id. Returns the cache path written."""
    annotations_dir().mkdir(parents=True, exist_ok=True)
    path = _digest_definition_cache_path() if digest else _definition_cache_path()
    path.write_text(json.dumps(
        {"id": def_id, "written_at": _cache_now_iso(), "pinned": True}))
    return str(path)


#: In-run memo: definition-name -> resolved uuid. Populated ONLY on a verified
#: resolve/create (never on a lookup error), so a run re-resolves at most once
#: and a failed lookup is free to retry (self-heal) on the next emit.
_DEF_ID_MEMO: dict[str, str] = {}


#: Canonical definition every Agent-Tasks moment groups under.
DEFINITION_NAME = "Agent Tasks"
DEFINITION_DESCRIPTION = (
    "Lifecycle moments for fulcra-coord agent coordination tasks "
    "(create / pickup / update / complete, plus needs-user asks)."
)


def _dedup_sort_key(match: dict[str, str]) -> tuple[str, str]:
    """Sort key for duplicate resolution: created_at ascending (oldest first),
    uuid as a stable tiebreak. Rows LACKING created_at sort AFTER dated rows
    (``￿`` sentinel) and among themselves by uuid — preserving the legacy
    deterministic-uuid convergence when the catalog omits created_at."""
    return (match.get("created_at") or "￿", match["id"])


def _resolve_def_via_cli(def_name: str, description: str, tag_names: list[str]) -> str:
    """Resolve-or-create the moment definition named ``def_name``, returning its
    UUID (or ``""`` on refuse/failure). FAIL-CLOSED: a catalog LOOKUP ERROR never
    creates; a create happens ONLY on a verified-absent definition. Duplicates
    resolve to the OLDEST (created_at ascending). An in-run memo short-circuits
    repeat resolves. Never raises."""
    memoized = _DEF_ID_MEMO.get(def_name)
    if memoized:
        return memoized

    lines = _fulcra_cli_lines_or_error(["catalog", "--name", def_name])
    if lines is None:
        # LOOKUP ERROR: refuse to write. Creating here is exactly the
        # 2026-07-03 definition-proliferation bug.
        logger.error(
            "annotations: catalog lookup for definition %r failed (CLI error); "
            "refusing to create to avoid definition proliferation", def_name)
        return ""

    matches: list[dict[str, str]] = []
    has_unreadable_same_name = False
    for e in lines:
        if not isinstance(e, dict) or e.get("name") != def_name:
            continue
        meta = e.get("metadata") or {}
        top_id = e.get("id")
        # Legacy catalog shape: metadata.{annotation_type,id,deleted_at}.
        if meta.get("annotation_type") == "moment" and meta.get("id"):
            if not meta.get("deleted_at"):
                matches.append({
                    "id": str(meta["id"]),
                    "created_at": str(meta.get("created_at") or e.get("created_at") or ""),
                })
            # else: RECOGNIZED legacy soft-delete (deleted_at set) — permits a
            # create (legacy behavior preserved); a known shape, not unreadable.
            continue
        # Current fulcra-api shape: TOP-LEVEL id "MomentAnnotation/<uuid>",
        # column_name "moment", no metadata.
        if (e.get("column_name") == "moment" and isinstance(top_id, str)
                and top_id.startswith("MomentAnnotation/")):
            if not e.get("deprecated"):
                matches.append({
                    "id": top_id.split("/", 1)[1],
                    "created_at": str(e.get("created_at") or ""),
                })
            # else: RECOGNIZED current soft-delete (deprecated) — permits create.
            continue
        # A same-name entry in NEITHER recognized shape: catalog schema drift.
        # NOT verifiably absent — refuse below rather than create a duplicate.
        has_unreadable_same_name = True

    if matches:
        matches.sort(key=_dedup_sort_key)
        # The public catalog LIES about deletion for user-defined definitions:
        # soft-deleted defs come back `deprecated: false` (live-verified
        # 2026-07-13 — every 07-03-deleted duplicate still read as live). The
        # authoritative state is the per-id GET's `deleted_at`
        # (/user/v1alpha1/annotation/{id}), so verify candidates there before
        # picking: writing against a soft-deleted def is accepted by ingest and
        # the moments are silently invisible on the timeline.
        token = _resolve_token()
        unknown: list[dict[str, str]] = []
        chosen = None
        for m in matches:  # oldest-first: first VERIFIED-LIVE wins
            live = _definition_live(m["id"], token)
            if live is True:
                chosen = m["id"]
                break
            if live is None:
                unknown.append(m)
            # live is False: authoritatively soft-deleted — never a candidate.
        if chosen is not None:
            if len(matches) > 1:
                logger.warning(
                    "annotations: %d definitions named %r exist (duplicates from "
                    "the 2026-07-03 proliferation bug); picking OLDEST verified-"
                    "live %s; candidates=%s; NOT creating another",
                    len(matches), def_name, chosen, [m["id"] for m in matches])
            _DEF_ID_MEMO[def_name] = chosen
            return chosen
        if unknown:
            # Liveness unverifiable (no token / flake) for every non-deleted
            # candidate: fall back to the oldest unverified one rather than
            # refusing to annotate — the pre-verification behavior, now loud.
            # Deliberately NOT memoized: the memo's invariant is verified-only,
            # and pinning an unverified pick for the process lifetime would
            # route every later emit to a possibly-deleted def with no retry.
            # The next resolve re-attempts verification instead.
            fallback = unknown[0]["id"]
            logger.warning(
                "annotations: liveness of %r candidates could not be verified; "
                "falling back to OLDEST unverified %s (not memoized — "
                "verification retries on the next resolve)", def_name, fallback)
            return fallback
        # Every same-name candidate is AUTHORITATIVELY soft-deleted: same as the
        # recognized-soft-delete catalog shapes above — permits a create.
        logger.info(
            "annotations: all %d same-name candidates for %r are soft-deleted "
            "per the authoritative per-id check; treating as absent",
            len(matches), def_name)

    if has_unreadable_same_name:
        # A same-name catalog entry exists but sits in a shape we can classify as
        # neither recognized-live nor recognized-soft-deleted (schema drift). An
        # unreadable same-name entry is NOT "verified absent" — creating here is
        # exactly the 2026-07-03 proliferation bug's fail-OPEN. Refuse.
        logger.error(
            "annotations: catalog for definition %r has a same-name entry in an "
            "unrecognized shape (neither recognized-live nor recognized-soft-"
            "deleted); refusing to create to avoid definition proliferation",
            def_name)
        return ""

    # VERIFIED ABSENT (catalog returned a clean, empty reply) -> create once.
    cmd = ["data-type", "create", "MomentAnnotation", def_name,
           "--description", description, "--add-to-timeline"]
    for t in tag_names:
        if t:
            cmd += ["--tag", t]
    made = _fulcra_cli_json(cmd)
    if isinstance(made, dict) and made.get("id"):
        logger.info("annotations: created definition %r -> %s (verified absent)",
                    def_name, made["id"])
        _DEF_ID_MEMO[def_name] = made["id"]
        return made["id"]
    logger.warning("annotations: definition %r absent and create returned no id",
                   def_name)
    return ""


def _resolve_definition_id(tag_names: list[str], *, token: Optional[str] = None) -> str:
    """Return the ``Agent Tasks`` moment-definition UUID (disk cache -> resolve).
    ``token`` accepted for back-compat but unused. Best-effort: a failure/refuse
    returns ``""`` (not cached)."""
    cached = _cached_definition_id()
    if cached:
        return cached
    def_id = _resolve_def_via_cli(DEFINITION_NAME, DEFINITION_DESCRIPTION, tag_names)
    if def_id:
        _store_definition_id(def_id)
    return def_id


#: The digest track's own definition — distinct so digests filter separately.
DIGEST_DEFINITION_NAME = "Agent Tasks — Digest"
DIGEST_DEFINITION_DESCRIPTION = (
    "Twice-daily + on-demand operator situational-awareness digests "
    "(what's blocked on you, upcoming, what each agent did, what's stale)."
)
DIGEST_TRACK_TAG = "agent-digest"


def _digest_definition_cache_path() -> Path:
    return annotations_dir() / "digest-definition.json"


def _cached_digest_definition_id() -> Optional[str]:
    path = _digest_definition_cache_path()
    try:
        if path.exists():
            data = json.loads(path.read_text())
            did = data.get("id")
            if did and (data.get("pinned") or _is_fresh(data.get("written_at"))):
                return did
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _store_digest_definition_id(def_id: str) -> None:
    try:
        annotations_dir().mkdir(parents=True, exist_ok=True)
        _digest_definition_cache_path().write_text(
            json.dumps({"id": def_id, "written_at": _cache_now_iso()}))
    except OSError:
        pass


def _resolve_digest_definition_id(tag_names: list[str], *, token: Optional[str] = None) -> str:
    """Return the ``Agent Tasks — Digest`` definition UUID (disk cache ->
    resolve). Same fail-closed/duplicate resolution as the per-event def."""
    cached = _cached_digest_definition_id()
    if cached:
        return cached
    def_id = _resolve_def_via_cli(
        DIGEST_DEFINITION_NAME, DIGEST_DEFINITION_DESCRIPTION, tag_names)
    if def_id:
        _store_digest_definition_id(def_id)
    return def_id


def _recorded_at(payload: dict[str, Any]) -> str:
    """ISO-8601 Z timestamp from the payload anchor else now."""
    for key in ("recorded_at", "at", "ts", "timestamp"):
        val = payload.get(key)
        if val:
            return str(val).replace("+00:00", "Z")
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# The typed record POST (base type endpoint; custom def referenced via sources)
# ---------------------------------------------------------------------------

def _post_typed_moment_record(
    token: str, *, inner: dict[str, Any], recorded_at: str,
    tag_ids: list[str], sources: list[str],
) -> None:
    """POST one MomentAnnotation record to the TYPED endpoint as jsonlines.

    The body is the UNWRAPPED, flat record — the moment's ``title``/``note``
    (``inner``) at top level, plus ``recorded_at`` / ``tags`` / ``sources`` —
    NOT the legacy ``DataRecordV1`` envelope. The path is the BASE type
    (``/ingest/v1/record/MomentAnnotation``); the custom definition is referenced
    by uuid inside ``sources`` (a custom type is not a valid path segment).
    One record per line, ``application/x-jsonl``."""
    record: dict[str, Any] = dict(inner)
    record["recorded_at"] = recorded_at
    record["tags"] = tag_ids
    record["sources"] = sources
    body = (json.dumps(record, sort_keys=True) + "\n").encode()
    _request(
        "POST",
        f"{_api_base()}/ingest/v1/record/MomentAnnotation",
        token,
        body=body,
        content_type="application/x-jsonl",
    )


def _api_base() -> str:
    """Fulcra API base URL — env ``FULCRA_API_BASE`` else the prod host."""
    base = os.environ.get("FULCRA_API_BASE", "https://api.fulcradynamics.com")
    return base.rstrip("/")


def _write_http(payload: dict[str, Any], *, backend: Optional[list[str]] = None) -> bool:
    """Write the annotation record via the typed Fulcra ingest endpoint.

    Steps: resolve/create each tag -> resolve the ``Agent Tasks`` definition
    (fail-closed) -> POST the flat typed record. BEST-EFFORT: a missing token,
    any resolution refuse (empty def id), or any urllib/HTTP error returns False
    and NEVER raises. (``emit_*`` records the idempotency marker only on a True
    return, so a failure leaves the transition free to retry.) ``backend`` is
    accepted for signature back-compat and ignored."""
    try:
        token = _resolve_token()
        if not token:
            return False

        tag_names = payload.get("cli_tags") or payload.get("tags") or []
        tag_ids: list[str] = []
        for name in tag_names:
            if not name:
                continue
            tag_id = _resolve_tag_id(name, token)
            if tag_id:
                tag_ids.append(tag_id)

        def_id = _resolve_definition_id(list(tag_names), token=token)
        if not def_id:
            # Fail-closed: resolution refused (lookup error) or failed. Do not
            # write a record that would be orphaned or mint a duplicate def.
            return False

        # The typed MomentAnnotation schema is flat and CLOSED —
        # {note, recorded_at, tags, sources, id} — and the server SILENTLY
        # STRIPS any other top-level key. ``title`` is not a served column, so
        # emitting it here would drop the title line (and the task_id it
        # carries) on 100% of writes. Fold the title into ``note`` (the only
        # served free-text slot) so nothing is lost.
        inner: dict[str, Any] = {}
        title = (payload.get("name") or "").strip()
        note = (payload.get("desc") or payload.get("description") or "").strip()
        merged = "\n\n".join(part for part in (title, note) if part)
        if merged:
            inner["note"] = merged

        # ``id`` is a served MomentAnnotation column (not silently stripped). When
        # the caller supplies a deterministic record id (projection), pass it
        # through so the record itself carries it — enabling record-level
        # idempotency on re-POST. ``_post_typed_moment_record`` preserves any id
        # already present in ``inner``.
        rec_id = payload.get("id")
        if rec_id:
            inner["id"] = rec_id

        lifecycle = payload.get("lifecycle") or "event"
        sources = [
            f"com.fulcradynamics.fulcra-coord.{lifecycle}.{uuid.uuid4()}",
            f"com.fulcradynamics.annotation.{def_id}",
        ]

        _post_typed_moment_record(
            token, inner=inner, recorded_at=_recorded_at(payload),
            tag_ids=tag_ids, sources=sources)
        return True
    except Exception:
        logger.debug("annotations: record write failed (best-effort)", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Public entry points (best-effort; never raise into the caller)
# ---------------------------------------------------------------------------

def emit_lifecycle_annotation(
    *,
    lifecycle: str,
    task: dict[str, Any],
    agent: str,
    backend: Optional[list[str]] = None,
) -> bool:
    """Emit one Agent-Tasks lifecycle annotation. BEST-EFFORT, NEVER RAISES.
    Returns True only when a record was actually written on THIS call. Guarded
    by a per-(task, lifecycle, transition-anchor) idempotency marker."""
    try:
        if _mode() == "off":
            return False
        if lifecycle not in LIFECYCLES:
            return False
        if _already_annotated(lifecycle, task):
            return False

        payload = build_annotation(lifecycle=lifecycle, task=task, agent=agent)
        wrote = _write_http(payload, backend=backend)
        if wrote:
            _record_annotated(lifecycle, task)
        return bool(wrote)
    except Exception:
        logger.debug("annotations: emit_lifecycle failed (best-effort)", exc_info=True)
        return False


def emit_needs_user_annotation(
    *,
    task: dict[str, Any],
    agent: str,
    backend: Optional[list[str]] = None,
) -> bool:
    """Emit one ``needs-user`` moment when a task is blocked on the human.
    Same gating/transport/idempotency contract as
    :func:`emit_lifecycle_annotation`. NEVER raises."""
    try:
        if _mode() == "off":
            return False
        if _already_annotated(NEEDS_USER_TAG, task):
            return False

        payload = build_needs_user_annotation(task=task, agent=agent)
        wrote = _write_http(payload, backend=backend)
        if wrote:
            _record_annotated(NEEDS_USER_TAG, task)
        return bool(wrote)
    except Exception:
        logger.debug("annotations: emit_needs_user failed (best-effort)", exc_info=True)
        return False


#: Provenance namespace for coord-*projected* moments — a transition annotated
#: mechanically from the reconcile heartbeat (model-free, platform-agnostic)
#: rather than by an in-process lifecycle writer. Mirrors
#: ``coord_engine.annotate.SOURCE_MARKER``; the record's first source lands under
#: this namespace (``_write_http`` appends a per-record uuid).
PROJECTION_SOURCE = "com.fulcradynamics.fulcra-coord.projection"


def emit_projection_annotation(
    *,
    note: str,
    tags: list[str],
    recorded_at: Optional[str],
    id: Optional[str] = None,
    agent: Optional[str] = None,
    backend: Optional[list[str]] = None,
) -> bool:
    """Emit ONE coord-*projected* timeline annotation. BEST-EFFORT, NEVER RAISES.

    This is the writer seam for coord-engine's projection fold (an
    ``AnnotationSpec`` -> a record). Unlike :func:`emit_lifecycle_annotation` it
    is deliberately NOT gated by the machine-local :func:`_mode` config and does
    NOT restrict the transition kind, because projection carries its OWN opt-in
    (the team's bus ``annotate resolution`` level) and its OWN idempotency (the
    projection cursor + ``seen_ids``), both owned by coord-engine's heartbeat —
    the whole point is to annotate a transition made by ANY host/harness, not
    only one running the in-process writer.

    It REUSES the same hardened typed-record path as the lifecycle writer
    (``_write_http`` -> ``_post_typed_moment_record``): fail-closed definition
    resolution, closed-schema-safe note folding, stdlib ``urllib`` POST. It does
    NOT open a second POST path. ``note`` is the already-composed one-line
    summary (title/kind/assignee/next-action folded in — ``title`` is not a
    served key); ``tags`` are the bare tag NAMES (e.g. ``[agent-tasks, update]``);
    ``recorded_at`` is the transition ts. ``agent`` is accepted for signature
    symmetry but unused — a projected moment is host-agnostic. Returns True only
    when a record was actually written on THIS call."""
    try:
        # Tripwire: projection is the SUCCESSOR to the in-process lifecycle writer,
        # and both emit Agent-Tasks moments for the same transition to a no-dedup
        # endpoint. If the legacy writer is enabled here, this call is the exact
        # point where both would be live and the timeline double-writes — surface
        # it loudly rather than let the duplication go silent.
        if _mode() == "on":
            logger.warning(
                "annotations: projection is emitting while the legacy in-process "
                "writer is ON (FULCRA_COORD_ANNOTATIONS) — both write the same "
                "transition to a no-dedup endpoint; disable the legacy writer")
        payload: dict[str, Any] = {
            "cli_tags": [t for t in (tags or []) if t],
            "desc": note or "",
            # lifecycle "projection" -> sources[0] under PROJECTION_SOURCE
            "lifecycle": "projection",
            "recorded_at": recorded_at,
        }
        # The projection's DETERMINISTIC id (keyed on team/task_id/kind/ts, computed
        # by coord_engine.annotate FOR idempotency). ``id`` IS a served
        # MomentAnnotation column, so threading it onto the record lets a record
        # re-POSTed after a cursor-persist failure carry the SAME id rather than
        # mint a distinct timeline row — the record-level idempotency layer this
        # id was designed for. Only set when supplied so we never write an empty id.
        if id:
            payload["id"] = id
        return bool(_write_http(payload, backend=backend))
    except Exception:
        logger.debug("annotations: emit_projection failed (best-effort)", exc_info=True)
        return False


def emit_digest_annotation(*, name: str, note: str, window: str, agent: str,
                           backend: Optional[list[str]] = None,
                           gated: bool = True,
                           id: Optional[str] = None) -> bool:
    """Emit ONE operator-digest moment on the ``Agent Tasks — Digest`` track.

    BEST-EFFORT, NEVER RAISES. Reuses the typed record path against
    ``_resolve_digest_definition_id`` so digests land on their OWN track. Tags:
    ``[agent-digest, <window>, agent:<kind>]``. No idempotency marker here — the
    per-window dedup guard (in the CLI) prevents a double digest.

    ``gated=True`` (default) keeps the legacy in-process-writer contract: the
    machine-local :func:`_mode` config must be ``on``. Engine-driven callers
    (coord-engine's ``digest --emit-timeline`` heartbeat leg) pass
    ``gated=False`` for the same reason :func:`emit_projection_annotation`
    skips the mode gate entirely: their opt-in and idempotency are OWNED by the
    engine, and the emit must work from any host/harness, not only one whose
    local writer config happens to be switched on.

    ``id``: optional explicit record id. The typed ingest endpoint UPSERTS on
    an explicit id (live-verified 2026-07-14: same-id re-POST returns 201 and
    the record count stays 1), so a caller passing a DETERMINISTIC id gets
    ingestion-layer idempotency — concurrent emitters of the same logical
    digest converge on one timeline record."""
    try:
        if gated and _mode() == "off":
            return False
        token = _resolve_token()
        if not token:
            return False
        kind = agent_kind(agent)
        tag_names = [DIGEST_TRACK_TAG, window, f"agent:{kind}"]
        tag_ids = []
        for n in tag_names:
            if not n:
                continue
            tag_id = _resolve_tag_id(n, token)
            if tag_id:
                tag_ids.append(tag_id)
        def_id = _resolve_digest_definition_id([n for n in tag_names if n], token=token)
        if not def_id:
            return False

        # Fold the digest title into ``note``; the typed schema is closed and
        # SILENTLY STRIPS a top-level ``title`` (see ``_write_http``).
        inner: dict[str, Any] = {}
        merged = "\n\n".join(part for part in (name.strip(), note.strip()) if part)
        if merged:
            inner["note"] = merged
        if id:
            inner["id"] = id
        sources = [
            f"com.fulcradynamics.fulcra-coord.digest.{uuid.uuid4()}",
            f"com.fulcradynamics.annotation.{def_id}",
        ]
        _post_typed_moment_record(
            token, inner=inner,
            recorded_at=datetime.now(timezone.utc).isoformat(
                timespec="microseconds").replace("+00:00", "Z"),
            tag_ids=tag_ids, sources=sources)
        return True
    except Exception:
        logger.debug("annotations: emit_digest failed (best-effort)", exc_info=True)
        return False

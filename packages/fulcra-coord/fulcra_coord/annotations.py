"""Fulcra "Agent Tasks" lifecycle annotations (feature #3).

WHAT THIS DOES
--------------
Every time an agent CREATES, PICKS UP, UPDATES, or COMPLETES a fulcra-coord
task, we want a single durable breadcrumb on the operator's Fulcra timeline — a
"moment annotation" on a track named **Agent Tasks** — so the human can see, on
their own Life timeline, *what the agents were doing and when*. One track, one
annotation per real lifecycle transition.

THE WRITE MECHANISM (confirmed live via the Fulcra CLI)
-------------------------------------------------------
The per-occurrence write is a *moment annotation* created on the timeline via
the Fulcra CLI's ``create-data-type`` command::

    fulcra create-data-type MomentAnnotation "<NAME>" \
        --description "<desc>" --add-to-timeline \
        --tag agent-tasks --tag <lifecycle> --tag agent:<kind> --tag session:<sess>

``--add-to-timeline`` makes it a real occurrence on the operator's Life
timeline; tags passed by name are auto-created. The shared track tag
``agent-tasks`` lets every Agent-Tasks moment be filtered together regardless
of lifecycle/agent. The created annotation returns JSON including its ``id`` and
``fulcra_source_id`` and is deletable via ``fulcra delete-data-type <id>`` (used
only by the live smoke test, never in the task path).

This support currently lives on the Fulcra CLI's ``create-annotations-commands``
branch (not yet on ``fulcra-api`` main). Until it merges, point fulcra-coord at
that build via ``FULCRA_CLI_COMMAND`` (e.g.
``FULCRA_CLI_COMMAND="uv run --project /path/to/fulcra-api-python fulcra"``);
once it lands on ``fulcra-api`` main and the installed CLI gains
``create-data-type``, no pointer is needed.

WHY IT IS CAPABILITY-GATED (and OFF by default)
-----------------------------------------------
Even though the write is now live, the feature stays GATED behind
``FULCRA_COORD_ANNOTATIONS`` so it cannot perturb task ops unless an operator
opts in, and so machines without the annotations-capable CLI stay inert:

  * unset / "off" / anything unrecognized  -> NO-OP (the default; safe)
  * "http" (or its alias "api")  -> POST to the Fulcra HTTP API directly via
    stdlib ``urllib`` (no httpx / fulcra-common dep), replicating the proven
    fulcra-collect write path: resolve/create tags, resolve/create the shared
    "Agent Tasks" moment definition (cached), then POST a JSONL record to
    ``/ingest/v1/record/batch``. See ``_write_http``.
  * "cli"  -> shell ``create-data-type MomentAnnotation ... --add-to-timeline``
    through the resolved Fulcra CLI (legacy; needs the annotations-capable CLI
    build, which the everyday installed CLI lacks — kept for back-compat)

The HTTP path is the recommended one; the public contract, tag mapping,
text/link building, gating, idempotency, and the CLI hook points are unchanged
regardless of transport.

CONTRACT
--------
``emit_lifecycle_annotation(*, lifecycle, task, agent, backend=None)`` is
BEST-EFFORT and MUST NEVER raise into the caller: a coordination task write must
succeed (or fail) entirely on its own merits, never because an annotation
backend was slow, missing, or broken. Everything is wrapped in try/except and a
bool is returned (True = an annotation was actually written this call).
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from . import cache, remote, remote_root


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRACK_NAME = "Agent Tasks"

#: Valid lifecycle tags. The CLI hook maps a command/transition onto one of these.
LIFECYCLES = ("create", "pickup", "update", "complete")

#: First-segment agent-kind normalization. The agent id is a colon-delimited
#: triple like ``claude-code:<host>:<repo>``; the first segment identifies the
#: agent family. ``claude-code`` is the everyday Claude Code id but the human
#: thinks of it as "claude"; ``codex`` is OpenAI's coding agent which the human
#: thinks of as "chatgpt". Anything unrecognized passes through lowercased so a
#: future agent family still gets a sensible tag without a code change.
_KIND_MAP = {
    "claude-code": "claude",
    "claude": "claude",
    "openclaw": "openclaw",
    "codex": "chatgpt",
    "chatgpt": "chatgpt",
}


# ---------------------------------------------------------------------------
# Tag / identity derivation
# ---------------------------------------------------------------------------

def agent_kind(agent: Optional[str]) -> str:
    """Map an agent id to its short kind tag (claude/openclaw/chatgpt/...).

    Keys off the FIRST colon-segment of the agent id. ``claude-code`` ->
    ``claude`` and ``codex`` -> ``chatgpt`` per the human's mental model;
    everything else is lowercased and passed through so an unknown family is
    still tagged usefully rather than dropped. Empty/None -> ``unknown`` (never
    raises, because this feeds a best-effort writer)."""
    if not agent:
        return "unknown"
    first = agent.split(":", 1)[0].strip().lower()
    if not first:
        return "unknown"
    return _KIND_MAP.get(first, first)


def session_tag(agent: Optional[str]) -> Optional[str]:
    """A short session/channel tag derived from the agent id.

    The agent id shape is ``<kind>:<host-or-session>:<repo-or-channel>``. We use
    the 2nd segment (host/session) when present, falling back to the 3rd
    (repo/channel) when the 2nd is blank. Returns None when there is nothing
    beyond the kind, so the caller simply omits the tag rather than emitting an
    empty one."""
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
    """Best-effort deep link to the task in the Fulcra library web app.

    ASSUMED URL SHAPE (documented, unconfirmed): the coordination tasks live in
    Fulcra Files under ``<remote_root>/tasks/<id>.json`` and the library web app
    is rooted at ``https://library.fulcradynamics.com``. We therefore link to
    the file path under the library file browser. If/when Fulcra exposes a
    canonical per-file or per-task permalink, swap this single function — callers
    only ever see the returned string in the annotation text.
    """
    task_id = task.get("id", "")
    path = f"{remote_root()}/tasks/{task_id}.json".lstrip("/")
    return f"https://library.fulcradynamics.com/files/{path}"


def build_annotation(
    *, lifecycle: str, task: dict[str, Any], agent: str
) -> dict[str, Any]:
    """Build the annotation payload (pure; no I/O).

    Shape::

        {
          "track": "Agent Tasks",
          "tags":     ["<lifecycle>", "<agent_kind>", "<session>"],
          "cli_tags": ["agent-tasks", "<lifecycle>", "agent:<kind>", "session:<sess>"],
          "name":  "<lifecycle>: <title> (<id>)",
          "desc":  "<next_action | current_summary | link>",
          "text":  "<lifecycle>: <title> (<id>) <link>",
          "link":  "https://library.fulcradynamics.com/...",
          "lifecycle": "<lifecycle>",
          "task_id": "<id>",
          "agent": "<full agent id>",
        }

    Two tag lists coexist on purpose:

      * ``tags`` is the legacy bare list (``[lifecycle, kind, session]``) kept for
        the API transport / existing readers.
      * ``cli_tags`` is the form the CLI ``create-data-type`` write uses:
        ``agent-tasks`` first as the shared TRACK tag (so every Agent-Tasks
        moment is filterable together), then the lifecycle, then PREFIXED
        ``agent:<kind>`` / ``session:<sess>`` so the timeline UI's flat tag space
        stays namespaced and unambiguous.

    ``name`` is the CLI annotation NAME — the concise, link-free
    ``<lifecycle>: <title> (<id>)`` (kept short; the deep link lives in ``desc``
    instead so the timeline label stays readable). ``desc`` prefers the task's
    ``next_action`` then ``current_summary`` for a one-line detail, falling back
    to the library link when neither is present.

    Kept separate from the writers so tests (and the API/CLI shaping steps)
    operate on a stable dict regardless of transport."""
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

    detail = (task.get("next_action") or "").strip() or \
        (task.get("current_summary") or "").strip()
    desc = detail or link

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
    }


#: The track tag for "the operator needs to do something" moments (situational
#: awareness piece 6). Shares the ``agent-tasks`` track with lifecycle moments so
#: they filter together, but carries its own ``needs-user`` tag so the human can
#: pull up exactly "what have my agents asked of me" on their Fulcra timeline.
NEEDS_USER_TAG = "needs-user"


def build_needs_user_annotation(
    *, task: dict[str, Any], agent: str
) -> dict[str, Any]:
    """Build the ``needs-user`` moment payload for a ``block --on-user`` (pure).

    Same shape as :func:`build_annotation` but tagged ``needs-user`` instead of a
    lifecycle, and the DESCRIPTION leads with the ask (``blocked_on``) so the
    timeline entry reads as "the thing the agent needs the human to do." The
    requesting agent's kind/session tags are carried so the human can see WHO
    asked. The ``needs-user`` tag occupies the lifecycle slot of ``tags`` for
    symmetry with the lifecycle payloads."""
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
    }


# ---------------------------------------------------------------------------
# Capability gate
# ---------------------------------------------------------------------------

def _mode() -> str:
    """Resolve the enable mode from ``FULCRA_COORD_ANNOTATIONS``.

    Returns one of ``off`` | ``cli`` | ``http``. Unset or unrecognized -> ``off``
    so the feature is inert by default and an operator must opt in explicitly.

    ``http`` is the proven path: it writes annotations directly over the Fulcra
    HTTP API exactly the way ``fulcra-collect`` does (tag resolve -> moment-def
    resolve/create -> JSONL record POST). ``api`` is kept as a back-compat ALIAS
    for ``http`` — the original deferred-stub flag value — so an operator who set
    ``=api`` before the HTTP writer landed gets the working path, not a no-op.
    ``cli`` still routes to the legacy ``create-data-type`` shell-out for any
    environment pinned to the annotations-capable CLI build."""
    raw = os.environ.get("FULCRA_COORD_ANNOTATIONS", "").strip().lower()
    if raw == "cli":
        return "cli"
    if raw in ("http", "api"):
        return "http"
    return "off"


# ---------------------------------------------------------------------------
# Idempotency marker (per remote-root cache)
# ---------------------------------------------------------------------------

def _transition_anchor(task: dict[str, Any]) -> str:
    """A stable key for "this specific lifecycle transition".

    A genuine transition appends a new event with a unique ``at`` timestamp; a
    write-RETRY re-uploads the identical task (identical events / ``at``). So
    keying the idempotency marker on the latest event means a retry of the same
    transition collides with the existing marker and is skipped, while a
    genuinely new transition gets a fresh anchor and emits again.

    The anchor includes the event COUNT and TYPE alongside ``at`` (M1): two
    distinct same-second transitions (e.g. an update then an immediate status
    change that both stamp the same ISO-second ``at``) would otherwise share an
    anchor and the second would be falsely deduped. ``len(events)`` differs
    between them (a transition appends an event) and ``type`` disambiguates
    further, so distinct transitions never collide — while a true retry, which
    re-uploads the identical task, reproduces the identical anchor and is
    correctly skipped."""
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
    return cache.has_annotation_marker(_marker_key(lifecycle, task))


def _record_annotated(lifecycle: str, task: dict[str, Any]) -> None:
    cache.write_annotation_marker(_marker_key(lifecycle, task))


# ---------------------------------------------------------------------------
# Transport writers
#   _write_cli  — legacy: create-data-type MomentAnnotation --add-to-timeline
#   _write_http — LIVE: direct Fulcra HTTP API via urllib (recommended path)
# ---------------------------------------------------------------------------

def _write_timeout() -> int:
    """Timeout (s) for the annotation create shell-out.

    Reuses the file-write timeout floor (>=15s) so a slow ``create-data-type``
    can't hang a task op indefinitely while still tolerating normal latency."""
    return remote._write_timeout()


def _annotation_cli_base() -> list[str]:
    """CLI base for the annotation write — separable from the file/coordination CLI.

    Annotations use ``create-data-type``, which today lives only on the fulcra-api
    ``create-annotations-commands`` branch — a build that does NOT carry the
    ``file`` command group the core coordination ops require (and the Files-capable
    build lacks ``create-data-type``). No single fulcra-api build has both yet, and
    file-ops + annotations otherwise resolve from the SAME base — so enabling
    annotations would break task I/O. Until both command sets land on fulcra-api
    ``main``, honour a dedicated ``FULCRA_COORD_ANNOTATION_CLI`` (whitespace-split)
    so an operator can point the annotation writer at the annotations build while
    ``FULCRA_CLI_COMMAND`` stays on the Files build. Falls back to the shared
    ``remote.cli_base_cmd()`` when unset (correct once one build has both)."""
    override = os.environ.get("FULCRA_COORD_ANNOTATION_CLI", "").strip()
    if override:
        return override.split()
    return remote.cli_base_cmd()


def _write_cli(payload: dict[str, Any], *, backend: Optional[list[str]] = None) -> bool:
    """Write the annotation by shelling out to the Fulcra CLI (CONFIRMED LIVE).

    Builds and runs::

        <cli-base> create-data-type MomentAnnotation "<NAME>" \
            --description "<desc>" --add-to-timeline \
            --tag agent-tasks --tag <lifecycle> --tag agent:<kind> --tag session:<sess>

    ``<cli-base>`` comes from :func:`remote.cli_base_cmd` — the SAME resolution
    file ops use (``FULCRA_CLI_COMMAND`` -> ``fulcra-api`` on PATH -> ``uv tool
    run fulcra-api``) — so we never hardcode the binary. ``--add-to-timeline``
    makes it a real moment occurrence; tags passed by name are auto-created.

    BEST-EFFORT: rc == 0 is success; any non-zero rc, missing CLI, timeout, or OS
    error returns False and is swallowed so it can NEVER raise into the caller's
    task op. (The public ``emit_lifecycle_annotation`` only records the
    idempotency marker on a True return, so a failure here leaves the transition
    free to be retried.)

    Implemented as a function (not inlined) so tests can monkeypatch
    ``subprocess.run`` and assert the exact invocation without a live backend.
    """
    base = backend if backend is not None else _annotation_cli_base()
    cmd = list(base) + [
        "create-data-type",
        "MomentAnnotation",
        payload["name"],
        "--description",
        payload["desc"],
        "--add-to-timeline",
    ]
    for tag in payload.get("cli_tags", []):
        cmd += ["--tag", tag]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_write_timeout(),
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


# ---------------------------------------------------------------------------
# HTTP transport (stdlib-only) — replicates the fulcra-collect / fulcra-common
# annotation write over urllib.request, so fulcra-coord needs NO httpx /
# fulcra-common dependency. Three Fulcra endpoints, in order:
#   1. resolve/create each tag        GET/POST  /user/v1alpha1/tag(/name/{n})
#   2. resolve/create the moment def  GET/POST  /user/v1alpha1/annotation
#   3. post the annotation record     POST      /ingest/v1/record/batch
# ---------------------------------------------------------------------------

#: Canonical name of the moment-annotation definition every Agent-Tasks moment
#: groups under. Matched by name in the user annotation catalog so all machines
#: converge on one definition instead of each creating a duplicate.
DEFINITION_NAME = "Agent Tasks"
DEFINITION_DESCRIPTION = (
    "Lifecycle moments for fulcra-coord agent coordination tasks "
    "(create / pickup / update / complete, plus needs-user asks)."
)


def _api_base() -> str:
    """Fulcra API base URL — env ``FULCRA_API_BASE`` else the prod host.

    Mirrors ``fulcra_common.client.DEFAULT_BASE_URL`` so fulcra-coord and the
    rest of fulcra-tools talk to the same API surface, with the same env knob
    for pointing tests / staging elsewhere. Trailing slash is stripped so path
    concatenation is unambiguous."""
    base = os.environ.get("FULCRA_API_BASE", "https://api.fulcradynamics.com")
    return base.rstrip("/")


def _resolve_token() -> Optional[str]:
    """Return a Fulcra bearer token, or None if one can't be obtained.

    Token source mirrors ``fulcra_common.client.BaseFulcraClient.get_token``:
    the ``FULCRA_ACCESS_TOKEN`` env var when set, else the stdout of
    ``fulcra auth print-access-token``. Best-effort: a missing CLI, a non-zero
    exit, a timeout, or an empty result all yield None rather than raising, so
    the caller cleanly no-ops instead of breaking the task op. The token is
    NEVER logged."""
    env = os.environ.get("FULCRA_ACCESS_TOKEN")
    if env and env.strip():
        return env.strip()
    try:
        result = subprocess.run(
            ["fulcra", "auth", "print-access-token"],
            capture_output=True,
            text=True,
            timeout=30,
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

    Raises ``urllib.error.HTTPError`` on a non-2xx response (so the caller can
    branch on, e.g., a 404 tag-not-found) and ``urllib.error.URLError`` on a
    transport failure. The Authorization header carries the bearer token; a
    JSON/JSONL body sets the matching content-type. A 30s timeout matches the
    httpx client's so a stuck API can't hang a task op."""
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "fulcra-coord")
    if body is not None:
        req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req, timeout=30) as resp:
        status = getattr(resp, "status", None) or resp.getcode()
        return status, resp.read()


def _resolve_tag_id(name: str, token: str) -> str:
    """Return the id of the tag named ``name``, creating it if absent.

    GET ``/user/v1alpha1/tag/name/{quoted}`` -> 200 ``{"id": ...}``; on a 404 the
    tag doesn't exist yet so POST ``/user/v1alpha1/tag`` ``{"name": name}`` to
    create it and return the new id. The name is percent-encoded (``safe=''``)
    so spaces / slashes in a tag name can't break the path — matching
    fulcra-common's ``_resolve_tag(quote_name=True)`` shape."""
    base = _api_base()
    quoted = urllib.parse.quote(name, safe="")
    try:
        status, raw = _request("GET", f"{base}/user/v1alpha1/tag/name/{quoted}", token)
        if status == 200:
            return json.loads(raw)["id"]
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    # Absent (404) -> create.
    payload = json.dumps({"name": name}).encode()
    _, raw = _request("POST", f"{base}/user/v1alpha1/tag", token, body=payload)
    return json.loads(raw)["id"]


def _definition_cache_path():
    """Path to the cached ``Agent Tasks`` definition-id json under the cache root.

    Scoped per remote root (alongside the annotation idempotency markers) so the
    id is resolved against the Fulcra API ONCE and reused on every subsequent
    annotation, rather than re-listing the catalog per write. Kept in the local
    cache (not the shared task JSON) because the def id is a per-account API
    handle, not coordination state."""
    return cache.annotations_dir() / "definition.json"


def _cached_definition_id() -> Optional[str]:
    path = _definition_cache_path()
    try:
        if path.exists():
            data = json.loads(path.read_text())
            did = data.get("id")
            if did:
                return did
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _store_definition_id(def_id: str) -> None:
    """Persist the resolved definition id. Best-effort: a cache-write failure
    just means the next call re-resolves, never a failed annotation."""
    try:
        cache.annotations_dir().mkdir(parents=True, exist_ok=True)
        _definition_cache_path().write_text(json.dumps({"id": def_id}))
    except OSError:
        pass


def _resolve_definition_id(token: str, tag_ids: list[str]) -> str:
    """Return the ``Agent Tasks`` moment-definition id, resolving once + caching.

    Cache hit -> return immediately (no HTTP). On a miss: GET
    ``/user/v1alpha1/annotation`` (the catalog of definitions), adopt the first
    live one named ``Agent Tasks`` if present; otherwise POST a new ``moment``
    definition (no measurement_spec — moments carry none) carrying the resolved
    tag ids. The resolved id is cached so this whole resolve/create dance runs
    at most once per machine per remote root."""
    cached = _cached_definition_id()
    if cached:
        return cached

    base = _api_base()
    status, raw = _request("GET", f"{base}/user/v1alpha1/annotation", token)
    for d in json.loads(raw) or []:
        if d.get("name") == DEFINITION_NAME and not d.get("deleted_at"):
            _store_definition_id(d["id"])
            return d["id"]

    body = json.dumps({
        "annotation_type": "moment",
        "name": DEFINITION_NAME,
        "description": DEFINITION_DESCRIPTION,
        "tags": tag_ids,
    }).encode()
    _, raw = _request("POST", f"{base}/user/v1alpha1/annotation", token, body=body)
    def_id = json.loads(raw)["id"]
    _store_definition_id(def_id)
    return def_id


def _recorded_at(payload: dict[str, Any]) -> str:
    """ISO-8601 Z timestamp for the annotation, from the payload anchor else now.

    Prefers a timestamp carried on the payload (``recorded_at``/``at``/``ts``) so
    the moment lands on the timeline at the transition time; falls back to the
    current UTC instant when none is present. Always normalized to a trailing
    ``Z``."""
    for key in ("recorded_at", "at", "ts", "timestamp"):
        val = payload.get(key)
        if val:
            return str(val).replace("+00:00", "Z")
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_http(payload: dict[str, Any], *, backend: Optional[list[str]] = None) -> bool:
    """Write the annotation via the Fulcra HTTP API (stdlib urllib only).

    Replicates the proven fulcra-collect path with NO httpx / fulcra-common
    dependency — three endpoints, in order:

      1. resolve/create each tag name (``payload['cli_tags']`` or ``['tags']``)
         to a tag id;
      2. resolve/create the shared ``Agent Tasks`` moment definition (cached
         locally so this happens once, not per annotation);
      3. POST a single JSONL record to ``/ingest/v1/record/batch`` with
         ``content-type: application/x-jsonl`` — metadata ``data_type:
         MomentAnnotation``, the resolved tag ids, and a ``source`` array
         carrying both a lifecycle-stamped fulcra-coord source id and the
         ``com.fulcradynamics.annotation.<def_id>`` definition source.

    BEST-EFFORT: a missing token, any urllib/HTTP error, or any other failure is
    caught and returns False — this MUST NEVER raise into the caller's task op.
    (``emit_*`` records the idempotency marker only on a True return, so a
    failure here leaves the transition free to retry.)

    The inner ``data`` dict is ``{"title": <name>, "note": <description>}`` with
    empty values omitted. ``backend`` is accepted for signature symmetry with the
    other writers; the HTTP path resolves its own base/token and ignores it."""
    try:
        token = _resolve_token()
        if not token:
            return False

        tag_names = payload.get("cli_tags") or payload.get("tags") or []
        tag_ids: list[str] = []
        for name in tag_names:
            if not name:
                continue
            tag_ids.append(_resolve_tag_id(name, token))

        def_id = _resolve_definition_id(token, tag_ids)

        inner: dict[str, Any] = {}
        title = (payload.get("name") or "").strip()
        note = (payload.get("desc") or payload.get("description") or "").strip()
        if title:
            inner["title"] = title
        if note:
            inner["note"] = note

        lifecycle = payload.get("lifecycle") or "event"
        source = [
            f"com.fulcradynamics.fulcra-coord.{lifecycle}.{uuid.uuid4()}",
            f"com.fulcradynamics.annotation.{def_id}",
        ]

        record = {
            "specversion": 1,
            "data": json.dumps(inner, sort_keys=True),
            "metadata": {
                "data_type": "MomentAnnotation",
                "recorded_at": _recorded_at(payload),
                "tags": tag_ids,
                "source": source,
                "content_type": "application/json",
            },
        }
        body = (json.dumps(record, sort_keys=True) + "\n").encode()
        _request(
            "POST",
            f"{_api_base()}/ingest/v1/record/batch",
            token,
            body=body,
            content_type="application/x-jsonl",
        )
        return True
    except Exception:
        # Best-effort contract: a timeline write must be invisible to the task op.
        return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def emit_lifecycle_annotation(
    *,
    lifecycle: str,
    task: dict[str, Any],
    agent: str,
    backend: Optional[list[str]] = None,
) -> bool:
    """Emit one Agent-Tasks lifecycle annotation. BEST-EFFORT, NEVER RAISES.

    Returns True only when an annotation was actually written on THIS call;
    False for every no-op path (feature off, deferred `api` transport, already
    annotated, or any swallowed error). The whole body is wrapped so a broken or
    slow annotation backend can never break — or even slow the failure of — the
    coordination task write that triggered it.

    Idempotency: guarded by a per-(task, lifecycle, transition-anchor) marker in
    the local cache, so a write-retry of the same transition does not double
    annotate, while a genuinely new transition does emit again.
    """
    try:
        mode = _mode()
        if mode == "off":
            return False

        if lifecycle not in LIFECYCLES:
            # Defensive: an unexpected lifecycle is a caller bug, not a reason to
            # raise into a task write. Drop it quietly.
            return False

        if _already_annotated(lifecycle, task):
            return False

        payload = build_annotation(lifecycle=lifecycle, task=task, agent=agent)

        # NOTE: the ``backend`` threaded in here is the FILE-OPS backend (e.g.
        # ``[... , "file"]`` or the test fake-backend emulator) — it speaks the
        # file protocol, NOT the CLI's top-level command surface, so it is the
        # wrong base for ``create-data-type``. We therefore do NOT forward it to
        # ``_write_cli``; that helper resolves the real CLI base itself via
        # ``remote.cli_base_cmd()`` (honouring ``FULCRA_CLI_COMMAND``). The
        # ``backend`` kwarg on the writers exists only for direct unit-test
        # injection.
        if mode == "cli":
            wrote = _write_cli(payload)
        elif mode == "http":
            wrote = _write_http(payload, backend=backend)
        else:  # pragma: no cover - _mode only returns off/cli/http
            wrote = False

        if wrote:
            _record_annotated(lifecycle, task)
        return bool(wrote)
    except Exception:
        # Best-effort contract: an annotation failure must be invisible to the
        # caller's task op. Swallow everything and report "did nothing".
        return False


def emit_needs_user_annotation(
    *,
    task: dict[str, Any],
    agent: str,
    backend: Optional[list[str]] = None,
) -> bool:
    """Emit one ``needs-user`` moment when a task is blocked on the human.

    Fired by ``block --on-user`` so "the agent needs me to do X" lands on the
    operator's Fulcra timeline (tagged ``needs-user`` + ``agent-tasks`` + the
    requesting agent). Same gating/transport/idempotency contract as
    :func:`emit_lifecycle_annotation`: honours ``FULCRA_COORD_ANNOTATIONS``
    (off by default -> no-op), routes through the same ``_write_cli``, dedupes on
    a per-(task, needs-user, transition-anchor) marker, and NEVER raises into the
    caller — a task op must not fail because a timeline write was slow/missing.
    Returns True only when a moment was actually written on THIS call."""
    try:
        mode = _mode()
        if mode == "off":
            return False
        if _already_annotated(NEEDS_USER_TAG, task):
            return False

        payload = build_needs_user_annotation(task=task, agent=agent)

        if mode == "cli":
            wrote = _write_cli(payload)
        elif mode == "http":
            wrote = _write_http(payload, backend=backend)
        else:  # pragma: no cover - _mode only returns off/cli/http
            wrote = False

        if wrote:
            _record_annotated(NEEDS_USER_TAG, task)
        return bool(wrote)
    except Exception:
        return False

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
  * "cli"  -> shell ``create-data-type MomentAnnotation ... --add-to-timeline``
    through the resolved Fulcra CLI (the same CLI base file ops use)
  * "api"  -> POST to the Fulcra annotations HTTP endpoint (still DEFERRED — no
    confirmed create endpoint in the core library; see ``_write_api``)

When the HTTP create path is confirmed, only ``_write_api`` changes; the public
contract, tag mapping, text/link building, gating, idempotency, and the CLI hook
points all stay put.

CONTRACT
--------
``emit_lifecycle_annotation(*, lifecycle, task, agent, backend=None)`` is
BEST-EFFORT and MUST NEVER raise into the caller: a coordination task write must
succeed (or fail) entirely on its own merits, never because an annotation
backend was slow, missing, or broken. Everything is wrapped in try/except and a
bool is returned (True = an annotation was actually written this call).
"""

from __future__ import annotations

import os
import subprocess
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


# ---------------------------------------------------------------------------
# Capability gate
# ---------------------------------------------------------------------------

def _mode() -> str:
    """Resolve the enable mode from ``FULCRA_COORD_ANNOTATIONS``.

    Returns one of ``off`` | ``cli`` | ``api``. Unset or unrecognized -> ``off``
    so the feature is inert by default and an operator must opt in explicitly."""
    raw = os.environ.get("FULCRA_COORD_ANNOTATIONS", "").strip().lower()
    if raw in ("cli", "api"):
        return raw
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
#   _write_cli — LIVE: create-data-type MomentAnnotation --add-to-timeline
#   _write_api — DEFERRED: no confirmed HTTP create endpoint yet
# ---------------------------------------------------------------------------

def _write_timeout() -> int:
    """Timeout (s) for the annotation create shell-out.

    Reuses the file-write timeout floor (>=15s) so a slow ``create-data-type``
    can't hang a task op indefinitely while still tolerating normal latency."""
    return remote._write_timeout()


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
    base = backend if backend is not None else remote.cli_base_cmd()
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


def _write_api(payload: dict[str, Any], *, backend: Optional[list[str]] = None) -> bool:
    """Write the annotation via the Fulcra annotations HTTP API.

    DEFERRED / UNCONFIRMED. The ``fulcra_api`` core library READS moment
    annotations from ``/data/v1alpha1/event/MomentAnnotation`` and lists defined
    annotations at ``/user/v1alpha1/annotation``, but exposes NO create/upload
    method, so the write endpoint is unconfirmed.

    ASSUMED endpoint + shape (documented, behind the flag):
        POST https://api.fulcradynamics.com/data/v1alpha1/event/MomentAnnotation
        Authorization: Bearer <fulcra access token>
        Content-Type: application/json
        {
          "annotation": "<UUID of the 'Agent Tasks' moment annotation type>",
          "time": "<ISO-8601>",
          "note": "<text>",
          "tags": ["create", "claude", "<session>"]
        }
    The 'Agent Tasks' track would first be resolved/created via the user
    annotation catalog. Until this is confirmed against fulcra-api source, this
    returns False so nothing is sent.
    """
    # TODO(annotations): confirm the create endpoint + body against fulcra-api
    # source, resolve/create the "Agent Tasks" MomentAnnotation type, then POST
    # through the same authenticated transport the file ops use.
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
        elif mode == "api":
            wrote = _write_api(payload, backend=backend)
        else:  # pragma: no cover - _mode only returns off/cli/api
            wrote = False

        if wrote:
            _record_annotated(lifecycle, task)
        return bool(wrote)
    except Exception:
        # Best-effort contract: an annotation failure must be invisible to the
        # caller's task op. Swallow everything and report "did nothing".
        return False

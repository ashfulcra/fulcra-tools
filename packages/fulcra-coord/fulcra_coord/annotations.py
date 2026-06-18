"""Fulcra "Agent Tasks" lifecycle annotations (feature #3).

WHAT THIS DOES
--------------
Every time an agent CREATES, PICKS UP, UPDATES, or COMPLETES a fulcra-coord
task, we want a single durable breadcrumb on the operator's Fulcra timeline — a
"moment annotation" on a track named **Agent Tasks** — so the human can see, on
their own Life timeline, *what the agents were doing and when*. One track, one
annotation per real lifecycle transition.

THE WRITE MECHANISM
-------------------
There is a SINGLE writer (``_write_http``) that does three things, in order:

  1. resolve/create each tag name to a tag id — via the public ``fulcra`` CLI
     (``fulcra tag get`` / ``fulcra tag create``), shelled out;
  2. resolve/create the shared "Agent Tasks" moment definition — also via the
     CLI (``fulcra catalog`` to find by name, ``fulcra data-type create`` to mint
     it), cached locally so this happens once, not per annotation;
  3. POST a single JSONL record to ``/ingest/v1/record/batch`` over the Python
     standard library's ``urllib`` — with metadata ``data_type:
     MomentAnnotation``, the resolved tag ids, and a ``source`` array carrying a
     lifecycle-stamped fulcra-coord source id plus the definition source.

The record lands as a real occurrence on the operator's Life timeline. The shared
track tag ``agent-tasks`` lets every Agent-Tasks moment be filtered together
regardless of lifecycle/agent.

WHY tags + defs go via the CLI, but records stay on urllib
----------------------------------------------------------
fulcra-coord is intentionally stdlib-only (see pyproject: "No OTHER non-stdlib
deps") — it is a coordination *bus* and must stay dependency-light. It does NOT
import a Fulcra client library; instead it SHELLS OUT to the public ``fulcra``
CLI for the operations the CLI supports. Tag and definition resolution/creation
have first-class CLI verbs (``tag``, ``data-type``, ``catalog``), so they go
through the CLI — no raw REST.

Records are the one exception, and only because they are *ingest-only*: the
Fulcra platform exposes no CLI or library record-WRITE verb today (the CLI's
``data-type`` group manages definitions, not record occurrences). So the single
record POST to ``/ingest/v1/record/batch`` is the ONE remaining raw-REST path in
this writer, done over stdlib ``urllib`` (no httpx / fulcra-common dependency).
That path migrates to the CLI as soon as the platform ships a first-class
record-write verb; until then it stays on ``urllib`` by necessity, not choice.

WHY IT IS CAPABILITY-GATED (and OFF by default)
-----------------------------------------------
The feature stays GATED behind ``FULCRA_COORD_ANNOTATIONS`` so it cannot perturb
task ops unless an operator opts in. The mode is simply on/off:

  * unset / "off" / anything unrecognized  -> NO-OP (the default; safe)
  * "on"  -> run the single ``urllib`` writer described above. (Legacy enable
    tokens ``http`` / ``api`` / ``cli`` from the old transport-duality era still
    normalize to "on" for back-compat — a machine with a persisted legacy value
    keeps emitting.)

The public contract, tag mapping, text/link building, gating, idempotency, and
the CLI hook points are independent of this gate.

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
from pathlib import Path
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


def _transition_at(task: dict[str, Any]) -> Optional[str]:
    """The timestamp of the transition this annotation records (BUG 12).

    Prefers the latest event's ``at`` (the moment the transition was logged),
    falling back to the task's ``updated_at``. Returned as-is for ``_recorded_at``
    to normalize; None when neither is present (so ``_recorded_at`` cleanly falls
    back to now() rather than anchoring to a falsy value)."""
    events = task.get("events") or []
    if events:
        at = events[-1].get("at")
        if at:
            return at
    return task.get("updated_at")


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
          "desc":  "[<workstream>/<kind>] <title> — <summary> · next: <action>",
          "text":  "<lifecycle>: <title> (<id>) <link>",
          "link":  "https://library.fulcradynamics.com/...",
          "lifecycle": "<lifecycle>",
          "task_id": "<id>",
          "agent": "<full agent id>",
        }

    Two tag lists coexist on purpose:

      * ``tags`` is the legacy bare list (``[lifecycle, kind, session]``) kept for
        existing readers.
      * ``cli_tags`` is the namespaced form the writer resolves/creates and
        attaches to the record (the historical field name predates the
        transport collapse and is retained to avoid a payload-shape change):
        ``agent-tasks`` first as the shared TRACK tag (so every Agent-Tasks
        moment is filterable together), then the lifecycle, then PREFIXED
        ``agent:<kind>`` / ``session:<sess>`` so the timeline UI's flat tag space
        stays namespaced and unambiguous.

    ``name`` is the annotation NAME — the concise, link-free
    ``<lifecycle>: <title> (<id>)`` (kept short; the deep link lives in ``desc``
    instead so the timeline label stays readable). ``desc`` carries the WORK
    SUBSTANCE (operator-digest spec §7): ``[<workstream>/<kind>] <title> —
    <current_summary> · next: <next_action>``, so a per-event moment conveys
    *what work* it was about, not just the lifecycle category. Every part is
    optional; a sparse task still yields a non-empty desc (prefix + title), and
    when nothing substantive is present it falls back to the library link.

    Kept separate from the writer so tests (and the record-shaping step) operate
    on a stable dict."""
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

    # COMPANION (operator-digest spec §7): carry the WORK SUBSTANCE in the note so
    # a per-event moment conveys *what work*, not just the lifecycle category.
    # Shape: "[<workstream>/<kind>] <title> — <summary> · next: <next_action>".
    # Backward-compatible: this only changes the human-readable note body (desc);
    # tags / name / link / payload shape are unchanged, so existing readers and
    # the idempotency/transport paths are untouched. Every part is optional —
    # a sparse task still yields a non-empty desc (at minimum the prefix + title).
    from .schema import _extract_kind_from_tags
    workstream = task.get("workstream", "") or ""
    kind = _extract_kind_from_tags(task.get("tags") or [])
    prefix = "/".join(p for p in (workstream, kind) if p)
    summary = (task.get("current_summary") or "").strip()
    nxt = (task.get("next_action") or "").strip()
    blurb_parts = []
    if prefix:
        blurb_parts.append(f"[{prefix}]")
    blurb_parts.append(title)
    tail = " · ".join(
        x for x in (summary, (f"next: {nxt}" if nxt else "")) if x)
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
        # BUG 12: anchor the moment at the TRANSITION time so _recorded_at lands
        # it on the timeline when it actually happened, not at emit-time now().
        # Without this the _recorded_at anchor branch was dead (the builders
        # never set any of its keys) and every moment stamped now().
        "recorded_at": _transition_at(task),
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
        # BUG 12: anchor at transition time (see build_annotation).
        "recorded_at": _transition_at(task),
    }


# ---------------------------------------------------------------------------
# Capability gate
# ---------------------------------------------------------------------------

def _annotations_config_path() -> "Path":
    """Path of the persisted annotation-mode file.

    Lives at ``${XDG_CONFIG_HOME:-~/.config}/fulcra-coord/annotations`` — the same
    config root the human handle and per-cwd identities use (via
    ``identity.config_root``), so all of fulcra-coord's persisted operator
    preferences sit together and tests isolate them with one ``XDG_CONFIG_HOME``.
    Imported lazily to avoid any import-order coupling with ``identity``."""
    from . import identity
    return identity.config_root() / "annotations"


def _normalize_mode(raw: str) -> Optional[str]:
    """Map a raw mode token to ``"on"``, or None if it does not enable the writer.

    There is ONE annotation writer (the stdlib-urllib HTTP path), so the mode is
    simply on/off. Any recognized enable value normalizes to ``"on"``:

      * ``on``  — the current enable token (what ``annotations on`` persists);
      * ``http`` / ``api`` / ``cli``  — legacy enable tokens from the old
        transport-duality era. They still mean "enabled" so a machine carrying a
        persisted legacy value (or an inherited legacy env export) keeps working
        after this collapse instead of silently going inert.

    Anything else — ``off``, empty, or an unrecognized token — returns None,
    preserving the "unknown value is off" contract (the writer stays inert unless
    an operator explicitly opts in). Normalization is applied HERE so it covers
    both the env var and the persisted config file uniformly."""
    raw = (raw or "").strip().lower()
    if raw in ("on", "http", "api", "cli"):
        return "on"
    return None


def _persisted_mode() -> Optional[str]:
    """Return the persisted annotation mode (``"on"``), or None when not enabled.

    The mode is enabled ONCE by the operator (``annotations on``) and stored as a
    single trimmed line in ``${XDG_CONFIG_HOME:-~/.config}/fulcra-coord/annotations``
    — exactly the human-handle persistence pattern in ``identity.py``. Persisting
    it (rather than requiring ``FULCRA_COORD_ANNOTATIONS`` exported in every
    shell) is what lets EVERY agent on the machine emit, so the operator's
    timeline actually fills. Tolerant of a missing/empty/unreadable/garbage file
    — a broken config must never wedge a task op, so we fall through to ``off``.
    A persisted legacy transport token (``http``/``api``/``cli``) normalizes to
    ``"on"`` here too (see ``_normalize_mode``), so an older config keeps working."""
    path = _annotations_config_path()
    if not path.exists():
        return None
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    return _normalize_mode(raw)


def set_persisted_mode(mode: str) -> "Path":
    """Persist ``mode`` (normalized to ``"on"``/``"off"``) so every agent emits.

    Mirrors ``identity.set_human``: creates the config dir and writes a single
    trimmed line. The value is normalized through ``_normalize_mode`` — any
    recognized enable token (``on`` or a legacy ``http``/``api``/``cli``) lands as
    ``"on"`` on disk; anything else lands as ``"off"``. Returns the file path."""
    normalized = _normalize_mode(mode) or "off"
    path = _annotations_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(normalized + "\n")
    return path


def clear_persisted_mode() -> bool:
    """Remove the persisted mode file. Returns True if a file was removed.

    After clearing, ``_mode`` resolves to ``off`` unless ``FULCRA_COORD_ANNOTATIONS``
    is set (env still wins). Mirrors ``identity.clear_human``."""
    path = _annotations_config_path()
    if path.exists():
        path.unlink()
        return True
    return False


def resolve_mode_source() -> tuple[str, str]:
    """Resolve the annotation mode AND report its source.

    Returns ``(mode, source)`` where ``mode`` ∈ ``off``|``on`` and
    ``source`` ∈ ``env``|``config``|``default``. Order: ``FULCRA_COORD_ANNOTATIONS``
    (when non-empty and recognized) > persisted config file > ``off``. Surfacing
    the source lets ``annotations status`` / ``doctor`` explain *why* it resolved
    the way it did (mirrors ``identity.resolve_human_source``). Env always wins so
    a single session can override the machine-wide persisted enablement."""
    env_raw = os.environ.get("FULCRA_COORD_ANNOTATIONS", "").strip()
    if env_raw:
        # A non-empty but unrecognized env value (e.g. "off"/"bogus") still
        # COUNTS as an explicit env decision -> off, source env. This preserves
        # the existing "unknown flag value is off" contract while letting a
        # session deliberately disable a machine that has it persisted on.
        return (_normalize_mode(env_raw) or "off", "env")
    persisted = _persisted_mode()
    if persisted:
        return (persisted, "config")
    return ("off", "default")


def _mode() -> str:
    """Resolve the enable mode. Returns one of ``off`` | ``on``.

    Resolution order: ``FULCRA_COORD_ANNOTATIONS`` env (when non-empty) > the
    persisted config file (``annotations on``) > ``off``. The persisted file is
    what lets the feature stay on across every shell/agent without a per-session
    export; env still wins so a session can override. Unset/unrecognized -> ``off``
    so the feature is inert by default and an operator must opt in explicitly.

    When ``on``, there is a SINGLE writer: the stdlib-``urllib`` HTTP path that
    writes annotations directly over the Fulcra API exactly the way
    ``fulcra-collect`` does (tag resolve -> moment-def resolve/create -> JSONL
    record POST). Legacy enable tokens (``http``/``api``/``cli``) all normalize to
    ``on`` for back-compat (see ``_normalize_mode``)."""
    return resolve_mode_source()[0]


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
    correctly skipped. Past the ``MAX_EVENTS_INLINE`` (20) cap ``len(events)`` is
    pinned at 20 and no longer varies, so disambiguation there rests on the
    microsecond ``at`` (+ ``type``), which never collides for sequential writes."""
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
# The single annotation writer (stdlib-only) — needs NO httpx / fulcra-common
# dependency. Tags AND the moment definition resolve through the public ``fulcra``
# CLI (shelled out via _fulcra_cli_json / _fulcra_cli_json_lines); only the RECORD
# write stays on urllib.request (the platform has no record-write CLI verb yet).
# Three steps, in order:
#   1. resolve/create each tag        fulcra tag get / tag create
#   2. resolve/create the moment def  fulcra catalog / data-type create
#   3. post the annotation record     POST  /ingest/v1/record/batch  (urllib)
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


def _annotation_cli_base() -> list[str]:
    """Resolved CLI base command (argv prefix) for annotation shell-outs.

    Thin wrapper over ``remote.cli_base_cmd()`` — the SAME base resolution the
    Files transport uses (honouring ``FULCRA_CLI_COMMAND`` -> ``fulcra-api`` ->
    ``uv tool run fulcra-api``). Kept as a named indirection here so the
    annotation writer (and its tests) have one patch point for the CLI base
    independent of the file transport, and so tag/definition resolution can
    never drift onto a hardcoded ``fulcra`` that a fulcra-api-only install
    lacks (BUG 11)."""
    return list(remote.cli_base_cmd())


def _write_timeout() -> int:
    """Subprocess timeout (seconds) for annotation CLI shell-outs.

    Delegates to ``remote._write_timeout()`` so annotation shell-outs share the
    same env-tunable write timeout as the Files transport instead of a constant.
    """
    return remote._write_timeout()


def _fulcra_cli_json(args: list[str], *, backend: Optional[list[str]] = None) -> Any:
    """Run ``<cli-base> <args>`` and return parsed stdout JSON, or None on ANY
    failure (rc!=0, timeout, missing CLI, non-JSON). Never raises — the
    annotation writer is best-effort. ``backend`` overrides the CLI base for
    tests (e.g. ``["false"]`` to force rc!=0). Uses the SAME base resolution as
    file ops (``_annotation_cli_base`` -> ``remote.cli_base_cmd``)."""
    base = backend if backend is not None else _annotation_cli_base()
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
    """Run ``<cli-base> <args>`` and parse stdout as JSONL — one JSON object PER
    LINE — returning the parsed list, or ``[]`` on ANY failure (rc!=0, timeout,
    missing CLI). Sibling of ``_fulcra_cli_json`` for commands like ``catalog``
    that emit one ``json.dumps(entry)`` per line rather than a single JSON value
    (``_fulcra_cli_json`` would fail to parse that multi-line output). Each
    non-empty line is parsed independently; an UNPARSEABLE line is SKIPPED (not
    fatal) so a stray log line can't drop the rest. Never raises — the annotation
    writer is best-effort. ``backend`` overrides the CLI base for tests; uses the
    SAME base resolution as file ops (``_annotation_cli_base``)."""
    base = backend if backend is not None else _annotation_cli_base()
    try:
        result = subprocess.run(
            list(base) + args, capture_output=True, text=True, timeout=_write_timeout(),
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    out: list = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _resolve_token() -> Optional[str]:
    """Return a Fulcra bearer token, or None if one can't be obtained.

    Token source mirrors ``fulcra_common.client.BaseFulcraClient.get_token``:
    the ``FULCRA_ACCESS_TOKEN`` env var when set, else the stdout of
    ``<cli-base> auth print-access-token``. BUG 11: the CLI base comes from
    ``remote.cli_base_cmd()`` (honouring ``FULCRA_CLI_COMMAND`` -> ``fulcra-api``
    -> ``uv tool run fulcra-api``), the SAME resolution every other CLI shell-out
    uses — NOT a hardcoded ``fulcra``, which doesn't exist on a fulcra-api-only
    install and silently killed annotations there. Best-effort: a missing CLI, a
    non-zero exit, a timeout, or an empty result all yield None rather than
    raising, so the caller cleanly no-ops instead of breaking the task op. The
    token is NEVER logged."""
    env = os.environ.get("FULCRA_ACCESS_TOKEN")
    if env and env.strip():
        return env.strip()
    try:
        result = subprocess.run(
            [*remote.cli_base_cmd(), "auth", "print-access-token"],
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


def _tag_cache_path():
    """Path to the per-name tag-id cache (``name -> id`` json map).

    Parallel to ``_definition_cache_path`` and scoped to the same cache dir, so
    tag ids — like the definition id — are resolved against the Fulcra API at
    most once per name per machine and reused on every subsequent emit, rather
    than re-resolved (GET, maybe POST) for every tag on every annotation."""
    return cache.annotations_dir() / "tags.json"


def _load_tag_cache() -> dict[str, str]:
    """Return the cached ``name -> id`` map, or {} on any read error OR expiry.

    The on-disk shape is ``{"written_at": ISO-Z, "tags": {name: id}}``. A whole
    cache older than the TTL (BUG 4) — or a pre-TTL legacy flat ``{name: id}``
    file with no ``written_at`` — reads as EMPTY so every tag re-resolves and the
    cache is re-stamped fresh, instead of returning ids the server may have
    deleted/renamed and silently failing all future annotations."""
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
    """Persist a resolved ``name -> id`` mapping with a write timestamp.

    Best-effort: a cache-write failure just means the next emit re-resolves the
    tag, never a failed annotation. Read-modify-write so concurrent names
    accumulate; the ``written_at`` stamp drives TTL expiry on read (BUG 4)."""
    try:
        cache.annotations_dir().mkdir(parents=True, exist_ok=True)
        # Build on top of any still-fresh entries (expired ones already dropped
        # by _load_tag_cache), then re-stamp the whole cache as freshly written.
        data = _load_tag_cache()
        data[name] = tag_id
        _tag_cache_path().write_text(
            json.dumps({"written_at": _cache_now_iso(), "tags": data}))
    except OSError:
        pass


def _resolve_tag_id(name: str, token: Optional[str] = None) -> str:
    """Return the id of the tag named ``name``, creating it if absent.

    Resolves via the ``fulcra tag`` CLI (through ``_fulcra_cli_json``), NOT raw
    urllib. ``token`` is accepted for caller back-compat (the writer still passes
    it positionally) but is UNUSED — the CLI carries its own auth.

    Flow:
      * Cache hit (BUG 6) -> return immediately, no CLI call.
      * ``fulcra tag get <name>`` emits ``json.dumps(tag_dict)`` with an ``id``
        (rc!=0 on a 404, so ``_fulcra_cli_json`` returns None). A dict with an
        ``id`` -> store + return it.
      * Otherwise ``fulcra tag create <name>`` MINTS the tag. Its stdout is a
        LIST ``[created_tag, ...]`` (a 409 "already exists" is skipped, so an
        existing name yields ``[]`` — which is why ``get`` is the resolver and
        ``create`` only mints new ones). Defensively also accept a bare dict.
        Extract the new id -> store + return it.
      * Total failure (both calls yield nothing usable) -> ``""``; the caller
        skips empty tag ids. NEVER raises and an empty id is NEVER cached.

    Unlike the old urllib path, the name is passed as a PLAIN argv argument — no
    percent-encoding — so colon-namespaced names (``agent:claude``,
    ``session:Mac``) go through verbatim."""
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
        # Best-effort: never raise, never cache an empty id (caller skips it).
        return ""
    _store_tag_id(name, tag_id)
    return tag_id


# BUG 4: the resolved definition-id / tag-id caches had NO expiry, so a
# server-side definition or tag deletion/rename left the cached id stale FOREVER
# — and since the id no longer resolves, every subsequent annotation silently
# failed with no way to self-heal short of manually clearing the cache. A TTL
# bounds that staleness window: an entry older than the TTL reads as a MISS so
# the resolver re-runs (and re-stamps a fresh id), while a fresh entry is still a
# zero-HTTP hit. 24h default keeps the common case HTTP-free while guaranteeing
# any drift heals within a day; overridable via env for tests / aggressive setups.
_DEFAULT_ANNOTATION_CACHE_TTL_SECONDS = 24 * 60 * 60


def _annotation_cache_ttl_seconds() -> float:
    """TTL (seconds) for the definition/tag id caches; env-overridable.

    ``FULCRA_COORD_ANNOTATION_CACHE_TTL_SECONDS`` lets tests force expiry and lets
    an operator tune the staleness window. A malformed value falls back to the
    24h default rather than disabling the cache or raising on a best-effort path."""
    raw = os.environ.get("FULCRA_COORD_ANNOTATION_CACHE_TTL_SECONDS")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return float(_DEFAULT_ANNOTATION_CACHE_TTL_SECONDS)


def _is_fresh(written_at: Any) -> bool:
    """True when a stored ``written_at`` ISO-Z stamp is within the current TTL.

    A missing/unparseable stamp (incl. a pre-TTL legacy cache file with no
    ``written_at``) is treated as STALE so it re-resolves once and gets
    re-stamped, rather than being trusted forever — the whole point of the TTL."""
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
    """Fixed-width-microsecond UTC stamp for cache-write timestamps (BUG 1)."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _definition_cache_path():
    """Path to the cached ``Agent Tasks`` definition-id json under the cache root.

    Scoped per remote root (alongside the annotation idempotency markers) so the
    id is resolved against the Fulcra API ONCE and reused on every subsequent
    annotation, rather than re-listing the catalog per write. Kept in the local
    cache (not the shared task JSON) because the def id is a per-account API
    handle, not coordination state."""
    return cache.annotations_dir() / "definition.json"


def _cached_definition_id() -> Optional[str]:
    """Return the cached definition id, or None on miss OR expiry (BUG 4).

    An entry older than the TTL (or one with no/garbled ``written_at`` — e.g. a
    pre-TTL legacy file) reads as a MISS so the resolver re-runs and heals a
    stale id, instead of returning an id the server may have deleted/renamed."""
    path = _definition_cache_path()
    try:
        if path.exists():
            data = json.loads(path.read_text())
            did = data.get("id")
            if did and _is_fresh(data.get("written_at")):
                return did
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _store_definition_id(def_id: str) -> None:
    """Persist the resolved definition id with a write timestamp. Best-effort: a
    cache-write failure just means the next call re-resolves, never a failed
    annotation. The ``written_at`` stamp drives TTL expiry on read (BUG 4)."""
    try:
        cache.annotations_dir().mkdir(parents=True, exist_ok=True)
        _definition_cache_path().write_text(
            json.dumps({"id": def_id, "written_at": _cache_now_iso()}))
    except OSError:
        pass


def _resolve_def_via_cli(def_name: str, description: str, tag_names: list[str]) -> str:
    """Resolve-or-create the moment definition named `def_name`, returning its
    UUID (or "" on total failure). Resolve by EXACT name via `fulcra catalog`
    (skipping substring-only and soft-deleted matches); else create it via
    `fulcra data-type create` with the lifecycle tag NAMES. Never raises.

    ``fulcra catalog --name`` is a SUBSTRING filter and emits JSONL (one entry
    per line), so we parse via ``_fulcra_cli_json_lines`` and keep only the entry
    whose ``name`` matches ``def_name`` EXACTLY, is a ``moment`` annotation, and
    is not soft-deleted — taking its ``metadata.id`` (the def UUID). On no live
    exact match we ``data-type create MomentAnnotation <def_name>
    --add-to-timeline`` with the tag NAMES (``--tag`` auto-creates them); its
    single-line stdout carries the new def's ``id``."""
    for e in _fulcra_cli_json_lines(["catalog", "--name", def_name]):
        if not isinstance(e, dict):
            continue
        meta = e.get("metadata") or {}
        if (e.get("name") == def_name and meta.get("annotation_type") == "moment"
                and not meta.get("deleted_at") and meta.get("id")):
            return meta["id"]
    cmd = ["data-type", "create", "MomentAnnotation", def_name,
           "--description", description, "--add-to-timeline"]
    for t in tag_names:
        if t:
            cmd += ["--tag", t]
    made = _fulcra_cli_json(cmd)
    if isinstance(made, dict) and made.get("id"):
        return made["id"]
    return ""


def _resolve_definition_id(tag_names: list[str], *, token: Optional[str] = None) -> str:
    """Return the ``Agent Tasks`` moment-definition UUID, resolving once + caching.

    Cache hit -> return immediately (no CLI). On a miss, resolve-or-create via
    ``_resolve_def_via_cli(DEFINITION_NAME, DEFINITION_DESCRIPTION, tag_names)``
    — ``fulcra catalog`` then ``fulcra data-type create`` — caching the non-empty
    UUID so the dance runs at most once per machine per remote root.

    Takes tag NAMES now (not ids), because ``data-type create --tag`` takes tag
    names (auto-created). ``token`` is accepted for caller back-compat but UNUSED
    — the CLI carries its own auth. Best-effort: a total failure returns ``""``
    (and is NOT cached) so the caller's source falls back gracefully."""
    cached = _cached_definition_id()
    if cached:
        return cached
    def_id = _resolve_def_via_cli(DEFINITION_NAME, DEFINITION_DESCRIPTION, tag_names)
    if def_id:
        _store_definition_id(def_id)
    return def_id


#: The digest track's own moment definition — distinct from DEFINITION_NAME
#: ("Agent Tasks") so the human-paced digest moments filter SEPARATELY from the
#: granular per-event lifecycle moments (the per-event track is kept, untouched).
DIGEST_DEFINITION_NAME = "Agent Tasks — Digest"
DIGEST_DEFINITION_DESCRIPTION = (
    "Twice-daily + on-demand operator situational-awareness digests "
    "(what's blocked on you, upcoming, what each agent did, what's stale)."
)
#: Track tag shared by every digest moment, so the operator can pull up exactly
#: their digests on the Fulcra timeline.
DIGEST_TRACK_TAG = "agent-digest"


def _digest_definition_cache_path():
    """Path to the cached ``Agent Tasks — Digest`` definition-id json.

    A SEPARATE file from ``_definition_cache_path`` (which caches the per-event
    "Agent Tasks" def): the two tracks are independent definitions, so caching
    both ids in one file would let one clobber the other. Same per-root cache
    dir so it's isolated per remote root like every other annotation handle."""
    return cache.annotations_dir() / "digest-definition.json"


def _cached_digest_definition_id() -> Optional[str]:
    """Return the cached digest definition id, or None on miss OR expiry.

    Mirrors _cached_definition_id but uses the digest-specific cache file so the
    "Agent Tasks" and "Agent Tasks — Digest" tracks cannot clobber each other
    while still sharing the same TTL self-heal behavior.
    """
    path = _digest_definition_cache_path()
    try:
        if path.exists():
            data = json.loads(path.read_text())
            did = data.get("id")
            if did and _is_fresh(data.get("written_at")):
                return did
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _store_digest_definition_id(def_id: str) -> None:
    """Persist the resolved digest definition id with a TTL stamp.

    Best-effort: a write failure just re-resolves next time, never a failed
    annotation.
    """
    try:
        cache.annotations_dir().mkdir(parents=True, exist_ok=True)
        _digest_definition_cache_path().write_text(
            json.dumps({"id": def_id, "written_at": _cache_now_iso()}))
    except OSError:
        pass


def _resolve_digest_definition_id(tag_names: list[str], *, token: Optional[str] = None) -> str:
    """Return the ``Agent Tasks — Digest`` moment-definition UUID (resolve once + cache).

    Same resolve/create dance as ``_resolve_definition_id`` but matched on
    ``DIGEST_DEFINITION_NAME`` and cached in the digest-specific file, so the two
    tracks converge on two distinct definitions across machines. Takes tag NAMES
    (not ids); ``token`` is accepted for back-compat but unused (the CLI carries
    its own auth). The em-dash in the name reaches the CLI verbatim as an argv
    string. Best-effort: a total failure returns ``""`` and is NOT cached."""
    cached = _cached_digest_definition_id()
    if cached:
        return cached
    def_id = _resolve_def_via_cli(
        DIGEST_DEFINITION_NAME, DIGEST_DEFINITION_DESCRIPTION, tag_names)
    if def_id:
        _store_digest_definition_id(def_id)
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
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


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
    empty values omitted. ``backend`` is accepted only for direct unit-test
    injection; the writer resolves its own API base/token and ignores it."""
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

        # The definition resolver takes tag NAMES (data-type create --tag takes
        # names); the record below still carries the resolved tag IDS.
        def_id = _resolve_definition_id(list(tag_names), token=token)
        if not def_id:
            return False

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
    False for every no-op path (feature off, already annotated, or any swallowed
    error). The whole body is wrapped so a broken or slow annotation backend can
    never break — or even slow the failure of — the coordination task write that
    triggered it.

    Idempotency: guarded by a per-(task, lifecycle, transition-anchor) marker in
    the local cache, so a write-retry of the same transition does not double
    annotate, while a genuinely new transition does emit again.
    """
    try:
        if _mode() == "off":
            return False

        if lifecycle not in LIFECYCLES:
            # Defensive: an unexpected lifecycle is a caller bug, not a reason to
            # raise into a task write. Drop it quietly.
            return False

        if _already_annotated(lifecycle, task):
            return False

        payload = build_annotation(lifecycle=lifecycle, task=task, agent=agent)

        wrote = _write_http(payload, backend=backend)

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
    (off by default -> no-op), routes through the same single ``_write_http``
    writer, dedupes on a per-(task, needs-user, transition-anchor) marker, and
    NEVER raises into the caller — a task op must not fail because a timeline
    write was slow/missing. Returns True only when a moment was actually written
    on THIS call."""
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
        return False


def emit_digest_annotation(*, name: str, note: str, window: str, agent: str,
                           backend: Optional[list[str]] = None) -> bool:
    """Emit ONE operator-digest moment on the ``Agent Tasks — Digest`` track.

    BEST-EFFORT, NEVER RAISES (same contract as emit_lifecycle_annotation): a
    slow/missing/broken timeline write must never break — or even slow — the
    scheduled digest tick. Returns True only when a moment was actually written.

    Reuses the proven HTTP path (tag resolve/create -> definition resolve/create
    -> JSONL record POST) but against ``_resolve_digest_definition_id`` so the
    digest lands on its OWN track, never the per-event "Agent Tasks" one. Tags:
    ``[agent-digest, <window>, agent:<kind>]``. Honours the same gating as the
    lifecycle writer (off unless FULCRA_COORD_ANNOTATIONS / persisted mode is on)
    so a machine that hasn't opted in stays inert. No idempotency marker here —
    the per-window DEDUP GUARD (cli, Task 5) is what prevents a double digest."""
    try:
        if _mode() == "off":
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
        # Definition resolver takes tag NAMES; the record still uses tag IDS.
        def_id = _resolve_digest_definition_id(
            [n for n in tag_names if n], token=token)
        if not def_id:
            return False

        inner: dict[str, Any] = {}
        if name.strip():
            inner["title"] = name.strip()
        if note.strip():
            inner["note"] = note.strip()
        source = [
            f"com.fulcradynamics.fulcra-coord.digest.{uuid.uuid4()}",
            f"com.fulcradynamics.annotation.{def_id}",
        ]
        record = {
            "specversion": 1,
            "data": json.dumps(inner, sort_keys=True),
            "metadata": {
                "data_type": "MomentAnnotation",
                "recorded_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                "tags": tag_ids,
                "source": source,
                "content_type": "application/json",
            },
        }
        body = (json.dumps(record, sort_keys=True) + "\n").encode()
        _request("POST", f"{_api_base()}/ingest/v1/record/batch", token,
                 body=body, content_type="application/x-jsonl")
        return True
    except Exception:
        # Best-effort: a timeline write must be invisible to the scheduled tick.
        return False

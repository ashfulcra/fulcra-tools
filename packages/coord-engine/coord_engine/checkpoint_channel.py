"""The checkpoint channel — a timeline moment for every continuity save.

WHY THIS EXISTS. A continuity checkpoint is the most consequential thing an
agent writes: it is the state the next session wakes on. Until now it landed as
a single file in the store and left no trace anywhere a human looks. A fleet
could be checkpointing perfectly and a person watching the Fulcra timeline
would see nothing — the same invisibility bug :mod:`coord_engine.bus_tags` was
built to end for events, one surface over. Visibility is the product, so every
successful checkpoint write now also emits ONE moment.

A SEPARATE CHANNEL, AND A SEPARATE CONFIG DOC. Checkpoints are not control-
plane events: nobody routes them, nobody acks them, they carry no ``to`` and no
priority. Mixing them into the events channel would put non-routable noise in
front of every queue reader. So they ride their own moment-annotation
definition, named by their own document:

    team/<team>/_coord/bus-v3/checkpoints.json

    {"schema": "coord.checkpoints-channel.v1",
     "data_type": "MomentAnnotation/a09350b2-e245-4348-ae63-bfb35c712c49",
     "api_version": "v1alpha1"}

It is a DIFFERENT FILE from ``records.json`` on purpose, not for tidiness. The
records config is the fleet's bus authority and older engines classify an
authority carrying fields they do not know as MALFORMED — which fails their
queue closed. Adding a checkpoint stream to that document would therefore take
the bus down for every host that had not upgraded yet. A new stream gets a new
document; the authority is never widened in place.

THE FAIL-OPEN RULE, and why it is the inverse of park's.

``continuity park`` is deliberately LOUD and non-zero when it cannot write a
checkpoint (``CHECKPOINT NOT WRITTEN``): a session runs park as it exits, so a
silent no-op discards the state the next session resumes from at exactly the
moment nobody is watching.

Emission here obeys the OPPOSITE rule, and the asymmetry is the point:

    the checkpoint file is the source of truth; the moment is its shadow.

Losing the shadow costs a row in a visualization. Failing the park because the
shadow could not be cast would cost the checkpoint itself — trading the
load-bearing act for its telemetry. So nothing in this module may raise, and no
outcome here may change a caller's exit code. Every failure is one line on
stderr and then out of the way.

THE CONFIG STATES.

- **absent** — this team has not adopted the checkpoint channel. Emit nothing,
  say nothing. Pre-adoption teams are the majority during a rollout and a
  warning on every park would train agents to ignore warnings.
- **ok** — emit, tagged by the SAME registry the bus uses, so a checkpoint
  moment filters by agent/platform/harness/model exactly like an event.
- **invalid** — bytes exist and do not parse. LOUD every time, no emission, and
  the file is NEVER auto-created: durable bytes a human wrote are evidence, and
  an engine that "repairs" them destroys the record of what went wrong.
- **error** — the store could not be consulted. UNKNOWN, not absent. One line,
  never memoized, so the next checkpoint re-reads. Unlike ``bus_tags`` this is
  not silent: reaching emission means the checkpoint file itself just wrote
  fine, so a config read that fails anyway is a real anomaly worth a line.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Optional

#: Team-relative path of the checkpoint-channel config. NOT ``records.json``:
#: see the module docstring — widening the bus authority breaks old engines.
CONFIG_NAME = "_coord/bus-v3/checkpoints.json"

#: The only schema string this engine understands. Anything else is INVALID,
#: not a shape to guess at.
SCHEMA = "coord.checkpoints-channel.v1"

DEFAULT_API_VERSION = "v1alpha1"

#: Note payload version. Independent of the control-plane payload version: this
#: is a different stream with a different shape and versions on its own clock.
NOTE_VERSION = 1

#: ``kind`` discriminator carried in every checkpoint note, so a reader sharing
#: the stream with anything else can tell what it is looking at.
NOTE_KIND = "checkpoint"

#: Objectives are free text an agent may write a paragraph into. A moment note
#: is a timeline label, not a document — the snapshot file already holds the
#: full text, and the note carries a pointer to it. Truncate to a label.
OBJECTIVE_MAX = 140

#: Per-process config memo: team -> (config|None, status). Populated only for
#: verdicts that cannot change without a human editing the store
#: ("ok"/"absent"/"invalid"), exactly like the tag registry; "error" is never
#: memoized because it is UNKNOWN and must be retried.
_CACHE: dict[str, tuple[Optional[dict[str, str]], str]] = {}


def config_path(team: str) -> str:
    return f"team/{team}/{CONFIG_NAME}"


def cache_clear() -> None:
    """Drop the per-process memo (provisioning writes; tests)."""
    _CACHE.clear()


def parse_config(raw: Any) -> Optional[dict[str, str]]:
    """``raw`` bytes -> ``{"data_type", "api_version"}``, or None if unusable.

    STRICT on the schema string. A document without it — including a
    ``records.json`` copied into place by mistake — is INVALID rather than
    something to interpret: emitting checkpoints into the events channel is a
    worse outcome than emitting none, because it puts unroutable records in
    front of every queue reader on the team.
    """
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    if doc.get("schema") != SCHEMA:
        return None
    data_type = doc.get("data_type")
    if not isinstance(data_type, str) or not data_type.strip():
        return None
    api_version = doc.get("api_version")
    if api_version is not None and (
            not isinstance(api_version, str) or not api_version.strip()):
        return None
    return {
        "data_type": data_type.strip(),
        "api_version": (api_version or DEFAULT_API_VERSION).strip(),
    }


def load_config(transport: Any, team: str, *, use_cache: bool = True
                ) -> tuple[Optional[dict[str, str]], str]:
    """``(config, status)`` with status in ok|absent|invalid|error.

    Mirrors ``records.load_config_classified`` and ``bus_tags.load_registry``:
    "absent" is claimed only on an affirmative not-found, and an unreadable
    store is "error" (UNKNOWN), never absent — a degraded transport must not be
    able to masquerade as a team that has not adopted the channel.
    """
    if use_cache and team in _CACHE:
        return _CACHE[team]
    path = config_path(team)
    reader = getattr(transport, "read_classified", None)
    if reader is None:
        try:
            raw = transport.read(path)
        except Exception:
            return None, "error"
        status = "absent" if raw is None else "ok"
    else:
        try:
            raw, status = reader(path)
        except Exception:
            return None, "error"
    if status == "error":
        return None, "error"  # UNKNOWN: never memoized, retry next checkpoint
    if raw is None:
        result: tuple[Optional[dict[str, str]], str] = (None, "absent")
    else:
        parsed = parse_config(raw)
        result = (parsed, "ok") if parsed is not None else (None, "invalid")
    _CACHE[team] = result
    return result


def truncate_objective(objective: Any) -> str:
    """The objective as a timeline label: first ``OBJECTIVE_MAX`` characters.

    Truncation is a hard slice with no ellipsis added, so the note's length is
    exactly bounded and a reader can compare the prefix against the snapshot
    file byte for byte.
    """
    return str(objective or "")[:OBJECTIVE_MAX]


def build_note(*, agent: str, task: str, objective: Any, path: str) -> str:
    """The compact-JSON note for one checkpoint moment.

    Key order is DECLARED, not sorted: this payload is read by humans scanning
    a timeline as often as by code, and ``v``/``kind`` first is what lets a
    reader classify a row without parsing it.
    """
    return json.dumps({
        "v": NOTE_VERSION,
        "kind": NOTE_KIND,
        "agent": agent,
        "task": task,
        "objective": truncate_objective(objective),
        "path": path,
    }, sort_keys=False, separators=(",", ":"))


def emit(transport: Any, team: Optional[str], *, agent: str, task: str,
         objective: Any, path: str, warn: bool = True) -> bool:
    """Emit ONE checkpoint moment. Returns whether it landed. NEVER raises.

    The return value is telemetry about telemetry — callers use it for tests
    and nothing else. No caller may branch its exit code on it: see the
    fail-open rule in the module docstring.
    """
    try:
        return _emit(transport, team, agent=agent, task=task,
                     objective=objective, path=path, warn=warn)
    except Exception as exc:  # defense in depth; _emit already catches
        if warn:
            print(f"checkpoint moment: emission failed ({exc!r}) — the "
                  f"checkpoint file was written and is unaffected",
                  file=sys.stderr)
        return False


def _emit(transport: Any, team: Optional[str], *, agent: str, task: str,
          objective: Any, path: str, warn: bool) -> bool:
    if not team:
        return False
    config, status = load_config(transport, team)
    if status == "absent":
        return False  # pre-adoption team: silent, by design
    if status == "invalid":
        if warn:
            print(f"checkpoint moment: {CONFIG_NAME} is INVALID (malformed "
                  f"bytes, or not schema {SCHEMA}) — no checkpoint moment was "
                  f"emitted; the checkpoint file itself was written. The "
                  f"engine will not recreate the config; a human fixes it.",
                  file=sys.stderr)
        return False
    if status != "ok" or not isinstance(config, dict):
        if warn:
            print(f"checkpoint moment: could not read {CONFIG_NAME} (store "
                  f"unreadable, not missing) — no moment emitted; the "
                  f"checkpoint file itself was written", file=sys.stderr)
        return False
    from . import bus_tags
    tags = bus_tags.tags_for_write(transport, team, agent)
    note = build_note(agent=agent, task=task, objective=objective, path=path)
    writer = getattr(transport, "record_write", None)
    if writer is None:
        if warn:
            print("checkpoint moment: this transport cannot write records — "
                  "no moment emitted; the checkpoint file itself was written",
                  file=sys.stderr)
        return False
    kwargs: dict[str, Any] = {}
    if tags:
        kwargs["tags"] = tags
    try:
        landed = bool(writer(config["data_type"], config["api_version"],
                             note, agent, **kwargs))
    except Exception as exc:
        landed = False
        if warn:
            print(f"checkpoint moment: record write raised ({exc!r}) — no "
                  f"moment emitted; the checkpoint file itself was written",
                  file=sys.stderr)
        return landed
    if not landed and warn:
        print(f"checkpoint moment: record write FAILED for {path} — the "
              f"checkpoint file was written and is unaffected (the moment is "
              f"telemetry, the checkpoint is the source of truth)",
              file=sys.stderr)
    return landed

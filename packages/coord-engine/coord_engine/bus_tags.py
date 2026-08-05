"""Per-identity tag registry for bus v3 event writes.

WHY THIS EXISTS. Coordination events are moment-annotation records on the
Fulcra timeline. A record carries no agent-visible label of its own: the sender
rides in ``sources`` (queryable, but not a facet the timeline UI groups by), so
in the visual explorer every agent's traffic collapses into one undifferentiated
stream. Tags are the facet the product surfaces.

FOUR DIMENSIONS, not one. "Who sent this" is only the first question a person
asks of a fleet; the others are "from what platform", "under which harness",
and "on what model". Each is a tag, so each is a timeline filter:

    agent:coord-boss   platform:claude-code   harness:ccr   model:opus-5

Every event carries whichever of those its sender has registered, plus the
channel's **base** tag — filtering on the base tag alone is the whole bus.

Records take tag **UUIDs**, never names (verified live 2026-08-04), and the
engine must not spend a round trip per write resolving them. So the mapping is
DURABLE ON THE BUS, one small document per team:

    team/<team>/_coord/bus-v3/tags.json

    {"schema": "coord.bus-tags.v2",
     "base": "cb951ecb-f21c-4aee-826e-2cb0b12517d6",
     "agents": {"coord-boss": {"agent": "0913d5df-…", "platform": "…",
                               "harness": "…", "model": "…"}}}

It is read once per process and cached: the registry changes at provisioning
time (a human act), never inside a run. That holds because every writer today
is a short-lived CLI invocation; provisioning in the SAME process already
invalidates. A future daemon or embedded writer would hold a stale registry for
its whole lifetime and must call :func:`cache_clear` on its own schedule.

MODEL IS DECLARED, NOT DETECTED. No engine can see which model is driving it.
``model`` is whatever the agent said at provisioning time, so a stale one is a
presence-integrity bug with a cheap fix: re-provision. A model switch is one
command, and it rewrites only the dimension it names.

THE STATES, and why each behaves as it does. Tag attachment is decoration on a
delivery mechanism. The durable file doc is the truth and the record is
delivery, so nothing here may ever COST a write:

- **absent** — this team has not adopted identity tagging. Write untagged, say
  nothing. Silence is correct: a team that never provisioned tags has not asked
  for them, and a warning on every send would train agents to ignore warnings.
- **ok, sender missing** — the team HAS adopted tagging and this agent was
  skipped. Attach the base tag only and warn on ONE line naming the fix. Never
  silent: a silently untagged fleet is exactly the invisible-timeline bug this
  module exists to end.
- **ok, sender partial** — some dimensions registered, some not. Attach what
  exists, SILENTLY. A partial entry is a deliberate state (not every agent has
  a meaningful harness), and warning about it on every write is noise that
  would bury the warning that matters.
- **invalid** — bytes exist and do not parse. LOUD, every time, and the file is
  NEVER auto-recreated: durable bytes a human wrote are evidence, and an engine
  that "repairs" them by overwriting destroys the only record of what went
  wrong. Write untagged and keep complaining until a person fixes it.

A transport failure ("error") is UNKNOWN, not absent — it is quiet like absent
(the store is down; nagging adds nothing actionable) but it is never cached, so
the next write re-reads.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any, Optional

from . import read_retry

#: Team-relative path of the tag registry.
TAGS_NAME = "_coord/bus-v3/tags.json"

#: The only schema string this engine understands. Anything else — including
#: the identity-only ``coord.bus-tags.v1`` that preceded it — is INVALID, not a
#: shape to guess at. A v1 registry is migrated by a human (add the dimension
#: objects); the engine will not rewrite it.
SCHEMA = "coord.bus-tags.v2"

#: Tag dimensions, in the order they are attached to a record. ``agent`` is the
#: identity and is REQUIRED in any registered entry — an entry that cannot say
#: who sent the event has no reason to exist. The rest are optional and may be
#: filled in later.
DIMENSIONS = ("agent", "platform", "harness", "model")
REQUIRED_DIMENSION = "agent"

_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

#: Per-process registry memo: team -> (registry|None, status). Populated only
#: for verdicts that cannot change without a human editing the store
#: ("ok"/"absent"/"invalid"); "error" is never memoized.
_CACHE: dict[str, tuple[Optional[dict[str, Any]], str]] = {}


def is_uuid(value: Any) -> bool:
    return isinstance(value, str) and _UUID.fullmatch(value.strip()) is not None


def tags_path(team: str) -> str:
    return f"team/{team}/{TAGS_NAME}"


def tag_name(dimension: str, value: str) -> str:
    """The account-level tag name for one dimension of one declaration.

    The ``<dimension>:<value>`` convention is what makes the timeline readable:
    a person filtering by ``model:`` sees every model in use without knowing
    any agent's name.
    """
    return f"{dimension}:{value}"


def cache_clear() -> None:
    """Drop the per-process memo (provisioning writes; tests)."""
    _CACHE.clear()


def _parse_entry(entry: Any) -> Optional[dict[str, str]]:
    """One agent's dimension map, or None if it cannot be trusted.

    STRICT: an unknown dimension key is a refusal, not something to ignore. The
    schema string exists precisely so a fifth dimension announces itself; an
    engine that silently skipped unknown keys would write half a taxonomy and
    call it success.
    """
    if not isinstance(entry, dict):
        return None
    out: dict[str, str] = {}
    for dim, tag_id in entry.items():
        if dim not in DIMENSIONS:
            return None
        if not is_uuid(tag_id):
            return None
        out[dim] = tag_id.strip()
    if REQUIRED_DIMENSION not in out:
        return None
    return out


def parse_registry(raw: Any) -> Optional[dict[str, Any]]:
    """``raw`` bytes -> ``{"base": uuid, "agents": {name: {dim: uuid}}}``.

    STRICT on purpose. A tag id that is not a UUID is refused rather than
    passed through, because the ingest endpoint validates record tags as UUIDs
    and would reject the whole write — a malformed registry entry must degrade
    tagging, never delivery. ``agents`` may be empty (a freshly seeded
    registry); ``base`` may not be missing, because the base tag is what makes
    an under-provisioned write still useful.
    """
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    if doc.get("schema") != SCHEMA:
        return None
    base = doc.get("base")
    if not is_uuid(base):
        return None
    agents = doc.get("agents")
    if agents is None:
        agents = {}
    if not isinstance(agents, dict):
        return None
    out: dict[str, dict[str, str]] = {}
    for name, entry in agents.items():
        if not isinstance(name, str) or not name.strip():
            return None
        parsed = _parse_entry(entry)
        if parsed is None:
            return None
        out[name.strip()] = parsed
    return {"base": base.strip(), "agents": out}


def load_registry(transport: Any, team: str, *,
                  use_cache: bool = True) -> tuple[Optional[dict[str, Any]], str]:
    """``(registry, status)`` with status in ok|absent|invalid|error.

    Mirrors ``records.load_config_classified``: "absent" is claimed only on an
    affirmative not-found, and an unreadable store is "error" (UNKNOWN), never
    absent — a degraded transport must not be able to masquerade as a team that
    declined tagging.
    """
    if use_cache and team in _CACHE:
        return _CACHE[team]
    path = tags_path(team)
    reader = getattr(transport, "read_classified", None)
    if reader is None:
        try:
            raw = transport.read(path)
        except Exception:
            return None, "error"
        status = "absent" if raw is None else "ok"
    else:
        try:
            raw, status = read_retry.read_classified_retrying(reader, path)
        except Exception:
            return None, "error"
    if status == "error":
        return None, "error"  # UNKNOWN: never memoized, retry next write
    if raw is None:
        result: tuple[Optional[dict[str, Any]], str] = (None, "absent")
    else:
        parsed = parse_registry(raw)
        result = (parsed, "ok") if parsed is not None else (None, "invalid")
    _CACHE[team] = result
    return result


def resolve(registry: Optional[dict[str, Any]], status: str,
            sender: str) -> tuple[list[str], Optional[str]]:
    """Tags for one event write -> ``(tag_uuids, warning_or_None)``.

    Order is ``[agent, platform, harness, model, base]`` — identity first,
    because that is the facet a human reads, and the channel tag last. A
    partial entry contributes the dimensions it has and warns about none of
    them. Returns ``([], None)`` for every state where tagging is not
    configured; the caller writes untagged and the event still delivers.
    """
    if status == "invalid":
        return [], (
            f"bus tags: {TAGS_NAME} is INVALID (malformed bytes, or a "
            f"pre-{SCHEMA} schema) — events write UNTAGGED until a human "
            "fixes it; the engine will not recreate it")
    if status != "ok" or not isinstance(registry, dict):
        return [], None  # absent / error: untagged, quietly
    base = registry.get("base")
    entry = registry.get("agents", {}).get(sender)
    if not entry:
        return [base], (
            f"bus tags: no identity tag for {sender!r} — event tagged with "
            f"the channel tag only; run `coord-engine bus-v3 tag-provision "
            f"<team> --agent {sender} --platform <p> --harness <h> "
            f"--model <m>`")
    return [entry[d] for d in DIMENSIONS if d in entry] + [base], None


def tags_for_write(transport: Any, team: Optional[str], sender: str, *,
                   warn: bool = True) -> list[str]:
    """The resolved tag list for an event write, warning on stderr as needed.

    Never raises and never returns anything but a (possibly empty) list of
    UUID strings: the write proceeds regardless of what the registry says.
    """
    if not team:
        return []
    try:
        registry, status = load_registry(transport, team)
        tags, warning = resolve(registry, status, sender)
    except Exception:
        return []
    if warning and warn:
        print(warning, file=sys.stderr)
    return [t for t in tags if is_uuid(t)]


def render_registry(base: str, agents: dict[str, dict[str, str]]) -> str:
    """Serialize a registry document (provisioning write path)."""
    ordered = {
        name: {d: agents[name][d] for d in DIMENSIONS if d in agents[name]}
        for name in sorted(agents)
    }
    return json.dumps({"schema": SCHEMA, "base": base, "agents": ordered},
                      indent=2, sort_keys=False) + "\n"

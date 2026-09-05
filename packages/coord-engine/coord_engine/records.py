"""Coord v3 — the typed-record control plane.

The bus began as files, and every read walks a directory tree that now holds
~1,500 task docs. Folds compensate with aggregate indexes, overlays and time
budgets, and they still lose: the fleet listener degrades on roughly nine ticks
in ten. All of that machinery exists because the file store has no index.

The typed-record API *is* an index — time-ordered, filterable by type,
answerable in one range query. This module carries coordination EVENTS on it.
Documents stay files; a record that needs a body points at one.

Measured 2026-07-27: a record is readable ~20s after write (one observation),
comfortably inside the router's 60s cadence, so records can serve as a trigger
and not merely as an index.

Payload contract
----------------
Only sanctioned annotation fields survive a write — we lost structured payload
fields silently before learning that — so the payload rides as compact JSON in
``note``, with the sender in ``sources`` where it is queryable:

    sources: ["coord-boss"]
    note:    {"v":1,"to":"codex-coder","kind":"directive","pri":"P0",
              "slug":"...","ptr":"task/....md"}

``ptr`` is present only when there is a body worth reading. Most events have no
body and cost no file read at all.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

from . import read_retry
from .budget import Deadline

#: Payload schema version. Bump only for incompatible shape changes; readers
#: must ignore payloads whose version they do not know rather than guess.
PAYLOAD_VERSION = 1

#: Bus protocol metadata stamped by engines that understand the convergence
#: contract.  These are deliberately separate from ``PAYLOAD_VERSION``: the
#: payload envelope can stay readable while cursor semantics evolve.
PROTOCOL_VERSION = 1
CURSOR_SCHEMA_VERSION = 2
READABLE_CURSOR_SCHEMAS = (1, 2)
WRITABLE_CURSOR_SCHEMAS = (1, 2)
CURSOR_SCHEMA_ENGINE_FLOORS = {2: "1.9.0"}

#: Event classes carried on the control plane. One data type carries all of
#: them, discriminated by ``kind``: ``get-records`` filters by type, so one type
#: plus a kind field costs one query where five types would cost five.
KINDS = ("directive", "response", "verdict", "claim", "blocked")

#: ``blocked`` is the newest class and the reason it could be added safely: the
#: read side returns None for an unrecognised ``kind`` (see ``parse_payload``),
#: so an engine older than this one SKIPS a blocked event as a non-control-plane
#: note rather than poisoning on it. Adding a kind is backward-compatible;
#: changing the meaning of an existing one would not be.
#:
#: Why it exists at all: ``blocked_on`` was a field seven modules READ and no
#: module ever announced. Every consumer therefore had to enumerate the task
#: corpus to discover what was blocked — the exact fold-by-enumeration the
#: stream architecture rejects — which is why only a bespoke tracker bridge
#: could ever see it, and only for one tracker. As an event it is available to
#: anything that reads the bus forward from a cursor.
BLOCKED_STATES = ("blocked", "cleared")

#: Prefix identifying records the engine's own projection wrote. Records from
#: any other source are data, not control-plane events.
PROJECTION_SOURCE_PREFIX = "com.fulcradynamics.fulcra-coord"


def engine_stamp() -> dict[str, Any]:
    """Version evidence attached to every engine-authored bus event."""
    from . import __version__
    return {
        "engine_version": __version__,
        "protocol_version": PROTOCOL_VERSION,
        "cursor_schema_version": CURSOR_SCHEMA_VERSION,
    }


def build_payload(*, to: str, kind: str, priority: str, slug: str,
                  ptr: Optional[str] = None, fyi: bool = False,
                  for_agent: Optional[str] = None,
                  on: Optional[str] = None, state: Optional[str] = None,
                  stamp: Optional[dict[str, Any]] = None) -> str:
    """Serialize a control-plane payload for the ``note`` field.

    Raises ``ValueError`` on an unknown ``kind`` — a mistyped event class must
    fail at the write, not decay into an event nobody routes.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")
    payload: dict[str, Any] = {
        "v": PAYLOAD_VERSION, "to": to, "kind": kind,
        "pri": priority, "slug": slug,
    }
    if ptr:
        payload["ptr"] = ptr
    if fyi:
        # A notification opens nothing (2026-08-11 ruling): the doc is born
        # closed, so the EVENT must say so too — a stream fold that cannot see
        # this flag replays every FYI as a permanent open obligation (measured
        # 2026-08-21: most of 92 stream-only "opens" were exactly this).
        payload["fyi"] = True
    if on:
        # WHAT it waits on, verbatim from the doc (`user:ash`, an agent name, a
        # role). Carried raw so a consumer can apply its own classifier rather
        # than inheriting ours.
        payload["on"] = on
    if state:
        if state not in BLOCKED_STATES:
            raise ValueError(f"unknown state {state!r}; expected one of {BLOCKED_STATES}")
        # A block that is never announced as CLEARED leaves every downstream
        # queue growing forever, and a queue that only grows stops being read.
        # The clear is half the signal.
        payload["state"] = state
    if for_agent:
        # A close names WHOM it discharges (2026-08-21 pilot round-trip): the
        # per-responder rule reads the sender, so a third-party close — an
        # owner superseding, a dispatcher abandoning — could never drop the
        # assignee's fold copy. ``for`` makes the discharge explicit; events
        # without it keep the sender fallback.
        payload["for"] = for_agent
    payload["writer"] = dict(stamp or engine_stamp())
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def parse_payload(note: Any) -> Optional[dict[str, Any]]:
    """Parse a ``note`` into a control-plane payload, or None if it isn't one.

    None means "not a control-plane event" — free-text notes on the same track
    are ordinary annotations and must be skipped silently, not treated as
    malformed. A payload carrying an unknown ``v`` also returns None: a reader
    that guesses at a shape it does not know is worse than one that waits.
    """
    if not isinstance(note, str) or not note.startswith("{"):
        return None
    try:
        obj = json.loads(note)
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("v") != PAYLOAD_VERSION:
        return None
    kind, to, slug = obj.get("kind"), obj.get("to"), obj.get("slug")
    if kind not in KINDS:
        return None
    if not isinstance(to, str) or not isinstance(slug, str) or not slug:
        return None
    fa = obj.get("for")
    on = obj.get("on")
    state = obj.get("state")
    return {
        "on": on if isinstance(on, str) and on else None,
        "state": state if state in BLOCKED_STATES else None,
        "to": to, "kind": kind, "slug": slug,
        "pri": obj.get("pri"), "ptr": obj.get("ptr"),
        "fyi": obj.get("fyi") is True,
        "for": fa if isinstance(fa, str) and fa else None,
        "writer": obj.get("writer") if isinstance(obj.get("writer"), dict)
        else None,
    }


def roundtrip_probe_payload(agent: str, nonce: str) -> str:
    """Build the delivery-proof payload consumed by ``doctor --delivery``.

    The synthetic recipient is owned by the caller, so proving the write/read
    path never consumes or pollutes another agent's queue. Claims are
    envelope-only and therefore owe no document pointer.
    """
    return build_payload(
        to=f"{agent}-probe",
        kind="claim",
        priority="P3",
        slug=f"delivery-probe-{nonce}",
    )


def sender_of(record: dict[str, Any]) -> Optional[str]:
    """The authoring agent from ``sources``, or None if unattributed.

    Attribution is self-declared today, exactly as it is in the file bus. That
    is fine inside one account and stops being fine at a share boundary, which
    is why platform-attested authorship is a MESH prerequisite rather than
    something this module pretends to solve.
    """
    for src in record.get("sources") or []:
        if isinstance(src, str) and src and not src.startswith("com.fulcradynamics."):
            return src
    return None


def immutable_record_identity(record: Any) -> Optional[str]:
    """Return a record's immutable identity only when its cursor shape proves it.

    Counts are deliberately not identities.  A detector may use a non-zero count
    solely to decide whether to perform one bounded record cursor read; only a
    concrete record id plus its orderable timestamp can enter the normalized
    change batch.
    """
    if not isinstance(record, dict):
        return None
    record_id, recorded_at = record.get("id"), record.get("recorded_at")
    if (not isinstance(record_id, str) or not record_id.strip()
            or not isinstance(recorded_at, str) or not recorded_at.strip()):
        return None
    return record_id.strip()


def observed_version_warnings(rows: Optional[list[Any]]) -> list[str]:
    """Describe mixed/unstamped engine traffic in an already-read window."""
    if rows is None:
        return []
    versions: set[tuple[Any, Any, Any]] = set()
    unstamped = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        payload = parse_payload(row.get("note"))
        if payload is None:
            continue
        stamp = payload.get("writer")
        if not isinstance(stamp, dict) or any(
                stamp.get(key) is None for key in (
                    "engine_version", "protocol_version",
                    "cursor_schema_version")):
            unstamped += 1
            continue
        versions.add((
            stamp.get("engine_version"),
            stamp.get("protocol_version"),
            stamp.get("cursor_schema_version"),
        ))
    warnings = []
    if unstamped:
        warnings.append(
            f"{unstamped} recognized event(s) lack writer-version stamps "
            "(legacy or unknown writer)")
    if len(versions) > 1:
        rendered = ", ".join(
            f"engine={v[0]}/protocol={v[1]}/cursor={v[2]}"
            for v in sorted(versions, key=str))
        warnings.append(f"mixed writer versions observed: {rendered}")
    return warnings


_LEGACY_NOTE_PREFIXES = (
    "create:", "update:", "done:", "block:", "REVIEW REQUEST", "assignee:",
)

# Cap the per-read census so a flooded window cannot bloat a wake: three named
# senders are enough to act on, the rest are counted in one tail line.
_CENSUS_SENDER_CAP = 3


def invisible_writer_census(window: Optional[list[Any]]) -> list[str]:
    """Name senders whose control-looking notes modern readers cannot parse.

    Ordinary free text remains silent. A missing/unreadable window is also
    silent here because the queue's window-level UNKNOWN path owns that
    diagnosis; absence of evidence must never become evidence of a clean
    writer fleet.

    At most ``_CENSUS_SENDER_CAP`` senders are named, lowest sender id first
    (deterministic), with a ``+ N more sender(s)`` tail when the window holds
    more — this runs on every queue read, so its worst case has to stay small.
    """
    if not window:
        return []
    offenders: dict[str, int] = {}
    for rec in window:
        if not isinstance(rec, dict):
            continue
        note = rec.get("note")
        if not isinstance(note, str) or parse_payload(note) is not None:
            continue
        if not any(prefix in note for prefix in _LEGACY_NOTE_PREFIXES):
            continue
        sender = sender_of(rec)
        if sender:
            offenders[sender] = offenders.get(sender, 0) + 1
    ranked = sorted(offenders.items())
    warnings = [
        f"{count} note(s) from {sender} look like control traffic but are "
        "not parseable (v1) — that agent's engine predates bus-v3; its "
        "messages are invisible to the fleet. It must run adopt-latest."
        for sender, count in ranked[:_CENSUS_SENDER_CAP]
    ]
    hidden = len(ranked) - _CENSUS_SENDER_CAP
    if hidden > 0:
        warnings.append(
            f"+ {hidden} more sender(s) writing unparseable control notes")
    return warnings


def fleet_version_census(presence_shards: list[Any],
                         record_rows: Optional[list[Any]]) -> dict[str, Any]:
    """Fold running-version evidence from presence and stamped claim events.

    Presence proves a process is running; a claim proves adoption intent.  The
    two are reported separately so "installed" is never mistaken for "active".
    Newer evidence wins per agent/source, and missing stamps remain visible.
    """
    evidence: dict[str, dict[str, Any]] = {}

    def add(agent: Any, at: Any, stamp: Any, source: str) -> None:
        if not isinstance(agent, str) or not agent:
            return
        row = evidence.setdefault(agent, {
            "agent": agent, "running": False, "adopted": False,
            "presence_at": None, "adoption_at": None,
            "running_engine_version": None,
            "running_protocol_version": None,
            "running_cursor_schema_version": None,
            "adopted_engine_version": None,
            "adopted_protocol_version": None,
            "adopted_cursor_schema_version": None,
        })
        at_value = at if isinstance(at, str) else None
        if source == "presence":
            row["running"] = True
            if str(at_value or "") >= str(row["presence_at"] or ""):
                row["presence_at"] = at_value
                if isinstance(stamp, dict):
                    row["running_engine_version"] = stamp.get("engine_version")
                    row["running_protocol_version"] = stamp.get("protocol_version")
                    row["running_cursor_schema_version"] = stamp.get(
                        "cursor_schema_version")
        else:
            row["adopted"] = True
            row["adoption_at"] = max(
                str(row["adoption_at"] or ""), str(at_value or "")) or None
            if isinstance(stamp, dict):
                row["adopted_engine_version"] = stamp.get("engine_version")
                row["adopted_protocol_version"] = stamp.get("protocol_version")
                row["adopted_cursor_schema_version"] = stamp.get(
                    "cursor_schema_version")

    for shard in presence_shards:
        if isinstance(shard, dict):
            add(shard.get("agent"), shard.get("timestamp"),
                shard.get("engine"), "presence")
    unknown_records = record_rows is None
    for row in record_rows or []:
        if not isinstance(row, dict):
            continue
        payload = parse_payload(row.get("note"))
        if payload is None or payload.get("kind") != "claim":
            continue
        sender = sender_of(row)
        stamp = payload.get("writer")
        if not isinstance(stamp, dict):
            match = _ADOPTION_SLUG.fullmatch(payload["slug"])
            # This is the exact bootstrap schema. The agent embedded in the
            # slug must agree with the record source, and rc0 alone is a
            # successful adoption. Other lookalikes remain UNKNOWN.
            if (match is not None and sender == match.group("agent")
                    and match.group("rc") == "0"):
                stamp = {"engine_version": match.group("version")}
        add(sender, row.get("recorded_at"), stamp, "adoption-claim")

    agents = sorted(evidence.values(), key=lambda item: item["agent"])
    versions = {
        (row["running_engine_version"], row["running_protocol_version"],
         row["running_cursor_schema_version"])
        for row in agents if row["running_engine_version"] is not None
    }
    unknown = [row["agent"] for row in agents
               if (not row["running"]
                   or row["running_engine_version"] is None
                   or row["running_protocol_version"] is None
                   or row["running_cursor_schema_version"] is None)]
    return {
        "agents": agents,
        "mixed": not agents or len(versions) > 1 or bool(unknown),
        "unknown_agents": unknown,
        "record_evidence_unknown": unknown_records,
    }


#: Recipient value that addresses every agent on the bus. Readers keep events
#: whose ``to`` is their own name OR this value; a reader that matches only its
#: literal name silently drops fleet-wide directives.
BROADCAST = "all"


def events_for(records: Optional[list], agent: str) -> Optional[list[dict[str, Any]]]:
    """Control-plane events addressed to ``agent`` (or broadcast), newest last.

    ``records is None`` propagates as None — an UNKNOWN window must never be
    presented as an empty one. That is the same fail-closed rule the file folds
    follow, and it matters more here: the caller advances a cursor on success.

    Duplicate records (same id) collapse to one event — the record API can
    return the same record more than once (observed live 2026-07-27).
    """
    if records is None:
        return None
    out: list[dict[str, Any]] = []
    seen_ids: set = set()
    for rec in records:
        if not isinstance(rec, dict):
            return None
        payload = parse_payload(rec.get("note"))
        if payload is None:
            continue  # ordinary annotation on the same track
        if payload["to"] not in (agent, BROADCAST):
            continue
        rec_id = rec.get("id")
        if rec_id is not None:
            if rec_id in seen_ids:
                continue
            seen_ids.add(rec_id)
        out.append({
            "slug": payload["slug"],
            "kind": payload["kind"],
            "priority": payload["pri"],
            "ptr": payload["ptr"],
            "to": payload["to"],
            # `parse_payload` preserves these and this projection used to drop
            # them, so every consumer reading the stream through here was blind
            # to the difference between a NOTIFICATION and an OBLIGATION. That
            # is the 2026-08-21 measurement — most of 92 stream-only "opens"
            # were FYIs replayed as permanent obligations — and the cause was
            # here, one layer below where it was diagnosed. `for` names whom a
            # close discharges; `on`/`state` carry the blocked signal. A fold
            # that cannot see them cannot be correct no matter how it is written.
            "fyi": payload.get("fyi") is True,
            "for": payload.get("for"),
            "on": payload.get("on"),
            "state": payload.get("state"),
            "from": sender_of(rec),
            "recorded_at": rec.get("recorded_at"),
            "record_id": rec.get("id"),
        })
    out.sort(key=lambda e: str(e.get("recorded_at") or ""))
    return out


def compare_to_file_fold(record_events: Optional[list[dict[str, Any]]],
                         file_slugs: set[str]) -> dict[str, Any]:
    """Shadow-comparison for the migration's read-path cutover.

    Returns ``{status, only_in_records, only_in_files}``. ``status`` is
    ``"unknown"`` when the record window was UNKNOWN — never ``"match"``, since
    an unknown window trivially "agrees" with anything and would green-light a
    cutover on no evidence. That mistake is the whole reason this function
    exists instead of a bare set comparison at the call site.
    """
    if record_events is None:
        return {"status": "unknown", "only_in_records": [], "only_in_files": []}
    rec_slugs = {e["slug"] for e in record_events}
    only_rec = sorted(rec_slugs - file_slugs)
    only_file = sorted(file_slugs - rec_slugs)
    return {
        "status": "match" if not only_rec and not only_file else "divergent",
        "only_in_records": only_rec,
        "only_in_files": only_file,
    }


# --- write side: config + emission (the timer leg, 2026-07-27) ---------------

#: Team-relative path of the records config: which annotation stream carries
#: this team's control plane. Kept ON THE BUS so every host resolves the same
#: stream — a host-local guess would silently fork the fleet across streams.
CONFIG_NAME = "_coord/bus-v3/records.json"

#: Env overrides (host- or test-level). When BOTH the type is set via env and
#: the store config exists, env wins — it is the operator's local override.
ENV_DATA_TYPE = "COORD_RECORDS_TYPE"
ENV_API_VERSION = "COORD_RECORDS_API_VERSION"

DEFAULT_API_VERSION = "v1alpha1"

_AUTHORITY_FIELDS = (
    "protocol_version", "cursor_schema_version",
    "minimum_reader_version", "minimum_writer_version",
    "cursor_generation", "cursor_activated_at",
)
#: The fleet's current engine pin. ADDITIVE and independent of the versioned
#: authority block: it is carried through verbatim (any type) so the currency
#: check can tell "absent" from "malformed", and its absence never makes an
#: otherwise-valid config partial.
CURRENT_ENGINE_FIELD = "current_engine_version"
SCHEMA1_MINIMUM_ENGINE_VERSION = "1.8.0"
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
_ADOPTION_SLUG = re.compile(
    r"^adopted-v(?P<version>\d+\.\d+\.\d+)-"
    r"(?P<agent>[a-zA-Z0-9._-]+)-rc(?P<rc>\d+)$")


def config_path(team: str) -> str:
    return f"team/{team}/{CONFIG_NAME}"


def schema1_authority_migration_target(
        raw: Any) -> tuple[Optional[dict[str, Any]], str]:
    """Classify one authority and build the narrow s5 schema-v1 target.

    Returns ``(target, readable-legacy|current)`` for the two safe states and
    ``(None, malformed-blocks|unsupported-blocks)`` otherwise.  The target is
    additive: transport fields and unknown sibling metadata are preserved,
    while the complete authority block is installed in one document write.

    This helper deliberately accepts raw store bytes instead of
    :func:`load_config`: an environment override may select a local transport
    stream, but it is not authority and must never be persisted by migration.
    """
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return None, "malformed-blocks"
    if not isinstance(doc, dict):
        return None, "malformed-blocks"
    parsed = _parse_config(raw)
    if parsed is None:
        return None, "malformed-blocks"
    if parsed.get("authority_mode") == "versioned":
        if (parsed.get("protocol_version") == 1
                and parsed.get("cursor_schema_version") == 1):
            return dict(doc), "current"
        return None, "unsupported-blocks"

    target = dict(doc)
    target.update({
        "protocol_version": 1,
        "cursor_schema_version": 1,
        "minimum_reader_version": SCHEMA1_MINIMUM_ENGINE_VERSION,
        "minimum_writer_version": SCHEMA1_MINIMUM_ENGINE_VERSION,
        "cursor_generation": 0,
        "cursor_activated_at": None,
    })
    # Keep target construction honest if the schema changes later.
    if _parse_config(json.dumps(target)) is None:
        return None, "malformed-blocks"
    return target, "readable-legacy"


def render_authority_config(doc: dict[str, Any]) -> str:
    """Stable store representation for a migrated authority document."""
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def _parse_config(raw: Any) -> Optional[dict[str, Any]]:
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    data_type = doc.get("data_type")
    if not isinstance(data_type, str) or not data_type.strip():
        return None
    api_version = doc.get("api_version")
    if api_version is not None and (
            not isinstance(api_version, str) or not api_version.strip()):
        return None
    out: dict[str, Any] = {
        "data_type": data_type.strip(),
        "api_version": (api_version or DEFAULT_API_VERSION).strip(),
    }
    if CURRENT_ENGINE_FIELD in doc:
        out[CURRENT_ENGINE_FIELD] = doc[CURRENT_ENGINE_FIELD]
    supplied = [name for name in _AUTHORITY_FIELDS if name in doc]
    if not supplied:
        return out
    # A partially upgraded authority is UNKNOWN, not a legacy default.
    if any(name not in doc for name in _AUTHORITY_FIELDS):
        return None
    if (type(doc["protocol_version"]) is not int
            or type(doc["cursor_schema_version"]) is not int
            or type(doc["cursor_generation"]) is not int
            or doc["protocol_version"] < 1
            or doc["cursor_schema_version"] < 1
            or doc["cursor_generation"] < 0):
        return None
    for name in ("minimum_reader_version", "minimum_writer_version"):
        if not isinstance(doc[name], str) or _SEMVER.fullmatch(doc[name]) is None:
            return None
    activated = doc["cursor_activated_at"]
    if activated is not None and (
            not isinstance(activated, str) or not activated.strip()):
        return None
    if doc["cursor_schema_version"] == 1 and (
            doc["cursor_generation"] != 0 or activated is not None):
        return None
    if doc["cursor_schema_version"] >= 2 and (
            doc["cursor_generation"] < 1 or activated is None):
        return None
    out.update({name: doc[name] for name in _AUTHORITY_FIELDS})
    out["authority_mode"] = "versioned"
    return out


def load_config(transport: Any, team: str) -> Optional[dict[str, Any]]:
    """Resolve the records stream for ``team`` → ``{data_type, api_version}``.

    Fail-closed: no env override and no readable, well-formed store config
    means None — callers then skip record emission rather than write into a
    guessed stream. A malformed store config is None, not a default: writing
    control-plane events to the wrong stream is worse than not writing them.
    """
    import os
    env_type = (os.environ.get(ENV_DATA_TYPE) or "").strip()
    raw = transport.read(config_path(team))
    if raw is None:
        if not env_type:
            return None
        return {
            "data_type": env_type,
            "api_version": (os.environ.get(ENV_API_VERSION) or "").strip()
            or DEFAULT_API_VERSION,
        }
    cfg = _parse_config(raw)
    if cfg is None:
        return None
    if env_type:
        cfg["data_type"] = env_type
        cfg["api_version"] = (
            (os.environ.get(ENV_API_VERSION) or "").strip()
            or cfg["api_version"])
    return cfg


def load_config_classified(
        transport: Any, team: str) -> tuple[Optional[dict[str, Any]], str]:
    """``load_config`` that separates absent from unreadable.

    Returns (config, "ok") | (None, "absent") | (None, "invalid") |
    (None, "error"). "absent" means the store affirmatively has no config;
    "invalid" means bytes exist but do not satisfy the atomic authority
    schema; "error" means the store could not be consulted, so the caller must
    treat the config as UNKNOWN (degraded), never as missing. Live incident
    2026-07-28: an expired-auth host reported "records configuration is still
    missing" for hours because the unreadable path was indistinguishable from
    the absent path.
    """
    import os
    env_type = (os.environ.get(ENV_DATA_TYPE) or "").strip()
    reader = getattr(transport, "read_classified", None)
    if reader is None:
        cfg = load_config(transport, team)
        return cfg, ("ok" if cfg is not None else "absent")
    raw, status = read_retry.read_classified_retrying(reader, config_path(team))
    if status == "error":
        return None, "error"
    if raw is None:
        if not env_type:
            return None, "absent"
        return {
            "data_type": env_type,
            "api_version": (os.environ.get(ENV_API_VERSION) or "").strip()
            or DEFAULT_API_VERSION,
        }, "ok"
    cfg = _parse_config(raw)
    if cfg is None:
        return None, "invalid"
    if env_type:
        cfg["data_type"] = env_type
        cfg["api_version"] = (
            (os.environ.get(ENV_API_VERSION) or "").strip()
            or cfg["api_version"])
    return cfg, "ok"


def load_canonical_config_classified(
        transport: Any, team: str, *, deadline: Deadline,
) -> tuple[Optional[dict[str, Any]], str]:
    """Read the stored queue authority within ``deadline``, without env overlays.

    Detection consumes fleet authority, whereas :func:`load_config_classified`
    intentionally supports host-local writer/test overrides.  Keeping this
    seam separate prevents a local ``COORD_RECORDS_TYPE`` from changing which
    live count the detector treats as the coordination queue.
    """
    if deadline.expired():
        return None, "error"
    reader = getattr(transport, "read_classified", None)
    if reader is None:
        return None, "error"

    def bounded_reader(path: str) -> tuple[Optional[str], str]:
        if deadline.expired():
            return None, "error"
        raw, status = reader(path, deadline=deadline)
        if deadline.expired():
            return None, "error"
        return raw, status

    try:
        raw, status = read_retry.read_classified_retrying(
            bounded_reader, config_path(team), deadline=deadline,
        )
    except Exception:
        return None, "error"
    if deadline.expired() or status == "error":
        return None, "error"
    if raw is None:
        return None, "absent"
    cfg = _parse_config(raw)
    if cfg is None:
        return None, "invalid"
    return cfg, "ok"


def _version_tuple(value: str) -> Optional[tuple[int, int, int]]:
    match = _SEMVER.fullmatch(value)
    return tuple(map(int, match.groups())) if match else None


def compatibility(config: dict[str, Any], *, engine_version: str,
                  write_cursor: bool = False) -> dict[str, Any]:
    """Decide whether this engine may read/write under the shared authority.

    Legacy configs remain readable for rollback, but are always loud.  A
    versioned authority is strict: unknown protocol/schema and an engine below
    either declared floor refuse before cursor state is written.
    """
    if config.get("authority_mode") != "versioned":
        return {"ok": True, "warnings": [
            "legacy bus-v3 authority has no fleet version fence; cursor v2 "
            "activation is forbidden"]}
    warnings: list[str] = []
    if config["protocol_version"] != PROTOCOL_VERSION:
        return {"ok": False, "warnings": [], "reason":
                f"unsupported bus protocol v{config['protocol_version']} "
                f"(engine supports v{PROTOCOL_VERSION})"}
    schema = config["cursor_schema_version"]
    supported = WRITABLE_CURSOR_SCHEMAS if write_cursor else READABLE_CURSOR_SCHEMAS
    if schema not in supported:
        action = "write" if write_cursor else "read"
        return {"ok": False, "warnings": [], "reason":
                f"cursor schema v{schema} is not safe to {action} "
                f"(supported: {','.join(map(str, supported))})"}
    own = _version_tuple(engine_version)
    floor_name = "minimum_writer_version" if write_cursor else "minimum_reader_version"
    floor = _version_tuple(config[floor_name])
    if own is None or floor is None:
        return {"ok": False, "warnings": [], "reason":
                f"unknown engine/floor version ({engine_version!r}, "
                f"{config[floor_name]!r})"}
    if own < floor:
        return {"ok": False, "warnings": [], "reason":
                f"coord-engine v{engine_version} is below {floor_name} "
                f"v{config[floor_name]}"}
    schema_floor_value = CURSOR_SCHEMA_ENGINE_FLOORS.get(schema)
    schema_floor = (_version_tuple(schema_floor_value)
                    if schema_floor_value is not None else None)
    if schema_floor is not None and own < schema_floor:
        return {"ok": False, "warnings": [], "reason":
                f"coord-engine v{engine_version} predates cursor schema "
                f"v{schema} support (requires v{schema_floor_value})"}
    if schema != CURSOR_SCHEMA_VERSION:
        warnings.append(
            f"mixed cursor semantics: authority v{schema}, this engine "
            f"stamps v{CURSOR_SCHEMA_VERSION}")
    return {"ok": True, "warnings": warnings}


def authority_currency(config: Optional[dict], *,
                       engine_version: str) -> Optional[str]:
    """One warning line when this engine is OLDER than the authority's
    current pin, else None. Rides the already-loaded config: zero transport.

    Why: a machine replacement restores a stale environment snapshot whose
    adopt-latest reinstalls an old engine (proven 2026-08-02, boot 30051b81);
    that engine then writes events modern readers skip. This makes the very
    first read loud instead. Absent field/config stays silent — an authority
    that has not adopted the field must not nag, and a DEV engine ahead of
    the pin is fine.
    """
    if not isinstance(config, dict):
        return None
    current = config.get(CURRENT_ENGINE_FIELD)
    own, cur = _version_tuple(engine_version), (
        _version_tuple(current) if isinstance(current, str) else None)
    if own is None or cur is None or own >= cur:
        return None
    # Says "minimum", never "pin": the word `pin` is already taken by the COMMIT
    # in `adopt-latest.sh`, and this is the semver FLOOR in `records.json`. An
    # operator told they are "below the pin" goes and reads the wrong file.
    return (f"this engine is v{engine_version}, below the fleet minimum engine "
            f"v{current} — a stale snapshot likely restored it; run the "
            f"store adopt-latest.sh before writing anything")


def authority_currency_state(config: Optional[dict], *,
                             engine_version: str) -> tuple[str, str]:
    """Tri-state currency verdict for ``doctor --self``: (state, detail).

    ``current`` is claimed ONLY when the pin exists, parses, and this engine
    is at or above it. Everything else that prevents the comparison is
    ``unknown`` and names WHY — an absent pin, a malformed one and an
    unreadable config all mean "could not compare", which is emphatically not
    "current" (reviewer correction 2026-08-03). ``stale`` carries the same
    actionable sentence the queue read prints.

    The queue-read path keeps silence on an absent field on purpose: that is
    the pre-activation compatibility phase, and it ends when the authority
    adopts the field. A deliberate health check has no such excuse.
    """
    if not isinstance(config, dict):
        return "unknown", (
            "the bus-v3 records config is absent or unreadable, so this "
            "engine cannot be compared with the fleet minimum")
    current = config.get(CURRENT_ENGINE_FIELD)
    if current is None:
        return "unknown", (
            f"the fleet authority declares no minimum engine "
            f"({CURRENT_ENGINE_FIELD}); comparison is impossible, which is not "
            "the same as current")
    if not isinstance(current, str) or _version_tuple(current) is None:
        return "unknown", (
            f"the fleet authority's minimum engine ({CURRENT_ENGINE_FIELD}) is "
            f"{current!r}, which is not a parseable version; comparison is "
            "impossible")
    if _version_tuple(engine_version) is None:
        return "unknown", (
            f"this engine reports version {engine_version!r}, which is not a "
            "parseable version; comparison is impossible")
    warning = authority_currency(config, engine_version=engine_version)
    if warning:
        return "stale", warning
    # "minimum", not "pin". `current_engine_version` is a FLOOR — the check is
    # at-or-above — while "the pin" means the COMMIT in adopt-latest.sh. The two
    # authorities wore the same word until 2026-08-09, and it cost a real
    # exchange: a pin was cut and adopted, this line still read v1.10.0, and the
    # obvious inference (the pin did not take) was wrong — adoption cannot move
    # this field at all. Naming the field in the sentence keeps the two apart.
    return "current", (
        f"this engine is v{engine_version}; the fleet minimum engine "
        f"({CURRENT_ENGINE_FIELD}) is v{current}")


def emit_event(transport: Any, config: dict[str, str], *, sender: str, to: str,
               kind: str, priority: str, slug: str, ptr: Optional[str] = None,
               fyi: bool = False, for_agent: Optional[str] = None,
               on: Optional[str] = None, state: Optional[str] = None,
               recorded_at: Optional[str] = None,
               team: Optional[str] = None) -> bool:
    """Emit one control-plane event; ``recorded_at`` in the future is a timer.

    ``build_payload`` raises on an unknown kind — a mistyped event class fails
    at the write. Returns the transport's verdict; False means the record did
    NOT land and the caller falls back to file-plane-only delivery (durable
    doc = truth, record = delivery).

    THE ONE TAGGING CHOKEPOINT. Every bus write in the engine funnels through
    here, so identity tags are attached here and nowhere else — no write verb
    has to opt in and none can be missed. ``team`` names the tag registry
    (:mod:`coord_engine.bus_tags`); omitting it writes untagged, which is what
    a caller that has no team context should do. Tag resolution can never fail
    the write: see the module docstring there for the absent/missing/invalid
    contract.
    """
    note = build_payload(to=to, kind=kind, priority=priority, slug=slug,
                         ptr=ptr, fyi=fyi, for_agent=for_agent, on=on, state=state)
    from . import bus_tags
    tags = bus_tags.tags_for_write(transport, team, sender)
    kwargs: dict[str, Any] = {"recorded_at": recorded_at}
    if tags:
        kwargs["tags"] = tags
    ok = bool(transport.record_write(
        config["data_type"], config["api_version"], note, sender, **kwargs))
    # Task 13 (plan 2026-09-04-coord-fold): mirror onto bus-v4 AFTER the v3 write, from this one chokepoint.
    # The mirror can never fail the v3 write (it does not raise) and is a no-op without a bus-v4 config.
    from . import dual_emit
    dual_emit.mirror(transport, team, sender=sender, to=to, kind=kind, priority=priority, slug=slug,
                     ptr=ptr, recorded_at=recorded_at)
    return ok


# --- read side: the durable cursor (the window rule, 2026-07-27) --------------

#: Team-relative per-agent cursor path. Durable ON THE BUS so a container roll
#: cannot reset coverage; the local disk is never the authority.
CURSOR_VERSION = 1
V2_CURSOR_VERSION = 2

#: First-run lookback when no cursor exists: long enough that a newly joining
#: (or long-dark) agent sweeps real history, bounded so it terminates.
DEFAULT_LOOKBACK_SECONDS = 7 * 24 * 3600

#: Windows overlap backwards by this skew so a record that indexed AFTER a
#: read closed (write->visibility lag, ~20s observed) is still covered by the
#: next read. Overlap is free: seen-id suppression collapses the repeats.
CURSOR_SKEW_SECONDS = 120

#: Bound on remembered record ids. Must comfortably exceed the events a fleet
#: writes within one skew overlap; 500 is ~two orders above today's rate.
SEEN_IDS_CAP = 500
COMMITTED_TOKENS_CAP = 100
DELIVERY_OUTCOMES = ("completed", "blocked", "superseded", "ignored")


def cursor_path(team: str, agent: str) -> str:
    return f"team/{team}/_coord/agents/{agent}/records-cursor.json"


def v2_cursor_path(team: str, agent: str, generation: int) -> str:
    """Physically isolated cursor namespace selected by the authority.

    A pre-convergence binary only knows :func:`cursor_path`; it therefore
    cannot overwrite this path.  Slice 2 owns the v2 document and CAS
    semantics.  Keeping path selection here makes the isolation testable
    before activation is allowed.
    """
    if not isinstance(generation, int) or generation < 1:
        raise ValueError("v2 cursor generation must be a positive integer")
    return (f"team/{team}/_coord/bus-v3/cursors/v2/"
            f"generation-{generation}/{agent}.json")


def v2_active(config: dict[str, Any]) -> bool:
    """True only for an explicitly activated, non-zero v2 generation."""
    return bool(
        config.get("authority_mode") == "versioned"
        and config.get("cursor_schema_version") == 2
        and isinstance(config.get("cursor_generation"), int)
        and config["cursor_generation"] > 0
        and isinstance(config.get("cursor_activated_at"), str)
        and config["cursor_activated_at"].strip()
    )


def v2_transport_ready(transport: Any) -> bool:
    """Whether this transport exposes a proven atomic cursor CAS."""
    return callable(getattr(transport, "compare_and_swap", None))


def _parse_legacy_cursor(raw: Any) -> Optional[dict[str, Any]]:
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("v") != CURSOR_VERSION:
        return None
    last = doc.get("last_read")
    if not isinstance(last, str) or not last.strip():
        return None
    seen = doc.get("seen_ids")
    if seen is not None and not isinstance(seen, list):
        return None
    return {"last_read": last.strip(),
            "seen_ids": [s for s in (seen or []) if isinstance(s, str)]}


def load_cursor(transport: Any, team: str, agent: str) -> Optional[dict[str, Any]]:
    """The agent's durable cursor, or None when absent/unreadable/malformed.

    None means "no trustworthy coverage claim exists" — the caller falls back
    to the default lookback. A malformed cursor must NOT be treated as a
    recent one: that would shrink coverage and silently skip work.
    """
    return _parse_legacy_cursor(transport.read(cursor_path(team, agent)))


def load_legacy_cursor_classified(
        transport: Any, team: str, agent: str
) -> tuple[Optional[dict[str, Any]], str]:
    """Classified legacy read for the one-time v2 migration seed.

    A transport without a classified read stays "error" here: seeding v2 off a
    coverage claim whose absence cannot be proven would be a guess.  The
    ordinary schema-v1 queue read uses :func:`load_cursor_classified`, whose
    plain-read fallback keeps old transports readable.
    """
    reader = getattr(transport, "read_classified", None)
    if reader is None:
        return None, "error"
    raw, status = reader(cursor_path(team, agent))
    if status == "error":
        return None, "error"
    if raw is None:
        return None, "absent"
    cursor = _parse_legacy_cursor(raw)
    return (cursor, "ok") if cursor is not None else (None, "invalid")


def load_cursor_classified(
        transport: Any, team: str, agent: str
) -> tuple[Optional[dict[str, Any]], str]:
    """``load_cursor`` with the four terminal read states kept apart.

    Returns (cursor, "ok") | (None, "absent") | (None, "invalid") |
    (None, "error").  "invalid" means bytes exist but do not parse as a
    cursor: a corrupt cursor is human-fixable evidence, so callers fail
    closed instead of adopting the default lookback and then OVERWRITING the
    corrupt document at cursor-save time (the auto-recreate that destroys the
    only copy of what went wrong).  On a transport without a classified read
    the absent/error ambiguity collapses to "absent" exactly as the plain
    reader always has — but malformed bytes still classify "invalid", because
    invalidity is a property of the content, not of the transport.
    """
    reader = getattr(transport, "read_classified", None)
    if reader is None:
        raw = transport.read(cursor_path(team, agent))
        if raw is None:
            return None, "absent"
        cursor = _parse_legacy_cursor(raw)
        return (cursor, "ok") if cursor is not None else (None, "invalid")
    return load_legacy_cursor_classified(transport, team, agent)


#: Team-relative prefix for durable takeover audit documents. Every
#: ``queue --consume`` that overrides the consumption guard writes one BEFORE
#: touching the target's cursor; an unauditable takeover does not happen.
CONSUME_AUDIT_PREFIX = "_coord/audit/consume"


def consume_audit_path(team: str, *, stamp: str, caller: str,
                       target: str) -> str:
    return f"team/{team}/{CONSUME_AUDIT_PREFIX}/{stamp}-{caller}-takes-{target}.md"


def save_cursor(transport: Any, team: str, agent: str, *, last_read: str,
                seen_ids: list[str]) -> bool:
    """Persist coverage. Returns False on failure — the caller WARNS and moves
    on: an unadvanced cursor re-covers the same window next read, and seen-id
    suppression makes the re-coverage free. Losing the write is latency;
    pretending it landed would be the real bug."""
    doc = {"v": CURSOR_VERSION, "last_read": last_read,
           "seen_ids": list(seen_ids)[-SEEN_IDS_CAP:]}
    return bool(transport.write(
        cursor_path(team, agent), json.dumps(doc, separators=(",", ":"))))


def _parse_v2_cursor(raw: Any, authority_generation: int) -> Optional[dict[str, Any]]:
    """Parse one transactional cursor without repairing or guessing.

    ``revision`` is the compare-and-swap generation for this agent cursor.  It
    is intentionally distinct from the authority generation in the path.
    """
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if (not isinstance(doc, dict)
            or doc.get("v") != V2_CURSOR_VERSION
            or doc.get("authority_generation") != authority_generation
            or type(doc.get("revision")) is not int
            or doc["revision"] < 0):
        return None
    committed = doc.get("committed")
    if not isinstance(committed, dict):
        return None
    last_read = committed.get("last_read")
    seen_ids = committed.get("seen_ids")
    if (last_read is not None and (
            not isinstance(last_read, str) or not last_read.strip())):
        return None
    if not isinstance(seen_ids, list) or any(
            not isinstance(value, str) for value in seen_ids):
        return None
    pending = doc.get("pending")
    if pending is not None:
        if not isinstance(pending, dict):
            return None
        required_strings = ("token", "staged_at", "window_start", "window_end")
        if any(not isinstance(pending.get(name), str)
               or not pending[name].strip() for name in required_strings):
            return None
        if (type(pending.get("base_revision")) is not int
                or pending["base_revision"] != doc["revision"]):
            return None
        events = pending.get("events")
        if not isinstance(events, list) or any(
                not isinstance(event, dict) for event in events):
            return None
    last_token = committed.get("last_token")
    if last_token is not None and not isinstance(last_token, str):
        return None
    committed_tokens = committed.get("committed_tokens")
    if not isinstance(committed_tokens, list) or any(
            not isinstance(value, str) for value in committed_tokens):
        return None
    handled = committed.get("handled")
    if not isinstance(handled, list) or any(
            not isinstance(row, dict)
            or not isinstance(row.get("record_id"), str)
            or row.get("outcome") not in DELIVERY_OUTCOMES
            or not isinstance(row.get("token"), str)
            for row in handled):
        return None
    return {
        "v": V2_CURSOR_VERSION,
        "authority_generation": authority_generation,
        "revision": doc["revision"],
        "committed": {
            "last_read": last_read.strip() if isinstance(last_read, str) else None,
            "seen_ids": list(seen_ids)[-SEEN_IDS_CAP:],
            "last_token": last_token,
            "committed_tokens": list(committed_tokens)[-COMMITTED_TOKENS_CAP:],
            "handled": list(handled)[-SEEN_IDS_CAP:],
        },
        "pending": pending,
    }


def initial_v2_cursor(authority_generation: int,
                      legacy: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Create the in-memory v2 bootstrap state.

    The one-time legacy seed preserves pre-activation coverage.  Once a v2
    document exists, the legacy path is never consulted again.
    """
    return {
        "v": V2_CURSOR_VERSION,
        "authority_generation": authority_generation,
        "revision": 0,
        "committed": {
            "last_read": legacy.get("last_read") if legacy else None,
            "seen_ids": list(legacy.get("seen_ids") or [])[-SEEN_IDS_CAP:]
            if legacy else [],
            "last_token": None,
            "committed_tokens": [],
            "handled": [],
        },
        "pending": None,
    }


def load_v2_cursor_classified(
        transport: Any, team: str, agent: str, authority_generation: int
) -> tuple[Optional[dict[str, Any]], Optional[str], str]:
    """Return ``(cursor, exact_raw, ok|absent|invalid|error)``.

    The exact bytes are the CAS precondition.  A transport without a classified
    read cannot distinguish absence from outage and therefore fails closed.
    """
    reader = getattr(transport, "read_classified", None)
    if reader is None:
        return None, None, "error"
    path = v2_cursor_path(team, agent, authority_generation)
    raw, status = reader(path)
    if status == "error":
        return None, None, "error"
    if raw is None:
        return None, None, "absent"
    cursor = _parse_v2_cursor(raw, authority_generation)
    if cursor is None:
        return None, raw, "invalid"
    return cursor, raw, "ok"


def _render_v2_cursor(doc: dict[str, Any]) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def _cas(transport: Any, path: str, expected_raw: Optional[str],
         new_doc: dict[str, Any]) -> Optional[bool]:
    """Perform a proven compare-and-swap.

    ``None`` means the transport has no atomic CAS primitive.  A read/write/
    read-back sequence is deliberately not accepted: on the File Store's
    last-writer-wins surface it cannot prove that a concurrent writer lost.
    """
    operation = getattr(transport, "compare_and_swap", None)
    if operation is None:
        return None
    return bool(operation(path, expected_raw, _render_v2_cursor(new_doc)))


def delivery_token(*, agent: str, authority_generation: int,
                   base_revision: int, staged_at: str, window_start: str,
                   window_end: str,
                   events: list[dict[str, Any]]) -> str:
    material = json.dumps({
        "agent": agent, "authority_generation": authority_generation,
        "base_revision": base_revision, "staged_at": staged_at,
        "window_start": window_start, "window_end": window_end,
        "event_ids": [event.get("record_id") for event in events],
    }, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(material).hexdigest()


def stage_v2_delivery(
        transport: Any, team: str, agent: str, authority_generation: int,
        *, cursor: dict[str, Any], expected_raw: Optional[str],
        staged_at: str, window_start: str, window_end: str,
        events: list[dict[str, Any]]
) -> dict[str, Any]:
    """CAS a pending delivery into the cursor.

    The cursor revision does not advance until commit.  A losing stage must
    reload and replay the winner; it may never overwrite it.
    """
    if cursor.get("pending") is not None:
        return {"status": "replay", "cursor": cursor}
    token = delivery_token(
        agent=agent, authority_generation=authority_generation,
        base_revision=cursor["revision"], staged_at=staged_at,
        window_start=window_start, window_end=window_end, events=events)
    next_doc = {
        **cursor,
        "pending": {
            "token": token,
            "base_revision": cursor["revision"],
            "staged_at": staged_at,
            "window_start": window_start,
            "window_end": window_end,
            "events": events,
        },
    }
    result = _cas(
        transport, v2_cursor_path(team, agent, authority_generation),
        expected_raw, next_doc)
    if result is None:
        return {"status": "unsupported"}
    if not result:
        return {"status": "lost"}
    return {"status": "staged", "cursor": next_doc}


def commit_v2_delivery(
        transport: Any, team: str, agent: str, authority_generation: int,
        *, token: str, classifications: dict[str, str]
) -> dict[str, Any]:
    """Advance coverage exactly once for the matching staged token."""
    cursor, raw, status = load_v2_cursor_classified(
        transport, team, agent, authority_generation)
    if status != "ok" or cursor is None:
        return {"status": status}
    committed = cursor["committed"]
    if token in committed["committed_tokens"]:
        return {"status": "idempotent", "cursor": cursor}
    pending = cursor.get("pending")
    if not isinstance(pending, dict) or pending.get("token") != token:
        return {"status": "stale", "cursor": cursor}
    event_ids = [event.get("record_id") for event in pending["events"]]
    if any(not isinstance(record_id, str) or not record_id
           for record_id in event_ids):
        return {"status": "invalid-events", "cursor": cursor}
    expected_ids = set(event_ids)
    if (set(classifications) != expected_ids
            or any(value not in DELIVERY_OUTCOMES
                   for value in classifications.values())):
        return {
            "status": "unclassified",
            "cursor": cursor,
            "missing": sorted(expected_ids - set(classifications)),
            "unexpected": sorted(set(classifications) - expected_ids),
        }
    seen = list(committed["seen_ids"])
    seen.extend(
        event["record_id"] for event in pending["events"]
        if isinstance(event.get("record_id"), str))
    next_doc = {
        **cursor,
        "revision": cursor["revision"] + 1,
        "committed": {
            "last_read": pending["window_end"],
            "seen_ids": seen[-SEEN_IDS_CAP:],
            "last_token": token,
            "committed_tokens": (
                committed["committed_tokens"] + [token]
            )[-COMMITTED_TOKENS_CAP:],
            "handled": (
                committed["handled"] + [{
                    "record_id": record_id,
                    "outcome": classifications[record_id],
                    "token": token,
                } for record_id in event_ids]
            )[-SEEN_IDS_CAP:],
        },
        "pending": None,
    }
    result = _cas(
        transport, v2_cursor_path(team, agent, authority_generation),
        raw, next_doc)
    if result is None:
        return {"status": "unsupported"}
    if not result:
        # A response lost after the CAS is indistinguishable from a CAS race.
        # Re-read: matching last_token proves idempotent success; every other
        # state is a stale loser.
        observed, _raw, observed_status = load_v2_cursor_classified(
            transport, team, agent, authority_generation)
        if (observed_status == "ok" and observed is not None
                and token in observed["committed"]["committed_tokens"]):
            return {"status": "idempotent", "cursor": observed}
        return {"status": "stale", "cursor": observed}
    return {"status": "committed", "cursor": next_doc}


# --- supersession-adoption metric (respec s7) --------------------------------
#
# Deputy-corrected definition (2026-07-30 provisional ruling, slug respec-s7):
# slug reuse is normally a THREAD, not a supersession — measured live, 11/11
# repeated sender+slug pairs in 24h were threads. Candidates are therefore
# directive→directive to the SAME recipient on the SAME slug only; an earlier
# directive already terminally classified completed/blocked is follow-up, not
# supersession; explicit `task supersede` evidence counts directly; anything
# the stream cannot distinguish is UNKNOWN, never silently in the denominator.
# The denominator is EXPECTED to be small.

def supersession_adoption(
    events: list[Any],
    outcomes: Optional[dict[str, str]],
    explicit_ids: Optional[set] = None,
) -> dict[str, Any]:
    """Fold the supersession-adoption metric over a window of bus events.

    ``events``: parsed v1 event dicts (need kind/to/slug/record_id, ordered or
    orderable by recorded_at). ``outcomes``: record_id → DELIVERY_OUTCOMES
    classification where durably known (v2 cursor ``handled`` rows);
    ``None`` means NO classification evidence exists for this window (legacy
    fleet) — the whole metric is then UNKNOWN, never 0% (absence of data is
    not evidence of non-adoption).

    ``explicit_ids``: EVENT record ids whose supersession went through the
    explicit ``task supersede --record`` verb — the task→record join added by
    the s7 verb-channel link (the pr-503 narrowing held only while no such
    mapping existed; :func:`explicit_supersessions` gathers it from task-doc
    frontmatter and ``cmd_doctor`` is the production caller). An explicit id
    counts its candidate directly (deputy rule 3) even when no cursor
    classified the predecessor. Precedence is unchanged: with ``outcomes``
    ``None`` the whole metric stays UNKNOWN — explicit ids refine within a
    measured window, they never conjure one.

    Returns ``{"status", "counted", "superseded", "unknown", "ratio"}``:
    ratio is ``None`` when nothing was countable — an empty denominator must
    read n/a, NEVER 100%.
    """
    if outcomes is None:
        return {"status": "unknown", "counted": 0, "superseded": 0,
                "unknown": 0, "ratio": None}
    explicit = explicit_ids or set()

    directives: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict) or event.get("kind") != "directive":
            continue
        rid, to, slug = (event.get("record_id"), event.get("to"),
                         event.get("slug"))
        if not (isinstance(rid, str) and isinstance(to, str)
                and isinstance(slug, str)):
            continue
        directives.append(
            {"record_id": rid, "to": to, "slug": slug,
             "at": event.get("recorded_at") or ""})
    directives.sort(key=lambda d: d["at"])

    counted = superseded = unknown = 0
    by_key: dict[tuple, dict[str, Any]] = {}
    for d in directives:
        key = (d["to"], d["slug"])
        earlier = by_key.get(key)
        if earlier is not None:
            outcome = outcomes.get(earlier["record_id"])
            if earlier["record_id"] in explicit or outcome == "superseded":
                counted += 1
                superseded += 1
            elif outcome in ("completed", "blocked"):
                pass  # terminally classified before re-issue: follow-up work
            elif outcome == "ignored":
                counted += 1  # implicit resolution of a re-issued directive
            else:
                unknown += 1  # unclassified/unmeasurable: NOT the denominator
        by_key[key] = d

    ratio = (superseded / counted) if counted else None
    return {"status": "ok", "counted": counted, "superseded": superseded,
            "unknown": unknown, "ratio": ratio}


def explicit_supersessions(frontmatters: list[Any]) -> set:
    """EVENT record ids evidenced by ``task supersede --record`` — the
    task→record join for :func:`supersession_adoption`'s explicit channel.

    Pure fold over parsed task-doc frontmatter dicts: keeps every non-empty
    string ``superseded_record_id``, dedupes, and tolerates malformed rows
    (a task doc that is not a dict, or whose field is not a usable string,
    contributes nothing — the fold's unknown bucket already covers
    supersessions without a usable join, so silence here is honest, not
    lossy)."""
    out: set = set()
    for fm in frontmatters:
        if not isinstance(fm, dict):
            continue
        rid = fm.get("superseded_record_id")
        if isinstance(rid, str) and rid.strip():
            out.add(rid.strip())
    return out


def outcome_mix(cursor: Optional[dict[str, Any]]) -> Optional[dict[str, int]]:
    """Per-agent classification mix from a v2 cursor's ``handled`` rows —
    the agent's own durable adoption signal (surfaced in ``queue --json``
    under the cursor block). ``None`` when there is no v2 evidence."""
    if not isinstance(cursor, dict):
        return None
    handled = (cursor.get("committed") or {}).get("handled")
    if not isinstance(handled, list) or not handled:
        return None
    mix = {outcome: 0 for outcome in DELIVERY_OUTCOMES}
    for row in handled:
        outcome = row.get("outcome") if isinstance(row, dict) else None
        if outcome in mix:
            mix[outcome] += 1
    return mix


def fleet_events(records: Optional[list]) -> Optional[list[dict[str, Any]]]:
    """All parsed v1 events regardless of recipient (fleet folds — the s7
    metric needs directive pairs across every recipient). Same None-propagation
    and id-dedupe rules as :func:`events_for`."""
    if records is None:
        return None
    out: list[dict[str, Any]] = []
    seen_ids: set = set()
    for rec in records:
        if not isinstance(rec, dict):
            return None
        payload = parse_payload(rec.get("note"))
        if payload is None:
            continue
        rec_id = rec.get("id")
        if rec_id is not None:
            if rec_id in seen_ids:
                continue
            seen_ids.add(rec_id)
        out.append({
            "slug": payload["slug"], "kind": payload["kind"],
            "priority": payload["pri"], "ptr": payload["ptr"],
            "to": payload["to"], "from": sender_of(rec),
            "recorded_at": rec.get("recorded_at"),
            "record_id": rec.get("id"),
        })
    out.sort(key=lambda e: str(e.get("recorded_at") or ""))
    return out

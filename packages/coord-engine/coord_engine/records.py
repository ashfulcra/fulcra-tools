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

import json
import re
from typing import Any, Optional

#: Payload schema version. Bump only for incompatible shape changes; readers
#: must ignore payloads whose version they do not know rather than guess.
PAYLOAD_VERSION = 1

#: Bus protocol metadata stamped by engines that understand the convergence
#: contract.  These are deliberately separate from ``PAYLOAD_VERSION``: the
#: payload envelope can stay readable while cursor semantics evolve.
PROTOCOL_VERSION = 1
CURSOR_SCHEMA_VERSION = 1
READABLE_CURSOR_SCHEMAS = (1,)
WRITABLE_CURSOR_SCHEMAS = (1,)

#: Event classes carried on the control plane. One data type carries all of
#: them, discriminated by ``kind``: ``get-records`` filters by type, so one type
#: plus a kind field costs one query where five types would cost five.
KINDS = ("directive", "response", "verdict", "claim")

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
                  ptr: Optional[str] = None,
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
    return {
        "to": to, "kind": kind, "slug": slug,
        "pri": obj.get("pri"), "ptr": obj.get("ptr"),
        "writer": obj.get("writer") if isinstance(obj.get("writer"), dict)
        else None,
    }


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
            "engine_version": None, "protocol_version": None,
            "cursor_schema_version": None,
        })
        at_value = at if isinstance(at, str) else None
        if source == "presence":
            row["running"] = True
            if str(at_value or "") >= str(row["presence_at"] or ""):
                row["presence_at"] = at_value
                if isinstance(stamp, dict):
                    for key in ("engine_version", "protocol_version",
                                "cursor_schema_version"):
                        row[key] = stamp.get(key)
        else:
            row["adopted"] = True
            row["adoption_at"] = max(
                str(row["adoption_at"] or ""), str(at_value or "")) or None
            # Adoption is useful version evidence only until presence proves
            # which binary is actually running.
            if not row["running"] and isinstance(stamp, dict):
                for key in ("engine_version", "protocol_version",
                            "cursor_schema_version"):
                    row[key] = stamp.get(key)

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
        add(sender_of(row), row.get("recorded_at"), payload.get("writer"),
            "adoption-claim")

    agents = sorted(evidence.values(), key=lambda item: item["agent"])
    versions = {
        (row["engine_version"], row["protocol_version"],
         row["cursor_schema_version"])
        for row in agents if row["engine_version"] is not None
    }
    unknown = [row["agent"] for row in agents
               if row["engine_version"] is None]
    return {
        "agents": agents,
        "mixed": len(versions) > 1 or bool(unknown),
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
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


def config_path(team: str) -> str:
    return f"team/{team}/{CONFIG_NAME}"


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
    if env_type:
        return {
            "data_type": env_type,
            "api_version": (os.environ.get(ENV_API_VERSION) or "").strip()
            or DEFAULT_API_VERSION,
        }
    raw = transport.read(config_path(team))
    if raw is None:
        return None
    return _parse_config(raw)


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
    if env_type:
        return {
            "data_type": env_type,
            "api_version": (os.environ.get(ENV_API_VERSION) or "").strip()
            or DEFAULT_API_VERSION,
        }, "ok"
    reader = getattr(transport, "read_classified", None)
    if reader is None:
        cfg = load_config(transport, team)
        return cfg, ("ok" if cfg is not None else "absent")
    raw, status = reader(config_path(team))
    if status == "error":
        return None, "error"
    if raw is None:
        return None, "absent"
    cfg = _parse_config(raw)
    return (cfg, "ok") if cfg is not None else (None, "invalid")


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
    if schema != CURSOR_SCHEMA_VERSION:
        warnings.append(
            f"mixed cursor semantics: authority v{schema}, this engine "
            f"stamps v{CURSOR_SCHEMA_VERSION}")
    return {"ok": True, "warnings": warnings}


def emit_event(transport: Any, config: dict[str, str], *, sender: str, to: str,
               kind: str, priority: str, slug: str, ptr: Optional[str] = None,
               recorded_at: Optional[str] = None) -> bool:
    """Emit one control-plane event; ``recorded_at`` in the future is a timer.

    ``build_payload`` raises on an unknown kind — a mistyped event class fails
    at the write. Returns the transport's verdict; False means the record did
    NOT land and the caller falls back to file-plane-only delivery (durable
    doc = truth, record = delivery).
    """
    note = build_payload(to=to, kind=kind, priority=priority, slug=slug, ptr=ptr)
    return bool(transport.record_write(
        config["data_type"], config["api_version"], note, sender,
        recorded_at=recorded_at))


# --- read side: the durable cursor (the window rule, 2026-07-27) --------------

#: Team-relative per-agent cursor path. Durable ON THE BUS so a container roll
#: cannot reset coverage; the local disk is never the authority.
CURSOR_VERSION = 1

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


def load_cursor(transport: Any, team: str, agent: str) -> Optional[dict[str, Any]]:
    """The agent's durable cursor, or None when absent/unreadable/malformed.

    None means "no trustworthy coverage claim exists" — the caller falls back
    to the default lookback. A malformed cursor must NOT be treated as a
    recent one: that would shrink coverage and silently skip work.
    """
    raw = transport.read(cursor_path(team, agent))
    if raw is None:
        return None
    try:
        doc = json.loads(raw)
    except ValueError:
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

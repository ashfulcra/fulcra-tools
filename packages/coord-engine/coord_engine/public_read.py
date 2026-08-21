"""Digest-verified, bounded public reads for projection generations.

The mutable ``current.json`` pointer is not freshness authority.  This module
validates its immutable target, proves one overlapping ``data-updates`` window,
applies deltas whose semantics are locally proven, and finally re-reads the
pointer.  Any doubt is typed and nonzero; callers never fall through to a
clean-looking projection after a skipped or partial overlay.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Any, Mapping, Optional

from . import generation, jsonutil, model, okf
from .budget import Deadline
from .change_detection import Change, ChangeDetector
from .outcome import CoverageState, OutcomeState, SurfaceCoverage


DEFAULT_READ_BUDGET_SECONDS = 30.0


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _instant(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        )
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class PublicReadResult:
    """One sealed authority decision shared by every public renderer."""

    state: OutcomeState
    coverage: tuple[SurfaceCoverage, ...]
    sections: Mapping[str, Mapping[str, Any]]
    coverage_horizon: Optional[str]
    generation: Optional[str]
    watermark: Optional[str]
    applied_update_ids: tuple[str, ...] = ()

    @property
    def rc(self) -> int:
        return 0 if self.state in (OutcomeState.CLEAR, OutcomeState.DATA) else 3

    def coverage_by_surface(self, name: str) -> SurfaceCoverage:
        return next(item for item in self.coverage if item.surface == name)

    def section(self, name: str) -> Mapping[str, Any]:
        return self.sections.get(name, {})

    def as_dict(self, *, result: Any = ... ) -> dict[str, Any]:
        value: dict[str, Any] = {
            "state": self.state.value,
            "coverage": [item.as_dict() for item in self.coverage],
            "coverage_horizon": self.coverage_horizon,
            "generation": self.generation,
            "watermark": self.watermark,
        }
        if result is not ...:
            value["result"] = result
        return value

    def render_json(self, *, result: Any = ...) -> str:
        return jsonutil.dumps(self.as_dict(result=result))

    def render_text_metadata(self) -> str:
        return (
            f"public-read: {self.state.value} generation={self.generation or '-'} "
            f"watermark={self.watermark or '-'} "
            f"coverage_horizon={self.coverage_horizon or '-'}"
        )


class SealedGenerationTransport:
    """Read inventory-backed public namespaces from one validated generation.

    Public folds predate generations and take a transport, so changing every
    helper into a second projection reader would create many subtly different
    authority paths.  This adapter preserves those domain folds while replacing
    their canonical inventory reads with the exact bytes already sealed in the
    shared public-read result.  Everything outside the four inventory-backed
    generation sections delegates to the wrapped transport.
    """

    def __init__(self, transport: Any, team: str, authority: PublicReadResult):
        self._transport = transport
        self._team = team
        self._prefix_sections = {
            f"team/{team}/roles/": "roles",
            f"team/{team}/presence/": "presence",
            f"team/{team}/_coord/acks/": "acknowledgments",
            f"team/{team}/_coord/responses/": "responses",
        }
        self._records: dict[str, str] = {}
        for prefix, section_name in self._prefix_sections.items():
            section = authority.section(section_name)
            records = section.get("records") if isinstance(section, Mapping) else None
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, Mapping):
                    continue
                path, content = record.get("path"), record.get("content")
                if (isinstance(path, str) and path.startswith(prefix)
                        and isinstance(content, str)):
                    self._records[path] = content

    def __getattr__(self, name: str) -> Any:
        return getattr(self._transport, name)

    def _sealed_prefix(self, path: str) -> Optional[str]:
        return next((prefix for prefix in self._prefix_sections
                     if path.startswith(prefix)), None)

    def read(self, path: str) -> Optional[str]:
        if self._sealed_prefix(path) is not None:
            return self._records.get(path)
        return self._transport.read(path)

    def read_classified(
        self, path: str, *, deadline: Optional[Deadline] = None,
    ) -> tuple[Optional[str], str]:
        if self._sealed_prefix(path) is not None:
            value = self._records.get(path)
            return (value, "ok") if value is not None else (None, "absent")
        classified = getattr(self._transport, "read_classified", None)
        if callable(classified):
            return classified(path, deadline=deadline)
        value = self._transport.read(path)
        return (value, "ok") if value is not None else (None, "absent")

    def list_dir(self, path: str) -> list[dict[str, Any]]:
        if self._sealed_prefix(path) is None:
            return self._transport.list_dir(path)
        children: dict[str, dict[str, Any]] = {}
        for record_path, content in self._records.items():
            if not record_path.startswith(path):
                continue
            remainder = record_path[len(path):]
            if not remainder:
                continue
            head, separator, _tail = remainder.partition("/")
            if separator:
                name = head + "/"
                children[name] = {
                    "name": name, "mtime": None, "size": None, "is_dir": True,
                }
            else:
                children[head] = {
                    "name": head, "mtime": None,
                    "size": f"{len(content)}B", "is_dir": False,
                }
        return [children[name] for name in sorted(children)]


def _coverage(
    manifest: CoverageState,
    immutable: CoverageState,
    overlay: CoverageState,
    *,
    manifest_reason: Optional[str] = None,
    immutable_reason: Optional[str] = None,
    overlay_reason: Optional[str] = None,
) -> tuple[SurfaceCoverage, ...]:
    return tuple(sorted((
        SurfaceCoverage("current-manifest", manifest, reason=manifest_reason),
        SurfaceCoverage("immutable-generation", immutable, reason=immutable_reason),
        SurfaceCoverage("freshness-overlay", overlay, reason=overlay_reason),
    ), key=lambda item: item.surface))


def _unknown(
    *, coverage: tuple[SurfaceCoverage, ...],
    sections: Optional[Mapping[str, Mapping[str, Any]]] = None,
    horizon: Optional[str] = None, generation_id: Optional[str] = None,
    watermark: Optional[str] = None,
) -> PublicReadResult:
    return PublicReadResult(
        OutcomeState.UNKNOWN, coverage, sections or {}, horizon, generation_id, watermark,
    )


def _section_value_reason(team: str, name: str, value: Any) -> str:
    """Positive schema validation before any domain fold receives bytes."""
    if not isinstance(value, Mapping):
        return f"{name} section value must be an object"
    if name == "tasks":
        if set(value) != {"rows"}:
            return "tasks section must contain exactly rows"
        rows = value.get("rows")
        if not isinstance(rows, list):
            return "tasks rows must be a list"
        for index, row in enumerate(rows):
            if (not isinstance(row, Mapping)
                    or not isinstance(row.get("name"), str) or not row.get("name")
                    or not isinstance(row.get("status"), str) or not row.get("status")):
                return f"tasks row {index} invalid"
        return ""
    if name == "reviews":
        if value.get("schema") != generation.REVIEW_PROJECTION_SCHEMA:
            return f"reviews projection schema unsupported: {value.get('schema')}"
        if value.get("complete") is not True or not isinstance(value.get("rows"), list):
            return "reviews projection incomplete or rows invalid"
        if generation.validated_review_projection(value) is None:
            return "reviews projection nested structure invalid"
        return ""
    if name == "forge":
        if value.get("schema") != generation.FORGE_PROJECTION_SCHEMA:
            return f"forge projection schema unsupported: {value.get('schema')}"
        if (value.get("complete") is not True
                or not isinstance(value.get("responsible"), Mapping)
                or not isinstance(value.get("feedback"), Mapping)):
            return "forge projection incomplete or invalid"
        return ""
    prefix = generation.inventory_prefix(team, name)
    if prefix is None:
        return f"{name} section unrecognized"
    if set(value) != {"records"}:
        return f"{name} inventory must contain exactly records"
    records = value.get("records")
    if not isinstance(records, list):
        return f"{name} inventory records must be a list"
    seen: set[str] = set()
    prior_path: Optional[str] = None
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            return f"{name} inventory record {index} must be an object"
        if set(record) != {"path", "content", "frontmatter"}:
            return f"{name} inventory record {index} fields invalid"
        path, content, frontmatter = (
            record.get("path"), record.get("content"), record.get("frontmatter"),
        )
        if not isinstance(path, str) or not path.startswith(prefix):
            return f"{name} inventory record {index} path outside namespace"
        if path in seen or (prior_path is not None and path <= prior_path):
            return f"{name} inventory record {index} path duplicate or unsorted"
        if not isinstance(content, str):
            return f"{name} inventory record {index} content must be a string"
        if not isinstance(frontmatter, Mapping):
            return f"{name} inventory record {index} frontmatter must be an object"
        parsed = okf.parse_frontmatter(content)
        if not isinstance(parsed, dict) or parsed != dict(frontmatter):
            return f"{name} inventory record {index} content/frontmatter mismatch"
        if not generation.canonical_inventory_document(name, path, frontmatter):
            return f"{name} inventory record {index} canonical document invalid"
        seen.add(path)
        prior_path = path
    return ""


def _read_generation(
    transport: Any, team: str,
) -> tuple[Optional[str], Optional[dict[str, Any]], Optional[dict[str, Any]], str]:
    """Return exact manifest bytes + validated docs, or one precise reason."""
    try:
        manifest_raw = transport.read(generation.current_path(team))
    except Exception:
        return None, None, None, "current manifest unreadable"
    if not isinstance(manifest_raw, str):
        return None, None, None, "current manifest absent or unreadable"
    try:
        manifest = json.loads(manifest_raw)
    except (TypeError, ValueError):
        return manifest_raw, None, None, "current manifest malformed"
    required = {
        "generation_id", "source_watermark", "schemas", "engine_version",
        "content_digest",
    }
    if (
        not isinstance(manifest, dict)
        or set(manifest) != required
        or not all(isinstance(manifest.get(key), str) and manifest.get(key)
                   for key in ("generation_id", "source_watermark", "engine_version",
                               "content_digest"))
        or _instant(manifest.get("source_watermark")) is None
        or not isinstance(manifest.get("schemas"), dict)
        or set(manifest["schemas"]) != set(generation.REQUIRED_SECTIONS)
        or any(not isinstance(value, str) or not value
               for value in manifest["schemas"].values())
    ):
        return manifest_raw, None, None, "current manifest schema invalid"
    if manifest["engine_version"] not in generation.SUPPORTED_ENGINE_VERSIONS:
        return manifest_raw, manifest, None, (
            f"unsupported engine version {manifest['engine_version']}"
        )
    for name in generation.REQUIRED_SECTIONS:
        schema = manifest["schemas"][name]
        if schema not in generation.SUPPORTED_SECTION_SCHEMAS[name]:
            return manifest_raw, manifest, None, (
                f"unsupported {name} section schema {schema}"
            )
    try:
        raw = transport.read(generation.generation_path(team, manifest["generation_id"]))
    except Exception:
        return manifest_raw, manifest, None, "immutable generation unreadable"
    if not isinstance(raw, str):
        return manifest_raw, manifest, None, "immutable generation unreadable"
    if sha256(raw.encode("utf-8")).hexdigest() != manifest["content_digest"]:
        return manifest_raw, manifest, None, "immutable generation digest mismatch"
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return manifest_raw, manifest, None, "immutable generation malformed"
    sections = doc.get("sections") if isinstance(doc, dict) else None
    required_generation_fields = {
        "schema", "id", "prior_generation_id", "source_watermark",
        "normalized_update_digest", "engine_version", "sections",
    }
    if (
        not isinstance(doc, dict)
        or set(doc) != required_generation_fields
        or doc.get("schema") != generation.GENERATION_SCHEMA
        or doc.get("id") != manifest["generation_id"]
        or doc.get("source_watermark") != manifest["source_watermark"]
        or doc.get("engine_version") != manifest["engine_version"]
        or not isinstance(doc.get("normalized_update_digest"), str)
        or not doc.get("normalized_update_digest")
        or not (
            doc.get("prior_generation_id") is None
            or isinstance(doc.get("prior_generation_id"), str)
        )
        or not isinstance(sections, dict)
        or set(sections) != set(generation.REQUIRED_SECTIONS)
    ):
        return manifest_raw, manifest, None, "immutable generation schema invalid"
    identity = {
        "prior_generation_id": doc["prior_generation_id"],
        "source_watermark": doc["source_watermark"],
        "normalized_update_digest": doc["normalized_update_digest"],
        "schema_version": doc["schema"],
        "engine_version": doc["engine_version"],
    }
    expected_id = sha256(json.dumps(
        identity, separators=(",", ":"), sort_keys=True, ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    if expected_id != doc["id"]:
        return manifest_raw, manifest, None, "immutable generation identity mismatch"
    for name in generation.REQUIRED_SECTIONS:
        section = sections.get(name)
        if (
            not isinstance(section, dict)
            or set(section) != {"schema", "state", "value"}
            or section.get("schema") != manifest["schemas"].get(name)
            or section.get("state") not in generation.COMPLETE_STATES
            or not isinstance(section.get("value"), dict)
        ):
            return manifest_raw, manifest, None, (
                f"immutable generation section {name} invalid"
            )
        value_reason = _section_value_reason(team, name, section["value"])
        if value_reason:
            return manifest_raw, manifest, None, value_reason
    return manifest_raw, manifest, doc, ""


def _envelope_attests_window(
    envelope: Any, *, start: datetime, horizon: datetime,
) -> tuple[bool, str]:
    if not isinstance(envelope, Mapping):
        return False, "freshness overlay envelope unavailable"
    after = _instant(envelope.get("after"))
    through = _instant(envelope.get("through"))
    if after is None or after != start:
        return False, "freshness overlay overlap boundary unproven"
    if through is None or through < horizon:
        return False, "freshness overlay coverage horizon unproven"
    return True, ""


def _task_delta(
    transport: Any, team: str, sections: dict[str, dict[str, Any]],
    changes: tuple[Change, ...], *, watermark: datetime, horizon: datetime,
) -> tuple[bool, str, tuple[str, ...]]:
    """Apply only task lifecycles after the sealed watermark through horizon."""
    eligible = [change for change in changes
                if (at := _instant(change.at)) is not None
                and watermark < at <= horizon]
    if any(change.namespace not in ("tasks", "projection_metadata")
           for change in eligible):
        unsupported = sorted({change.namespace for change in eligible
                              if change.namespace != "projection_metadata"})
        return False, (
            "freshness overlay has unsupported delta(s) for "
            + ", ".join(unsupported)
            + "; run coord-engine reconcile before retrying"
        ), ()
    task_changes = [change for change in eligible if change.namespace == "tasks"]
    latest: dict[str, Change] = {}
    for change in task_changes:
        prior = latest.get(change.path)
        if prior is None or (_instant(change.at), change.update_id) > (
                _instant(prior.at), prior.update_id):
            latest[change.path] = change
    task_value = sections.get("tasks")
    rows = task_value.get("rows") if isinstance(task_value, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return False, "tasks generation section cannot accept a delta", ()
    by_name = {str(row.get("name")): dict(row) for row in rows if row.get("name")}
    prefix = f"team/{team}/task/"
    applied: list[str] = []
    for path, change in sorted(latest.items()):
        if (not path.startswith(prefix) or not path.endswith(".md")
                or "/" in path[len(prefix):]
                or path[len(prefix):] in ("index.md", "log.md")):
            return False, (
                f"freshness overlay task delta path unsupported: {path}; "
                "run coord-engine reconcile before retrying"
            ), ()
        name = path[len(prefix):-3]
        if change.state in ("archived", "deleted"):
            by_name.pop(name, None)
            applied.append(change.update_id)
            continue
        classified = getattr(transport, "read_classified", None)
        try:
            if callable(classified):
                raw, state = classified(path)
                if state != "ok":
                    raw = None
            else:
                raw = transport.read(path)
        except Exception:
            raw = None
        frontmatter = okf.parse_frontmatter(raw) if isinstance(raw, str) else None
        if not model.is_task(frontmatter):
            return False, f"freshness overlay task shard unreadable: {path}", ()
        by_name[name] = model.row_from_frontmatter(
            frontmatter, name=name, path=f"task/{name}.md", mtime=change.at,
        )
        applied.append(change.update_id)
    sections["tasks"] = {**task_value, "rows": [by_name[name] for name in sorted(by_name)]}
    return True, "", tuple(applied)


def read_current(
    transport: Any,
    team: str,
    *,
    now: datetime,
    epsilon_seconds: Optional[float],
    epsilon_verified: bool,
    deadline: Optional[Deadline] = None,
) -> PublicReadResult:
    """Validate current generation and cover it through ``now - epsilon``."""
    manifest_raw, manifest, doc, reason = _read_generation(transport, team)
    if manifest is None:
        return _unknown(coverage=_coverage(
            CoverageState.UNKNOWN, CoverageState.NOT_RUN, CoverageState.NOT_RUN,
            manifest_reason=reason,
            immutable_reason="manifest validation did not license generation read",
            overlay_reason="generation validation did not license overlay",
        ))
    generation_id = manifest["generation_id"]
    watermark_text = manifest["source_watermark"]
    if doc is None:
        return _unknown(
            coverage=_coverage(
                CoverageState.CLEAR, CoverageState.UNKNOWN, CoverageState.NOT_RUN,
                immutable_reason=reason,
                overlay_reason="immutable generation validation did not license overlay",
            ),
            generation_id=generation_id,
            watermark=watermark_text,
        )
    section_values = {
        name: deepcopy(doc["sections"][name]["value"])
        for name in generation.REQUIRED_SECTIONS
    }
    if (
        epsilon_verified is not True
        or not isinstance(epsilon_seconds, (int, float))
        or isinstance(epsilon_seconds, bool)
        or epsilon_seconds <= 0
    ):
        return _unknown(
            coverage=_coverage(
                CoverageState.CLEAR, CoverageState.CLEAR, CoverageState.NOT_RUN,
                overlay_reason="fleet-verified epsilon unavailable; overlay not run",
            ),
            sections=section_values,
            generation_id=generation_id,
            watermark=watermark_text,
        )
    watermark = _instant(watermark_text)
    if watermark is None:
        return _unknown(
            coverage=_coverage(
                CoverageState.CLEAR, CoverageState.CLEAR, CoverageState.NOT_RUN,
                overlay_reason="generation watermark unparseable; overlay not run",
            ), sections=section_values, generation_id=generation_id,
            watermark=watermark_text,
        )
    epsilon = timedelta(seconds=float(epsilon_seconds))
    start, horizon = watermark - epsilon, now.astimezone(timezone.utc) - epsilon
    horizon_text = _iso(horizon)
    if horizon < watermark:
        return _unknown(
            coverage=_coverage(
                CoverageState.CLEAR, CoverageState.CLEAR, CoverageState.NOT_RUN,
                overlay_reason="bounded-staleness horizon precedes generation watermark",
            ), sections=section_values, generation_id=generation_id,
            watermark=watermark_text,
        )
    poll_deadline = deadline or Deadline.open(DEFAULT_READ_BUDGET_SECONDS)
    batch = ChangeDetector(transport).poll(team, _iso(start), poll_deadline)
    if not batch.trusted:
        return _unknown(
            coverage=_coverage(
                CoverageState.CLEAR, CoverageState.CLEAR, CoverageState.UNKNOWN,
                overlay_reason="freshness overlay feed coverage UNKNOWN",
            ), sections=section_values,
            generation_id=generation_id, watermark=watermark_text,
        )
    if any(getattr(state, "value", state) not in generation.COMPLETE_STATES
           for state in batch.coverage.values()):
        return _unknown(
            coverage=_coverage(
                CoverageState.CLEAR, CoverageState.CLEAR, CoverageState.UNKNOWN,
                overlay_reason="freshness overlay coverage was not run to completion",
            ), sections=section_values,
            generation_id=generation_id, watermark=watermark_text,
        )
    attested, reason = _envelope_attests_window(
        batch.envelope, start=start, horizon=horizon,
    )
    if not attested:
        return _unknown(
            coverage=_coverage(
                CoverageState.CLEAR, CoverageState.CLEAR, CoverageState.UNKNOWN,
                overlay_reason=reason,
            ), sections=section_values,
            generation_id=generation_id, watermark=watermark_text,
        )
    applied_ok, reason, applied = _task_delta(
        transport, team, section_values, batch.changes,
        watermark=watermark, horizon=horizon,
    )
    if not applied_ok:
        return _unknown(
            coverage=_coverage(
                CoverageState.CLEAR, CoverageState.CLEAR, CoverageState.UNKNOWN,
                overlay_reason=reason,
            ), sections=section_values, horizon=horizon_text,
            generation_id=generation_id, watermark=watermark_text,
        )
    try:
        current_after = transport.read(generation.current_path(team))
    except Exception:
        current_after = None
    if current_after != manifest_raw:
        return _unknown(
            coverage=_coverage(
                CoverageState.UNKNOWN, CoverageState.CLEAR, CoverageState.CLEAR,
                manifest_reason="current manifest changed during freshness overlay",
            ), sections=section_values, horizon=horizon_text,
            generation_id=generation_id, watermark=watermark_text,
        )
    state = OutcomeState.DATA if applied else OutcomeState.CLEAR
    overlay_state = CoverageState.DATA if applied else CoverageState.CLEAR
    return PublicReadResult(
        state,
        _coverage(CoverageState.CLEAR, CoverageState.CLEAR, overlay_state),
        section_values,
        horizon_text,
        generation_id,
        watermark_text,
        applied,
    )

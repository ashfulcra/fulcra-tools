"""Storage layer: per-marker NumericAnnotation tracks, idempotent ingest.

Design (spiked live and verified against the Fulcra API 2026-07):
  * One custom ``NumericAnnotation`` definition PER marker (LDL-C, HbA1c, …).
    The composite catalog id is ``NumericAnnotation/<uuid>``.
  * Writes go through the single-record ingest path
    (``POST /ingest/v1/record``) with ``wire.build_record`` — the same
    envelope every fulcra-tools importer emits. Fulcra dedupes on the
    deterministic source id, so re-ingest is a server-side no-op.
  * Reads come back from ``GET /data/v1alpha1/metric/NumericAnnotation%2F<uuid>``.

Definition ids are cached in ``~/.config/fulcra-labs/markers.json`` and
re-validated against the live catalog (``definition_exists``) so a cache that
points at a soft-deleted or wrong-account def self-heals. Source PDFs are
archived LOCALLY ONLY under ``~/.config/fulcra-labs/documents/`` — medical
data never leaves the machine except as the numeric values the operator
confirms.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fulcra_common import wire
from fulcra_common.client import BaseFulcraClient

from . import markers as _markers
from .logging_setup import get_logger
from .validate import OK, ValidationReport, parse_collected_at, validate_extraction

log = get_logger(__name__)

# Whether to set the canonical unit in measurement_spec at definition-creation
# time. The CLI (`fulcra data-type create NumericAnnotation`) rejects a unit,
# but the API's create body MAY accept a non-null numeric unit — UNVERIFIED
# against the live account (creating a def is a write; not tested live). The
# SAFE, spiked-verified default is False: unit lives in the description text and
# in every record's data payload, which is proven to work. Flip to True only
# after a live smoke confirms the API accepts it. Both paths are unit-tested.
SET_UNIT_AT_CREATION = False


def config_home() -> Path:
    return Path(
        os.environ.get("FULCRA_LABS_HOME")
        or os.path.expanduser("~/.config/fulcra-labs")
    )


def state_path() -> Path:
    return config_home() / "markers.json"


def documents_dir() -> Path:
    return config_home() / "documents"


# --------------------------------------------------------------------------
# On-disk state
# --------------------------------------------------------------------------

@dataclass
class MarkerEntry:
    def_id: str
    canonical_unit: str
    created_at: str


@dataclass
class LabsState:
    markers: dict[str, MarkerEntry] = field(default_factory=dict)
    last_ingest: str | None = None

    def to_dict(self) -> dict:
        return {
            "markers": {k: asdict(v) for k, v in self.markers.items()},
            "last_ingest": self.last_ingest,
        }


def load_state(path: Path | None = None) -> LabsState:
    p = path or state_path()
    if not p.exists():
        return LabsState()
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        # Fail open to empty state — a truncated markers.json must never brick
        # ingest; defs re-adopt from the live catalog on the next run.
        log.warning("markers.json unreadable at %s; starting from empty state", p)
        return LabsState()
    markers = {
        k: MarkerEntry(def_id=v["def_id"], canonical_unit=v.get("canonical_unit", ""),
                       created_at=v.get("created_at", ""))
        for k, v in (raw.get("markers") or {}).items()
    }
    return LabsState(markers=markers, last_ingest=raw.get("last_ingest"))


def save_state(state: LabsState, path: Path | None = None) -> None:
    p = path or state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state.to_dict(), indent=2, sort_keys=True))
    # 0600: def ids let any local process write into the user's Fulcra
    # annotations; the ingest cadence is also mildly sensitive.
    os.chmod(p, 0o600)


# --------------------------------------------------------------------------
# Document archiving (local only)
# --------------------------------------------------------------------------

def source_doc_token(pdf_path: str | os.PathLike) -> tuple[str, str]:
    """Return (sha256_hex, basename) for a source PDF."""
    p = Path(pdf_path)
    h = hashlib.sha256(p.read_bytes()).hexdigest()
    return h, p.name


def archive_document(pdf_path: str | os.PathLike) -> str:
    """Copy the source PDF into the local archive keyed by content hash.
    Returns the ``<sha256>:<basename>`` token stored on every record from it.
    Medical PII — local filesystem only, never uploaded."""
    sha, basename = source_doc_token(pdf_path)
    dest_dir = documents_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(dest_dir, 0o700)
    dest = dest_dir / f"{sha}.pdf"
    if not dest.exists():
        shutil.copyfile(pdf_path, dest)
        os.chmod(dest, 0o600)
    log.info("archived source document %s -> %s", basename, dest)
    return f"{sha}:{basename}"


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

class LabsClient(BaseFulcraClient):
    USER_AGENT = "fulcra-labs/0.1"

    def create_marker_definition(self, marker: _markers.Marker) -> str:
        """Create the per-marker NumericAnnotation definition; return its uuid.

        The description carries the canonical unit, LOINC, and a stable
        ``[com.fulcra.labs.marker.<key>]`` token so the track can be adopted
        across machines/runs (see ``find_existing_marker_def``)."""
        desc = f"{marker.display_name}. Canonical unit: {marker.canonical_unit}."
        if marker.loinc:
            desc += f" LOINC {marker.loinc}."
        desc += f" [{_markers.marker_namespace(marker.key)}]"
        body = {
            "name": marker.display_name,
            "description": desc,
            "annotation_type": "numeric",
            "measurement_spec": {
                "measurement_type": "custom",
                "value_type": "real",
                "metric_kind": "discrete",
                "unit": marker.canonical_unit if SET_UNIT_AT_CREATION else None,
            },
            "tags": [],
            "spec": None,
        }
        r = self._client().post(
            "/user/v1alpha1/annotation", json=body, headers=self._authed_headers()
        )
        r.raise_for_status()
        def_id = r.json()["id"]
        log.info("created NumericAnnotation track for %s -> %s", marker.key, def_id)
        return def_id

    def find_existing_marker_def(self, marker: _markers.Marker) -> str | None:
        """Adopt an existing per-marker track by scanning the live catalog for
        a non-deleted numeric def whose description carries the marker token.
        Returns the def uuid or None. Network failure → None (fall back to
        create/local-state)."""
        token = _markers.marker_namespace(marker.key)
        try:
            catalog = self._lib().annotations_catalog()
        except Exception:  # noqa: BLE001 — catalog unreachable; caller falls back
            log.warning("catalog unreachable while adopting %s", marker.key)
            return None
        for d in catalog:
            if d.get("annotation_type") != "numeric" or d.get("deleted_at"):
                continue
            if token in (d.get("description") or ""):
                return d.get("id")
        return None

    def marker_series(self, def_uuid: str, start: datetime, end: datetime) -> list[dict]:
        """Read a marker's series via the metric endpoint."""
        composite = quote(f"NumericAnnotation/{def_uuid}", safe="")
        r = self._client().get(
            f"/data/v1alpha1/metric/{composite}",
            params={
                "start_time": start.isoformat().replace("+00:00", "Z"),
                "end_time": end.isoformat().replace("+00:00", "Z"),
            },
            headers=self._authed_headers(),
        )
        r.raise_for_status()
        body = r.json()
        if isinstance(body, list):
            return body
        return body.get("data", []) or []

    def ingest_record(self, record: dict) -> None:
        """POST one wire record to the single-record ingest endpoint (→ 204).
        Falls back to the batch endpoint on a 404/405 (deploy without the
        single-record route), mirroring IngestPipeline.ingest_one."""
        r = self._client().post(
            "/ingest/v1/record", json=record, headers=self._authed_headers()
        )
        if r.status_code in (404, 405):
            body = wire.encode_batch([record])
            r = self._client().post(
                "/ingest/v1/record/batch",
                content=body,
                headers={**self._authed_headers(), "content-type": "application/x-jsonl"},
            )
        r.raise_for_status()


# --------------------------------------------------------------------------
# Ingest orchestration
# --------------------------------------------------------------------------

@dataclass
class IngestOutcome:
    total: int = 0
    ingested: int = 0
    skipped_duplicate: int = 0          # deduped within this run
    review_held: int = 0
    rejected: int = 0
    tracks_created: list[str] = field(default_factory=list)  # marker keys
    tracks_adopted: list[str] = field(default_factory=list)
    ingested_markers: list[str] = field(default_factory=list)
    review_items: list[dict] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _ensure_marker_def(
    client: LabsClient, marker: _markers.Marker, state: LabsState,
    *, dry_run: bool, outcome: IngestOutcome, resolved: dict[str, str],
) -> str | None:
    """Resolve the marker's def id: this-run cache → cached-in-state
    (revalidated) → adopted → created. Returns None in dry-run when it would
    have to create (nothing is created)."""
    if marker.key in resolved:
        return resolved[marker.key]
    entry = state.markers.get(marker.key)
    if entry and client.definition_exists(entry.def_id):
        resolved[marker.key] = entry.def_id
        return entry.def_id
    if entry:
        log.warning("cached def %s for %s is gone; re-resolving", entry.def_id, marker.key)

    adopted = client.find_existing_marker_def(marker)
    if adopted:
        state.markers[marker.key] = MarkerEntry(
            def_id=adopted, canonical_unit=marker.canonical_unit,
            created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        if marker.key not in outcome.tracks_adopted:
            outcome.tracks_adopted.append(marker.key)
        resolved[marker.key] = adopted
        return adopted

    if dry_run:
        return None
    def_id = client.create_marker_definition(marker)
    state.markers[marker.key] = MarkerEntry(
        def_id=def_id, canonical_unit=marker.canonical_unit,
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    outcome.tracks_created.append(marker.key)
    resolved[marker.key] = def_id
    return def_id


def ingest_extraction(
    client: LabsClient,
    extraction: dict,
    *,
    state: LabsState,
    source_doc: str | os.PathLike | None = None,
    dry_run: bool = False,
    confirmed_keys: set[str] | None = None,
    report: ValidationReport | None = None,
) -> tuple[IngestOutcome, ValidationReport]:
    """Validate then ingest an extraction. Only ``ok`` rows ingest, plus
    ``review`` rows whose marker key is in ``confirmed_keys``. Idempotent:
    each record's deterministic source id makes re-ingest a server no-op."""
    confirmed_keys = confirmed_keys or set()
    report = report or validate_extraction(extraction)
    outcome = IngestOutcome(total=len(report.verdicts), dry_run=dry_run)

    doc_token: str | None = None
    if source_doc is not None:
        if dry_run:
            sha, basename = source_doc_token(source_doc)
            doc_token = f"{sha}:{basename}"
        else:
            doc_token = archive_document(source_doc)

    lab = report.lab
    posted_det: set[str] = set()
    resolved_defs: dict[str, str] = {}
    for v in report.verdicts:
        ingestable = v.verdict == OK or (
            v.det_source_id is not None and v.marker_key in confirmed_keys
        )
        if not ingestable:
            if v.verdict == "reject":
                outcome.rejected += 1
            else:
                outcome.review_held += 1
                outcome.review_items.append(
                    {"marker_raw": v.marker_raw, "marker_key": v.marker_key,
                     "reasons": v.reasons}
                )
            log.info("HOLD %s (%s): %s", v.marker_raw, v.verdict, v.reasons)
            continue

        # Ingestable but defensively require the computed pieces.
        if not (v.marker_key and v.canonical_value is not None
                and v.det_source_id and v.collected_at):
            outcome.review_held += 1
            outcome.review_items.append(
                {"marker_raw": v.marker_raw, "reasons": ["incomplete row"]}
            )
            continue

        if v.det_source_id in posted_det:
            outcome.skipped_duplicate += 1
            log.info("skip in-run duplicate %s", v.det_source_id)
            continue
        posted_det.add(v.det_source_id)

        marker = _markers.BY_KEY[v.marker_key]
        def_id = _ensure_marker_def(client, marker, state, dry_run=dry_run,
                                    outcome=outcome, resolved=resolved_defs)

        payload = {
            "value": v.canonical_value,
            "unit": v.canonical_unit,
            "raw_value": v.raw_value,
            "raw_unit": v.raw_unit,
            "reference_range": v.reference_range,
            "flag": v.flag,
            "qualifier": v.qualifier,
            "lab": lab,
            "source_doc": doc_token,
        }
        collected_dt, _ = parse_collected_at(v.collected_at)
        assert collected_dt is not None  # verdict.collected_at was parseable

        if dry_run:
            outcome.ingested += 1
            outcome.ingested_markers.append(v.marker_key)
            log.info("DRY-RUN would ingest %s = %s %s (src=%s)",
                     v.marker_key, v.canonical_value, v.canonical_unit, v.det_source_id)
            continue

        record = wire.build_record(
            data_type="NumericAnnotation",
            start_time=collected_dt,
            data=payload,
            source_id=v.det_source_id,
            definition_id=def_id,
            tags=[],
        )
        client.ingest_record(record)
        outcome.ingested += 1
        outcome.ingested_markers.append(v.marker_key)
        log.info("ingested %s = %s %s (src=%s)",
                 v.marker_key, v.canonical_value, v.canonical_unit, v.det_source_id)

    if not dry_run:
        state.last_ingest = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        save_state(state)
    return outcome, report

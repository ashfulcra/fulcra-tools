"""Storage layer: per-marker NumericAnnotation tracks, idempotent ingest.

Design (spiked live and verified against the Fulcra API 2026-07):
  * One custom ``NumericAnnotation`` definition PER marker (LDL-C, HbA1c, …).
    The composite catalog id is ``NumericAnnotation/<uuid>``.
  * Writes go through the TYPED ingest path
    (``POST /ingest/v1/record/NumericAnnotation`` via
    ``wire.build_typed_record`` + ``IngestPipeline.ingest_typed``), so
    ``value`` AND ``unit`` land as first-class schema fields (the legacy
    wrapped path stored record-level ``unit: null``). The typed endpoint has
    NO free-form data slot — unknown fields are silently stripped — so the
    traceability fields (raw value/unit, reference range, flag, lab, source
    doc) ride in the record ``note`` (see ``_traceability_note``) and the
    local archive. It is also ASYNC with NO server-side source-id dedup and
    silently drops bad JSONL lines (all live-verified 2026-07-08): the store
    brackets every POST client-side — a pre-POST already-present query on the
    deterministic source ids (so re-ingesting the same report never
    duplicates records; see ``_already_present``) and a post-POST landing
    poll (so 201 is never read as stored; see ``_poll_landed``).
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
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fulcra_common import wire
from fulcra_common.client import BaseFulcraClient
from fulcra_common.ingest import IngestPipeline

from . import markers as _markers
from .logging_setup import get_logger
from .validate import OK, ValidationReport, Verdict, parse_collected_at, validate_extraction

log = get_logger(__name__)

# Landed-verification poll for the typed ingest endpoint. POST returns
# 201 {"upload_id": …} and processing is ASYNC — records take ~1-2 min to
# become queryable (live-verified 2026-07-08), there is NO upload-status
# endpoint, and a JSONL batch silently drops bad lines. Re-querying is the
# only landing confirmation, so we poll up to 6 times, 30 s apart (~2.5 min,
# comfortably past the observed lag) before declaring a record missing.
_LANDING_POLL_ATTEMPTS = 6
_LANDING_POLL_SLEEP_S = 30


class LandingVerificationError(RuntimeError):
    """Raised when typed records did not become visible after the async lag.

    201 means QUEUED, not stored: the operator must never read a silent
    success when a record was dropped, so a missing landing is a hard,
    nonzero-exit failure (surfaced via ``cli.main``)."""


class IngestPrecheckError(RuntimeError):
    """Raised when the pre-POST already-present check could not run.

    The typed endpoint has NO server-side source-id dedup (live-verified
    2026-07-08), so this query is the ONLY thing standing between a re-run
    of the same lab report and duplicate medical records. If it cannot run,
    labs refuses to ingest (verify-before-ingest, the opposite tradeoff
    from media's fail-open): labs batches are small and operator-triggered,
    so a retry is cheap; duplicate medical records are not."""

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

# --------------------------------------------------------------------------
# Traceability note + landed verification (typed-ingest compensations)
# --------------------------------------------------------------------------

def _provenance_segment(lab: str | None, doc_token: str | None) -> str:
    """``<lab> <source_doc_basename>#<sha16>`` — the provenance tail of a
    traceability note. ``doc_token`` is the ``<sha256>:<basename>`` archive
    token; the sha is truncated to 16 hex chars. Either half may be absent."""
    parts: list[str] = []
    if lab:
        parts.append(lab)
    if doc_token:
        sha, _, basename = doc_token.partition(":")
        parts.append(f"{basename}#{sha[:16]}")
    return " ".join(parts)


def _traceability_note(
    v: Verdict, *, lab: str | None, doc_token: str | None
) -> str:
    """Pack a lab observation's traceability fields into a record ``note``.

    ``note`` is the ONLY free-form slot on the typed endpoint (unknown fields
    are silently stripped, live-verified 2026-07-08); the full-fidelity source
    lives in the local archive, so this note is a compact, PARSEABLE summary.

    Fixed format (segments in order, single spaces):

        "<FLAG><SPACE>"                # omitted when no flag
        "<raw_value> <raw_unit>"       # the measurement, as printed
        " [ref <reference_range>]"     # omitted when no reference range
        " — <lab> <source_doc_basename>#<sha16>"   # omitted when no provenance

    Example:
        "H 210 mg/dL [ref 100-199] — LabCorp 2026-06-01-quest.pdf#a1b2c3d4e5f60718"

    To parse: split once on `" — "` into (measurement, provenance); in the
    measurement, an optional single-token leading flag precedes
    `<raw_value> <raw_unit>`, followed by an optional `"[ref …]"` group;
    provenance is `"<lab> <basename>#<sha16>"`.
    """
    head = f"{v.flag} " if v.flag else ""
    head += f"{v.raw_value} {v.raw_unit or ''}".rstrip()
    if v.reference_range:
        head += f" [ref {v.reference_range}]"
    prov = _provenance_segment(lab, doc_token)
    if prov:
        head += f" — {prov}"
    return head


def _poll_landed(
    client: LabsClient, det_ids: set[str],
    window_start: datetime, window_end: datetime,
) -> set[str]:
    """Poll ``records_visible`` until every det-id is queryable, or the poll
    budget is exhausted. Returns the subset that became visible. The typed
    endpoint is async (201 ≠ stored), so the first check often lags."""
    landed: set[str] = set()
    for attempt in range(1, _LANDING_POLL_ATTEMPTS + 1):
        landed = client.records_visible(
            "NumericAnnotation", det_ids, window_start, window_end)
        if landed >= det_ids:
            log.info("all %d typed record(s) visible on attempt %d/%d",
                     len(det_ids), attempt, _LANDING_POLL_ATTEMPTS)
            return landed
        log.info("landing check %d/%d: %d/%d record(s) visible so far",
                 attempt, _LANDING_POLL_ATTEMPTS, len(landed), len(det_ids))
        if attempt < _LANDING_POLL_ATTEMPTS:
            time.sleep(_LANDING_POLL_SLEEP_S)
    return landed


@dataclass
class _PendingRecord:
    """A validated row ready to post: the built typed record plus the
    bookkeeping fields the pre-check / landing verification need."""
    record: dict
    marker_key: str
    det_id: str
    collected_at: datetime


def _already_present(
    client: LabsClient, det_ids: set[str],
    window_start: datetime, window_end: datetime,
) -> set[str]:
    """Which det-ids are ALREADY in Fulcra from a prior run (single query,
    no poll — records that landed in earlier runs are long past the async
    lag). The typed endpoint has NO server-side source-id dedup
    (live-verified 2026-07-08), so this pre-check is what keeps a re-run of
    the same lab report from duplicating records.

    If the check itself fails, REFUSE to ingest (IngestPrecheckError) rather
    than post blind — verify-before-ingest, the opposite tradeoff from
    media's fail-open: labs batches are small and operator-triggered, so a
    retry is cheap; duplicate medical records are not."""
    try:
        return client.records_visible(
            "NumericAnnotation", det_ids, window_start, window_end)
    except Exception as exc:
        raise IngestPrecheckError(
            f"could not check which records already exist ({exc}); refusing "
            "to ingest — the typed endpoint has no server-side dedup, so "
            "posting without this check risks duplicate records. Nothing was "
            "posted; retry when the query endpoint is reachable."
        ) from exc


def _post_and_verify(
    client: LabsClient, pending: list[_PendingRecord], outcome: IngestOutcome,
) -> None:
    """Pre-check → POST only new det-ids → landing-verify what was posted.

    Both brackets compensate the typed endpoint's live-verified hazards
    (2026-07-08): NO server-side source-id dedup (pre-check skips det-ids
    already in Fulcra, so re-runs never duplicate), and async 201 with
    silent JSONL line drops (landing poll raises LandingVerificationError —
    nonzero CLI exit — for anything posted that never became visible)."""
    det_ids = {p.det_id for p in pending}
    times = [p.collected_at for p in pending]
    window_start = min(times) - timedelta(days=1)   # ±1 day: tz safety
    window_end = max(times) + timedelta(days=1)

    already = _already_present(client, det_ids, window_start, window_end)
    for sid in sorted(already):
        log.info("already in Fulcra (skipped): %s", sid)
    outcome.skipped_already_present = len(already)

    to_post = [p for p in pending if p.det_id not in already]
    if not to_post:
        log.info("all %d record(s) already in Fulcra; nothing to post",
                 len(pending))
        return
    IngestPipeline(client=client).ingest_typed(
        "NumericAnnotation", [p.record for p in to_post])
    for p in to_post:
        outcome.ingested += 1
        outcome.ingested_markers.append(p.marker_key)

    posted_ids = {p.det_id for p in to_post}
    landed = _poll_landed(client, posted_ids, window_start, window_end)
    for sid in sorted(landed):
        log.info("verified landed: %s", sid)
    missing = posted_ids - landed
    if missing:
        for sid in sorted(missing):
            log.error("NOT visible after %d checks: %s",
                      _LANDING_POLL_ATTEMPTS, sid)
        raise LandingVerificationError(
            f"{len(missing)}/{len(posted_ids)} typed NumericAnnotation "
            f"record(s) did not become visible after {_LANDING_POLL_ATTEMPTS} "
            f"checks ({_LANDING_POLL_SLEEP_S}s apart): {sorted(missing)}. The "
            "typed ingest endpoint is async (201 = queued, not stored), has "
            "no upload-status endpoint, and silently drops bad JSONL lines "
            "(live-verified 2026-07-08) — re-check the source and re-ingest "
            "the missing rows."
        )


# --------------------------------------------------------------------------
# Ingest orchestration
# --------------------------------------------------------------------------

@dataclass
class IngestOutcome:
    total: int = 0
    ingested: int = 0
    skipped_duplicate: int = 0          # deduped within this run
    skipped_already_present: int = 0    # already in Fulcra from a prior run
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
    ``review`` rows whose marker key is in ``confirmed_keys``. Idempotent
    CLIENT-side: the typed endpoint has no server dedup, so before posting
    we query for the batch's deterministic source ids and skip any already
    in Fulcra (``skipped_already_present``); after posting we poll until the
    rest are visible. Re-ingesting the same report is therefore a no-op."""
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
    pending: list[_PendingRecord] = []
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

        collected_dt, _ = parse_collected_at(v.collected_at)
        assert collected_dt is not None  # verdict.collected_at was parseable

        if dry_run:
            outcome.ingested += 1
            outcome.ingested_markers.append(v.marker_key)
            log.info("DRY-RUN would ingest %s = %s %s (src=%s)",
                     v.marker_key, v.canonical_value, v.canonical_unit, v.det_source_id)
            continue

        # Typed numeric write: value+unit are first-class schema fields; the
        # traceability fields have no typed slot, so they ride in note (+ the
        # local archive). build_typed_record raises if unit is passed without
        # value or value on a non-numeric — ok rows always carry both.
        record = wire.build_typed_record(
            base_type="NumericAnnotation",
            start_time=collected_dt,
            source_id=v.det_source_id,
            definition_id=def_id,
            value=v.canonical_value,
            unit=v.canonical_unit,
            note=_traceability_note(v, lab=lab, doc_token=doc_token),
            tags=[],
        )
        pending.append(_PendingRecord(
            record=record, marker_key=v.marker_key,
            det_id=v.det_source_id, collected_at=collected_dt,
        ))
        log.info("queued %s = %s %s (src=%s)",
                 v.marker_key, v.canonical_value, v.canonical_unit, v.det_source_id)

    if not dry_run:
        state.last_ingest = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        # Persist created/adopted tracks BEFORE verification: the defs exist
        # server-side regardless of whether every record has become visible.
        save_state(state)
        if pending:
            _post_and_verify(client, pending, outcome)
    return outcome, report

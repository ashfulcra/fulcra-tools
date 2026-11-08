"""Local archive, retained legacy state evidence and native traceability helpers.

Actual ingestion is native Metric through native_v1 and the standalone kernel.
Legacy NumericAnnotation write entry points refuse before accessing their inputs.
The original markers.json is read-only evidence for the native command.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

from fulcra_common.client import BaseFulcraClient
from .logging_setup import get_logger
from .validate import Verdict

log = get_logger(__name__)


def config_home() -> Path:
    return Path(os.environ.get("FULCRA_LABS_HOME") or os.path.expanduser("~/.config/fulcra-labs"))


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
    """Strict read-only evidence; a missing cache alone is known empty."""
    try:
        raw = json.loads((path or state_path()).read_text())
    except FileNotFoundError:
        return LabsState()
    except (OSError, ValueError):
        raise RuntimeError("legacy labs state UNKNOWN; retain pending work") from None
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("markers"), dict)
        or not isinstance(raw.get("last_ingest"), (str, type(None)))
    ):
        raise RuntimeError("legacy labs state UNKNOWN; retain pending work")
    for key, entry in raw["markers"].items():
        if (
            not isinstance(key, str)
            or not isinstance(entry, dict)
            or not isinstance(entry.get("def_id"), str)
            or not entry["def_id"]
            or not isinstance(entry.get("canonical_unit", ""), str)
            or not isinstance(entry.get("created_at", ""), str)
        ):
            raise RuntimeError("legacy labs state UNKNOWN; retain pending work")
        identity = entry["def_id"]
        if identity.startswith("NumericAnnotation/"):
            identity = identity.split("/", 1)[1]
        try:
            if str(UUID(identity)) != identity:
                raise ValueError
        except (ValueError, AttributeError):
            raise RuntimeError("legacy labs identifier UNKNOWN; retain pending work") from None
    return LabsState(
        markers={
            key: MarkerEntry(
                entry["def_id"], entry.get("canonical_unit", ""), entry.get("created_at", "")
            )
            for key, entry in raw["markers"].items()
        },
        last_ingest=raw.get("last_ingest"),
    )


def save_state(*args, **kwargs):
    """Retain original migration evidence: native ingestion never rewrites it."""
    raise RuntimeError("legacy labs cache writer retired; original evidence is read-only")


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
    # One byte snapshot owns both content identity and archive bytes.
    import tempfile

    source = Path(pdf_path)
    content = source.read_bytes()
    sha, basename = hashlib.sha256(content).hexdigest(), source.name
    dest_dir = documents_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(dest_dir, 0o700)
    dest = dest_dir / f"{sha}.pdf"
    if dest.exists():
        if dest.read_bytes() != content:
            raise RuntimeError("local source archive integrity UNKNOWN")
        os.chmod(dest, 0o600)
    else:
        fd, name = tempfile.mkstemp(dir=dest_dir, prefix=".labs-")
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, dest)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    return f"{sha}:{basename}"


class LabsClient(BaseFulcraClient):
    USER_AGENT = "fulcra-labs/0.1"

    def create_marker_definition(self, *args, **kwargs):
        raise RuntimeError("legacy labs writer retired; use native fulcra-labs ingest")

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


def _traceability_note(v: Verdict, *, lab: str | None, doc_token: str | None) -> str:
    """Pack a lab observation's traceability fields into a record ``note``.

    Native records also carry declared structured provenance fields. Preserve
    this compact summary's established spelling and segment order; source PDF
    bytes remain in the local archive.

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


def ingest_extraction(*args, **kwargs):
    """Retired legacy API: no input, auth, empty-source or preview bypass."""
    raise RuntimeError("legacy labs writer retired; use native fulcra-labs ingest")

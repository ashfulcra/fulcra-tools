"""The verification engine — verify BEFORE ingest.

Input: an extraction-schema dict (see the module docstring below / the SKILL).
Output: one ``Verdict`` per observation — ``ok`` (auto-ingestable), ``review``
(needs an operator decision, with reasons), or ``reject`` (malformed, never
ingestable). Nothing reaches Fulcra unless it is ``ok`` or the operator
explicitly confirms a ``review`` row.

Extraction schema (transcribed EXACTLY as printed by the model — no conversion,
no rounding, no inferred units):

    {"lab": "LabCorp|Quest|<other>",
     "report_date": "YYYY-MM-DD",
     "collected_at": "YYYY-MM-DDTHH:MM:SS±TZ" | "YYYY-MM-DD",
     "observations": [
        {"marker_raw": "as printed", "value_raw": "as printed",
         "unit_raw": "as printed or null",
         "reference_range_raw": "as printed or null",
         "flag_raw": "H|L|A|null"}
     ]}
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from . import markers as _markers
from .logging_setup import get_logger

log = get_logger(__name__)

# Verdict levels, most-severe last for max().
OK = "ok"
REVIEW = "review"
REJECT = "reject"
_SEVERITY = {OK: 0, REVIEW: 1, REJECT: 2}

QUALIFIER_LT = "lt"
QUALIFIER_GT = "gt"

# Reject a collection date this far in the future (small skew tolerance for
# timezone-naive dates) or before this floor year.
_FUTURE_SKEW = timedelta(days=1)
_MIN_YEAR = 1990

_VALUE_RE = re.compile(r"^\s*([<>≤≥]=?)?\s*([0-9][0-9,]*\.?[0-9]*)\s*$")


@dataclass
class Verdict:
    marker_raw: str
    raw_value: str
    raw_unit: str | None
    verdict: str
    reasons: list[str] = field(default_factory=list)
    marker_key: str | None = None
    marker_display: str | None = None
    canonical_value: float | None = None
    canonical_unit: str | None = None
    qualifier: str | None = None
    flag: str | None = None
    reference_range: str | None = None
    collected_at: str | None = None
    det_source_id: str | None = None
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ValidationReport:
    verdicts: list[Verdict]
    lab: str | None
    report_date: str | None
    collected_at: str | None

    @property
    def counts(self) -> dict[str, int]:
        c = {OK: 0, REVIEW: 0, REJECT: 0}
        for v in self.verdicts:
            c[v.verdict] += 1
        return c


def fmt_canonical(value: float) -> str:
    """Stable string form of a canonical value for the deterministic id and
    in-batch dedupe. 6 significant figures — enough to distinguish real lab
    values, coarse enough to swallow float round-trip noise."""
    return f"{value:.6g}"


def det_source_id(marker_key: str, collected_at_iso: str, canonical_value: float) -> str:
    """Idempotent per-observation source id. Fulcra dedupes on source id, so
    re-ingesting the same (marker, collection time, value) is a server-side
    no-op — the pipeline's idempotency contract."""
    payload = f"{marker_key}|{collected_at_iso}|{fmt_canonical(canonical_value)}"
    digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return f"com.fulcra.labs.v1.{digest}"


def parse_value(value_raw: str) -> tuple[float | None, str | None]:
    """Parse a printed value into (numeric, qualifier).

    Handles thousands commas and the ``<`` / ``>`` / ``≤`` / ``≥`` bound
    qualifiers (``<0.1`` → (0.1, 'lt'); ``>300`` → (300.0, 'gt')). Returns
    (None, None) when there is no number to parse (e.g. "Negative", "TNP")."""
    if value_raw is None:
        return None, None
    m = _VALUE_RE.match(str(value_raw))
    if not m:
        return None, None
    sign, num = m.group(1), m.group(2)
    try:
        val = float(num.replace(",", ""))
    except ValueError:
        return None, None
    qualifier: str | None = None
    if sign:
        if sign[0] in "<≤":
            qualifier = QUALIFIER_LT
        elif sign[0] in ">≥":
            qualifier = QUALIFIER_GT
    return val, qualifier


def parse_collected_at(raw: str | None) -> tuple[datetime | None, str | None]:
    """Parse a report collection timestamp to (aware-UTC datetime, ISO-Z str).

    Accepts a full ISO datetime (with or without timezone) or a bare date. A
    date-only value is assumed to be UTC midnight — no local-time guessing. A
    naive datetime is assumed UTC. Returns (None, None) on unparseable input."""
    if raw is None:
        return None, None
    s = str(raw).strip()
    if not s:
        return None, None
    dt: datetime | None = None
    # Bare date (YYYY-MM-DD).
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            d = datetime.fromisoformat(s)
            dt = d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None, None
    else:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None, None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    iso = dt.isoformat().replace("+00:00", "Z")
    return dt, iso


def _disambiguate_by_unit(candidate_keys: list[str], unit_norm: str | None):
    """From several markers sharing an alias, pick the one whose accepted
    units include ``unit_norm``. Returns the Marker or None if 0 or >1 match."""
    if unit_norm is None:
        return None
    hits = [
        _markers.BY_KEY[k]
        for k in candidate_keys
        if unit_norm in _markers.BY_KEY[k].accepted_units
    ]
    return hits[0] if len(hits) == 1 else None


def validate_observation(obs: dict, collected_iso: str | None,
                         collected_dt: datetime | None,
                         now: datetime) -> Verdict:
    """Validate one observation against the registry. Pure — no I/O."""
    marker_raw = str(obs.get("marker_raw", "") or "")
    value_raw = obs.get("value_raw")
    unit_raw = obs.get("unit_raw")
    v = Verdict(
        marker_raw=marker_raw,
        raw_value="" if value_raw is None else str(value_raw),
        raw_unit=None if unit_raw is None else str(unit_raw),
        verdict=OK,
        flag=(str(obs["flag_raw"]) if obs.get("flag_raw") else None),
        reference_range=(str(obs["reference_range_raw"])
                         if obs.get("reference_range_raw") else None),
        collected_at=collected_iso,
    )

    def flag(level: str, reason: str) -> None:
        v.verdict = level if _SEVERITY[level] > _SEVERITY[v.verdict] else v.verdict
        v.reasons.append(reason)

    # --- Collection date (report-level, inherited) ---
    if collected_dt is None or collected_iso is None:
        flag(REJECT, "no parseable collection date")
    else:
        if collected_dt > now + _FUTURE_SKEW:
            flag(REJECT, f"collection date {collected_iso} is in the future")
        if collected_dt.year < _MIN_YEAR:
            flag(REJECT, f"collection date {collected_iso} is before {_MIN_YEAR}")

    # --- Marker resolution ---
    unit_norm = _markers.normalize_unit(v.raw_unit)
    res = _markers.resolve_marker(marker_raw)
    marker = res.marker
    if marker is None and res.candidates:
        marker = _disambiguate_by_unit(res.candidates, unit_norm)
        if marker is None:
            flag(REVIEW, f"marker {marker_raw!r} is ambiguous across "
                         f"{res.candidates}; unit did not disambiguate")
    if marker is None:
        if not res.candidates:
            v.suggestions = res.suggestions
            hint = f" (did you mean {res.suggestions}?)" if res.suggestions else ""
            flag(REVIEW, f"unresolved marker {marker_raw!r}{hint}")
    else:
        v.marker_key = marker.key
        v.marker_display = marker.display_name
        v.canonical_unit = marker.canonical_unit

    # --- Value parse ---
    num, qualifier = parse_value(v.raw_value)
    v.qualifier = qualifier
    if num is None:
        flag(REJECT, f"value {v.raw_value!r} is not numeric")

    # --- Unit + conversion + range (only when marker & value are known) ---
    if marker is not None and num is not None:
        if unit_norm is None:
            flag(REVIEW, f"missing unit for {marker.display_name} — never inferred")
        elif unit_norm not in marker.accepted_units:
            accepted = sorted(marker.accepted_units)
            flag(REVIEW, f"unknown unit {v.raw_unit!r} for {marker.display_name} "
                         f"(accepted: {accepted})")
        else:
            canon = round(marker.convert(num, unit_norm), 6)
            v.canonical_value = canon
            lo, hi = marker.plausible_range
            if not (lo <= canon <= hi):
                flag(REVIEW, f"implausible value {canon} {marker.canonical_unit} "
                             f"for {marker.display_name} (plausible {lo}-{hi}) — "
                             f"likely a unit mixup or transcription error")

    # --- Deterministic id (needs marker + canonical value + date) ---
    if v.marker_key and v.canonical_value is not None and collected_iso:
        v.det_source_id = det_source_id(v.marker_key, collected_iso, v.canonical_value)

    log.debug("row marker=%r -> key=%s verdict=%s reasons=%s",
              marker_raw, v.marker_key, v.verdict, v.reasons)
    return v


def validate_extraction(extraction: dict, *, now: datetime | None = None) -> ValidationReport:
    """Validate a full extraction dict. Adds in-batch duplicate detection on
    top of the per-observation checks."""
    if not isinstance(extraction, dict) or not isinstance(
        extraction.get("observations"), list
    ):
        raise ValueError("extraction must be an object with an 'observations' list")
    now = now or datetime.now(timezone.utc)
    lab = extraction.get("lab")
    report_date = extraction.get("report_date")
    # collected_at is report-level; fall back to report_date when absent.
    collected_raw = extraction.get("collected_at") or report_date
    collected_dt, collected_iso = parse_collected_at(collected_raw)

    verdicts: list[Verdict] = []
    seen_det: dict[str, int] = {}
    for idx, obs in enumerate(extraction["observations"]):
        if not isinstance(obs, dict):
            raise ValueError(f"observation #{idx} is not an object")
        v = validate_observation(obs, collected_iso, collected_dt, now)
        # In-batch duplicate detection (same det id = same marker+time+value).
        if v.det_source_id is not None:
            if v.det_source_id in seen_det:
                first = seen_det[v.det_source_id]
                v.verdict = REVIEW if _SEVERITY[REVIEW] > _SEVERITY[v.verdict] else v.verdict
                v.reasons.append(f"duplicate of observation #{first} within this batch")
            else:
                seen_det[v.det_source_id] = idx
        verdicts.append(v)

    report = ValidationReport(
        verdicts=verdicts, lab=lab, report_date=report_date, collected_at=collected_iso
    )
    log.info("validated %d observations: %s", len(verdicts), report.counts)
    return report

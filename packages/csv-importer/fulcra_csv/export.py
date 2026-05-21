"""Export Fulcra annotations / events back out as CSV.

The inverse of the import flow. Given a time range + a target (definition
id OR built-in data type), fetch the records, pull configurable columns
out of each one, and write CSV to a file or stdout.

The column-selection model is deliberately uniform across record shapes
because the wire schema isn't quite uniform: user-defined annotations
keep their content under `data` (which is itself a JSON-encoded string),
while query endpoints sometimes lift fields to the top level. The
`select_column` helper hides that.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import IO, Any, Iterable

# ---- supported "well-known" columns ----------------------------------
#
# Each entry maps a column name to an accessor that takes a record dict
# (as returned by /data/v1alpha1/event/<type>) and returns a string. The
# accessors tolerate either the top-level shape or the data-payload-as-
# JSON-string shape.

DEFAULT_COLUMNS: tuple[str, ...] = ("start_time", "end_time", "tag", "note", "value")

WELLKNOWN_FIELDS: tuple[str, ...] = (
    "start_time",
    "end_time",
    "note",
    "title",
    "value",
    "unit",
    "tag",
    "tags",
    "category",
    "source_id",
    "definition_id",
    "url",
    "favicon_url",
    "og_description",
    "og_type",
    "lang",
    "chrome_identity",
    "host",
    "client",
)


# Leading characters Excel / Sheets / Numbers interpret as formula
# triggers. Any cell starting with one is prefixed with a single quote
# at write time when `guard_formulas=True` (the default).
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class ExportOptions:
    """Knobs the CLI surfaces to the user."""
    columns: tuple[str, ...] = DEFAULT_COLUMNS
    date_format: str = "iso"   # "iso" | "epoch" | "local"
    local_tz: tzinfo | None = None  # required when date_format == "local"
    # When True, prefix cells that start with `= + - @ \t \r` with a
    # single quote so spreadsheets don't interpret them as formulas.
    # Defense against CSV-injection — see OWASP CSV injection. Off-switch
    # exists for power users who pipe through a downstream parser.
    guard_formulas: bool = True

    def __post_init__(self) -> None:
        if self.date_format == "local" and self.local_tz is None:
            raise ValueError("date_format='local' requires local_tz")


def _parse_data_payload(rec: dict) -> dict:
    """Records under user-defined annotation defs carry their payload as
    a JSON-encoded string in `data`. Built-in types may expose fields
    directly under `data` as a dict. Normalise to a dict either way."""
    d = rec.get("data")
    if d is None:
        return {}
    if isinstance(d, str):
        try:
            return json.loads(d)
        except json.JSONDecodeError:
            return {}
    if isinstance(d, dict):
        return d
    return {}


def _fmt_ts(value: Any, opts: ExportOptions) -> str:
    """Format an ISO-8601 timestamp string according to date_format."""
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        # epoch seconds — already-parsed
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str):
        try:
            s = value.replace("Z", "+00:00") if value.endswith("Z") else value
            dt = datetime.fromisoformat(s)
        except ValueError:
            return value  # leave it alone if we can't parse it
    else:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if opts.date_format == "epoch":
        return f"{int(dt.timestamp())}"
    if opts.date_format == "local":
        assert opts.local_tz is not None  # enforced in __post_init__
        return dt.astimezone(opts.local_tz).isoformat()
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def select_column(rec: dict, column: str, opts: ExportOptions) -> str:
    """Resolve one column name against one record. Returns a CSV-ready
    string (never None — empty cells are "").

    Supports:
      - well-known top-level fields ("start_time", "end_time", ...)
      - `data.<key>` for fields under the JSON-encoded data payload
      - `external_ids.<key>` for fields under data.external_ids
      - `source_id` as a special — pulls the first non-def source string
    """
    if column.startswith("data."):
        key = column[len("data."):]
        return _stringify(_parse_data_payload(rec).get(key))
    if column.startswith("external_ids."):
        key = column[len("external_ids."):]
        ext = _parse_data_payload(rec).get("external_ids") or {}
        return _stringify(ext.get(key))

    # Timestamps from the recorded_at sub-object, or the top-level
    # shape (some endpoints lift them).
    recorded_at = rec.get("recorded_at") or {}
    if column == "start_time":
        return _fmt_ts(recorded_at.get("start_time") or rec.get("start_time"), opts)
    if column == "end_time":
        return _fmt_ts(recorded_at.get("end_time") or rec.get("end_time"), opts)

    if column == "tags":
        data_payload = _parse_data_payload(rec)
        tag_names = (rec.get("tag_names") or [])
        if tag_names:
            return ",".join(tag_names)
        # Fall back to data.tag (single tag, set by the importer when
        # parsing single-tag CSVs).
        single = data_payload.get("tag")
        return _stringify(single)
    if column == "tag":
        # Single tag — first of tag_names, or data.tag
        names = rec.get("tag_names")
        if names:
            return names[0]
        return _stringify(_parse_data_payload(rec).get("tag"))

    if column == "source_id":
        # First non-definition source: the per-row dedup key.
        sources = rec.get("sources") or (rec.get("metadata") or {}).get("source") or []
        for s in sources:
            if not str(s).startswith("com.fulcradynamics.annotation."):
                return str(s)
        # Fallback: the literal top-level source_id (some endpoints
        # don't expose the array).
        return _stringify(rec.get("source_id"))

    if column == "definition_id":
        sources = rec.get("sources") or (rec.get("metadata") or {}).get("source") or []
        for s in sources:
            ss = str(s)
            if ss.startswith("com.fulcradynamics.annotation."):
                return ss[len("com.fulcradynamics.annotation."):]
        return ""

    # Default lookup: top-level, then data payload.
    if column in rec and rec[column] is not None:
        return _stringify(rec[column])
    return _stringify(_parse_data_payload(rec).get(column))


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    # isinstance(True, int) is True in Python — the bool branch MUST
    # come before the int branch, or booleans render as "True"/"False"
    # (which then doesn't round-trip through coerce_value's truthy set).
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    # Lists / dicts → JSON so CSV stays single-line.
    try:
        return json.dumps(value, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _guard_formula(s: str) -> str:
    """Prefix a `'` to a cell whose first char Excel/Sheets treats as a
    formula trigger. Idempotent: if the string is already escaped or
    empty, returns it unchanged."""
    if not s:
        return s
    if s[0] in _FORMULA_TRIGGERS:
        return "'" + s
    return s


def write_csv(
    records: Iterable[dict],
    out: IO[str],
    opts: ExportOptions,
) -> int:
    """Write `records` to `out` as CSV with `opts.columns` headers. Returns
    the number of rows written."""
    writer = csv.writer(out)
    writer.writerow(opts.columns)
    n = 0
    for rec in records:
        row = [select_column(rec, c, opts) for c in opts.columns]
        if opts.guard_formulas:
            row = [_guard_formula(c) for c in row]
        writer.writerow(row)
        n += 1
    return n

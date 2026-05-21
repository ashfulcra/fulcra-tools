"""fulcra-csv CLI: bootstrap an annotation def, import a CSV."""

from __future__ import annotations

import sys
from datetime import datetime, timezone, tzinfo
from pathlib import Path

import click

from .events import DURATION, INSTANT, ColumnMap
from .export import DEFAULT_COLUMNS, ExportOptions, write_csv
from .fulcra import FulcraClient
from .parser import parse_csv


def _parse_kv_list(items: tuple[str, ...], flag: str) -> tuple[tuple[str, str], ...]:
    """Parse `--flag COL=KEY` repeats into ((col, key), ...)."""
    pairs: list[tuple[str, str]] = []
    for item in items:
        if "=" not in item:
            raise click.UsageError(f"{flag} requires COL=KEY format, got {item!r}")
        col, key = item.split("=", 1)
        pairs.append((col.strip(), key.strip()))
    return tuple(pairs)


def _resolve_tz(tz_name: str) -> tzinfo:
    if tz_name == "UTC":
        return timezone.utc
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(tz_name)
    except Exception as exc:
        raise click.UsageError(f"unknown timezone {tz_name!r}: {exc}") from exc


@click.group(help="Import any CSV into Fulcra as annotation events.")
def cli() -> None:
    pass


@cli.command(
    "import",
    help=(
        "Import a CSV into a Fulcra annotation type.\n\n"
        "Three target modes:\n"
        "  (1) User-defined annotation: pass --definition-id <uuid>.\n"
        "  (2) Built-in Fulcra type (BodyMass, HeartRate, ...): pass\n"
        "      --data-type <Name> alone. The importer skips the\n"
        "      annotation-def source entry; dedup is purely source_id-based.\n"
        "  (3) Generic annotation: omit both, defaults to DurationAnnotation /\n"
        "      InstantAnnotation per --annotation-type."
    ),
)
@click.argument("csv_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--definition-id", default=None,
              help="User-defined annotation UUID. Omit when targeting a built-in data type.")
@click.option("--annotation-type",
              type=click.Choice([DURATION, INSTANT]),
              default=DURATION, show_default=True,
              help="duration: events have start+end; instant: single point in time")
@click.option("--data-type", default=None,
              help="Wire data_type. Defaults to DurationAnnotation / InstantAnnotation. "
                   "Pass a built-in type name (e.g. BodyMass) to write to a native Fulcra "
                   "time series instead of a user-defined annotation.")
@click.option("--ts-col", default="timestamp", show_default=True)
@click.option("--end-col", default=None, help="Optional end-time column (duration only)")
@click.option("--duration-col", default=None,
              help="Optional duration-in-seconds column (alternative to --end-col)")
@click.option("--title-col", default="title", show_default=True)
@click.option("--subtitle-col", default=None,
              help="Joined with title for a default note (artist, show, ...)")
@click.option("--note-col", default=None,
              help="Override note column (otherwise built from title + subtitle)")
@click.option("--value-col", default=None,
              help="Numeric/scalar measurement column lifted into data.value")
@click.option("--value-type",
              type=click.Choice(["float", "int", "str", "bool"]),
              default="float", show_default=True)
@click.option("--unit", default=None,
              help="Unit for the value (e.g. 'kg', 'bpm', 'usd'). "
                   "Lifted into data.unit; commonly required by built-in types.")
@click.option("--source-id-col", default=None,
              help="Per-content id column. Combined with timestamp in the hash, "
                   "not used verbatim (replays of same content stay distinct).")
@click.option("--tag-col", default=None, help="Per-row tag column")
@click.option("--tag", "default_tag", default=None, help="Default tag for all rows")
@click.option("--data-field", "data_fields", multiple=True, metavar="COL=KEY",
              help="Lift CSV column into data.<KEY>. Repeatable.")
@click.option("--extra", "extras", multiple=True, metavar="COL=KEY",
              help="Lift CSV column into data.external_ids[KEY]. Repeatable.")
@click.option("--tz", "tz_name", default="UTC", show_default=True,
              help="IANA tz for naive timestamps")
@click.option("--source-id-prefix", default="com.fulcradynamics.csv.v1",
              show_default=True, help="Deterministic source-id prefix")
@click.option("--dry-run", is_flag=True, help="Parse and print, don't ingest")
def import_csv(
    csv_path: Path,
    definition_id: str | None,
    annotation_type: str,
    data_type: str | None,
    ts_col: str,
    end_col: str | None,
    duration_col: str | None,
    title_col: str,
    subtitle_col: str | None,
    note_col: str | None,
    value_col: str | None,
    value_type: str,
    unit: str | None,
    source_id_col: str | None,
    tag_col: str | None,
    default_tag: str | None,
    data_fields: tuple[str, ...],
    extras: tuple[str, ...],
    tz_name: str,
    source_id_prefix: str,
    dry_run: bool,
) -> None:
    if annotation_type == INSTANT and (end_col or duration_col):
        raise click.UsageError(
            "--end-col / --duration-col only apply to --annotation-type duration"
        )
    if not definition_id and not data_type:
        raise click.UsageError(
            "Pass --definition-id <uuid> (user annotation) or --data-type <Name> "
            "(built-in type). Without one of those there's no target."
        )
    # Roll --unit into data_fields so it travels with the payload.
    data_field_pairs = list(_parse_kv_list(data_fields, "--data-field"))
    colmap = ColumnMap(
        timestamp=ts_col,
        end_time=end_col,
        duration_seconds=duration_col,
        title=title_col,
        subtitle=subtitle_col,
        note=note_col,
        source_id=source_id_col,
        tag=tag_col,
        value=value_col,
        value_type=value_type,
        data_fields=tuple(data_field_pairs),
        extras=_parse_kv_list(extras, "--extra"),
    )
    tz = _resolve_tz(tz_name)
    events = list(parse_csv(
        csv_path,
        column_map=colmap,
        tz=tz,
        source_id_prefix=source_id_prefix,
        default_tag=default_tag,
        annotation_type=annotation_type,
    ))
    if unit:
        # Constant unit applies to every row — fold it in after parsing so
        # callers don't need a unit column for the common single-unit case.
        for e in events:
            e.data_fields.setdefault("unit", unit)
    click.echo(f"parsed {len(events)} events from {csv_path}")
    if dry_run:
        for e in events[:5]:
            value_repr = f" value={e.value}" if e.value is not None else ""
            click.echo(
                f"  {e.start_time.isoformat()} | {e.tag or '-'} | {e.note}{value_repr}"
            )
        if len(events) > 5:
            click.echo(f"  ... and {len(events) - 5} more")
        return

    client = FulcraClient()
    tag_id_for: dict[str, str] = {}
    unique_tags = {e.tag for e in events if e.tag}
    for tag in unique_tags:
        tag_id_for[tag] = client.ensure_tag(tag)
    result = client.run_import(
        events,
        definition_id=definition_id,
        tag_id_for=tag_id_for,
        data_type=data_type,
    )
    click.echo(
        f"total={result.total} skipped_existing={result.skipped_existing} "
        f"posted={result.posted} verified={result.verified}"
    )


@cli.command("soft-delete", help="Soft-delete an annotation definition.")
@click.argument("definition_id")
@click.option("--confirm", is_flag=True,
              help="Required. Soft-delete is the closest thing to a reset Fulcra "
                   "offers, and events under the def stay visible in queries.")
def soft_delete(definition_id: str, confirm: bool) -> None:
    if not confirm:
        raise click.UsageError(
            "Pass --confirm. Soft-delete does NOT hide events from queries; "
            "you also need to bump the source-id prefix on importers."
        )
    client = FulcraClient()
    ok = client.soft_delete_definition(definition_id)
    if ok:
        click.echo(f"soft-deleted {definition_id}")
    else:
        click.echo(f"definition {definition_id} not found", err=True)
        raise click.exceptions.Exit(1)


@cli.command(help="Create a generic annotation definition.")
@click.option("--name", required=True)
@click.option("--description", default="")
@click.option("--annotation-type",
              type=click.Choice([DURATION, INSTANT]),
              default=DURATION, show_default=True)
@click.option("--value-type",
              type=click.Choice(["float", "int", "str", "bool", "none"]),
              default="none", show_default=True,
              help="If the annotation carries a measurement value, what type")
@click.option("--unit", default=None,
              help="Unit string for measurement annotations (e.g. 'kg', 'usd', 'bpm')")
@click.option("--tag", "tags", multiple=True, help="Tags to attach (creates if missing)")
def bootstrap(
    name: str, description: str,
    annotation_type: str, value_type: str, unit: str | None,
    tags: tuple[str, ...],
) -> None:
    client = FulcraClient()
    tag_ids = [client.ensure_tag(t) for t in tags]
    measurement_spec: dict = {
        "measurement_type": "duration" if annotation_type == DURATION else "instant",
        "value_type": "duration" if annotation_type == DURATION and value_type == "none"
                      else (value_type if value_type != "none" else "none"),
        "unit": unit,
    }
    body = {
        "annotation_type": annotation_type,
        "name": name,
        "description": description,
        "tags": tag_ids,
        "measurement_spec": measurement_spec,
    }
    r = client._client().post(
        "/user/v1alpha1/annotation",
        json=body,
        headers=client._authed_headers(),
    )
    r.raise_for_status()
    click.echo(r.json()["id"])


def _parse_time_arg(value: str, tz: tzinfo) -> datetime:
    """Parse a time argument. Accepts ISO-8601 or `dateparser`-style
    relative strings ('1 week ago', 'yesterday')."""
    try:
        s = value.replace("Z", "+00:00") if value.endswith("Z") else value
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt
    except ValueError:
        pass
    import dateparser  # hard dep — see pyproject.toml
    settings = {"RETURN_AS_TIMEZONE_AWARE": True, "TIMEZONE": str(tz)}
    parsed = dateparser.parse(value, settings=settings)
    if parsed is None:
        raise click.UsageError(f"can't parse time argument {value!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


@cli.command(
    "export",
    help=(
        "Export Fulcra annotations as CSV.\n\n"
        "Pass --definition-id to scope to a user-defined annotation, or "
        "--data-type to pull a built-in time series (BodyMass, HeartRate, "
        "DurationAnnotation, etc.). At least one must be given.\n\n"
        "Columns are configurable via --columns: well-known fields like "
        "start_time, end_time, note, tag, value, source_id, definition_id, "
        "plus dotted paths data.<key> and external_ids.<key>."
    ),
)
@click.option("--definition-id", default=None,
              help="Scope export to a user-defined annotation UUID.")
@click.option("--data-type", default=None,
              help="Wire data_type to query. Defaults to DurationAnnotation "
                   "when --definition-id is given, otherwise required.")
@click.option("--start", "start", required=True,
              help="ISO-8601 or relative ('1 week ago', 'yesterday').")
@click.option("--end", "end", default="now", show_default=True,
              help="ISO-8601 or relative.")
@click.option("--columns", default=",".join(DEFAULT_COLUMNS), show_default=True,
              help=("Comma-separated column list. Supports well-known fields, "
                    "`data.<key>`, and `external_ids.<key>`."))
@click.option("--date-format",
              type=click.Choice(["iso", "epoch", "local"]),
              default="iso", show_default=True)
@click.option("--tz", "tz_name", default="UTC", show_default=True,
              help="IANA tz. Used for parsing relative times and for "
                   "--date-format local.")
@click.option("--out", "out_path",
              type=click.Path(dir_okay=False, writable=True, path_type=Path),
              default=None,
              help="Output path. Default: stdout.")
def export_cmd(
    definition_id: str | None,
    data_type: str | None,
    start: str,
    end: str,
    columns: str,
    date_format: str,
    tz_name: str,
    out_path: Path | None,
) -> None:
    if not definition_id and not data_type:
        raise click.UsageError(
            "Pass --definition-id <uuid> or --data-type <Name>."
        )
    tz = _resolve_tz(tz_name)
    start_dt = _parse_time_arg(start, tz)
    end_dt = _parse_time_arg(end, tz)
    if start_dt >= end_dt:
        raise click.UsageError(f"--start must be before --end ({start_dt!s} >= {end_dt!s})")

    cols = tuple(c.strip() for c in columns.split(",") if c.strip())
    if not cols:
        raise click.UsageError("--columns produced an empty list")

    opts = ExportOptions(
        columns=cols,
        date_format=date_format,
        local_tz=tz if date_format == "local" else None,
    )

    # When the user picked a user-defined annotation, query its underlying
    # data_type (default DurationAnnotation) and filter rows whose source
    # array references the target def. This mirrors run_import's
    # only_for_defs filter so the export sees the same records the
    # importer would dedup against.
    read_data_type = data_type or "DurationAnnotation"
    client = FulcraClient()
    records = client.fetch_records(start_dt, end_dt, data_type=read_data_type)

    if definition_id:
        target_def_marker = f"com.fulcradynamics.annotation.{definition_id}"
        records = [
            r for r in records
            if _record_references_def(r, target_def_marker)
        ]

    if out_path:
        with out_path.open("w", newline="", encoding="utf-8") as f:
            n = write_csv(records, f, opts)
        click.echo(f"wrote {n} rows → {out_path}", err=True)
    else:
        n = write_csv(records, sys.stdout, opts)
        click.echo(f"wrote {n} rows", err=True)


def _record_references_def(rec: dict, target: str) -> bool:
    sources = rec.get("sources") or (rec.get("metadata") or {}).get("source") or []
    return any(str(s) == target for s in sources)


if __name__ == "__main__":
    cli()

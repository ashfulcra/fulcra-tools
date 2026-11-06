"""CSV v1 Event/Metric import and explicitly legacy annotation audit/admin."""

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


@click.group(help="Import CSV observations into owned v1 Event/Metric destinations.")
def cli() -> None:
    pass


@cli.command(
    "import", help="Import strict CSV rows into an explicit owned v1 Event/UUID or Metric/UUID."
)
@click.argument("csv_path", type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--definition-id", default=None, help="Opaque historical selection token; requires --data-type."
)
@click.option(
    "--annotation-type", type=click.Choice([DURATION, INSTANT]), default=DURATION, show_default=True
)
@click.option(
    "--data-type",
    default=None,
    help="Required full canonical Event/UUID or Metric/UUID destination.",
)
@click.option(
    "--aggregation",
    type=click.Choice(["discrete", "cumulative"]),
    default="discrete",
    show_default=True,
)
@click.option("--ts-col", default="timestamp", show_default=True)
@click.option("--end-col", default=None, help="Optional end-time column (duration only)")
@click.option(
    "--duration-col",
    default=None,
    help="Optional duration-in-seconds column (alternative to --end-col)",
)
@click.option("--title-col", default="title", show_default=True)
@click.option(
    "--subtitle-col", default=None, help="Joined with title for a default note (artist, show, ...)"
)
@click.option(
    "--note-col", default=None, help="Override note column (otherwise built from title + subtitle)"
)
@click.option(
    "--value-col", default=None, help="Scalar source value; Metric requires a finite number"
)
@click.option(
    "--value-type",
    type=click.Choice(["float", "int", "str", "bool"]),
    default="float",
    show_default=True,
)
@click.option(
    "--unit",
    default=None,
    help="Explicit Metric destination unit and constant nested source-field fallback.",
)
@click.option(
    "--source-id-col",
    default=None,
    help="Per-content id column. Combined with timestamp in the hash, "
    "not used verbatim (replays of same content stay distinct).",
)
@click.option("--tag-col", default=None, help="Per-row tag column")
@click.option("--tag", "default_tag", default=None, help="Default tag for all rows")
@click.option(
    "--data-field",
    "data_fields",
    multiple=True,
    metavar="COL=KEY",
    help="Preserve CSV column in csv_data_fields[KEY]. Repeatable.",
)
@click.option(
    "--extra",
    "extras",
    multiple=True,
    metavar="COL=KEY",
    help="Preserve CSV column in external_ids[KEY]. Repeatable.",
)
@click.option(
    "--tz", "tz_name", default="UTC", show_default=True, help="IANA tz for naive timestamps"
)
@click.option(
    "--source-id-prefix",
    default="com.fulcradynamics.csv.v1",
    show_default=True,
    help="Deterministic source-id prefix",
)
@click.option("--dry-run", is_flag=True, help="Parse and print, don't ingest")
def import_csv(
    csv_path: Path,
    definition_id: str | None,
    annotation_type: str,
    data_type: str | None,
    aggregation: str,
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
    from fulcra_common.v1 import _destination

    try:
        destination = _destination(data_type)
        if destination != data_type:
            raise ValueError("destination must be canonical")
    except ValueError:
        raise click.UsageError(
            "Pass --data-type Event/<canonical UUID> or Metric/<canonical UUID>."
        ) from None
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

    def read_events():
        events = list(
            parse_csv(
                csv_path,
                column_map=colmap,
                tz=tz,
                source_id_prefix=source_id_prefix,
                default_tag=default_tag,
                annotation_type=annotation_type,
                strict=True,
                required_columns=(title_col,)
                if click.get_current_context().get_parameter_source("title_col")
                == click.core.ParameterSource.COMMANDLINE
                else (),
            )
        )
        if unit is not None:
            for event in events:
                event.data_fields.setdefault("unit", unit)
        return events

    from .v1 import build_csv_record

    if dry_run:
        try:
            events = read_events()
            seen = {}
            for number, event in enumerate(events, 2):
                try:
                    from fulcra_common.v1 import record_matches
                    from fulcra_common.data_types import tag_create_name

                    row = build_csv_record(event, destination, unit=unit)
                    for label in ([event.tag] if event.tag else []) + list(event.extra_tags):
                        tag_create_name(label)
                    if row["id"] in seen and not (
                        record_matches(row, seen[row["id"]])
                        and record_matches(seen[row["id"]], row)
                    ):
                        raise ValueError("conflicting input rows for one CSV identity")
                    seen[row["id"]] = row
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"CSV row {number}: {exc}") from None
        except (ValueError, TypeError, OSError) as exc:
            raise click.ClickException(str(exc)) from None
        click.echo(f"parsed {len(events)} events (local preview; destination not verified)")
        for event in events[:5]:
            click.echo(f"  {event.start_time.isoformat()} | {event.tag or '-'} | {event.note}")
        return

    from .standalone_v1 import authority, open_csv, Progress

    writer = None
    captured = []
    phase = "recovery"
    try:
        with open_csv(
            authority(
                destination, unit=unit, aggregation=aggregation, legacy_selection=definition_id
            ),
            client=FulcraClient(),
            on_writer=captured.append,
        ) as writer:
            phase = "source"
            events = read_events()
            phase = "current import"
            writer.run_import(events)
    except Exception as exc:
        writer = writer or (captured[0] if captured else None)
        progress = writer.progress if writer else Progress()
        recovered = writer.recovered if writer else 0
        raise click.ClickException(
            f"{phase} failed ({type(exc).__name__}: {exc}); "
            f"known current lower bounds total={progress.total} skipped_existing={progress.skipped_existing} "
            f"posted={progress.posted} verified={progress.verified}; "
            f"recovered={recovered} separately; pending may remain, reconcile before retry"
        ) from None
    progress = writer.progress
    click.echo(
        f"total={progress.total} skipped_existing={progress.skipped_existing} "
        f"posted={progress.posted} verified={progress.verified} recovered={writer.recovered}"
    )


@cli.command("soft-delete", help="Legacy annotation definition soft-delete administration.")
@click.argument("definition_id")
@click.option(
    "--confirm",
    is_flag=True,
    help="Required explicit legacy administration; historical records remain visible.",
)
def soft_delete(definition_id: str, confirm: bool) -> None:
    if definition_id.startswith(("Event/", "Metric/")):
        raise click.UsageError(
            "Legacy annotation administration cannot target v1 Event/Metric destinations."
        )
    if not confirm:
        raise click.UsageError(
            "Pass --confirm for legacy annotation administration; records remain visible."
        )
    client = FulcraClient()
    ok = client.soft_delete_definition(definition_id)
    if ok:
        click.echo(f"soft-deleted {definition_id}")
    else:
        click.echo(f"definition {definition_id} not found", err=True)
        raise click.exceptions.Exit(1)


@cli.command(help="Resolve or create an owned CSV v1 Event/Metric destination.")
@click.option("--base-type", type=click.Choice(["Event", "Metric"]), required=True)
@click.option("--name", required=True)
@click.option("--description", default="Timestamped CSV observations with preserved source fields.")
@click.option("--unit", default=None)
@click.option("--aggregation", type=click.Choice(["discrete", "cumulative"]), default="discrete")
@click.pass_context
def bootstrap(ctx, base_type, name, description, unit, aggregation):
    from click.core import ParameterSource

    if base_type == "Event" and any(
        ctx.get_parameter_source(key) == ParameterSource.COMMANDLINE
        for key in ("unit", "aggregation")
    ):
        raise click.UsageError("Event bootstrap rejects Metric --unit/--aggregation options.")
    from .v1 import csv_spec
    from fulcra_collect.standalone_v1 import standalone_runtime

    try:
        spec = csv_spec(
            base_type,
            name=name,
            description=description,
            unit=unit,
            aggregation=aggregation,
            bootstrap=True,
        )
        client = FulcraClient()
        with standalone_runtime(
            "cli-csv",
            {"bootstrap_spec": spec.fingerprint},
            client=client,
            transport=client._transport,
            identity_transport=client._transport,
        ) as (_, session):
            resolved = session.resolve_destination(spec, allow_create=True)
        click.echo(resolved.data_type)
    except Exception as exc:
        raise click.ClickException(
            f"CSV setup failed ({type(exc).__name__}: {exc}); retain original setup intent"
        ) from None


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
        "Legacy historical annotation audit export as CSV (not v1 import verification).\n\n"
        "Pass --definition-id to scope to a user-defined annotation, or "
        "--data-type to pull a built-in time series (BodyMass, HeartRate, "
        "DurationAnnotation, etc.). At least one must be given.\n\n"
        "Columns are configurable via --columns: well-known fields like "
        "start_time, end_time, note, tag, value, source_id, definition_id, "
        "plus dotted paths data.<key> and external_ids.<key>."
    ),
)
@click.option(
    "--definition-id", default=None, help="Scope export to a user-defined annotation UUID."
)
@click.option(
    "--data-type",
    default=None,
    help="Wire data_type to query. Defaults to DurationAnnotation "
    "when --definition-id is given, otherwise required.",
)
@click.option(
    "--start", "start", required=True, help="ISO-8601 or relative ('1 week ago', 'yesterday')."
)
@click.option("--end", "end", default="now", show_default=True, help="ISO-8601 or relative.")
@click.option(
    "--columns",
    default=",".join(DEFAULT_COLUMNS),
    show_default=True,
    help=(
        "Comma-separated column list. Supports well-known fields, "
        "`data.<key>`, and `external_ids.<key>`."
    ),
)
@click.option(
    "--date-format", type=click.Choice(["iso", "epoch", "local"]), default="iso", show_default=True
)
@click.option(
    "--tz",
    "tz_name",
    default="UTC",
    show_default=True,
    help="IANA tz. Used for parsing relative times and for --date-format local.",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=None,
    help="Output path. Default: stdout.",
)
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
        raise click.UsageError("Pass --definition-id <uuid> or --data-type <Name>.")
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
    # array references the historical target def. This is a legacy audit
    # filter, independent of native v1 import verification.
    read_data_type = data_type or "DurationAnnotation"
    client = FulcraClient()
    records = client.fetch_records(start_dt, end_dt, data_type=read_data_type)

    if definition_id:
        target_def_marker = f"com.fulcradynamics.annotation.{definition_id}"
        records = [r for r in records if _record_references_def(r, target_def_marker)]

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

"""fulcra-csv CLI: bootstrap an annotation def, import a CSV."""

from __future__ import annotations

from datetime import timezone, tzinfo
from pathlib import Path

import click

from .events import DURATION, INSTANT, ColumnMap
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


if __name__ == "__main__":
    cli()
